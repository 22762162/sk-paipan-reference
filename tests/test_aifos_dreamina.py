"""即梦官方 CLI(dreamina)适配器测试:用假二进制验证命令拼装与降级。"""

import json
import os
import stat
from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.production.dreamina import (
    REQUIRED_MODEL_VERSION,
    SEEDANCE_PHYSICS_CLAUSE,
    SEEDANCE_PROMPT_CHAR_LIMIT,
    SEEDANCE_PROMPT_TARGET_CHAR_LIMIT,
    DreaminaProvider,
)


def test_latest_submit_id_recovers_last_remote_task(tmp_path):
    log_path = tmp_path / "shot_009.dreamina.log"
    log_path.write_text(
        '{"submit_id":"old-task","gen_status":"querying"}\n'
        '{"submit_id":"generated-task","gen_status":"querying"}\n'
        'download video 1: context deadline exceeded\n',
        encoding="utf-8")

    assert DreaminaProvider._latest_submit_id(log_path) == "generated-task"

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
    # 视频默认并行生产；每个假 CLI 进程必须有独立输出，避免多个进程
    # 同时截断同一个 result.mp4，制造与真实 Provider 无关的空文件竞态。
    out = os.path.join(here, "result-%s.mp4" % os.getpid())
    open(out, "wb").write(b"\\x00\\x00\\x00 ftypisom-fake")
    print(json.dumps({"status": "done", "video_path": out}))
    sys.exit(0)
sys.exit(2)
'''

FAKE_ASYNC_DREAMINA = '''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(json.dumps(args, ensure_ascii=False) + "\\n")
if args and args[0] in ("frames2video", "multimodal2video"):
    print(json.dumps({
        "submit_id": "async-video-1",
        "gen_status": "querying",
        "queue_info": {"queue_status": "Generating"},
    }))
    sys.exit(0)
if args and args[0] == "query_result":
    download = next(
        (x.split("=", 1)[1] for x in args if x.startswith("--download_dir=")),
        here)
    os.makedirs(download, exist_ok=True)
    out = os.path.join(download, "async-result.mp4")
    open(out, "wb").write(b"\\x00\\x00\\x00 ftypisom-async")
    print(json.dumps({
        "submit_id": "async-video-1",
        "gen_status": "success",
        "video_path": out,
    }))
    sys.exit(0)
if args and args[0] == "user_credit":
    print("credit balance: 420")
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


@pytest.fixture()
def fake_async_dreamina(tmp_path):
    binary = tmp_path / "async-bin" / "dreamina"
    binary.parent.mkdir(parents=True)
    binary.write_text(FAKE_ASYNC_DREAMINA, encoding="utf-8")
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
        "providers": {"jimeng": conf},
        # 假 CLI 只写最小 ftyp 字节来验证命令/路由，并非可解码成片；
        # 端到端适配器测试显式开启免检，避免把假媒体当正式技术质检样本。
        "defaults": {"preview_qc_bypass": True}})


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
            "duration": 4.5,
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
        prompt_arg = next(arg for arg in call if arg.startswith("--prompt="))
        assert prompt_arg.startswith("--prompt=妖气翻涌的古镇长街")
        assert SEEDANCE_PHYSICS_CLAUSE in prompt_arg
        assert "--duration=5" in call
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


def test_async_querying_result_is_polled_until_video(
        tmp_path, fake_async_dreamina):
    """CLI 返回 querying 是正常排队，必须继续查结果，不能回退并重复扣费。"""
    app = _make_app(
        tmp_path, fake_async_dreamina,
        {"poll": 0, "query_interval": 0.05, "timeout": 5})
    try:
        result = app.router.call("video", {
            "shot_no": 3,
            "prompt": "人物沿行动路线向镜头走来",
            "first": "/tmp/first.png",
            "last": "/tmp/last.png",
        }, app.workspace.artifacts_dir)
        assert result.provider == "jimeng"
        assert result.uri.endswith("shot_003.mp4")
        assert Path(result.uri).exists()
        calls = _calls(fake_async_dreamina)
        assert [call[0] for call in calls] == [
            "frames2video", "query_result"]
        assert "--submit_id=async-video-1" in calls[1]
        assert any(
            arg.startswith("--download_dir=") for arg in calls[1])
        assert app.router.quota_remaining("jimeng") == 999
        log_path = app.workspace.artifacts_dir / "shot_003.dreamina.log"
        assert "query_result #1" in log_path.read_text(encoding="utf-8")
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
            # 1080p 属最终成片档;预算闸门要求显式确认,否则 fail-closed
            "video_final_confirmed": resolution == "1080p",
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
        # 正式导演默认走全能参考，把人物/道具/空间资产与图1起点、图2
        # 终点一起提交；提示词必须保持精简并明确每张图的单一职责。
        calls = [c for c in _calls(fake_dreamina)
                 if c[0] in ("frames2video", "multimodal2video")]
        assert calls and all(
            "--model_version=seedance2.0fast_vip" in c for c in calls)
        assert any(c[0] == "multimodal2video" for c in calls)
        for call in calls:
            prompt = next(arg for arg in call if arg.startswith("--prompt="))
            assert len(prompt[9:]) <= SEEDANCE_PROMPT_TARGET_CHAR_LIMIT
            if call[0] == "multimodal2video":
                assert "首尾帧职责" in prompt
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
            "duration": 5.0,
        }, app.workspace.artifacts_dir)
    finally:
        app.close()
    (call,) = _calls(fake_dreamina)
    prompt = next(a for a in call if a.startswith("--prompt="))
    assert "妖气不对劲" in prompt and "自动配音" in prompt


