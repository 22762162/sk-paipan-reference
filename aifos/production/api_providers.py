"""直连 API Provider:剧本 / 图片 / 视频 / 配音的 API 模式产线。

与 CLI 桥(claude_script / codex_image / dreamina)互为主备,在 routing
里排在对应 CLI 之后:CLI 不可用或失败时自动切换到 API,全部只用标准库。

  claude_api  script/storyboard —— Anthropic Messages API(默认 claude-opus-4-8)
  image_api   image/frames/cover —— OpenAI 兼容 /v1/images/generations
  seedream5_lite image/frames/cover —— 火山方舟 Seedream 图片生成
  ark_video   video —— 火山方舟 Ark 内容生成任务(创建 → 轮询 → 下载 mp4)
  doubao_tts  voice —— 豆包(火山引擎)语音合成;仅当视频产线不带配音时使用
"""

import base64
import http.client
import json
import re
import shutil
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ..adapters.codex_image import SUBJECT_DIRECTIVE as _SUBJECT_DIRECTIVE
from ..adapters.codex_image import (
    CHARACTER_BACKGROUND_DIRECTIVE as _CHARACTER_BACKGROUND_DIRECTIVE,
)
from ..adapters.codex_image import STUDIO_ASSET_RULES as _STUDIO_ASSET_RULES
from ..adapters.codex_image import _keyframe_phase as _api_keyframe_phase
from ..adapters.codex_image import _keyframe_uri as _api_keyframe_uri
from ..adapters.codex_image import _style_line as _api_style_line
from ..adapters.codex_image import _space_line as _api_space_line
from ..adapters.claude_script import (
    _merge_storyboard_full_repair,
    _merge_storyboard_shot_repairs,
    _storyboard_error_fields,
    build_prompt,
    extract_json,
    validate_script,
    validate_storyboard,
)
from ..config import is_official_deepseek_endpoint
from ..script_import import sanitize_script_entities
from ..story_analysis import validate_story_analysis
from ..story_logic import reconcile_storyboard_prop_registry
from ..errors import ProviderError
from .base import Provider, ProviderResult


def _urlopen(target, timeout):
    """Open URLs while keeping loopback API routes out of system proxies."""
    url = target.full_url if isinstance(
        target, urllib.request.Request) else str(target)
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(target, timeout=timeout)
    return urllib.request.urlopen(target, timeout=timeout)


def _request_json(name, url, headers, body=None, timeout=300, method=None):
    """发 JSON 请求收 JSON 应答;任何网络/协议错误统一转 ProviderError。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(
        url, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"))
    try:
        with _urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ProviderError(
            f"{name} API HTTP {exc.code}: {detail or exc.reason}") from exc
    except Exception as exc:
        raise ProviderError(f"{name} API 调用失败: {exc}") from exc


def _download(name, url, dest, timeout=600):
    try:
        with _urlopen(url, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
    except Exception as exc:
        raise ProviderError(f"{name} 下载产物失败: {exc}") from exc


def _parse_sse_data(raw):
    """SSE 一行 `data: {...}` → dict;非 JSON 数据行返回 None。"""
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _claude_message_text(name, reply):
    """Extract text from an ordinary Anthropic Messages JSON response."""
    if not isinstance(reply, dict):
        return ""
    if reply.get("type") == "error" or "error" in reply:
        detail = reply.get("error") or {}
        message = detail.get("message") if isinstance(detail, dict) else detail
        raise ProviderError(f"{name} API 错误: {message or reply}")
    return "".join(
        str(block.get("text") or "")
        for block in (reply.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text")


def _stream_claude_text(name, url, headers, body, timeout):
    """流式调用 Anthropic Messages API,聚合全文文本返回。

    整集剧本/制作圣经生成常超 10 分钟,非流式请求会被远端断连
    (Remote end closed connection without response)。流式下 socket
    超时按「两次数据块之间的静默」计,长生成不再被掐。仅标准库。
    """
    body = dict(body)
    body["stream"] = True
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream", **headers})
    parts = []
    plain_lines = []
    try:
        with _urlopen(request, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    if line:
                        # 兼容不认 stream 参数的网关/镜像端点:先攒下
                        # 非 SSE 行,流式事件一无所获时按普通应答解析。
                        plain_lines.append(line)
                    continue
                event = _parse_sse_data(line[5:].strip())
                if event is None:
                    continue
                etype = event.get("type")
                if etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        parts.append(str(delta.get("text") or ""))
                elif etype == "error":
                    detail = (event.get("error") or {}).get(
                        "message") or "未知流式错误"
                    raise ProviderError(f"{name} API 流式错误: {detail}")
                elif etype == "message_delta":
                    reason = (event.get("delta") or {}).get("stop_reason")
                    if reason == "max_tokens":
                        raise ProviderError(
                            f"{name} 输出达到 max_tokens 上限被截断,"
                            "请调大 providers 配置中的 max_tokens")
                elif etype == "message_stop":
                    break
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ProviderError(
            f"{name} API HTTP {exc.code}: {detail or exc.reason}") from exc
    except Exception as exc:
        # Some Anthropic-compatible gateways accept ordinary Messages JSON
        # but close the socket immediately when ``stream=true`` is present.
        # This happens before any model output exists, so one non-streaming
        # compatibility retry is safe and avoids classifying a usable route
        # as offline.  Timeouts and mid-stream failures are deliberately not
        # retried here: those may already have consumed a full generation.
        if isinstance(exc, (http.client.RemoteDisconnected,
                            ConnectionResetError)) and not parts:
            fallback_body = dict(body)
            fallback_body["stream"] = False
            reply = _request_json(
                name, url, headers, fallback_body, timeout=timeout)
            return _claude_message_text(name, reply)
        raise ProviderError(f"{name} API 调用失败: {exc}") from exc
    if parts:
        return "".join(parts)
    reply = _parse_sse_data("\n".join(plain_lines))
    if isinstance(reply, dict):
        return _claude_message_text(name, reply)
    return ""


_IMG_MEDIA = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp"}


def sniff_image_media(data, fallback="image/png"):
    """按真实字节判定图片 media_type;后缀只作兜底。

    生成产线存在「.png 文件装着 JPEG 字节」的产物(下游模型按内容
    输出、按约定名落盘);Anthropic API 校验声明与字节一致,后缀猜
    media_type 必 400——这曾把 image_qc 阶梯的 claude_api 级整个打挂,
    质检全部落到 codex 挤占出图通道。
    """
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return fallback


_CANONICAL_IMAGE_SIZES = {
    "9:16": (1080, 1920),
}


def _image_dimensions(path):
    """Read PNG/JPEG geometry from real bytes, regardless of suffix."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"):
        return struct.unpack(">II", data[16:24])
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0x01, *range(0xD0, 0xDA)):
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return int(width), int(height)
        offset += length
    return None


def _center_crop_box(source_width, source_height, target_width,
                     target_height):
    """Return an integer center crop with the target orientation/ratio."""
    if source_width * target_height > source_height * target_width:
        crop_height = source_height
        crop_width = max(
            1, round(source_height * target_width / target_height))
        crop_width = min(source_width, crop_width)
        crop_x = (source_width - crop_width) // 2
        crop_y = 0
    elif source_width * target_height < source_height * target_width:
        crop_width = source_width
        crop_height = max(
            1, round(source_width * target_height / target_width))
        crop_height = min(source_height, crop_height)
        crop_x = 0
        crop_y = (source_height - crop_height) // 2
    else:
        crop_x = crop_y = 0
        crop_width, crop_height = source_width, source_height
    return crop_x, crop_y, crop_width, crop_height


def _image_converter():
    """Return a deterministic local raster converter; macOS stays zero-install."""
    sips = shutil.which("sips")
    if sips:
        return "sips", sips
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        bundled = Path.home() / ".local" / "bin" / "ffmpeg"
        ffmpeg = str(bundled) if bundled.is_file() else ""
    if ffmpeg:
        return "ffmpeg", ffmpeg
    return "", ""


