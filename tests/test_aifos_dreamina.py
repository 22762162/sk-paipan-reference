"""即梦官方 CLI(dreamina)适配器测试:用假二进制验证命令拼装与降级。"""

import json
import os
import stat

import pytest

from aifos.app import App
from aifos.production.dreamina import REQUIRED_MODEL_VERSION, DreaminaProvider

FAKE_DREAMINA = '''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(json.dumps(args, ensure_ascii=False) + "\\n")
if args and args[0] == "user_credit":
    print("credit balance: 420")
    sys.exit(0)
if args and args[0] == "frames2video":
    out = os.path.join(here, "result.mp4")
    open(out, "wb").write(b"fake-mp4")
    print(json.dumps({"status": "done", "video_path": out}))
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture()
def fake_dreamina(tmp_path):
    binary = tmp_path / "bin" / "dreamina"
    binary.parent.mkdir(parents=True)
    binary.write_text(FAKE_DREAMINA, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _calls(binary):
    log = binary.parent / "calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines()]


def _make_app(tmp_path, binary, extra=None):
    conf = {"enabled": True, "command": [str(binary)]}
    conf.update(extra or {})
    return App(tmp_path / "ws", config_overrides={
        "providers": {"jimeng": conf}})


def test_frames2video_command_shape(tmp_path, fake_dreamina):
    app = _make_app(tmp_path, fake_dreamina)
    try:
        result = app.router.call("video", {
            "shot_no": 1, "prompt": "妖气翻涌的古镇长街",
            "first": "/tmp/first.png", "last": "/tmp/last.png",
            "duration": 3.0,
        }, app.workspace.artifacts_dir)
        assert result.provider == "jimeng"
        assert result.uri.endswith("result.mp4")
        assert os.path.exists(result.uri)
        assert result.data["model_version"] == REQUIRED_MODEL_VERSION

        (call,) = _calls(fake_dreamina)
        assert call[0] == "frames2video"
        assert "--first=/tmp/first.png" in call
        assert "--last=/tmp/last.png" in call
        assert "--prompt=妖气翻涌的古镇长街" in call
        assert "--duration=8" in call
        assert "--video_resolution=720p" in call
        assert "--model_version=seedance2.0fast_vip" in call
        assert "--poll=30" in call
        # 严禁回退到旧的普通 VIP 模型
        assert "--model_version=seedance2.0_vip" not in call
        # 订阅额度本地计数 -1
        assert app.router.quota_remaining("jimeng") == 999
    finally:
        app.close()


def test_default_config_pins_fast_vip():
    from aifos.config import DEFAULTS
    jimeng = DEFAULTS["providers"]["jimeng"]
    assert jimeng["type"] == "dreamina"
    assert jimeng["model_version"] == "seedance2.0fast_vip"
    assert jimeng["video_resolution"] == "720p"


def test_missing_binary_falls_back_to_mock(tmp_path):
    app = _make_app(tmp_path, "/definitely/missing/dreamina")
    try:
        result = app.router.call(
            "video", {"shot_no": 1, "first": "a", "last": "b"},
            app.workspace.artifacts_dir)
        assert result.provider == "mock"
    finally:
        app.close()


def test_missing_frames_falls_back(tmp_path, fake_dreamina):
    app = _make_app(tmp_path, fake_dreamina)
    try:
        result = app.router.call(
            "video", {"shot_no": 1}, app.workspace.artifacts_dir)
        assert result.provider == "mock"
    finally:
        app.close()


def test_credit_query_and_min_credit_gate(tmp_path, fake_dreamina):
    provider = DreaminaProvider("jimeng", {
        "enabled": True, "capabilities": ["video"],
        "command": [str(fake_dreamina)]})
    assert "420" in provider.credit()

    gated = DreaminaProvider("jimeng", {
        "enabled": True, "capabilities": ["video"],
        "command": [str(fake_dreamina)], "min_credit": 500})
    ok, reason = gated.available("video")
    assert not ok and "额度不足" in reason

    open_gate = DreaminaProvider("jimeng", {
        "enabled": True, "capabilities": ["video"],
        "command": [str(fake_dreamina)], "min_credit": 100})
    ok, _ = open_gate.available("video")
    assert ok


def test_extract_uri_variants():
    extract = DreaminaProvider._extract_uri
    assert extract('{"video_path": "/a/b.mp4"}') == "/a/b.mp4"
    assert extract('{"url": "https://cdn.x.com/v.mp4"}') == \
        "https://cdn.x.com/v.mp4"
    assert extract("done: https://cdn.x.com/out.mp4 ok") == \
        "https://cdn.x.com/out.mp4"
    assert extract("saved to /tmp/out/final.mp4") == "/tmp/out/final.mp4"
    assert extract("no video here") == ""


def test_qc_accepts_remote_url():
    from aifos.qc_center import _artifact_exists
    assert _artifact_exists("https://cdn.x.com/v.mp4")
    assert not _artifact_exists("/definitely/missing.mp4")
    assert not _artifact_exists("")


def test_full_pipeline_with_fake_dreamina(tmp_path, fake_dreamina):
    """启用假 dreamina 后端到端制作:视频阶段应全部由 jimeng 产出。"""
    app = _make_app(tmp_path, fake_dreamina)
    try:
        summary = app.director.produce("万妖图录", 15)
        assert summary["status"] == "done"
        videos_stage = next(
            s for s in summary["stages"] if s["stage"] == "videos")
        assert videos_stage["providers"] == ["jimeng"]
        calls = [c for c in _calls(fake_dreamina) if c[0] == "frames2video"]
        assert calls and all(
            "--model_version=seedance2.0fast_vip" in c for c in calls)
    finally:
        app.close()
