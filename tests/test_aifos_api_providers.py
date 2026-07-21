"""API Provider 测试:Claude 编剧 / OpenAI 兼容出图 / Ark 视频。

用本地假 HTTP 服务模拟三家 API,验证请求形状(路径/鉴权头/模型)
与产物落盘,及路由器 CLI → API 的回退。
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aifos.app import App
from aifos.errors import ProviderError
from aifos.production.api_providers import (ArkVideoProvider,
                                            ClaudeApiProvider,
                                            OpenAIImageProvider)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
MP4_FAKE = b"\x00\x00\x00 ftypisom" + b"\x00" * 64

SCRIPT_JSON = {
    "project_title": "万妖图录", "episode_number": 15,
    "episode_title": "夜探古镇", "logline": "少年初遇妖影",
    "characters": [{"name": "林昭", "role": "主角"}],
    "scenes": [{"scene_no": 1, "location": "古镇长街",
                "characters": ["林昭"], "action": "夜色渐深",
                "lines": [{"character": "林昭", "dialogue": "妖气不对劲"}]}],
}
STORYBOARD_JSON = {
    "episode_title": "夜探古镇",
    "shots": [{"shot_no": 3, "scene_no": 1, "kind": "environment",
               "description": "古镇夜景", "camera": "远景", "duration": 2.5,
               "characters": [], "dialogue": None, "prompt": "古镇夜景"}],
}


class _FakeApi(BaseHTTPRequestHandler):
    """可编排的假 API:类属性 routes = {(method, path): handler(body)->dict}。"""

    calls = []
    routes = {}

    def log_message(self, *args):
        pass

    def _reply(self, data, status=200, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        _FakeApi.calls.append({
            "method": method, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        handler = _FakeApi.routes.get((method, self.path))
        if handler is None:
            return self._reply({"error": "not found"}, status=404)
        result = handler(body)
        if isinstance(result, bytes):
            return self._reply(None, raw=result, ctype="video/mp4")
        if isinstance(result, tuple):          # (状态码, 应答体)
            return self._reply(result[1], status=result[0])
        return self._reply(result)

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")


@pytest.fixture
def fake_api():
    _FakeApi.calls = []
    _FakeApi.routes = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", _FakeApi
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------- Claude API 编剧 ----------------

def _claude_conf(endpoint):
    return {"type": "claude_api", "enabled": True,
            "capabilities": ["script", "storyboard"],
            "endpoint": endpoint, "api_key": "sk-ant-test-1234",
            "model": "claude-opus-4-8", "max_tokens": 16000,
            "cost_per_call": 0.8, "timeout": 30}


def test_claude_api_script(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/messages")] = lambda body: {
        "content": [
            {"type": "thinking", "thinking": "…"},
            {"type": "text",
             "text": "好的,剧本如下:\n"
                     + json.dumps(SCRIPT_JSON, ensure_ascii=False)},
        ]}
    provider = ClaudeApiProvider("claude_api", _claude_conf(endpoint))
    assert provider.available("script") == (True, "")
    result = provider.generate("script", {
        "project_title": "万妖图录", "episode_number": 15,
        "style": "国风", "premise": "夜探古镇",
    }, tmp_path)
    assert result.data["scenes"][0]["location"] == "古镇长街"
    assert result.cost == 0.8
    call = fake.calls[0]
    assert call["path"] == "/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-ant-test-1234"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["body"]["model"] == "claude-opus-4-8"
    prompt = call["body"]["messages"][0]["content"]
    assert "万妖图录" in prompt and "第15集" in prompt


def test_claude_api_storyboard_and_errors(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/messages")] = lambda body: {
        "content": [{"type": "text",
                     "text": json.dumps(STORYBOARD_JSON,
                                        ensure_ascii=False)}]}
    provider = ClaudeApiProvider("claude_api", _claude_conf(endpoint))
    result = provider.generate(
        "storyboard", {"script": SCRIPT_JSON}, tmp_path)
    # 校验器强制镜头连续编号
    assert result.data["shots"][0]["shot_no"] == 1

    fake.routes[("POST", "/v1/messages")] = lambda body: {
        "content": [{"type": "text", "text": "这不是 JSON"}]}
    with pytest.raises(ProviderError):
        provider.generate("script", {"project_title": "x",
                                     "episode_number": 1}, tmp_path)


def test_claude_api_requires_key(tmp_path):
    conf = _claude_conf("http://127.0.0.1:1")
    conf["api_key"] = ""
    provider = ClaudeApiProvider("claude_api", conf)
    ok, reason = provider.available("script")
    assert not ok and "api_key" in reason


# ---------------- OpenAI 兼容出图 API ----------------

def _image_conf(endpoint):
    return {"type": "image_api", "enabled": True,
            "capabilities": ["image", "frames", "cover"],
            "endpoint": endpoint, "api_key": "sk-img-test",
            "model": "gpt-image-1", "cost_per_call": 1.5, "timeout": 30}


def test_image_api_portrait_and_shot(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/images/generations")] = lambda body: {
        "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    result = provider.generate("image", {
        "portrait": True, "art_name": "林昭", "prompt": "角色立绘:林昭",
        "aspect": "9:16", "width": 1080, "height": 1920,
    }, tmp_path)
    assert result.uri.endswith(".png")
    assert (tmp_path / "portrait_林昭.png").read_bytes() == PNG_1PX
    call = fake.calls[0]
    assert call["headers"]["authorization"] == "Bearer sk-img-test"
    assert call["body"]["model"] == "gpt-image-1"
    assert call["body"]["size"] == "1024x1536"   # 9:16 → 竖版

    result = provider.generate("image", {
        "shot_no": 7, "characters": ["林昭"], "prompt": "古镇夜景",
        "aspect": "16:9",
    }, tmp_path)
    assert result.uri.endswith("shot_007.keyframe.png")
    assert fake.calls[-1]["body"]["size"] == "1536x1024"


def test_image_api_frames_url_mode(fake_api, tmp_path):
    """b64_json 之外兼容 url 应答(先出链接再下载)。"""
    endpoint, fake = fake_api
    fake.routes[("GET", "/img.png")] = lambda body: PNG_1PX
    fake.routes[("POST", "/v1/images/generations")] = lambda body: {
        "data": [{"url": f"{endpoint}/img.png"}]}
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    result = provider.generate("frames", {
        "shot_no": 2, "prompt": "镜头", "aspect": "9:16",
    }, tmp_path)
    assert (tmp_path / "shot_002.first.png").read_bytes() == PNG_1PX
    assert (tmp_path / "shot_002.last.png").read_bytes() == PNG_1PX
    assert result.data["first"].endswith("shot_002.first.png")
    # 首尾两张 → 双倍单价
    assert result.cost == 3.0


def test_image_api_feedback_into_prompt(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/images/generations")] = lambda body: {
        "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    provider.generate("image", {
        "scene_art": True, "art_name": "古镇", "prompt": "场景概念图",
        "feedback": "改成夜晚", "aspect": "9:16",
    }, tmp_path)
    assert "改成夜晚" in fake.calls[0]["body"]["prompt"]


# ---------------- Ark 视频 API ----------------

def test_ark_video_task_flow(fake_api, tmp_path):
    endpoint, fake = fake_api
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(PNG_1PX)
    last.write_bytes(PNG_1PX)
    polls = {"n": 0}

    def poll(body):
        polls["n"] += 1
        if polls["n"] < 2:
            return {"id": "t1", "status": "running"}
        return {"id": "t1", "status": "succeeded",
                "content": {"video_url": f"{endpoint}/out.mp4"}}

    fake.routes[("POST", "/api/v3/contents/generations/tasks")] = \
        lambda body: {"id": "t1", "status": "queued"}
    fake.routes[("GET", "/api/v3/contents/generations/tasks/t1")] = poll
    fake.routes[("GET", "/out.mp4")] = lambda body: MP4_FAKE

    provider = ArkVideoProvider("ark", {
        "type": "ark_video", "enabled": True, "capabilities": ["video"],
        "endpoint": endpoint, "api_key": "ark-key-1",
        "model": "seedance-2.0-fast", "video_resolution": "720p",
        "duration": 8, "poll": 0.01, "cost_per_call": 2.5, "timeout": 30})
    out_dir = tmp_path / "videos"
    result = provider.generate("video", {
        "shot_no": 3, "prompt": "古镇夜景 镜头缓推", "duration": 8,
        "first": str(first), "last": str(last), "aspect": "9:16",
    }, out_dir)
    assert result.uri.endswith("shot_003.mp4")
    assert (out_dir / "shot_003.mp4").read_bytes() == MP4_FAKE
    create = fake.calls[0]
    assert create["headers"]["authorization"] == "Bearer ark-key-1"
    assert create["body"]["model"] == "seedance-2.0-fast"
    text = create["body"]["content"][0]["text"]
    assert "--duration 8" in text and "720p" in text
    roles = [c.get("role") for c in create["body"]["content"][1:]]
    assert roles == ["first_frame", "last_frame"]
    assert create["body"]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,")
    assert polls["n"] == 2


def test_ark_requires_real_model_id(fake_api, tmp_path):
    """模型 ID 必须从方舟控制台复制;404 NotFound 给中文开通指引。"""
    conf = {"type": "ark_video", "enabled": True, "capabilities": ["video"],
            "endpoint": "http://127.0.0.1:1", "api_key": "k", "model": ""}
    provider = ArkVideoProvider("ark", conf)
    ok, reason = provider.available("video")
    assert not ok and "doubao-seedance" in reason

    endpoint, fake = fake_api
    fake.routes[("POST", "/api/v3/contents/generations/tasks")] = \
        lambda body: (404, {"error": {
            "code": "InvalidEndpointOrModel.NotFound",
            "message": "The model or endpoint x does not exist"}})
    provider = ArkVideoProvider("ark", {
        "type": "ark_video", "enabled": True, "capabilities": ["video"],
        "endpoint": endpoint, "api_key": "k", "model": "错的ID",
        "timeout": 30})
    ok, detail = provider.ping()
    assert not ok
    assert "Key 已通过" in detail and "开通管理" in detail


def test_ark_video_task_failed(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/api/v3/contents/generations/tasks")] = \
        lambda body: {"id": "t2"}
    fake.routes[("GET", "/api/v3/contents/generations/tasks/t2")] = \
        lambda body: {"status": "failed",
                      "error": {"message": "内容审核未通过"}}
    provider = ArkVideoProvider("ark", {
        "type": "ark_video", "enabled": True, "capabilities": ["video"],
        "endpoint": endpoint, "api_key": "k",
        "model": "doubao-seedance-2-0-test",
        "poll": 0.01, "timeout": 30})
    with pytest.raises(ProviderError, match="内容审核未通过"):
        provider.generate("video", {"shot_no": 1, "prompt": "x"}, tmp_path)


# ---------------- 豆包 TTS 配音 API ----------------

def _doubao_conf(endpoint):
    return {"type": "doubao_tts", "enabled": True,
            "capabilities": ["voice"],
            "endpoint": f"{endpoint}/api/v1/tts",
            "appid": "app-123", "api_key": "tok-456",
            "cluster": "volcano_tts", "voice_type": "BV700_streaming",
            "cost_per_call": 0.2, "timeout": 30}


def test_doubao_tts_voice(fake_api, tmp_path):
    from aifos.production.api_providers import DoubaoTtsProvider
    endpoint, fake = fake_api
    mp3 = b"ID3fake-mp3-bytes"
    fake.routes[("POST", "/api/v1/tts")] = lambda body: {
        "code": 3000, "message": "success",
        "data": base64.b64encode(mp3).decode(),
        "addition": {"duration": "2350"}}
    provider = DoubaoTtsProvider("doubao_tts", _doubao_conf(endpoint))
    assert provider.available("voice") == (True, "")
    result = provider.generate("voice", {
        "line_no": 3, "character": "林昭", "text": "妖气不对劲",
    }, tmp_path)
    assert result.uri.endswith("line_003.mp3")
    assert (tmp_path / "line_003.mp3").read_bytes() == mp3
    assert result.data["duration"] == 2.35
    call = fake.calls[-1]
    assert call["headers"]["authorization"] == "Bearer;tok-456"
    assert call["body"]["app"]["appid"] == "app-123"
    assert call["body"]["app"]["cluster"] == "volcano_tts"
    assert call["body"]["audio"]["voice_type"] == "BV700_streaming"
    assert call["body"]["request"]["text"] == "妖气不对劲"

    ok, detail = provider.ping()
    assert ok and "BV700_streaming" in detail

    # 鉴权失败 code≠3000 → 明确报错
    fake.routes[("POST", "/api/v1/tts")] = lambda body: {
        "code": 4001, "message": "invalid token"}
    with pytest.raises(ProviderError, match="4001"):
        provider.generate("voice", {"line_no": 1, "text": "你好"}, tmp_path)


def test_doubao_tts_requires_appid(tmp_path):
    from aifos.production.api_providers import DoubaoTtsProvider
    conf = _doubao_conf("http://127.0.0.1:1")
    conf["appid"] = ""
    provider = DoubaoTtsProvider("doubao_tts", conf)
    ok, reason = provider.available("voice")
    assert not ok and "appid" in reason


# ---------------- 真实连通性测试(ping) ----------------

def test_pings(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/messages")] = lambda body: {
        "content": [{"type": "text", "text": "pong"}]}
    claude = ClaudeApiProvider("claude_api", _claude_conf(endpoint))
    ok, detail = claude.ping()
    assert ok and "claude-opus-4-8" in detail
    assert fake.calls[-1]["body"]["max_tokens"] == 1

    fake.routes[("GET", "/v1/models")] = lambda body: {
        "data": [{"id": "gpt-image-1"}, {"id": "dall-e-3"}]}
    image = OpenAIImageProvider("image_api", _image_conf(endpoint))
    ok, detail = image.ping()
    assert ok and "2" in detail

    # Ark:空任务 HTTP 400 = 端点可达且鉴权通过;401 = Key 错误
    fake.routes[("POST", "/api/v3/contents/generations/tasks")] = \
        lambda body: (400, {"error": {"message": "InvalidParameter"}})
    ark = ArkVideoProvider("ark", {
        "type": "ark_video", "enabled": True, "capabilities": ["video"],
        "endpoint": endpoint, "api_key": "k", "timeout": 30})
    ok, detail = ark.ping()
    assert ok and "鉴权通过" in detail
    fake.routes[("POST", "/api/v3/contents/generations/tasks")] = \
        lambda body: (401, {"error": {"message": "invalid api key"}})
    ok, detail = ark.ping()
    assert not ok and "401" in detail


def test_settings_test_provider_real_ping(fake_api, tmp_path):
    """设置中心「测试连接」= 配置检查 + 真实请求。"""
    from aifos.settings import test_provider
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/messages")] = lambda body: {
        "content": [{"type": "text", "text": "pong"}]}
    app = App(tmp_path / "ws", config_overrides={
        "providers": {"claude_api": {
            "enabled": True, "endpoint": endpoint,
            "api_key": "sk-ping", "timeout": 30}}})
    try:
        report = test_provider(app, "claude_api")
        assert report["ok"] is True
        assert "真实连通成功" in report["extra"]
        # Key 失效 → 401 → 测试必须报失败,而不是显示配置正常
        fake.routes[("POST", "/v1/messages")] = lambda body: (
            401, {"error": {"message": "invalid x-api-key"}})
        report = test_provider(app, "claude_api")
        assert report["ok"] is False
        assert "401" in report["extra"]
    finally:
        app.close()


# ---------------- 路由回退:CLI 不可用 → API ----------------

def test_router_falls_back_to_claude_api(fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/messages")] = lambda body: {
        "content": [{"type": "text",
                     "text": json.dumps(SCRIPT_JSON, ensure_ascii=False)}]}
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            # claude CLI 启用但命令不存在 → 自动回退 claude_api
            "claude": {"enabled": True,
                       "command": ["definitely-missing-claude-cli"]},
            "claude_api": {"enabled": True, "endpoint": endpoint,
                           "api_key": "sk-ant-live", "timeout": 30},
        }})
    try:
        result = app.router.call(
            "script", {"project_title": "万妖图录", "episode_number": 15},
            tmp_path / "out")
        assert result.provider == "claude_api"
        assert result.data["scenes"]
    finally:
        app.close()
