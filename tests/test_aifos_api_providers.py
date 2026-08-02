"""API Provider 测试:Claude 编剧 / OpenAI 兼容出图 / Ark 视频。

用本地假 HTTP 服务模拟三家 API,验证请求形状(路径/鉴权头/模型)
与产物落盘,及路由器 CLI → API 的回退。
"""

import base64
import json
from pathlib import Path
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


def test_candidate_api_prompt_locks_project_style_and_identity():
    provider = OpenAIImageProvider("image_api", {"enabled": True})
    payload = {"portrait_candidate": True, "style": "现代都市半写实"}
    prompt = provider._semantic_prompt("四张同词初始候选", payload, [object()])
    assert "人物身份与脸是最高标准" in prompt
    assert "不得改脸" in prompt
    assert "同一人物四张候选必须复用完全相同" in prompt
    assert "只靠模型随机采样" in prompt
    assert "纯净无场景背景" in prompt
    assert "禁止换装、换妆、换动作" in prompt


def test_complete_character_prompt_is_not_reexpanded_with_story_biography():
    provider = OpenAIImageProvider("image_api", {"enabled": True})
    visual = (
        "单人角色定妆母图：朱慈烺；男，约十五岁；明代束发无辫；"
        "明代交领中衣；全身正面；纯净中性背景；无文字")
    payload = {
        "portrait": True,
        "portrait_candidate": True,
        "prompt_contract_complete": True,
        "style": "电影级半写实精品漫剧",
        "character_background": {
            "identity_facts": "父崇祯帝、母周皇后",
            "motivation": "距亡国不足108天，谋划说服父皇",
            "image_prompt": visual,
        },
    }
    prompt = provider._semantic_prompt(visual, payload, [])
    assert prompt.count(visual) == 1
    assert "人物剧情设定硬约束" not in prompt
    assert "父崇祯帝" not in prompt
    assert "108天" not in prompt
    assert "说服父皇" not in prompt


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
               "characters": [], "dialogue": None, "prompt": "古镇夜景",
               "frame_targets": {
                   "keyframe": {
                       "phase": "freeze", "state": "古镇夜景建立镜头",
                       "fallback": False,
                   },
                   "first_frame": {
                       "phase": "start", "state": "空街灯影刚进入画面",
                       "fallback": False,
                   },
                   "last_frame": {
                       "phase": "end", "state": "空街灯影稳定收束",
                       "fallback": False,
                   },
               }}],
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
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"_multipart": True}   # multipart(edits 多图)不解析

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


def test_claude_image_qc_uploads_actual_reference_manifest_in_order(tmp_path):
    provider = ClaudeApiProvider(
        "claude_api", _claude_conf("http://127.0.0.1:1"))
    generated = tmp_path / "generated.png"
    identity = tmp_path / "identity.png"
    spatial = tmp_path / "spatial.png"
    scene = tmp_path / "scene.png"
    for path in (generated, identity, spatial, scene):
        path.write_bytes(PNG_1PX)

    content = provider._qc_content("检查本镜输入是否正确", {
        "image_uri": str(generated),
        "reference_manifest": [
            {
                "index": 1,
                "uri": str(identity),
                "label": "林昭最终立绘",
                "role": "identity",
                "binding": "锁定林昭身份",
            },
            {
                "index": 2,
                "uri": str(spatial),
                "label": "镜头空间调度图",
                "role": "spatial",
                "binding": "锁定站位与镜头路线",
            },
            {
                "index": 3,
                "uri": str(scene),
                "label": "古镇场景母图",
                "role": "scene",
                "binding": "锁定场景结构",
            },
        ],
        # manifest 存在时不得退回只检查人物身份图。
        "identity_references": [{
            "uri": str(identity), "character": "错误的兼容回退",
        }],
    })

    image_blocks = [
        item for item in content if item.get("type") == "image"]
    labels = [
        item["text"] for item in content if item.get("type") == "text"]
    assert len(image_blocks) == 4
    assert labels[0] == "下面第一张是待检图。"
    assert labels[1:4] == [
        "下面是图1「林昭最终立绘」，职责=identity：锁定林昭身份",
        "下面是图2「镜头空间调度图」，职责=spatial：锁定站位与镜头路线",
        "下面是图3「古镇场景母图」，职责=scene：锁定场景结构",
    ]
    assert "错误的兼容回退" not in "\n".join(labels)
    assert labels[-1] == "检查本镜输入是否正确"


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
    assert result.data["image_normalization"]["target_dimensions"] == {
        "width": 1080, "height": 1920}
    original = Path(result.data["image_normalization"]["original_uri"])
    assert original.read_bytes() == PNG_1PX
    call = fake.calls[0]
    assert call["headers"]["authorization"] == "Bearer sk-img-test"
    assert call["body"]["model"] == "gpt-image-1"
    assert call["body"]["size"] == "1024x1536"   # 9:16 → 竖版

    result = provider.generate("image", {
        "shot_no": 7, "characters": ["林昭"],
        "prompt": "FULL_EPISODE_PLOT_SENTINEL_整集剧情",
        "prompt_compact": "CURRENT_SHOT_SENTINEL_林昭抬眼看向门外",
        "prompt_contract_complete": True,
        "character_background": {
            "林昭": {"backstory": "FULL_BIO_SENTINEL_人物身世"}},
        "aspect": "16:9",
    }, tmp_path)
    assert result.uri.endswith("shot_007.keyframe.png")
    assert fake.calls[-1]["body"]["size"] == "1536x1024"
    sent_prompt = fake.calls[-1]["body"]["prompt"]
    assert "CURRENT_SHOT_SENTINEL" in sent_prompt
    assert "FULL_EPISODE_PLOT_SENTINEL" not in sent_prompt
    assert "FULL_BIO_SENTINEL" not in sent_prompt