def _run_image_conversion(source, target, crop_box, target_size):
    """Center-crop then resize to a lossless PNG, returning converter name."""
    converter, executable = _image_converter()
    if not executable:
        raise ProviderError(
            "图片已生成，但本机没有 sips/ffmpeg，无法自动归一化为标准9:16；"
            "这是本地确定性后处理问题，禁止用同一提示词重复付费抽图")
    crop_x, crop_y, crop_width, crop_height = crop_box
    target_width, target_height = target_size
    target = Path(target)
    source = Path(source)
    token = uuid.uuid4().hex
    crop_stage = target.with_name(f".{target.name}.{token}.crop.png")
    output_stage = target.with_name(f".{target.name}.{token}.normalized.png")
    try:
        if converter == "sips":
            first = subprocess.run([
                executable, "--cropToHeightWidth", str(crop_height),
                str(crop_width), "--cropOffset", str(crop_y), str(crop_x),
                str(source), "--out", str(crop_stage),
            ], capture_output=True, text=True, timeout=120, check=False)
            if first.returncode != 0:
                raise ProviderError(
                    "sips 图片安全区裁切失败: "
                    + (first.stderr or first.stdout or "未知错误")[-500:])
            second = subprocess.run([
                executable, "--resampleHeightWidth", str(target_height),
                str(target_width), str(crop_stage), "--out",
                str(output_stage),
            ], capture_output=True, text=True, timeout=120, check=False)
            if second.returncode != 0:
                raise ProviderError(
                    "sips 图片标准化失败: "
                    + (second.stderr or second.stdout or "未知错误")[-500:])
        else:
            crop_filter = (
                f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
                f"scale={target_width}:{target_height}:flags=lanczos")
            completed = subprocess.run([
                executable, "-y", "-loglevel", "error", "-i", str(source),
                "-vf", crop_filter, "-frames:v", "1", str(output_stage),
            ], capture_output=True, text=True, timeout=120, check=False)
            if completed.returncode != 0:
                raise ProviderError(
                    "ffmpeg 图片标准化失败: "
                    + (completed.stderr or completed.stdout or "未知错误")[-500:])
        actual = _image_dimensions(output_stage)
        if actual != target_size:
            raise ProviderError(
                f"图片后处理应输出 {target_width}x{target_height}，"
                f"实际为 {actual or '无法读取'}；禁止盲目重复付费生成")
        output_stage.replace(target)
        return converter
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            "图片9:16标准化超过120秒；这是本地后处理失败，"
            "禁止用同一提示词重复付费生成") from exc
    finally:
        for stage in (crop_stage, output_stage):
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass


def _normalize_generated_image(path, payload):
    """Make formal 9:16 artifacts canonical before returning downstream.

    OpenAI's portrait-native 1024x1536 is 2:3, not 9:16.  The paid source is
    retained in a hidden audit directory, while callers only receive the
    deterministic center-safe crop standardized to 1080x1920.
    """
    aspect = str((payload or {}).get("aspect") or "9:16")
    target_size = _CANONICAL_IMAGE_SIZES.get(aspect)
    if target_size is None:
        return {
            "applied": False, "requested_aspect": aspect,
            "policy": "provider_native",
        }
    path = Path(path)
    dimensions = _image_dimensions(path)
    if dimensions is None:
        raise ProviderError(
            "图片供应商已返回产物，但无法读取真实宽高；"
            "禁止登记或用同一提示词盲目重复生成")
    source_width, source_height = dimensions
    crop_box = _center_crop_box(
        source_width, source_height, *target_size)
    crop_x, crop_y, crop_width, crop_height = crop_box
    retained_width = crop_width / source_width
    retained_height = crop_height / source_height
    metadata = {
        "applied": dimensions != target_size,
        "requested_aspect": aspect,
        "policy": "center_safe_crop_then_standard_scale",
        "source_dimensions": {
            "width": source_width, "height": source_height,
        },
        "crop_box": {
            "x": crop_x, "y": crop_y,
            "width": crop_width, "height": crop_height,
        },
        "safe_area": {
            "retained_width_fraction": round(retained_width, 6),
            "retained_height_fraction": round(retained_height, 6),
            "discard_left_fraction": round(crop_x / source_width, 6),
            "discard_right_fraction": round(
                (source_width - crop_x - crop_width) / source_width, 6),
            "discard_top_fraction": round(crop_y / source_height, 6),
            "discard_bottom_fraction": round(
                (source_height - crop_y - crop_height) / source_height, 6),
        },
        "target_dimensions": {
            "width": target_size[0], "height": target_size[1],
        },
        "original_uri": str(path),
        "formal_uri": str(path),
        "converter": "none",
    }
    if dimensions == target_size:
        return metadata

    try:
        media = sniff_image_media(path.read_bytes(), "image/png")
    except OSError as exc:
        raise ProviderError(f"读取图片供应商原始产物失败: {exc}") from exc
    suffix = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        media, ".png")
    audit_dir = path.parent / ".provider-originals"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / (
        f"{path.stem}.{uuid.uuid4().hex}.provider-original{suffix}")
    path.replace(audit_path)
    metadata["original_uri"] = str(audit_path)
    try:
        metadata["converter"] = _run_image_conversion(
            audit_path, path, crop_box, target_size)
    except Exception:
        # The paid source remains in audit storage.  No nonconforming formal
        # path is exposed to downstream consumers.
        raise
    return metadata


def _local_refs(payload):
    """本地参考图路径：最终立绘优先，其后才是衔接/场景/风格/用户图。
    有 reference_manifest 时严格按对照表顺序(与提示词"图N"编号一致)。"""
    manifest = payload.get("reference_manifest") or []
    if manifest:
        refs, seen = [], set()
        for item in manifest:
            uri = str((item or {}).get("uri") or "").strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            path = Path(uri)
            if path.exists() and path.suffix.lower() in _IMG_MEDIA:
                refs.append(path)
        return refs
    order = []
    order.extend(
        ref.get("uri") for ref in (payload.get("identity_references") or [])
        if isinstance(ref, dict) and ref.get("uri"))
    order.extend(payload.get("character_refs") or [])
    order.extend(payload.get("prop_refs") or [])
    for key in ("spatial_ref", "chain_first_uri", "frame_keyframe_uri",
                "keyframe_reference_uri", "keyframe_last_uri",
                "scene_ref", "style_ref"):
        val = payload.get(key)
        if val:
            order.append(val)
    order.extend(payload.get("reference_images") or [])
    seen, refs = set(), []
    for uri in order:
        if not uri or uri in seen:
            continue
        seen.add(uri)
        path = Path(uri)
        if path.exists() and path.suffix.lower() in _IMG_MEDIA:
            refs.append(path)
    # 不能为迁就接口静默丢弃人物参考图。若目标端点有数量上限，应明确
    # 报错并回退到能接收全部参考图的 Provider，而不是退化成文字描述。
    return refs


def _reference_entries(payload):
    """按人物身份优先级收集参考图，并保留 Seedream 需要的
    图序语义。identity_references 的顺序就是 P01/P02/... 的顺序；
    character_refs 中重复的最终立绘不再二次上传。

    payload 带 reference_manifest(导演中心前置生成的参考图对照表)时
    直接按对照表顺序与标签提交——保证提示词里的"图N"与实际第 N 张
    图片完全一致。
    """
    manifest = payload.get("reference_manifest") or []
    if manifest:
        entries, seen = [], set()
        for item in manifest:
            uri = str((item or {}).get("uri") or "").strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            entries.append({"uri": uri,
                            "label": str(item.get("label") or "参考图")})
        return entries
    entries, seen = [], set()

    def add(uri, label):
        value = str(uri or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        entries.append({"uri": value, "label": label})

    identities = payload.get("identity_references") or []
    for index, ref in enumerate(identities, 1):
        if not isinstance(ref, dict) or not ref.get("uri"):
            continue
        actor_id = str(ref.get("actor_id") or f"P{index:02d}")
        character = str(ref.get("character") or "角色")
        add(ref["uri"], f"{actor_id}/{character}最终立绘")
    for uri in payload.get("character_refs") or []:
        add(uri, "人物设定图")
    for uri in payload.get("prop_refs") or []:
        add(uri, "核心道具母资产")
    for key, label in (
            ("spatial_ref", "本镜空间调度图"),
            ("chain_first_uri", "上一镜尾帧"),
            ("frame_keyframe_uri", "本镜关键图"),
            ("keyframe_reference_uri", "本镜关键图"),
            ("keyframe_last_uri", "本镜动作终点关键图"),
            ("scene_ref", "场景基准图"),
            ("style_ref", "风格基准图")):
        add(payload.get(key), label)
    for uri in payload.get("reference_images") or []:
        add(uri, "用户参考图")
    return entries


def _data_image(path):
    media = _IMG_MEDIA.get(path.suffix.lower())
    if media is None:
        raise ProviderError(f"不支持的参考图格式: {path}")
    data = path.read_bytes()
    media = sniff_image_media(data, media)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media};base64,{encoded}"


