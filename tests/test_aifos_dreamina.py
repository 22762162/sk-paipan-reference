"""即梦官方 CLI(dreamina)适配器测试:用假二进制验证命令拼装与降级。"""

import json
import os
import stat
from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import AifosError
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
if args and args[0] in ("frames2video", "multimodal2video"):
    out = os.path.join(here, "result.mp4")
    open(out, "wb").write(b"\\x00\\x00\\x00 ftypisom-fake")
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


def _lock_cast_and_continue(app, title, number):
    summary = app.director.produce(title, number)
    assert summary["status"] == "awaiting_cast"
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    script, _ = app.projects.latest_document(episode["id"], "script")
    for character in script["characters"]:
        app.director.select_character_candidate(
            title, number, character["name"], 1)
    return app.director.produce(title, number)


def test_frames2video_command_shape(tmp_path, fake_dreamina):
    app = _make_app(tmp_path, fake_dreamina)
    try:
        result = app.router.call("video", {
            "shot_no": 1, "prompt": "妖气翻涌的古镇长街",
            "first": "/tmp/first.png", "last": "/tmp/last.png",
            "duration": 2.5,
        }, app.workspace.artifacts_dir)
        assert result.provider == "jimeng"
        # 成片从即梦输出路径归档进平台产物目录
        assert result.uri.endswith("shot_001.mp4")
        assert os.path.exists(result.uri)
        assert str(app.workspace.artifacts_dir.resolve()) in result.uri
        assert result.data["model_version"] == REQUIRED_MODEL_VERSION

        (call,) = _calls(fake_dreamina)
        assert call[0] == "frames2video"
        assert f"--first={Path('/tmp/first.png').resolve()}" in call
        assert f"--last={Path('/tmp/last.png').resolve()}" in call
        assert "--prompt=妖气翻涌的古镇长街" in call
        assert "--duration=3" in call
        assert "--video_resolution=720p" in call
        assert "--model_version=seedance2.0fast_vip" in call
        assert "--poll=30" in call
        # 严禁回退到旧的普通 VIP 模型
        assert "--model_version=seedance2.0_vip" not in call
        # 订阅额度本地计数 -1
        assert app.router.quota_remaining("jimeng") == 999
    finally:
        app.close()


def test_asset_references_use_multimodal2video(tmp_path, fake_dreamina):
    app = _make_app(tmp_path, fake_dreamina)
    try:
        result = app.router.call("video", {
            "shot_no": 2, "prompt": "人物走进会议室",
            "first": "/tmp/first.png", "last": "/tmp/last.png",
            "reference_images": ["/tmp/hero.png", "/tmp/room.png"],
            "reference_assets": [{"asset_id": 8, "name": "女主立绘"}],
        }, app.workspace.artifacts_dir)
        (call,) = _calls(fake_dreamina)
        assert call[0] == "multimodal2video"
        images = [arg for arg in call if arg.startswith("--image=")]
        assert len(images) == 4
        assert images[0].endswith(str(Path("/tmp/first.png").resolve()))
        assert images[1].endswith(str(Path("/tmp/last.png").resolve()))
        assert result.data["reference_images_used"] == [
            str(Path("/tmp/hero.png").resolve()),
            str(Path("/tmp/room.png").resolve())]
        assert result.data["reference_assets"][0]["asset_id"] == 8
    finally:
        app.close()


def test_default_config_pins_fast_vip():
    from aifos.config import DEFAULTS
    jimeng = DEFAULTS["providers"]["jimeng"]
    assert jimeng["type"] == "dreamina"
    assert jimeng["model_version"] == "seedance2.0fast_vip"
    assert jimeng["video_resolution"] == "720p"