def test_final_seedance_prompt_is_at_most_4000_including_all_suffixes(
        tmp_path, fake_dreamina):
    """长度在最后一层按完整字符串计数，配音和参考条款也不能越界。"""
    app = _make_app(tmp_path, fake_dreamina)
    long_sections = "\n".join(
        f"【{label}】{label}细节" + ("甲乙丙，；？！()[]" * 160)
        for label in (
            "输入", "主体", "起点", "单一主动作", "镜头", "终点",
            "道具状态变化", "物理/空间逻辑", "参考图职责", "硬约束"))
    try:
        result = app.router.call("video", {
            "shot_no": 12,
            "prompt_compact": long_sections,
            "dialogue": {"character": "顾清让", "dialogue": "我来查清此案"},
            "first": "/tmp/first.png", "last": "/tmp/last.png",
            "reference_images": ["/tmp/hero.png"],
            "reference_videos": ["/tmp/motion.mp4"],
            "duration": 8,
        }, app.workspace.artifacts_dir)
    finally:
        app.close()
    (call,) = _calls(fake_dreamina)
    prompt = next(arg for arg in call if arg.startswith("--prompt="))[9:]
    assert len(prompt) <= SEEDANCE_PROMPT_CHAR_LIMIT
    assert len(prompt) <= SEEDANCE_PROMPT_TARGET_CHAR_LIMIT
    assert result.data["prompt_char_count"] == len(prompt)
    assert result.data["prompt_char_limit"] == SEEDANCE_PROMPT_CHAR_LIMIT
    assert result.data["prompt_target_char_limit"] == \
        SEEDANCE_PROMPT_TARGET_CHAR_LIMIT
    assert SEEDANCE_PHYSICS_CLAUSE in prompt
    assert "人和物品道具的运动轨迹必须符合真实物理世界" in prompt
    assert "我来查清此案" in prompt
    assert "多图参考边界" in prompt
    assert "参考视频边界" in prompt


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


class _ReachedCliStop(RuntimeError):
    """策略闸全过,已到达真实付费调用点。"""


def _capture_cmd(captured):
    def _capture(_tag, cmd, _cwd, _timeout, cancel=None):
        captured["cmd"] = cmd
        raise _ReachedCliStop()
    return _capture


def test_reference_video_rides_multimodal_on_seedance20(tmp_path):
    """2.0 全家族即支持参考视频:--video 上车、运动骨架边界条款注入。"""
    from unittest import mock
    from aifos.production import dreamina as dreamina_module
    captured = {}
    provider = DreaminaProvider("jimeng", {
        "enabled": True, "capabilities": ["video"]})
    payload = {
        "shot_no": 3, "first": "/tmp/a.png", "last": "/tmp/b.png",
        "duration": 8, "prompt": "single action",
        "video_resolution": "720p",
        "reference_videos": ["/tmp/motion.mp4"],
    }
    with mock.patch.object(dreamina_module, "run_interruptible",
                           side_effect=_capture_cmd(captured)):
        with pytest.raises(_ReachedCliStop):
            provider.generate("video", payload, tmp_path)
    cmd = captured["cmd"]
    assert "multimodal2video" in cmd            # 无图片参考也走全能参考
    assert any(c.startswith("--video=") and c.endswith("motion.mp4")
               for c in cmd)
    prompt_arg = next(c for c in cmd if c.startswith("--prompt="))
    assert "运动骨架" in prompt_arg              # 身份泄漏防线
    assert "禁止从参考视频复制人脸" in prompt_arg


def test_more_than_three_reference_videos_fail_closed(tmp_path):
    provider = DreaminaProvider("jimeng", {
        "enabled": True, "capabilities": ["video"]})
    payload = {
        "shot_no": 3, "first": "/tmp/a.png", "last": "/tmp/b.png",
        "duration": 8, "prompt": "p", "video_resolution": "720p",
        "reference_videos": [f"/tmp/m{i}.mp4" for i in range(4)],
    }
    with pytest.raises(Exception) as exc:
        provider.generate("video", payload, tmp_path)
    assert "最多 3 条参考视频" in str(exc.value)
    assert "seedance2_5" in str(exc.value)      # 指路升级而不是静默丢弃