def _multipart_post(name, url, headers, fields, files, timeout):
    """multipart/form-data POST(stdlib);files=[(field, Path)]。"""
    boundary = "----aifos" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    for field, path in files:
        payload_bytes = path.read_bytes()
        media = sniff_image_media(
            payload_bytes, _IMG_MEDIA.get(path.suffix.lower(), "image/png"))
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{field}"; '
                 f'filename="{path.name}"\r\n'
                 f"Content-Type: {media}\r\n\r\n").encode("utf-8")
        body += payload_bytes
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        url, data=bytes(body), method="POST",
        headers={**headers,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with _urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ProviderError(
            f"{name} API HTTP {exc.code}: {detail or exc.reason}") from exc
    except Exception as exc:
        raise ProviderError(f"{name} API 调用失败: {exc}") from exc


def _safe_name(value):
    return "".join(c if c.isalnum() else "_" for c in str(value))[:40]


class ClaudeApiProvider(Provider):
    """Claude 官方 API 编剧:与 Claude CLI 桥同一套提示词与校验。"""

    DEFAULT_ENDPOINT = "https://api.anthropic.com"
    DEFAULT_MODEL = "claude-opus-4-8"

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key"
        return True, ""

    def ping(self):
        """真实连通性测试:发 1 token 的最小请求,返回 (ok, 说明)。"""
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        model = self.conf.get("model") or self.DEFAULT_MODEL
        try:
            _request_json(
                self.name, f"{endpoint}/v1/messages",
                headers={
                    "x-api-key": self.conf["api_key"],
                    "anthropic-version": "2023-06-01",
                },
                body={"model": model, "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ping"}]},
                timeout=30)
        except ProviderError as exc:
            return False, str(exc)
        return True, f"真实连通成功(model={model})"

    QC_MEDIA = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}

    def _qc_content(self, prompt, payload):
        """图片质检：上传待检图及本次生成实际使用的全部参考图。

        只给质检模型最终立绘，它就无法判断场景图、空间图或连续性图是否
        选错。优先按 reference_manifest 的真实提交顺序上传；旧请求没有
        manifest 时仍兼容 identity_references。
        """
        import base64
        from pathlib import Path as _P
        def image_block(uri, label):
            path = _P(uri)
            if not path.exists():
                raise ProviderError(f"{label}不存在: {path}")
            media = self.QC_MEDIA.get(path.suffix.lower())
            if media is None:
                raise ProviderError(f"质检不支持的图片格式: {path.suffix}")
            data = path.read_bytes()
            if len(data) > 20 * 1024 * 1024:
                raise ProviderError(f"{label}超过 20MB")
            # 后缀会撒谎(.png 装 JPEG 字节),声明必须跟真实字节走,
            # 否则 API 400、QC 阶梯断级。
            media = sniff_image_media(data, media)
            return {"type": "image", "source": {
                "type": "base64", "media_type": media,
                "data": base64.b64encode(data).decode()}}

        content = [{"type": "text", "text": "下面第一张是待检图。"},
                   image_block(payload.get("image_uri", ""), "待检图")]
        manifest = [
            ref for ref in (payload.get("reference_manifest") or [])
            if isinstance(ref, dict) and ref.get("uri")
        ]
        references = manifest or [
            {
                **ref,
                "label": f"{ref.get('character', '角色')}最终立绘",
                "role": "identity",
                "binding": "锁定人物身份",
            }
            for ref in (payload.get("identity_references") or [])
            if isinstance(ref, dict) and ref.get("uri")
        ]
        seen = set()
        for ref in references:
            if not isinstance(ref, dict) or not ref.get("uri"):
                continue
            uri = str(ref["uri"])
            if uri in seen:
                continue
            seen.add(uri)
            label = str(ref.get("label")
                        or f"{ref.get('character', '角色')}参考图")
            role = str(ref.get("role") or "reference")
            binding = str(ref.get("binding") or "按标签职责使用")
            index = ref.get("index")
            prefix = f"图{index}" if index is not None else "参考图"
            content.append({
                "type": "text",
                "text": f"下面是{prefix}「{label}」，职责={role}：{binding}",
            })
            content.append(image_block(uri, label))
        content.append({"type": "text", "text": prompt})
        return content

    def _design_content(self, prompt, payload):
        """人物设定带参考图:参考图作为真实图像块上传,脸部特征与风格
        以参考图为最高标准;单张图不可读时跳过该图(不阻断设定)。"""
        import base64
        from pathlib import Path as _P
        blocks = []
        for character in payload.get("characters", []):
            name = character.get("name", "角色")
            for uri in character.get("reference_images") or []:
                path = _P(uri)
                media = self.QC_MEDIA.get(path.suffix.lower())
                if media is None or not path.exists():
                    continue
                data = path.read_bytes()
                if len(data) > 20 * 1024 * 1024:
                    continue
                media = sniff_image_media(data, media)
                blocks.append({"type": "text",
                               "text": f"下面是{name}的参考图,该角色脸部"
                                       "特征与风格以此图为最高标准。"})
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": media,
                    "data": base64.b64encode(data).decode()}})
        if not blocks:
            return prompt
        blocks.append({"type": "text", "text": prompt})
        return blocks

    def generate(self, capability, payload, out_dir, cancel=None):
        try:
            prompt = build_prompt(capability, payload)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        if capability in ("image_qc", "scene_annotate"):
            content = self._qc_content(prompt, payload)
        elif capability == "script" and payload.get("character_design"):
            content = self._design_content(prompt, payload)
        else:
            content = prompt
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        # 流式必选:整集剧本/圣经生成常超 10 分钟,非流式会被远端断连。
        text = _stream_claude_text(
            self.name, f"{endpoint}/v1/messages",
            headers={
                "x-api-key": self.conf["api_key"],
                "anthropic-version": "2023-06-01",
            },
            body={
                "model": self.conf.get("model") or self.DEFAULT_MODEL,
                "max_tokens": int(self.conf.get("max_tokens", 16000)),
                "messages": [{"role": "user", "content": content}],
            },
            timeout=self.conf.get("timeout", 600))
        data = extract_json(text)
        if data is None:
            raise ProviderError(f"{self.name} 应答中未找到 JSON 对象")
        if (capability == "script" and isinstance(data, dict)
                and isinstance(data.get("scenes"), list)):
            sanitize_script_entities(data)
        if capability == "storyboard" and isinstance(data, dict):
            reconcile_storyboard_prop_registry(
                data, payload.get("script") or {})
        try:
            if capability == "scene_annotate":
                from ..adapters.claude_script import validate_scene_annotation
                error = validate_scene_annotation(data)
                if error:
                    raise ProviderError(f"场景标注无效: {error}")
            elif capability == "image_qc":
                from ..adapters.claude_script import validate_image_qc
                error = validate_image_qc(data)
            elif capability == "script" and payload.get("prompt_refine"):
                from ..adapters.claude_script import validate_prompt_refine
                error = validate_prompt_refine(data)
            elif capability == "script" and payload.get("asset_prompt"):
                from ..adapters.claude_script import validate_asset_prompt
                error = validate_asset_prompt(data)
            elif capability == "script" and payload.get("shot_repair"):
                from ..adapters.claude_script import validate_shot_repair
                error = validate_shot_repair(data, payload)
            elif capability == "script" and payload.get("prop_design"):
                from ..adapters.claude_script import validate_prop_design
                error = validate_prop_design(data, payload)
            elif capability == "script" and payload.get("ai_director"):
                from ..adapters.claude_script import validate_ai_director
                error = validate_ai_director(data, payload)
            elif capability == "script" and payload.get("rule_appeal"):
                from ..adapters.claude_script import validate_rule_appeal
                error = validate_rule_appeal(data, payload)
            elif capability == "script" and payload.get("lesson_distill"):
                from ..adapters.claude_script import validate_lesson_distill
                error = validate_lesson_distill(data, payload)
            elif capability == "script" and payload.get("story_analysis"):
                # 与 CLI 桥(claude_script.run)保持同一分支:制作圣经/剧本
                # 自动分析的输出没有 scenes,必须用 story_analysis 校验器,
                # 否则永远"缺少 scenes"并静默回退 mock 污染事实源。
                error = validate_story_analysis(
                    data, require_resolved_identity=False)
            elif capability == "script":
                error = validate_script(data, payload)
            else:
                error = validate_storyboard(data)
        except Exception as exc:   # 任何畸形结构都转产线错误,可自动回退
            raise ProviderError(
                f"{self.name} 输出结构异常: {exc}") from exc
        if error:
            raise ProviderError(f"{self.name} 输出校验失败: {error}")
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call, data=data)