def test_image_api_frames_url_mode(fake_api, tmp_path):
    """b64_json 之外兼容 url 应答(先出链接再下载)。"""
    endpoint, fake = fake_api
    fake.routes[("GET", "/img.png")] = lambda body: PNG_1PX
    fake.routes[("POST", "/v1/images/generations")] = lambda body: {
        "data": [{"url": f"{endpoint}/img.png"}]}
    # 尾帧以首帧为参考走 edits(保持连贯);url 模式同样支持
    fake.routes[("POST", "/v1/images/edits")] = lambda body: {
        "data": [{"url": f"{endpoint}/img.png"}]}
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    result = provider.generate("frames", {
        "shot_no": 2, "prompt": "镜头", "aspect": "9:16",
    }, tmp_path)
    assert result.data["image_normalization"]["first"][
        "target_dimensions"] == {"width": 1080, "height": 1920}
    assert result.data["image_normalization"]["last"][
        "target_dimensions"] == {"width": 1080, "height": 1920}
    assert result.data["first"].endswith("shot_002.first.png")
    # 首尾两张 → 双倍单价
    assert result.cost == 3.0


def test_image_api_frames_reuse_keyframe_charges_one_call(
        fake_api, tmp_path, monkeypatch):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/images/edits")] = lambda body: {
        "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    keyframe = tmp_path / "shot_004.keyframe.png"
    keyframe.write_bytes(PNG_1PX)
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    sent_prompts = []
    original_gen = provider._gen_image

    def capture_prompt(prompt, *args, **kwargs):
        sent_prompts.append(prompt)
        return original_gen(prompt, *args, **kwargs)

    monkeypatch.setattr(provider, "_gen_image", capture_prompt)
    result = provider.generate("frames", {
        "shot_no": 4, "prompt": "镜头", "aspect": "9:16",
        "image_uri": str(keyframe),
        "frame_target": {"phase": "start", "state": "动作起点"},
        "frame_prompt_compacts": {
            "first_frame": "FIRST_PHASE_ONLY_未穿风衣",
            "last_frame": "LAST_PHASE_ONLY_穿风衣离开",
        },
        "spatial_constraint": "空间调度锁：严格 2 人，保持轴线。",
    }, tmp_path / "frames")
    assert result.data["image_normalization"]["first"][
        "target_dimensions"] == {"width": 1080, "height": 1920}
    assert result.data["first_source"] == "keyframe"
    assert result.data["last_source"] == "generated"
    assert result.data["keyframe_phase"] == "start"
    assert result.data["generation_calls"] == 1
    assert result.cost == 1.5
    assert len(fake.calls) == 1
    sent = sent_prompts[0]
    assert "LAST_PHASE_ONLY_穿风衣离开" in sent
    assert "FIRST_PHASE_ONLY_未穿风衣" not in sent
    assert "空间调度锁" in provider._semantic_prompt(
        "镜头", {"spatial_constraint": "空间调度锁：严格 2 人。"}, [])


