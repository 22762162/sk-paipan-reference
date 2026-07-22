"""直连 API Provider:剧本 / 图片 / 视频 / 配音的 API 模式产线。

与 CLI 桥(claude_script / codex_image / dreamina)互为主备,在 routing
里排在对应 CLI 之后:CLI 不可用或失败时自动切换到 API,全部只用标准库。

  claude_api  script/storyboard —— Anthropic Messages API(默认 claude-opus-4-8)
  image_api   image/frames/cover —— OpenAI 兼容 /v1/images/generations
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

from ..adapters.codex_image import GEN_DIRECTIVE as _GEN_DIRECTIVE
from ..adapters.codex_image import SUBJECT_DIRECTIVE as _SUBJECT_DIRECTIVE
from ..adapters.codex_image import _style_line as _api_style_line
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
    """本地参考图路径:风格基准图 + 人物设定图 + 用户参考图(去重存在的)。"""
    order = []
    for key in ("style_ref", "chain_first_uri"):
        val = payload.get(key)
        if val:
            order.append(val)
    for key in ("character_refs", "reference_images"):
        order.extend(payload.get(key) or [])
    seen, refs = set(), []
    for uri in order:
        if not uri or uri in seen:
            continue
        seen.add(uri)
        path = Path(uri)
        if path.exists() and path.suffix.lower() in _IMG_MEDIA:
            refs.append(path)
    return refs[:4]   # gpt-image edits 上限,取最关键的几张


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
        """图片质检:图片以 base64 随请求送入(视觉核验)。"""
        import base64
        from pathlib import Path as _P
        image_path = _P(payload.get("image_uri", ""))
        if not image_path.exists():
            raise ProviderError(f"质检图片不存在: {image_path}")
        media = self.QC_MEDIA.get(image_path.suffix.lower())
        if media is None:
            raise ProviderError(f"质检不支持的图片格式: {image_path.suffix}")
        data = image_path.read_bytes()
        if len(data) > 20 * 1024 * 1024:
            raise ProviderError("质检图片超过 20MB")
        return [
            {"type": "image", "source": {
                "type": "base64", "media_type": media,
                "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": prompt},
        ]

    def generate(self, capability, payload, out_dir, cancel=None):
        try:
            prompt = build_prompt(capability, payload)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        content = (self._qc_content(prompt, payload)
                   if capability == "image_qc" else prompt)
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

    def _semantic_prompt(self, prompt, payload, refs):
        """API 提示词补齐与 CLI 一致的语义约束;有参考图时点明"以所附
        图片为准",保证同一角色跨图一致。"""
        parts = [prompt, _api_style_line(payload),
                 _SUBJECT_DIRECTIVE, _GEN_DIRECTIVE]
        if refs:
            parts.append(
                "已随请求附上参考图(第一张为全项目风格基准/上一镜衔接图,"
                "其余为该角色的人物设定图与用户参考图):画风、线条、上色、"
                "光影,以及人物的脸型、发型、发色、服装、配色和标志特征"
                "必须与参考图完全一致,是同一个人/同一实体,禁止漂移。")
        return "".join(p for p in parts if p)

    def _gen_image(self, prompt, size, dest, payload=None):
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        timeout = self.conf.get("timeout", 300)
        model = self.conf.get("model") or self.DEFAULT_MODEL
        refs = _local_refs(payload or {})
        full_prompt = self._semantic_prompt(prompt, payload or {}, refs)
        if refs:
            # 有参考图 → images/edits 多图输入(真正把参考图喂给模型),
            # 与 CLI 用参考图一致
            reply = _multipart_post(
                self.name, f"{endpoint}/v1/images/edits",
                headers={"Authorization": f"Bearer {self.conf['api_key']}"},
                fields={"model": model, "prompt": full_prompt[:32000],
                        "size": size, "n": "1"},
                files=[("image[]", path) for path in refs],
                timeout=timeout)
        else:
            reply = _request_json(
                self.name, f"{endpoint}/v1/images/generations",
                headers={"Authorization": f"Bearer {self.conf['api_key']}"},
                body={"model": model, "prompt": full_prompt[:32000],
                      "n": 1, "size": size},
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
        prompt = payload.get("prompt", "")
        if payload.get("feedback"):
            prompt = f"{prompt}。修改意见(必须落实):{payload['feedback']}"
        if capability == "image":
            safe = _safe_name(payload.get("art_name", ""))
            if payload.get("portrait"):
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
                                  cost=self.cost_per_call,
                                  data=data, uri=str(target),
                                  model=(self.conf.get("model")
                                         or self.DEFAULT_MODEL))
        if capability == "frames":
            shot_no = int(payload["shot_no"])
            first = out_dir / f"shot_{shot_no:03d}.first.png"
            last = out_dir / f"shot_{shot_no:03d}.last.png"
            chain_first = payload.get("chain_first_uri", "")
            if chain_first and Path(chain_first).exists():
                # 帧链:首帧固定为上一镜尾帧,只生成尾帧(拼接连贯)
                import shutil as _sh
                _sh.copyfile(chain_first, first)
            else:
                self._gen_image(
                    f"{prompt}。首帧:动作起始瞬间,构图稳定", size,
                    first, payload)
            if cancel is not None and cancel():
                from ..errors import ProduceCancelled
                raise ProduceCancelled("已手动停止")
            # 尾帧以首帧为参考,保证同角色同场景连贯
            self._gen_image(
                f"{prompt}。尾帧:动作结束瞬间,与首帧同场景同角色、"
                "人物服装道具一致",
                size, last, {**payload, "chain_first_uri": str(first)})
            return ProviderResult(
                provider=self.name, cost=self.cost_per_call * 2,
                data={"first": str(first), "last": str(last)},
                uri=str(first), model=(self.conf.get("model")
                                       or self.DEFAULT_MODEL))
        if capability == "cover":
            target = out_dir / "cover.png"
            self._gen_image(
                f"短视频封面:《{payload.get('title', '')}》"
                f"第{payload.get('episode', 0)}集,"
                f"{payload.get('tagline', '')}。{prompt}", size, target,
                payload)
            return ProviderResult(provider=self.name,
                                  cost=self.cost_per_call,
                                  data={}, uri=str(target),
                                  model=(self.conf.get("model")
                                         or self.DEFAULT_MODEL))
        raise ProviderError(f"{self.name} 不支持能力: {capability}")


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
        prompt = payload.get("prompt", "")
        dialogue = payload.get("dialogue") or {}
        if self.conf.get("audio_in_video", True) and dialogue.get("dialogue"):
            # Seedance2 有声视频:台词随视频自动配音
            prompt += (f"。让角色开口说出这句台词并自动配音"
                       f"(中文自然人声,口型对应):「{dialogue['dialogue']}」")
        content = [{
            "type": "text",
            "text": f"{prompt} "
                    f"--duration {duration} --resolution "
                    f"{self.conf.get('video_resolution', '720p')}",
        }]
        for key, role in (("first", "first_frame"), ("last", "last_frame")):
            if payload.get(key):
                content.append(self._frame_content(payload[key], role))
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
            data={"task_id": task_id, "duration": duration},
            uri=str(dest))