class OpenAIChatProvider(Provider):
    """OpenAI 兼容对话 API 编剧(DeepSeek / 通义 / Kimi / 本地网关等)。

    与 Claude/Codex 编剧共用同一套提示词与校验器,产出口径完全一致;
    校验失败时按"就地修复"原则先让同一模型改错误字段并复检,
    复检仍不过才抛错交回路由。
    """

    DEFAULT_ENDPOINT = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"
    LEGACY_MODEL_MODES = {
        "deepseek-chat": "disabled",
        "deepseek-reasoner": "enabled",
    }

    def _model_and_thinking(self, endpoint=None):
        """返回实际请求型号，并保留旧 DeepSeek 别名的思考语义。"""
        endpoint = str(
            endpoint or self.conf.get("endpoint")
            or self.DEFAULT_ENDPOINT).rstrip("/")
        model = str(
            self.conf.get("model") or self.DEFAULT_MODEL).strip()
        official = is_official_deepseek_endpoint(endpoint)
        legacy_mode = self.LEGACY_MODEL_MODES.get(model) if official else None
        if legacy_mode:
            model = self.DEFAULT_MODEL
        raw_mode = self.conf.get("thinking_mode")
        if raw_mode is None:
            raw_mode = legacy_mode
        if raw_mode is None and official and model == self.DEFAULT_MODEL:
            # deepseek-v4-flash 默认会开启思考；AIFOS 旧 deepseek-chat
            # 通道是秒级非思考回退，升级时保持行为、延迟与成本稳定。
            raw_mode = "disabled"
        if isinstance(raw_mode, bool):
            mode = "enabled" if raw_mode else "disabled"
        else:
            mode = str(raw_mode or "").strip().lower()
        if mode not in ("", "enabled", "disabled"):
            raise ProviderError(
                "thinking_mode 只允许 enabled 或 disabled")
        return model, mode or None

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key"
        try:
            self._model_and_thinking()
        except ProviderError as exc:
            return False, str(exc)
        return True, ""

    def ping(self):
        """真实连通性测试:发一个极小的对话请求。

        max_tokens 极小时 finish_reason 必然是 length,那是预期而非
        故障——连通性只看请求有没有被正常受理。
        """
        try:
            self._chat([{"role": "user", "content": "ping"}],
                       max_tokens=4, timeout=30)
        except ProviderError as exc:
            if "max_tokens 上限被截断" not in str(exc):
                return False, str(exc)
        model, _ = self._model_and_thinking()
        return True, f"真实连通成功(model={model})"

    def _chat(self, messages, max_tokens=None, timeout=None):
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        if endpoint.endswith("/chat/completions"):
            url = endpoint
        elif endpoint.endswith("/v1"):
            url = f"{endpoint}/chat/completions"
        else:
            url = f"{endpoint}/v1/chat/completions"
        model, thinking_mode = self._model_and_thinking(endpoint)
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_tokens
                              or self.conf.get("max_tokens", 32768)),
            "stream": False,
        }
        if thinking_mode:
            body["thinking"] = {"type": thinking_mode}
        temperature = self.conf.get("temperature")
        if temperature is not None and thinking_mode != "enabled":
            body["temperature"] = float(temperature)
        reply = _request_json(
            self.name, url,
            headers={"Authorization": f"Bearer {self.conf['api_key']}"},
            body=body,
            timeout=timeout or self.conf.get("timeout", 1800))
        choices = reply.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name} 应答缺少 choices")
        message = (choices[0] or {}).get("message") or {}
        finish = str((choices[0] or {}).get("finish_reason") or "")
        text = str(message.get("content") or "")
        if finish == "length":
            raise ProviderError(
                f"{self.name} 输出达到 max_tokens 上限被截断,"
                "请调大 providers 配置中的 max_tokens")
        return text

    def _validate(self, capability, payload, data):
        from ..adapters.claude_script import (validate_image_qc,
                                              validate_prompt_refine,
                                              validate_shot_repair)
        if capability == "image_qc":
            return validate_image_qc(data)
        if capability == "script" and payload.get("prompt_refine"):
            return validate_prompt_refine(data)
        if capability == "script" and payload.get("asset_prompt"):
            from ..adapters.claude_script import validate_asset_prompt
            return validate_asset_prompt(data)
        if capability == "script" and payload.get("shot_repair"):
            return validate_shot_repair(data, payload)
        if capability == "script" and payload.get("prop_design"):
            from ..adapters.claude_script import validate_prop_design
            return validate_prop_design(data, payload)
        if capability == "script" and payload.get("ai_director"):
            from ..adapters.claude_script import validate_ai_director
            return validate_ai_director(data, payload)
        if capability == "script" and payload.get("rule_appeal"):
            from ..adapters.claude_script import validate_rule_appeal
            return validate_rule_appeal(data, payload)
        if capability == "script" and payload.get("lesson_distill"):
            from ..adapters.claude_script import validate_lesson_distill
            return validate_lesson_distill(data, payload)
        if capability == "script" and payload.get("story_analysis"):
            return validate_story_analysis(
                data, require_resolved_identity=False)
        if capability == "script":
            return validate_script(data, payload)
        return validate_storyboard(data)

    def _postprocess(self, capability, data, payload=None):
        if (capability == "script" and isinstance(data, dict)
                and isinstance(data.get("scenes"), list)):
            sanitize_script_entities(data)
        if capability == "storyboard" and isinstance(data, dict):
            reconcile_storyboard_prop_registry(
                data, (payload or {}).get("script") or {})
        return data

    def generate(self, capability, payload, out_dir, cancel=None):
        try:
            prompt = build_prompt(capability, payload)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        messages = [
            {"role": "system",
             "content": "你只输出一个 JSON 对象,不要任何解释或 Markdown 代码块。"},
            {"role": "user", "content": prompt},
        ]
        text = self._chat(messages)
        data = extract_json(text)
        if data is None:
            raise ProviderError(f"{self.name} 应答中未找到 JSON 对象")
        data = self._postprocess(capability, data, payload)
        try:
            error = self._validate(capability, payload, data)
        except Exception as exc:
            raise ProviderError(
                f"{self.name} 输出结构异常: {exc}") from exc
        if error:
            # 就地修复:内容已在手,只是字段不合规,先让同一模型改错处。
            fixed, note = self._repair(capability, payload, data, error)
            if fixed is None:
                raise ProviderError(
                    f"{self.name} 输出校验失败: {error}{note}")
            data = fixed
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call, data=data,
            model=self._model_and_thinking()[0])

    @staticmethod
    def _error_shot_positions(error):
        """从校验判词里取出出问题的镜头序号(1-indexed 位置)。"""
        return sorted({
            int(value) for value in re.findall(r"镜头(\d+)", str(error or ""))
        })

    def _repair_shots(self, payload, data, error):
        """只回传出问题的镜头,本地合并——整份重发必被 max_tokens 截断。

        一整集分镜 JSON 常 40KB 以上,远超对话模型的输出上限;要求模型
        「输出完整 JSON」时,修复调用必然中途截断,连带把这条产线判死
        (《长夏记事》实案:codex_writer 与 deepseek 双双卡在同一校验,
        deepseek 的补救又被截断,整集 storyboard 阶段无可用 Provider)。
        """
        shots = data.get("shots")
        positions = self._error_shot_positions(error)
        if not positions or not isinstance(shots, list):
            return None, ""
        broken = [
            {"_position": position, **shots[position - 1]}
            for position in positions
            if 1 <= position <= len(shots)
            and isinstance(shots[position - 1], dict)
        ]
        if not broken or len(broken) >= len(shots):
            return None, ""  # 整份都坏就没有省的余地,走原路
        messages = [
            {"role": "system",
             "content": "你只输出一个 JSON 对象,不要任何解释或代码块。"},
            {"role": "user", "content":
                "你生成的分镜未通过机器校验:\n" + str(error) + "\n\n"
                "下面只给出出问题的镜头(每个带 _position 标明它在整份"
                "分镜里的序号)。只修复校验指出的字段,其余一字不动。"
                '输出 {"shots":[...]},数组里每个对象保留原 _position,'
                "不要包含其它镜头。\n" +
                json.dumps(broken, ensure_ascii=False)},
        ]
        try:
            text = self._chat(messages)
        except ProviderError as exc:
            return None, f"(镜头级就地修复调用失败: {exc})"
        patch = extract_json(text)
        rows = (patch or {}).get("shots")
        if not isinstance(rows, list) or not rows:
            return None, "(镜头级就地修复未返回 shots)"
        merged = _merge_storyboard_shot_repairs(
            data, patch, positions,
            allowed_fields=_storyboard_error_fields(error))
        if merged is None:
            return None, "(镜头级就地修复没有可合并的镜头)"
        applied = sum(
            1 for position in positions
            if 1 <= position <= len(merged["shots"])
            and merged["shots"][position - 1] != data["shots"][position - 1])
        return merged, f"(镜头级就地修复合并 {applied} 个镜头)"

    def _repair(self, capability, payload, data, error):
        # 分镜整份太大,先试镜头级增量修复;失败再退回整份重发。
        if capability == "storyboard" and isinstance(data, dict):
            patched, note = self._repair_shots(payload, data, error)
            if patched is not None:
                fixed = self._postprocess(capability, patched, payload)
                if not self._validate(capability, payload, fixed):
                    return fixed, note
        try:
            source = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return None, "(产出不可序列化,跳过就地修复)"
        messages = [
            {"role": "system",
             "content": "你只输出一个 JSON 对象,不要任何解释或 Markdown 代码块。"},
            {"role": "user", "content":
                "你刚生成的 JSON 产出未通过机器校验:\n"
                f"{error}\n\n只修复校验指出的字段,其余内容一字不动;"
                "输出完整 JSON。\n原 JSON:\n" + source},
        ]
        try:
            text = self._chat(messages)
        except ProviderError as exc:
            return None, f"(就地修复调用失败: {exc})"
        fixed = extract_json(text)
        if fixed is None:
            return None, "(就地修复输出中未找到 JSON)"
        if capability == "storyboard":
            positions = self._error_shot_positions(error)
            fixed = _merge_storyboard_full_repair(
                data, fixed, positions=positions,
                allowed_fields=_storyboard_error_fields(error))
            if fixed is None:
                return None, "(就地修复输出未返回可安全合并的 shots)"
        fixed = self._postprocess(capability, fixed, payload)
        try:
            fixed_error = self._validate(capability, payload, fixed)
        except Exception as exc:
            return None, f"(就地修复复检异常: {exc})"
        if fixed_error:
            return None, f"(就地修复复检仍未通过: {str(fixed_error)[:300]})"
        return fixed, ""