@pytest.mark.parametrize(("quality", "resolution"), [
    ("low", "480p"), ("medium", "720p"), ("high", "1080p"),
])
def test_payload_can_select_three_seedance_quality_levels(
        tmp_path, fake_dreamina, quality, resolution):
    app = _make_app(tmp_path, fake_dreamina)
    try:
        result = app.router.call("video", {
            "shot_no": 1, "prompt": "镜头缓推",
            "first": "/tmp/first.png", "last": "/tmp/last.png",
            "video_quality": quality, "video_resolution": resolution,
        }, app.workspace.artifacts_dir)
        (call,) = _calls(fake_dreamina)
        assert f"--video_resolution={resolution}" in call
        assert result.data["video_quality"] == quality
        assert result.data["video_resolution"] == resolution
    finally:
        app.close()


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
        summary = _lock_cast_and_continue(app, "万妖图录", 15)
        assert summary["status"] == "done"
        videos_stage = next(
            s for s in summary["stages"] if s["stage"] == "videos")
        assert videos_stage["providers"] == ["jimeng"]
        # 必要参考图(分镜图/人物立绘/场景图)自动选入后,视频默认走
        # multimodal2video(首尾帧+参考图一起提交);无参考图的镜头
        # 仍走 frames2video。两种模式都必须锁定 fast_vip 模型。
        calls = [c for c in _calls(fake_dreamina)
                 if c[0] in ("frames2video", "multimodal2video")]
        assert calls and all(
            "--model_version=seedance2.0fast_vip" in c for c in calls)
        assert any(c[0] == "multimodal2video" for c in calls), \
            "自动参考图选入后应有镜头走 multimodal2video"
    finally:
        app.close()


def test_dialogue_voiced_in_video_prompt(tmp_path, fake_dreamina):
    """有台词的镜头:台词写进视频提示词,Seedance2 自动配音。"""
    app = _make_app(tmp_path, fake_dreamina)
    try:
        app.router.call("video", {
            "shot_no": 2, "prompt": "特写镜头",
            "dialogue": {"character": "林昭", "dialogue": "妖气不对劲"},
            "first": "/tmp/first.png", "last": "/tmp/last.png",
            "duration": 3.0,
        }, app.workspace.artifacts_dir)
    finally:
        app.close()
    (call,) = _calls(fake_dreamina)
    prompt = next(a for a in call if a.startswith("--prompt="))
    assert "妖气不对劲" in prompt and "自动配音" in prompt


def test_produce_skips_tts_when_video_carries_audio(tmp_path, fake_dreamina):
    """即梦产视频(有声)→ 配音阶段跳过独立 TTS,质检不再要求配音文件。"""
    app = _make_app(tmp_path, fake_dreamina)
    try:
        summary = _lock_cast_and_continue(app, "有声视频验证", 1)
        assert summary["status"] == "done"
        assert summary["qc_score"] >= 80
        voices_stage = next(s for s in summary["stages"]
                            if s["stage"] == "voices")
        assert voices_stage["status"] == "done"
        assert "随视频配音(seedance2)" in voices_stage["providers"]
        # 不再登记独立配音资产
        project = app.projects.get_project("有声视频验证")
        rows = app.db.query(
            "SELECT * FROM assets WHERE project_id=? AND kind='voice'",
            (project["id"],))
        assert list(rows) == []
    finally:
        app.close()


@pytest.mark.parametrize(("audio_states", "message"), [
    ([True, False], "禁止混用有声与无声"),
    ([False, False], "已阻止错位的独立 TTS"),
])
def test_professional_voice_stage_blocks_invalid_audio_modes(
        tmp_path, audio_states, message):
    """V3.2 禁止混声道或把分镜台词静默退化为独立 TTS。"""
    app = App(tmp_path / "ws")
    try:
        ctx = {
            "production_profile": {
                "pipeline_version": "sk-manju-v5",
                "voice": "jimeng_builtin",
                "lip_sync": True,
                "burn_subtitles": False,
            },
            "script": {"scenes": [{"lines": [
                {"character": "林昭", "dialogue": "妖气不对劲"},
            ]}]},
            "videos": [
                {"shot_no": index, "audio_in_video": state}
                for index, state in enumerate(audio_states, 1)
            ],
        }
        with pytest.raises(AifosError, match=message):
            app.director._stage_voices(ctx)
        assert ctx["voices"] == []
        assert ctx["subtitles"] == []
    finally:
        app.close()


def test_doctor_voice_carried_by_video(tmp_path, fake_dreamina):
    from aifos.doctor import run_doctor
    app = _make_app(tmp_path, fake_dreamina)
    try:
        report = run_doctor(app)
        voice = next(c for c in report["capabilities"]
                     if c["capability"] == "voice")
        assert voice["real"] is True
        assert "随视频自动配音" in voice["provider_label"]
    finally:
        app.close()
