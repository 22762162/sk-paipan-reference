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
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..adapters.codex_image import SUBJECT_DIRECTIVE as _SUBJECT_DIRECTIVE
from ..adapters.codex_image import (
    CHARACTER_BACKGROUND_DIRECTIVE as _CHARACTER_BACKGROUND_DIRECTIVE,
)
from ..adapters.codex_image import _style_line as _api_style_line
from ..adapters.codex_image import _space_line as _api_space_line
from ..adapters.claude_script import (build_prompt, extract_json,
                                      validate_script, validate_storyboard)
from ..errors import ProviderError
from .base import Provider, ProviderResult


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
        with urllib.request.urlopen(request, timeout=timeout) as resp:
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
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            dest.write_bytes(resp.read())
    except Exception as exc:
        raise ProviderError(f"{name} 下载产物失败: {exc}") from exc


_IMG_MEDIA = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp"}


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
    for key in ("spatial_ref", "chain_first_uri", "scene_ref", "style_ref"):
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
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
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
        media = _IMG_MEDIA.get(path.suffix.lower(), "image/png")
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{field}"; '
                 f'filename="{path.name}"\r\n'
                 f"Content-Type: {media}\r\n\r\n").encode("utf-8")
        body += path.read_bytes()
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        url, data=bytes(body), method="POST",
        headers={**headers,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
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
        if capability == "image_qc":
            content = self._qc_content(prompt, payload)
        elif capability == "script" and payload.get("character_design"):
            content = self._design_content(prompt, payload)
        else:
            content = prompt
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        reply = _request_json(
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
        text = "".join(block.get("text", "")
                       for block in reply.get("content", [])
                       if block.get("type") == "text")
        data = extract_json(text)
        if data is None:
            raise ProviderError(f"{self.name} 应答中未找到 JSON 对象")
        try:
            if capability == "image_qc":
                from ..adapters.claude_script import validate_image_qc
                error = validate_image_qc(data)
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
                    "同一个人，不得改脸、换发型或换妆造；候选只允许在同一项目画风"
                    "下比较剧情服装细节、表情和轻微姿态，禁止通过更换媒介、渲染、"
                    "色彩系统或时代制造不同画风。")
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
        return "".join(p for p in parts if p)

    def _gen_image(self, prompt, size, dest, payload=None):
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        timeout = self.conf.get("timeout", 300)
        payload = payload or {}
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
            if payload.get("prop_candidate"):
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
            self._gen_image(prompt, size, target, payload)
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
            calls = 0
            if chain_first and Path(chain_first).exists():
                # 帧链:首帧固定为上一镜尾帧,只生成尾帧(拼接连贯)
                import shutil as _sh
                _sh.copyfile(chain_first, first)
                first_source = "previous_tail"
            else:
                keyframe = Path(payload.get("image_uri", ""))
                if (keyframe.exists() and keyframe.suffix.lower()
                        in _IMG_MEDIA):
                    import shutil as _sh
                    _sh.copyfile(keyframe, first)
                    first_source = "keyframe"
                else:
                    self._gen_image(
                        f"{prompt}。首帧:动作起始瞬间,构图稳定", size,
                        first, payload)
                    calls += 1
            if cancel is not None and cancel():
                from ..errors import ProduceCancelled
                raise ProduceCancelled("已手动停止")
            # 尾帧以首帧为参考,保证同角色同场景连贯
            self._gen_image(
                f"{prompt}。尾帧:动作结束瞬间,与首帧同场景同角色、"
                "人物服装道具一致",
                size, last, {**payload, "chain_first_uri": str(first)})
            calls += 1
            return ProviderResult(
                provider=self.name, cost=call_cost * calls,
                data=self._audit_data({
                    "first": str(first), "last": str(last),
                    "first_source": first_source,
                    "generation_calls": calls,
                }, payload, call_cost),
                uri=str(first), model=self._request_model(payload))
        if capability == "cover":
            target = out_dir / "cover.png"
            self._gen_image(
                f"短视频封面:《{payload.get('title', '')}》"
                f"第{payload.get('episode', 0)}集,"
                f"{payload.get('tagline', '')}。{prompt}", size, target,
                payload)
            return ProviderResult(provider=self.name,
                                  cost=call_cost,
                                  data=self._audit_data(
                                      {}, payload, call_cost),
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
        # 固定 seed:导演层按资产+版本派生,同版重画可复现、可控修图。
        if payload.get("seed") not in (None, ""):
            try:
                body["seed"] = int(payload["seed"])
            except (TypeError, ValueError):
                pass
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
        for uri in (payload.get("reference_images") or [])[:7]:
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
                  "reference_images_used": list(
                      (payload.get("reference_images") or [])[:7]),
                  "reference_assets": list(
                      payload.get("reference_assets") or [])},
            uri=str(dest))