def test_image_api_end_keyframe_is_reused_as_last_and_first_is_generated(
        fake_api, tmp_path, monkeypatch):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/images/edits")] = lambda body: {
        "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    keyframe = tmp_path / "shot_005.keyframe.png"
    keyframe.write_bytes(PNG_1PX)
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    sent_prompts = []
    original_gen = provider._gen_image

    def capture_prompt(prompt, *args, **kwargs):
        sent_prompts.append(prompt)
        return original_gen(prompt, *args, **kwargs)

    monkeypatch.setattr(provider, "_gen_image", capture_prompt)

    payload = {
        "shot_no": 5,
        "prompt": "女人从车门外走向远处",
        "aspect": "9:16",
        "keyframe_reference_uri": str(keyframe),
        "keyframe_last_uri": str(keyframe),
        "keyframe_boundary_phase": "end",
        # 新导演的显式边界裁决必须压过可能残留的旧代表帧字段。
        "frame_target": {"phase": "freeze", "state": "旧代表帧字段"},
        "frame_prompt_compacts": {
            "first_frame": "FIRST_PHASE_ONLY_床上仰躺且未穿鞋",
            "last_frame": "LAST_PHASE_ONLY_穿风衣走向电梯",
        },
        "start_state": {"女人": {"position": "刚站在车门外"}},
        "end_state": {"女人": {"position": "已经走远"}},
    }
    assert provider.validate_request("frames", payload) == []
    result = provider.generate(
        "frames", payload, tmp_path / "frames_end")

    assert result.data["first_source"] == "generated"
    assert result.data["last_source"] == "keyframe"
    assert result.data["keyframe_phase"] == "end"
    assert result.data["generation_calls"] == 1
    assert result.cost == 1.5
    assert len(fake.calls) == 1
    sent = sent_prompts[0]
    assert "FIRST_PHASE_ONLY_床上仰躺且未穿鞋" in sent
    assert "LAST_PHASE_ONLY_穿风衣走向电梯" not in sent


@pytest.mark.parametrize("phase", ["freeze", "", "unexpected"])
def test_image_api_non_boundary_keyframe_generates_both_frames(
        fake_api, tmp_path, phase):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/images/edits")] = lambda body: {
        "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    keyframe = tmp_path / f"shot_{phase or 'unknown'}.keyframe.png"
    keyframe.write_bytes(PNG_1PX)
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))
    frame_target = {"state": "代表性构图"}
    if phase:
        frame_target["phase"] = phase

    result = provider.generate("frames", {
        "shot_no": 6,
        "prompt": "人物从站立到坐下",
        "aspect": "9:16",
        "image_uri": str(keyframe),
        "frame_target": frame_target,
    }, tmp_path / f"frames_{phase or 'unknown'}")

    assert result.data["first_source"] == "generated"
    assert result.data["last_source"] == "generated"
    assert result.data["keyframe_phase"] == phase
    assert result.data["generation_calls"] == 2
    assert result.cost == 3.0
    assert len(fake.calls) == 2


def test_image_api_chain_start_and_end_keyframe_need_no_paid_call(
        fake_api, tmp_path):
    endpoint, fake = fake_api
    previous_tail = tmp_path / "previous_tail.png"
    end_keyframe = tmp_path / "shot_007.keyframe.png"
    previous_tail.write_bytes(PNG_1PX)
    end_keyframe.write_bytes(PNG_1PX)
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))

    result = provider.generate("frames", {
        "shot_no": 7,
        "prompt": "人物从门边走到走廊尽头",
        "aspect": "9:16",
        "chain_first_uri": str(previous_tail),
        "keyframe_reference_uri": str(end_keyframe),
        "keyframe_last_uri": str(end_keyframe),
        "keyframe_boundary_phase": "end",
        "frame_target": {"phase": "freeze", "state": "旧代表帧字段"},
    }, tmp_path / "frames_chain_end")

    assert result.data["first_source"] == "previous_tail"
    assert result.data["last_source"] == "keyframe"
    assert result.data["generation_calls"] == 0
    assert result.cost == 0
    assert fake.calls == []