class OpenAIImageProvider(Provider):
    """OpenAI 兼容出图 API:/v1/images/generations,b64_json 或 url 均可。

    产物文件名与 Codex 出图桥保持一致,两条产线可互换、增量互认。
    """

    DEFAULT_ENDPOINT = "https://api.openai.com"
    DEFAULT_MODEL = "gpt-image-2"
    QUALITY_MAP = {"low": "low", "medium": "medium", "high": "high"}
    QUALITY_ALIASES = {"lite": "low"}
    HIGH_QUALITY_TASKS = {"important", "final", "complex_text"}

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key"
        return True, ""

    def ping(self):
        """真实连通性测试:GET /v1/models 验证端点与 Key。"""
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        try:
            reply = _request_json(
                self.name, f"{endpoint}/v1/models",
                headers={"Authorization": f"Bearer {self.conf['api_key']}"},
                timeout=30)
        except ProviderError as exc:
            return False, str(exc)
        count = len(reply.get("data") or [])
        return True, ("真实连通成功" +
                      (f"(可用模型 {count} 个)" if count else ""))

    def _size(self, payload):
        aspect = payload.get("aspect", "9:16")
        if aspect == "16:9":
            return "1536x1024"
        if aspect == "9:16":
            return "1024x1536"
        if aspect == "2:1":
            raise ProviderError(
                f"{self.name} 不支持 2:1 等距圆柱全景尺寸，"
                "必须回退到原生支持 2:1 的图片产线")
        return "1024x1024"

    def _quality(self, payload):
        """返回 (AIFOS 质量档, OpenAI quality)。GPT high 是硬隔离
        资源：只允许 final/complex_text；batch/important 即使误传
        high 也钳制为 medium，防止批量或人物候选图误消耗高档。
        """
        quality = str(payload.get("image_quality") or "medium").lower()
        quality = self.QUALITY_ALIASES.get(quality, quality)
        if quality not in self.QUALITY_MAP:
            allowed = "|".join(self.QUALITY_MAP)
            raise ProviderError(
                f"未知 image_quality={quality!r};只允许 {allowed}")
        task_class = payload.get("image_task_class")
        if quality == "high" and task_class not in self.HIGH_QUALITY_TASKS:
            quality = "medium"
        return quality, self.QUALITY_MAP[quality]

    def _call_cost(self, payload):
        quality, _api_quality = self._quality(payload)
        costs = self.conf.get("cost_by_quality") or {}
        return float(costs.get(
            quality, costs.get("lite") if quality == "low"
            else self.cost_per_call))

    def _request_model(self, payload=None):
        payload = payload or {}
        configured = self.conf.get("model") or self.DEFAULT_MODEL
        requested = str(payload.get("model_override") or "").strip()
        if requested and requested != configured:
            raise ProviderError(
                f"{self.name} 请求模型 {requested} 与配置 {configured} 不匹配")
        return requested or configured

    def validate_request(self, capability, payload, model=""):
        """网络调用前验证模型、质量以及全部参考图都能真实上传。"""
        issues = []
        try:
            selected = self._request_model(
                {**(payload or {}), "model_override": model})
            if not selected:
                issues.append("未配置图片模型")
            self._quality(payload or {})
        except ProviderError as exc:
            issues.append(str(exc))
        prompt = str(
            (payload or {}).get("prompt_compact")
            or (payload or {}).get("prompt") or "").strip()
        if not prompt:
            issues.append("最终提示词为空")
        entries = _reference_entries(payload or {})
        if not entries:
            issues.append("API 加速必须携带至少一张真实参考图")
        for entry in entries:
            uri = entry["uri"]
            if uri.startswith(("http://", "https://", "data:image/")):
                issues.append(
                    f"{self.name} 多图编辑只接受可上传的本地参考图: {uri}")
                continue
            path = Path(uri)
            if not path.exists() or not path.is_file():
                issues.append(f"必需参考图不存在: {uri}")
            elif path.suffix.lower() not in _IMG_MEDIA:
                issues.append(f"不支持参考图格式: {path.suffix}")
        return issues

    def _audit_data(self, data, payload, unit_cost):
        quality, _api_quality = self._quality(payload)
        return {
            **data,
            "model": self._request_model(payload),
            "image_task_class": payload.get("image_task_class", "legacy"),
            "image_quality": quality,
            "unit_cost": unit_cost,
        }

    def _semantic_prompt(self, prompt, payload, refs):
        """API 提示词补齐与 CLI 一致的语义约束;有参考图时点明"以所附
        图片为准",保证同一角色跨图一致。"""
        complete = bool(payload.get("prompt_contract_complete"))
        parts = [prompt]
        if not complete:
            parts.extend((_api_style_line(payload), _api_space_line(payload)))
        parts.append(_SUBJECT_DIRECTIVE)
        if payload.get("studio_asset"):
            # 资产工坊自建资产:与 CLI 桥同一套单一职责约束,避免
            # 「画件道具」返回一张有人举着它的剧照。
            parts.append(
                _STUDIO_ASSET_RULES.get(str(payload["studio_asset"]), ""))
            parts.append(
                "这张图会进入用户的资产库并在后续制作中作为参考图复用,"
                "必须干净可复用:不加字幕条、水印、Logo、边框和拼图分格。")
        if (payload.get("portrait") or payload.get("portrait_candidate")
                or payload.get("character_sheet")) and not complete:
            parts.append(_CHARACTER_BACKGROUND_DIRECTIVE)
        story = payload.get("character_background")
        if story and not complete:
            if isinstance(story, (dict, list)):
                story = json.dumps(story, ensure_ascii=False, separators=(",", ":"))
            parts.append(
                "人物剧情设定硬约束(优先于通用造型模板):"
                f"{story};服装、发型、材质、配色和道具必须符合其时代/世界观、"
                "职业、性格、关系与当前剧情场合,不得默认现代都市便服。")
        if refs:
            if payload.get("portrait_candidate"):
                parts.append(
                    "已随请求真实上传定角参考图。参考图人物身份与脸是最高标准；"
                    "脸型、五官比例、眼鼻嘴结构、肤色、年龄感、性别表达、"
                    "发际线、发型轮廓、发量、发色家族、妆造和稳定身份特征必须保持"
                    "同一个人，不得改脸、换发型或换妆造；参考图服装、配饰、"
                    "手持物、姿势、背景和光线不得覆盖"
                    "本次初始造型合同；同一人物四张候选必须复用完全相同的最终"
                    "提示词，只靠模型随机采样，禁止换装、换妆、换动作、加入"
                    "淋湿/泥污/伤情或通过不同剧情阶段制造差异。")
            elif payload.get("reference_manifest"):
                parts.append(
                    "本请求已提供逐图参考对照表。每张参考图只能执行表中声明的"
                    "单一职责；身份图不得强制服装/姿势/背景，服装图不得覆盖脸，"
                    "场景图不得带入人物，构图图不得覆盖身份，风格图只控制媒介与"
                    "色光。禁止跨用途传播或把一个人的任何属性复制给另一个人。")
            else:
                parts.append(
                    "已随请求真实上传参考图(人物镜头第一组为人工锁定最终立绘,"
                    "其后为上一镜衔接/场景/风格与用户参考)。人物参考图的身份优先级为:"
                    "脸型、五官比例、眼鼻嘴结构、肤色与年龄感、发际线、发型轮廓、"
                    "发色、眉眼妆/眼线/睫毛、唇妆和身份配饰;这些必须保持为同一个人,"
                    "禁止脸和发型漂移,禁止漂移。服装、服装颜色与材质、动作、场景和光影按本镜"
                    "剧本及当集造型执行,允许与人物参考图服装不同;除非提示词明确要求"
                    "保留参考图服装,不得把参考图服装当作必须复制的身份条件。")
        safe_area = self._native_crop_safe_area_directive(payload)
        if safe_area:
            parts.append(safe_area)
        return "".join(p for p in parts if p)

    def _native_crop_safe_area_directive(self, payload):
        """Tell the model which native margins post-processing will remove."""
        aspect = str((payload or {}).get("aspect") or "9:16")
        target_size = _CANONICAL_IMAGE_SIZES.get(aspect)
        if target_size is None:
            return ""
        size = str(self._size(payload or {}))
        match = re.fullmatch(r"(\d+)x(\d+)", size)
        if not match:
            return ""
        source_width, source_height = map(int, match.groups())
        crop_x, crop_y, crop_width, crop_height = _center_crop_box(
            source_width, source_height, *target_size)
        if (crop_x, crop_y, crop_width, crop_height) == (
                0, 0, source_width, source_height):
            return ""
        if crop_x:
            retained = crop_width / source_width * 100
            margin = crop_x / source_width * 100
            return (
                f"画幅安全区硬约束:接口原生画布为{source_width}x{source_height}，"
                f"落盘时会确定性中心裁切为9:16；所有人物脸、手、关键道具和"
                f"可读文字必须完整位于中央{retained:.1f}%宽度内，左右各约"
                f"{margin:.1f}%仅为可丢弃背景，不得放置关键内容。")
        retained = crop_height / source_height * 100
        margin = crop_y / source_height * 100
        return (
            f"画幅安全区硬约束:接口原生画布为{source_width}x{source_height}，"
            f"落盘时会确定性中心裁切为9:16；所有人物脸、手、关键道具和"
            f"可读文字必须完整位于中央{retained:.1f}%高度内，上下各约"
            f"{margin:.1f}%仅为可丢弃背景，不得放置关键内容。")

    def _normalize_output(self, path, payload):
        return _normalize_generated_image(path, payload)

    def _normalization_preflight(self, payload):
        """Refuse a paid call before dispatch when local canonicalization cannot run."""
        aspect = str((payload or {}).get("aspect") or "9:16")
        target_size = _CANONICAL_IMAGE_SIZES.get(aspect)
        if target_size is None:
            return
        size = str(self._size(payload or {}))
        requested = re.fullmatch(r"(\d+)x(\d+)", size)
        if requested and tuple(map(int, requested.groups())) == target_size:
            return
        if not _image_converter()[1]:
            raise ProviderError(
                f"{self.name} 原生输出 {size} 需要本地归一化为"
                f"{target_size[0]}x{target_size[1]}，但本机没有 sips/ffmpeg；"
                "已在调用图片 API 前停止，禁止重复付费抽图")

    def _gen_image(self, prompt, size, dest, payload=None):
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        timeout = self.conf.get("timeout", 300)
        payload = payload or {}
        self._normalization_preflight(payload)
        model = self._request_model(payload)
        refs = _local_refs(payload)
        _quality, api_quality = self._quality(payload)
        if payload.get("require_reference_images") and not refs:
            raise ProviderError(
                f"{self.name} 人物出图要求参考图，但没有可上传的本地图片")
        if payload.get("require_reference_images"):
            declared = _reference_entries(payload)
            accepted = {str(path) for path in refs}
            missing = [entry["uri"] for entry in declared
                       if (not Path(entry["uri"]).exists()
                           or str(Path(entry["uri"])) not in accepted)]
            if missing:
                raise ProviderError(
                    f"{self.name} 未能上传全部必需参考图: "
                    + "、".join(missing))
        full_prompt = self._semantic_prompt(prompt, payload, refs)
        if refs:
            # 有参考图 → images/edits 多图输入(真正把参考图喂给模型),
            # 与 CLI 用参考图一致
            reply = _multipart_post(
                self.name, f"{endpoint}/v1/images/edits",
                headers={"Authorization": f"Bearer {self.conf['api_key']}"},
                fields={"model": model, "prompt": full_prompt[:32000],
                        "size": size, "quality": api_quality, "n": "1"},
                files=[("image[]", path) for path in refs],
                timeout=timeout)
        else:
            reply = _request_json(
                self.name, f"{endpoint}/v1/images/generations",
                headers={"Authorization": f"Bearer {self.conf['api_key']}"},
                body={"model": model, "prompt": full_prompt[:32000],
                      "n": 1, "size": size, "quality": api_quality},
                timeout=timeout)
        item = (reply.get("data") or [{}])[0]
        if item.get("b64_json"):
            dest.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            _download(self.name, item["url"], dest, timeout)
        else:
            raise ProviderError(f"{self.name} 应答缺少 b64_json/url")
        return self._normalize_output(dest, payload)

    def generate(self, capability, payload, out_dir, cancel=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        size = self._size(payload)
        call_cost = self._call_cost(payload)
        # 有镜头合同时只把短版交给模型；payload['prompt'] 仍作为审计原文保存。
        prompt = payload.get("prompt_compact") or payload.get("prompt", "")
        if payload.get("feedback"):
            prompt = f"{prompt}。修改意见(必须落实):{payload['feedback']}"
        if capability == "image":
            safe = _safe_name(payload.get("art_name", ""))
            if payload.get("studio_asset"):
                kind = str(payload["studio_asset"])
                target = out_dir / f"studio_{kind}_{safe}.png"
                data = {"name": payload.get("art_name"),
                        "studio_asset": kind}
            elif payload.get("prop_candidate"):
                target = out_dir / f"prop_{safe}.png"
                data = {
                    "name": payload.get("art_name"),
                    "prop": payload.get("prop_name", ""),
                }
            elif payload.get("portrait"):
                target = out_dir / f"portrait_{safe}.png"
                data = {"name": payload.get("art_name")}
            elif payload.get("character_sheet"):
                key = payload["character_sheet"]
                target = out_dir / f"sheet_{safe}_{key}.png"
                data = {"name": payload.get("art_name"), "sheet": key}
            elif payload.get("scene_art"):
                target = out_dir / f"scene_{safe}.png"
                data = {"name": payload.get("art_name")}
            else:
                shot_no = int(payload["shot_no"])
                target = out_dir / f"shot_{shot_no:03d}.keyframe.png"
                prompt = (f"{prompt}。出场角色:"
                          f"{'、'.join(payload.get('characters', []))}")
                data = {"shot_no": shot_no}
            normalization = self._gen_image(prompt, size, target, payload)
            data["image_normalization"] = normalization
            return ProviderResult(provider=self.name,
                                  cost=call_cost,
                                  data=self._audit_data(
                                      data, payload, call_cost),
                                  uri=str(target),
                                  model=self._request_model(payload))
        if capability == "frames":
            shot_no = int(payload["shot_no"])
            first = out_dir / f"shot_{shot_no:03d}.first.png"
            last = out_dir / f"shot_{shot_no:03d}.last.png"
            chain_first = payload.get("chain_first_uri", "")
            first_source = "generated"
            last_source = "generated"
            calls = 0
            normalizations = {}
            keyframe_phase = _api_keyframe_phase(payload)
            frame_prompts = payload.get("frame_prompt_compacts") or {}
            frame_manifests = payload.get("frame_reference_manifests") or {}
            first_prompt = str(
                frame_prompts.get("first_frame") or prompt)
            last_prompt = str(
                frame_prompts.get("last_frame") or prompt)
            keyframe = Path(_api_keyframe_uri(payload, keyframe_phase))
            keyframe_valid = (
                keyframe.exists() and keyframe.suffix.lower() in _IMG_MEDIA)
            if chain_first and Path(chain_first).exists():
                # 帧链:首帧固定为上一镜尾帧,只生成尾帧(拼接连贯)
                import shutil as _sh
                _sh.copyfile(chain_first, first)
                first_source = "previous_tail"
                normalizations["first"] = self._normalize_output(
                    first, payload)
            elif keyframe_valid and keyframe_phase == "start":
                # 关键图明确属于动作起点时，才允许直接复用为首帧。
                import shutil as _sh
                _sh.copyfile(keyframe, first)
                first_source = "keyframe"
                normalizations["first"] = self._normalize_output(
                    first, payload)

            if keyframe_valid and keyframe_phase == "end":
                # 关键图明确属于动作终点时，只能成为尾帧。即使已有上一镜
                # 尾帧作为本镜首帧，也保持两端各归其位，零调用即可复用。
                import shutil as _sh
                _sh.copyfile(keyframe, last)
                last_source = "keyframe"
                normalizations["last"] = self._normalize_output(last, payload)

            if first_source == "generated":
                first_payload = dict(payload)
                first_payload["prompt_compact"] = first_prompt
                first_payload["reference_manifest"] = (
                    frame_manifests["first_frame"]
                    if "first_frame" in frame_manifests
                    else payload.get("reference_manifest") or [])
                if keyframe_valid and keyframe_phase != "end":
                    # end/freeze/未知关键图只能作身份、场景、构图参考；
                    # 绝不能通过文件换名冒充动作起点。
                    first_payload["frame_keyframe_uri"] = str(keyframe)
                normalizations["first"] = self._gen_image(
                    f"{first_prompt}。首帧:动作起始瞬间，发生在动作终点之前；"
                    "严格按起始状态生成，不得复制或倒置终点状态，构图稳定",
                    size, first, first_payload)
                calls += 1
            if cancel is not None and cancel():
                from ..errors import ProduceCancelled
                raise ProduceCancelled("已手动停止")
            if last_source == "generated":
                # 尾帧以首帧为参考,保证同角色同场景连贯。freeze/未知
                # 关键图仍可作为非边界构图参考，但不能替代任一端点。
                last_payload = {**payload, "chain_first_uri": str(first)}
                last_payload["prompt_compact"] = last_prompt
                last_payload["reference_manifest"] = (
                    frame_manifests["last_frame"]
                    if "last_frame" in frame_manifests
                    else payload.get("reference_manifest") or [])
                if keyframe_valid and keyframe_phase not in {"start", "end"}:
                    last_payload["frame_keyframe_uri"] = str(keyframe)
                normalizations["last"] = self._gen_image(
                    f"{last_prompt}。尾帧:动作结束瞬间；与首帧保持人物身份"
                    "和不可变场景结构连续，服装、发型、持物及位置严格服从"
                    "尾帧合同；剧本明确变化时不得强行沿用首帧",
                    size, last, last_payload)
                calls += 1
            return ProviderResult(
                provider=self.name, cost=call_cost * calls,
                data=self._audit_data({
                    "first": str(first), "last": str(last),
                    "first_source": first_source,
                    "last_source": last_source,
                    "keyframe_phase": keyframe_phase,
                    "generation_calls": calls,
                    "image_normalization": normalizations,
                }, payload, call_cost),
                uri=str(first), model=self._request_model(payload))
        if capability == "cover":
            target = out_dir / "cover.png"
            normalization = self._gen_image(
                f"短视频封面:《{payload.get('title', '')}》"
                f"第{payload.get('episode', 0)}集,"
                f"{payload.get('tagline', '')}。{prompt}", size, target,
                payload)
            return ProviderResult(provider=self.name,
                                  cost=call_cost,
                                  data=self._audit_data(
                                      {"image_normalization": normalization},
                                      payload, call_cost),
                                  uri=str(target),
                                  model=self._request_model(payload))
        raise ProviderError(f"{self.name} 不支持能力: {capability}")


class SeedreamImageProvider(OpenAIImageProvider):
    """火山方舟 Seedream 5.0 Lite 批量出图。

    与 OpenAIImageProvider 共用产物命名/帧链逻辑，但调用 Ark
    /api/v3/images/generations，参考图以 URL 或 Base64 data URL
    真实放入 image[]。组图功能始终关闭，保证每次只计费一张。
    """

    DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com"
    DEFAULT_MODEL = "doubao-seedream-5-0-lite-260128"

    def available(self, capability):
        ok, reason = Provider.available(self, capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key"
        if not (self.conf.get("model") or self.DEFAULT_MODEL):
            return False, "未配置模型 ID"
        return True, ""

    def _size(self, payload):
        configured = str(self.conf.get("image_size") or "2K")
        if configured.upper() != "2K":
            return configured
        # 2K 档使用明确像素尺寸，避免只传 "2K" 时画幅被模型猜错。
        return {
            "16:9": "2560x1440",
            "9:16": "1440x2560",
            "4:3": "2304x1728",
            "3:4": "1728x2304",
            "3:2": "2496x1664",
            "2:3": "1664x2496",
            "1:1": "2048x2048",
            # 场景全景母版:等距圆柱投影必须是 2:1,退化成方图会把
            # 360° 环视压成普通广角,四向视角就对不上同一个空间。
            # Seedream 5 Lite 同时要求至少 3,686,400 像素；
            # 2880x1440 保持严格 2:1 且满足该真实 API 下限。
            "2:1": "2880x1440",
        }.get(payload.get("aspect", "9:16"), "2048x2048")

    def _audit_data(self, data, payload, unit_cost):
        quality = str(payload.get("image_quality") or "medium").lower()
        quality = self.QUALITY_ALIASES.get(quality, quality)
        return {
            **data,
            "model": self._request_model(payload),
            "image_task_class": payload.get("image_task_class", "legacy"),
            "image_quality": quality,
            "unit_cost": unit_cost,
        }

    def validate_request(self, capability, payload, model=""):
        issues = []
        try:
            self._request_model({**(payload or {}), "model_override": model})
        except ProviderError as exc:
            issues.append(str(exc))
        if not str(
                (payload or {}).get("prompt_compact")
                or (payload or {}).get("prompt") or "").strip():
            issues.append("最终提示词为空")
        entries = _reference_entries(payload or {})
        if not entries:
            issues.append("API 加速必须携带至少一张真实参考图")
        max_refs = int(self.conf.get("max_reference_images", 10))
        if len(entries) > max_refs:
            issues.append(
                f"参考图 {len(entries)} 张超过模型上限 {max_refs} 张")
        max_bytes = int(self.conf.get(
            "max_reference_bytes", 10 * 1024 * 1024))
        for entry in entries:
            uri = entry["uri"]
            if uri.startswith(("http://", "https://", "data:image/")):
                continue
            path = Path(uri)
            if not path.exists() or not path.is_file():
                issues.append(f"必需参考图不存在: {uri}")
            elif path.suffix.lower() not in _IMG_MEDIA:
                issues.append(f"不支持参考图格式: {path.suffix}")
            elif path.stat().st_size > max_bytes:
                issues.append(f"参考图超过 {max_bytes} 字节: {uri}")
        return issues

    def _seedream_references(self, payload):
        entries = _reference_entries(payload)
        if payload.get("require_reference_images") and not entries:
            raise ProviderError(
                f"{self.name} 任务要求参考图，但请求未携带图片")
        max_refs = int(self.conf.get("max_reference_images", 10))
        if len(entries) > max_refs:
            raise ProviderError(
                f"{self.name} 参考图 {len(entries)} 张超过上限 {max_refs};"
                "禁止丢图后降级生成")
        max_bytes = int(self.conf.get(
            "max_reference_bytes", 10 * 1024 * 1024))
        images = []
        for entry in entries:
            uri = entry["uri"]
            if uri.startswith(("https://", "http://", "data:image/")):
                images.append(uri)
                continue
            path = Path(uri)
            if not path.exists() or not path.is_file():
                raise ProviderError(
                    f"{self.name} 必需参考图不存在: {uri};"
                    "禁止退化为纯文字生图")
            if path.suffix.lower() not in _IMG_MEDIA:
                raise ProviderError(
                    f"{self.name} 不支持参考图格式: {path.suffix}")
            if path.stat().st_size > max_bytes:
                raise ProviderError(
                    f"{self.name} 参考图超过 {max_bytes} 字节: {uri}")
            images.append(_data_image(path))
        return entries, images

    def _seedream_prompt(self, prompt, payload, entries):
        full_prompt = self._semantic_prompt(prompt, payload, entries)
        if not entries:
            return full_prompt
        mapping = "；".join(
            f"图{index}={entry['label']}"
            for index, entry in enumerate(entries, 1))
        return (
            f"{full_prompt}\n参考图顺序与身份硬映射:{mapping}。"
            "必须按该映射识别并保持每位角色身份，不得串角。"
            "P01/P02 等空间编号、图序、箭头、坐标和标签只用于"
            "规划与身份映射，最终成片不得画出任何这类编号、箭头或标签。")

    def _gen_image(self, prompt, size, dest, payload=None):
        payload = payload or {}
        self._normalization_preflight(payload)
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        timeout = self.conf.get("timeout", 300)
        model = self._request_model(payload)
        entries, images = self._seedream_references(payload)
        body = {
            "model": model,
            "prompt": self._seedream_prompt(prompt, payload, entries)[:32000],
            "size": size,
            "sequential_image_generation": "disabled",
            "response_format": "b64_json",
            "watermark": bool(self.conf.get("watermark", False)),
        }
        if images:
            body["image"] = images
        reply = _request_json(
            self.name, f"{endpoint}/api/v3/images/generations",
            headers={"Authorization": f"Bearer {self.conf['api_key']}"},
            body=body, timeout=timeout)
        items = reply.get("data") or []
        if len(items) != 1:
            raise ProviderError(
                f"{self.name} 已强制单图但应答包含 {len(items)} 张图")
        item = items[0]
        if item.get("b64_json"):
            dest.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            _download(self.name, item["url"], dest, timeout)
        else:
            raise ProviderError(f"{self.name} 应答缺少 b64_json/url")
        return self._normalize_output(dest, payload)


class DoubaoTtsProvider(Provider):
    """豆包(火山引擎)语音合成 TTS:配音的第三方 API 通道。

    默认配音方案是 Seedance2 有声视频(配音随视频生成,无需本步);
    只有视频产线不带配音时才会走到这里。需要 appid + access token。
    """

    DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"
    DEFAULT_VOICE = "BV700_streaming"     # 通用女声;可在设置里换音色

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key(access token)"
        if not self.conf.get("appid"):
            return False, "未配置 appid"
        return True, ""

    def _synthesize(self, text):
        """→ (音频字节, 时长秒)。"""
        endpoint = self.conf.get("endpoint") or self.DEFAULT_ENDPOINT
        reply = _request_json(
            self.name, endpoint,
            headers={"Authorization": f"Bearer;{self.conf['api_key']}"},
            body={
                "app": {"appid": self.conf["appid"],
                        "token": self.conf["api_key"],
                        "cluster": self.conf.get("cluster", "volcano_tts")},
                "user": {"uid": "aifos"},
                "audio": {
                    "voice_type": self.conf.get("voice_type")
                    or self.DEFAULT_VOICE,
                    "encoding": "mp3",
                    "speed_ratio": float(self.conf.get("speed_ratio", 1.0)),
                },
                "request": {"reqid": uuid.uuid4().hex, "text": text,
                            "operation": "query"},
            },
            timeout=self.conf.get("timeout", 60))
        if reply.get("code") not in (3000, 0):
            raise ProviderError(
                f"{self.name} 合成失败 code={reply.get('code')}: "
                f"{reply.get('message', '')}")
        audio_b64 = reply.get("data")
        if not audio_b64:
            raise ProviderError(f"{self.name} 应答缺少音频数据")
        duration_ms = (reply.get("addition") or {}).get("duration")
        try:
            duration = round(int(duration_ms) / 1000, 2)
        except (TypeError, ValueError):
            duration = round(max(1.0, len(text) * 0.18), 2)
        return base64.b64decode(audio_b64), duration

    def ping(self):
        """真实连通性测试:合成一个字,验证 appid/token/音色。"""
        try:
            self._synthesize("在")
        except ProviderError as exc:
            return False, str(exc)
        voice = self.conf.get("voice_type") or self.DEFAULT_VOICE
        return True, f"真实连通成功(voice_type={voice})"

    def generate(self, capability, payload, out_dir, cancel=None):
        if capability != "voice":
            raise ProviderError(f"{self.name} 不支持能力: {capability}")
        text = payload.get("text", "")
        if not text:
            raise ProviderError("配音文本为空")
        audio, duration = self._synthesize(text)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        line_no = int(payload.get("line_no", 0))
        dest = out_dir / f"line_{line_no:03d}.mp3"
        dest.write_bytes(audio)
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call,
            data={"line_no": line_no, "duration": duration},
            uri=str(dest))


class ArkVideoProvider(Provider):
    """火山方舟 Ark 视频 API(Seedance2 的 API 模式,即梦 CLI 的备用)。

    首尾帧图生视频:创建内容生成任务 → 轮询状态 → 下载 mp4 到产物目录,
    文件名与即梦 CLI 产线一致(shot_NNN.mp4)。
    """

    DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com"
    MODEL_HINT = ("模型 ID 需从方舟控制台【开通管理】复制:形如 "
                  "doubao-seedance-2-0-…(带日期后缀)或推理接入点 ep-…")

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key"
        if not self.conf.get("model"):
            return False, f"未配置模型 ID;{self.MODEL_HINT}"
        return True, ""

    def ping(self):
        """真实连通性测试:提交空任务,HTTP 400 = 端点可达且鉴权通过。"""
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        try:
            _request_json(
                self.name,
                f"{endpoint}/api/v3/contents/generations/tasks",
                {"Authorization": f"Bearer {self.conf['api_key']}"},
                body={"model": self.conf.get("model", ""),
                      "content": []},
                timeout=30)
        except ProviderError as exc:
            message = str(exc)
            if "HTTP 400" in message:
                return True, "真实连通成功(端点可达,鉴权通过)"
            if "NotFound" in message or "HTTP 404" in message:
                return False, (f"Key 已通过,但模型 ID 不存在或未开通。"
                               f"{self.MODEL_HINT};开通入口:方舟控制台 "
                               f"console.volcengine.com/ark → 开通管理 → "
                               f"Doubao-Seedance 2.0")
            return False, message
        return True, "真实连通成功"

    def _frame_content(self, path, role):
        if str(path).startswith(("http://", "https://")):
            return {
                "type": "image_url", "role": role,
                "image_url": {"url": str(path)},
            }
        raw = Path(path).read_bytes()
        suffix = Path(path).suffix.lower().lstrip(".") or "png"
        encoded = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url", "role": role,
            "image_url": {"url": f"data:image/{suffix};base64,{encoded}"},
        }

    def generate(self, capability, payload, out_dir, cancel=None):
        if capability != "video":
            raise ProviderError(f"{self.name} 不支持能力: {capability}")
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        headers = {"Authorization": f"Bearer {self.conf['api_key']}"}
        duration = int(payload.get("duration")
                       or self.conf.get("duration", 8))
        video_quality = str(payload.get("video_quality") or "medium")
        video_resolution = str(payload.get(
            "video_resolution") or self.conf.get("video_resolution", "720p"))
        if video_resolution.lower() not in ("480p", "720p", "1080p"):
            raise ProviderError(
                "Seedance video_resolution 只允许 480p/720p/1080p")
        prompt = payload.get("prompt_compact") or payload.get("prompt", "")
        dialogue = payload.get("dialogue") or {}
        if self.conf.get("audio_in_video", True) and dialogue.get("dialogue"):
            # Seedance2 有声视频:台词随视频自动配音
            prompt += (f"。让角色开口说出这句台词并自动配音"
                       f"(中文自然人声,口型对应):「{dialogue['dialogue']}」")
        content = [{
            "type": "text",
            "text": f"{prompt} "
                    f"--duration {duration} --resolution "
                    f"{video_resolution}",
        }]
        for key, role in (("first", "first_frame"), ("last", "last_frame")):
            if payload.get(key):
                content.append(self._frame_content(payload[key], role))
        requested_references = list(payload.get("reference_images") or [])[:7]
        # Ark/Seedance rejects a last-frame boundary mixed with generic
        # reference_image or draft_task content.  In a first+last contract the
        # two authored boundaries are the stronger continuity source, so keep
        # both boundaries and defer generic identity/scene references instead
        # of submitting an invalid request that can never start.
        submitted_references = (
            [] if payload.get("last") else requested_references)
        for uri in submitted_references:
            content.append(self._frame_content(uri, "reference_image"))
        tasks_url = f"{endpoint}/api/v3/contents/generations/tasks"
        if not self.conf.get("model"):
            raise ProviderError(f"{self.name} 未配置模型 ID;{self.MODEL_HINT}")
        created = _request_json(
            self.name, tasks_url, headers,
            body={"model": self.conf["model"],
                  "content": content},
            timeout=self.conf.get("timeout", 1800))
        task_id = created.get("id")
        if not task_id:
            raise ProviderError(f"{self.name} 创建任务未返回 id: {created}")
        poll = float(self.conf.get("poll", 5))
        deadline = time.monotonic() + float(self.conf.get("timeout", 1800))
        while True:
            status_reply = _request_json(
                self.name, f"{tasks_url}/{task_id}", headers,
                timeout=self.conf.get("timeout", 1800))
            status = status_reply.get("status")
            if status == "succeeded":
                break
            if status in ("failed", "cancelled"):
                error = status_reply.get("error") or {}
                raise ProviderError(
                    f"{self.name} 任务失败: "
                    f"{error.get('message', status)}")
            if time.monotonic() >= deadline:
                raise ProviderError(f"{self.name} 任务超时: {task_id}")
            if cancel is not None and cancel():
                from ..errors import ProduceCancelled
                raise ProduceCancelled(
                    f"已手动停止(不再等待 {self.name} 任务 {task_id})")
            time.sleep(poll)
        video_url = (status_reply.get("content") or {}).get("video_url")
        if not video_url:
            raise ProviderError(f"{self.name} 任务成功但没有 video_url")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        shot_no = int(payload.get("shot_no", 0))
        dest = out_dir / f"shot_{shot_no:03d}.mp4"
        _download(self.name, video_url, dest,
                  self.conf.get("timeout", 1800))
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call,
            data={"task_id": task_id, "duration": duration,
                  "video_quality": video_quality,
                  "video_resolution": video_resolution,
                  "reference_images_used": submitted_references,
                  "reference_images_deferred": [
                      uri for uri in requested_references
                      if uri not in submitted_references],
                  "reference_policy": (
                      "first_last_boundaries_exclusive"
                      if payload.get("last") else
                      "generic_references_allowed"),
                  "reference_assets": list(
                      payload.get("reference_assets") or [])},
            uri=str(dest))
