"""直连 API Provider:剧本 / 图片 / 视频的 API 模式产线。

与 CLI 桥(claude_script / codex_image / dreamina)互为主备,在 routing
里排在对应 CLI 之后:CLI 不可用或失败时自动切换到 API,全部只用标准库。

  claude_api  script/storyboard —— Anthropic Messages API(默认 claude-opus-4-8)
  image_api   image/frames/cover —— OpenAI 兼容 /v1/images/generations
  ark_video   video —— 火山方舟 Ark 内容生成任务(创建 → 轮询 → 下载 mp4)
"""

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

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

    def generate(self, capability, payload, out_dir):
        try:
            prompt = build_prompt(capability, payload)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
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
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.conf.get("timeout", 600))
        text = "".join(block.get("text", "")
                       for block in reply.get("content", [])
                       if block.get("type") == "text")
        data = extract_json(text)
        if data is None:
            raise ProviderError(f"{self.name} 应答中未找到 JSON 对象")
        error = (validate_script(data, payload) if capability == "script"
                 else validate_storyboard(data))
        if error:
            raise ProviderError(f"{self.name} 输出校验失败: {error}")
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call, data=data)


class OpenAIImageProvider(Provider):
    """OpenAI 兼容出图 API:/v1/images/generations,b64_json 或 url 均可。

    产物文件名与 Codex 出图桥保持一致,两条产线可互换、增量互认。
    """

    DEFAULT_ENDPOINT = "https://api.openai.com"
    DEFAULT_MODEL = "gpt-image-1"

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

    def _gen_image(self, prompt, size, dest):
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        timeout = self.conf.get("timeout", 300)
        reply = _request_json(
            self.name, f"{endpoint}/v1/images/generations",
            headers={"Authorization": f"Bearer {self.conf['api_key']}"},
            body={
                "model": self.conf.get("model") or self.DEFAULT_MODEL,
                "prompt": prompt, "n": 1, "size": size,
            },
            timeout=timeout)
        item = (reply.get("data") or [{}])[0]
        if item.get("b64_json"):
            dest.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            _download(self.name, item["url"], dest, timeout)
        else:
            raise ProviderError(f"{self.name} 应答缺少 b64_json/url")

    def generate(self, capability, payload, out_dir):
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
            elif payload.get("scene_art"):
                target = out_dir / f"scene_{safe}.png"
                data = {"name": payload.get("art_name")}
            else:
                shot_no = int(payload["shot_no"])
                target = out_dir / f"shot_{shot_no:03d}.keyframe.png"
                prompt = (f"{prompt}。出场角色:"
                          f"{'、'.join(payload.get('characters', []))}")
                data = {"shot_no": shot_no}
            self._gen_image(prompt, size, target)
            return ProviderResult(provider=self.name,
                                  cost=self.cost_per_call,
                                  data=data, uri=str(target))
        if capability == "frames":
            shot_no = int(payload["shot_no"])
            first = out_dir / f"shot_{shot_no:03d}.first.png"
            last = out_dir / f"shot_{shot_no:03d}.last.png"
            self._gen_image(
                f"{prompt}。首帧:动作起始瞬间,构图稳定", size, first)
            self._gen_image(
                f"{prompt}。尾帧:动作结束瞬间,与首帧同场景同角色",
                size, last)
            return ProviderResult(
                provider=self.name, cost=self.cost_per_call * 2,
                data={"first": str(first), "last": str(last)},
                uri=str(first))
        if capability == "cover":
            target = out_dir / "cover.png"
            self._gen_image(
                f"短视频封面:《{payload.get('title', '')}》"
                f"第{payload.get('episode', 0)}集,"
                f"{payload.get('tagline', '')}。{prompt}", size, target)
            return ProviderResult(provider=self.name,
                                  cost=self.cost_per_call,
                                  data={}, uri=str(target))
        raise ProviderError(f"{self.name} 不支持能力: {capability}")


class ArkVideoProvider(Provider):
    """火山方舟 Ark 视频 API(Seedance2 的 API 模式,即梦 CLI 的备用)。

    首尾帧图生视频:创建内容生成任务 → 轮询状态 → 下载 mp4 到产物目录,
    文件名与即梦 CLI 产线一致(shot_NNN.mp4)。
    """

    DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com"
    DEFAULT_MODEL = "seedance-2.0-fast"   # 按实际开通的模型/接入点 ID 配置

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("api_key"):
            return False, "未配置 api_key"
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
                body={"model": self.conf.get("model") or self.DEFAULT_MODEL,
                      "content": []},
                timeout=30)
        except ProviderError as exc:
            message = str(exc)
            if "HTTP 400" in message:
                return True, "真实连通成功(端点可达,鉴权通过)"
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

    def generate(self, capability, payload, out_dir):
        if capability != "video":
            raise ProviderError(f"{self.name} 不支持能力: {capability}")
        endpoint = (self.conf.get("endpoint")
                    or self.DEFAULT_ENDPOINT).rstrip("/")
        headers = {"Authorization": f"Bearer {self.conf['api_key']}"}
        duration = int(payload.get("duration")
                       or self.conf.get("duration", 8))
        content = [{
            "type": "text",
            "text": f"{payload.get('prompt', '')} "
                    f"--duration {duration} --resolution "
                    f"{self.conf.get('video_resolution', '720p')}",
        }]
        for key, role in (("first", "first_frame"), ("last", "last_frame")):
            if payload.get(key):
                content.append(self._frame_content(payload[key], role))
        tasks_url = f"{endpoint}/api/v3/contents/generations/tasks"
        created = _request_json(
            self.name, tasks_url, headers,
            body={"model": self.conf.get("model") or self.DEFAULT_MODEL,
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