def test_image_api_reference_only_keyframe_never_becomes_boundary(
        fake_api, tmp_path):
    endpoint, fake = fake_api
    fake.routes[("POST", "/v1/images/edits")] = lambda body: {
        "data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    keyframe = tmp_path / "shot_reference_only.keyframe.png"
    keyframe.write_bytes(PNG_1PX)
    provider = OpenAIImageProvider("image_api", _image_conf(endpoint))

    result = provider.generate("frames", {
        "shot_no": 8,
        "prompt": "人物从站立到坐下",
        "aspect": "9:16",
        "keyframe_reference_uri": str(keyframe),
        "keyframe_boundary_phase": "reference_only",
        # 旧字段即使残留 end，也不能推翻新导演的 reference_only 裁决。
        "frame_target": {"phase": "end", "state": "旧终点字段"},
    }, tmp_path / "frames_reference_only")

    assert result.data["first_source"] == "generated"
    assert result.data["last_source"] == "generated"
    assert result.data["keyframe_phase"] == "reference_only"
    assert result.data["generation_calls"] == 2
    assert result.cost == 3.0
    assert len(fake.calls) == 2


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
    reference = tmp_path / "reference.png"
    first.write_bytes(PNG_1PX)
    last.write_bytes(PNG_1PX)
    reference.write_bytes(PNG_1PX)
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
        "reference_images": [str(reference)],
        "reference_assets": [{"asset_id": 9, "name": "角色立绘"}],
        "video_quality": "high", "video_resolution": "1080p",
    }, out_dir)
    assert result.uri.endswith("shot_003.mp4")
    assert (out_dir / "shot_003.mp4").read_bytes() == MP4_FAKE
    create = fake.calls[0]
    assert create["headers"]["authorization"] == "Bearer ark-key-1"
    assert create["body"]["model"] == "seedance-2.0-fast"
    text = create["body"]["content"][0]["text"]
    assert "--duration 8" in text and "1080p" in text
    assert result.data["video_quality"] == "high"
    assert result.data["video_resolution"] == "1080p"
    roles = [c.get("role") for c in create["body"]["content"][1:]]
    assert roles == ["first_frame", "last_frame", "reference_image"]
    assert create["body"]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,")
    assert polls["n"] == 2
    assert result.data["reference_assets"][0]["asset_id"] == 9


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


def test_stream_claude_text_aggregates_sse(monkeypatch):
    """流式 SSE 聚合全文;10 分钟以上长生成不再被远端断连掐死。"""
    import io
    import urllib.request as _ur
    from aifos.production.api_providers import _stream_claude_text

    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start"}\n\n'
        b'data: {"type":"content_block_delta",'
        b'"delta":{"type":"text_delta","text":"{\\"scenes\\""}}\n\n'
        b'data: {"type":"content_block_delta",'
        b'"delta":{"type":"text_delta","text":": []}"}}\n\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        b'data: {"type":"message_stop"}\n\n')

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["stream"] = json.loads(
            request.data.decode("utf-8"))["stream"]
        return _FakeResp(sse)

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    text = _stream_claude_text(
        "claude_api", "https://x/v1/messages", {"x-api-key": "k"},
        {"model": "m", "max_tokens": 100,
         "messages": [{"role": "user", "content": "hi"}]}, 600)
    assert text == '{"scenes": []}'
    assert captured["stream"] is True


def test_stream_claude_text_reports_max_tokens_truncation(monkeypatch):
    import io
    import urllib.request as _ur
    from aifos.production.api_providers import _stream_claude_text

    sse = (
        b'data: {"type":"content_block_delta",'
        b'"delta":{"type":"text_delta","text":"partial"}}\n\n'
        b'data: {"type":"message_delta",'
        b'"delta":{"stop_reason":"max_tokens"}}\n\n')

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(_ur, "urlopen",
                        lambda request, timeout=None: _FakeResp(sse))
    with pytest.raises(ProviderError, match="max_tokens"):
        _stream_claude_text(
            "claude_api", "https://x/v1/messages", {}, {
                "model": "m", "max_tokens": 1,
                "messages": []}, 600)
