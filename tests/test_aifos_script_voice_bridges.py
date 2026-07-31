"""Claude 编剧桥与 macOS say 配音桥测试(假二进制)。"""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aifos.adapters.claude_script import (
    _merge_storyboard_shot_repairs,
    _postprocess_and_validate,
    _storyboard_error_shot_positions,
    extract_json,
    validate_script,
    validate_storyboard,
)
from aifos.app import App
from aifos.story_analysis import build_story_analysis

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# 假 claude:根据提示词内容返回剧本或分镜 JSON(带杂讯前后缀)
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, sys
prompt = sys.argv[-1]
if "分镜师" in prompt:
    data = {"episode_title": "T", "shots": [
        {"shot_no": 9, "scene_no": 1, "kind": "environment",
         "description": "d", "camera": "远景", "duration": 2.5,
         "characters": ["甲"], "dialogue": None, "prompt": "p1"},
        {"shot_no": 3, "scene_no": 1, "kind": "dialogue",
         "description": "d", "camera": "特写", "duration": 3.0,
         "characters": ["甲"],
         "dialogue": {"character": "甲", "dialogue": "你好"},
         "prompt": "p2"}]}
else:
    data = {"episode_title": "妖王之章", "logline": "一句话",
            "characters": [{"name": "甲", "role": "主角", "gender": "男",
                            "age_range": "25-30"}],
            "scenes": [{"scene_no": 1, "location": "古镇",
                        "characters": ["甲"], "action": "走",
                        "lines": [{"character": "甲", "dialogue": "你好"}]}]}
print("好的,以下是结果:")
print(json.dumps(data, ensure_ascii=False))
print("(完)")
'''

# 假 codex:校验编剧引擎参数(exec/只读沙箱/最终答复文件),
# stdout 只有进度杂讯,JSON 落在 --output-last-message 文件里
FAKE_CODEX = '''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
assert args[0] == "exec", args
assert args[args.index("--sandbox") + 1] == "read-only", args
assert "--skip-git-repo-check" in args, args
out = args[args.index("--output-last-message") + 1]
prompt = args[-1]
import os, pathlib
probe = pathlib.Path(sys.argv[0]).parent / "codex_home_probe.txt"
probe.write_text(os.environ.get("CODEX_HOME", ""), encoding="utf-8")
if "分镜师" in prompt:
    data = {"episode_title": "T", "shots": [
        {"shot_no": 1, "scene_no": 1, "kind": "environment",
         "description": "d", "camera": "远景", "duration": 2.5,
         "characters": ["甲"], "dialogue": None, "prompt": "p1"}]}
else:
    data = {"episode_title": "妖王之章", "logline": "一句话",
            "characters": [{"name": "甲", "role": "主角", "gender": "男",
                            "age_range": "25-30"}],
            "scenes": [{"scene_no": 1, "location": "古镇",
                        "characters": ["甲"], "action": "走",
                        "lines": [{"character": "甲", "dialogue": "你好"}]}]}
print("[progress] thinking {not-json}")
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(data, ensure_ascii=False))
'''

# 假 codex(故障):模拟账号限流/断流,错误只写 stdout
FAKE_CODEX_DOWN = '''#!/usr/bin/env python3
import sys
print("stream error: exhausted retries")
sys.exit(1)
'''

# 假 say:解析 -o 输出路径,写合法 WAV(1 秒静音)
FAKE_SAY = '''#!/usr/bin/env python3
import sys, wave
args = sys.argv[1:]
out = args[args.index("-o") + 1]
with wave.open(out, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"\\x00\\x00" * 22050)
'''


def _make_bin(tmp_path, name, content):
    binary = tmp_path / "bin" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(content, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _bridge(module, request, extra_args):
    proc = subprocess.run(
        [sys.executable, "-m", module, *extra_args],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- Claude 编剧桥 ----
def test_claude_script_generation(tmp_path):
    claude = _make_bin(tmp_path, "claude", FAKE_CLAUDE)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {"project_title": "万妖图录", "episode_number": 15,
                    "premise": "", "style": ""},
        "out_dir": str(tmp_path / "out")}, ["--claude", str(claude)])
    assert reply["ok"], reply
    assert reply["data"]["scenes"]
    assert reply["data"]["project_title"] == "万妖图录"


def test_claude_storyboard_renumbers_shots(tmp_path):
    claude = _make_bin(tmp_path, "claude", FAKE_CLAUDE)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "storyboard",
        "payload": {"script": {"scenes": []}},
        "out_dir": str(tmp_path / "out")}, ["--claude", str(claude)])
    assert reply["ok"], reply
    # 假 claude 返回乱序编号 9/3 → 桥强制连续 1..n
    assert [s["shot_no"] for s in reply["data"]["shots"]] == [1, 2]


def test_claude_missing_binary(tmp_path):
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script", "payload": {},
        "out_dir": str(tmp_path)}, ["--claude", "/missing/claude"])
    assert not reply["ok"]


# ---- Codex 编剧引擎(同一桥,--engine codex) ----
def test_codex_writer_script_generation(tmp_path):
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {"project_title": "万妖图录", "episode_number": 15,
                    "premise": "", "style": ""},
        "out_dir": str(tmp_path / "out")},
        ["--engine", "codex", "--codex", str(codex)])
    assert reply["ok"], reply
    assert reply["data"]["scenes"]
    assert reply["data"]["project_title"] == "万妖图录"


def test_codex_writer_failure_reports_stdout(tmp_path):
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX_DOWN)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script", "payload": {},
        "out_dir": str(tmp_path)},
        ["--engine", "codex", "--codex", str(codex)])
    assert not reply["ok"]
    assert "codex 编剧退出码 1" in reply["error"]
    assert "exhausted retries" in reply["error"]


def test_codex_writer_uses_dedicated_codex_home(tmp_path):
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX)
    home_b = tmp_path / "codex-home-b"
    home_b.mkdir()
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {"project_title": "万妖图录", "episode_number": 15,
                    "premise": "", "style": ""},
        "out_dir": str(tmp_path / "out")},
        ["--engine", "codex", "--codex", str(codex),
         "--codex-home", str(home_b)])
    assert reply["ok"], reply
    probe = (codex.parent / "codex_home_probe.txt").read_text("utf-8")
    assert probe == str(home_b.resolve())


def test_codex_writer_missing_codex_home_fails_clearly(tmp_path):
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script", "payload": {},
        "out_dir": str(tmp_path)},
        ["--engine", "codex", "--codex", str(codex),
         "--codex-home", str(tmp_path / "no-such-home")])
    assert not reply["ok"]
    assert "CODEX_HOME 不存在" in reply["error"]


def test_extract_json_tolerates_noise():
    assert extract_json('前缀 {"a": 1} 后缀')["a"] == 1
    assert extract_json("{broken} {\"b\": 2}")["b"] == 2
    assert extract_json("没有对象") is None


def test_storyboard_repair_targets_only_reported_shots_and_merges():
    error = (
        "镜头1的 PROP-A 起止状态变化但缺少 start→end prop_transitions；"
        "镜头6的 PROP-A 起止状态变化但缺少 start→end prop_transitions")
    assert _storyboard_error_shot_positions(error, 8) == [1, 6]
    source = {
        "episode_title": "袖中官凭",
        "shots": [
            {"shot_no": index, "keep": f"source-{index}"}
            for index in range(1, 9)
        ],
    }
    repaired = {"shots": [
        {"_position": 1, "shot_no": 1, "fixed": True},
        {"_position": 6, "shot_no": 6, "fixed": True},
    ]}
    merged = _merge_storyboard_shot_repairs(
        source, repaired, positions=[1, 6])
    assert merged["shots"][0] == {
        "shot_no": 1, "fixed": True}
    assert merged["shots"][5] == {
        "shot_no": 6, "fixed": True}
    assert merged["shots"][1] == {
        "shot_no": 2, "keep": "source-2"}
    assert source["shots"][0]["keep"] == "source-1"


def test_validators():
    assert validate_script({"scenes": []}, {}) is not None
    ok_script = {"characters": [{"name": "甲"}],
                 "scenes": [{"scene_no": 1, "location": "x",
                             "lines": [{"character": "甲",
                                        "dialogue": "y"}]}]}
    assert validate_script(ok_script, {"project_title": "T"}) is None
    assert ok_script["project_title"] == "T"
    assert validate_storyboard({"shots": [{"scene_no": 1}]}) is not None
    assert validate_storyboard(
        {"shots": [{"scene_no": 1, "duration": 2.0, "prompt": "p"}]}) is None


def test_script_bible_is_required_normalized_and_declared_cast_only():
    from aifos.adapters.claude_script import build_prompt

    script = {
        "characters": [{"name": "甲", "role": "主角"}],
        "scenes": [{"scene_no": 1, "location": "旧车站",
                    "characters": ["甲"],
                    "lines": [{"character": "甲", "dialogue": "我回来了"}]}],
    }
    payload = {"project_title": "归途", "episode_number": 1,
               "premise": "甲回到十年前离开的故乡",
               "style": "九十年代现实主义"}
    assert validate_script(script, payload) is None
    assert script["story_bible_version"] == 1
    assert script["story_world"]["overview"]
    assert script["story_background"]["prior_events"]
    assert script["characters"][0]["introduction"]
    assert script["characters"][0]["gender"].startswith("未指定")

    prompt = build_prompt("script", payload)
    assert "story_world" in prompt
    assert "story_background" in prompt
    assert "人物设定介绍" in prompt
    storyboard_prompt = build_prompt("storyboard", {"script": script})
    assert "世界观" in storyboard_prompt
    assert "不得新增人物表之外的角色" in storyboard_prompt

    invalid = {
        "characters": [{"name": "甲"}],
        "scenes": [{"scene_no": 1, "location": "旧车站",
                    "characters": ["甲", "乙"],
                    "lines": [{"character": "乙", "dialogue": "你终于回来了"}]}],
    }
    assert "未在人物设定中介绍" in validate_script(invalid, payload)

    with_extra = {
        "characters": [
            {"name": "甲", "role": "主角"},
            {"name": "站台路人", "role": "背景路人",
             "introduction": "不应保留的独立设定",
             "gender": "男", "age_range": "中年",
             "identity": "路人", "personality": "着急",
             "background_prompt": "不应触发独立人物图"},
        ],
        "scenes": [{"scene_no": 1, "location": "旧车站",
                    "characters": ["甲", "站台路人"],
                    "lines": [
                        {"character": "甲", "dialogue": "我回来了"},
                        {"character": "站台路人", "dialogue": "借过"},
                    ]}],
    }
    assert validate_script(with_extra, payload) is None
    extra = with_extra["characters"][1]
    assert extra["candidate_count"] == 0
    assert extra["asset_policy"] == "scene_only_no_individual_asset"
    assert extra["crowd_function"]
    assert "introduction" not in extra
    assert "background_prompt" not in extra


def test_story_analysis_missing_derived_visual_field_is_normalized_locally():
    """新增派生字段缺失时本地补齐，不能要求模型重发整份制作圣经。"""
    script = {
        "project_title": "归途",
        "characters": [{"name": "甲", "role": "主角", "gender": "男",
                        "age_range": "25至30岁"}],
        "scenes": [{"scene_no": 1, "location": "旧车站",
                    "characters": ["甲"],
                    "action": "甲走进站台",
                    "lines": [{"character": "甲", "dialogue": "我回来了"}]}],
    }
    assert validate_script(
        script, {"project_title": "归途", "episode_number": 1,
                 "premise": "甲回到故乡", "style": "九十年代现实主义"}
    ) is None
    raw = build_story_analysis(script, "九十年代现实主义")
    raw["visual"].pop("visual_effect_language")

    normalized, error = _postprocess_and_validate(
        "script",
        {"story_analysis": True, "script": script,
         "style": "九十年代现实主义"},
        raw,
    )

    assert error is None
    assert normalized["schema"] == "aifos.story-analysis/v1"
    assert normalized["visual"]["visual_effect_language"]


def test_router_uses_claude_for_script(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", REPO_ROOT)
    claude = _make_bin(tmp_path, "claude", FAKE_CLAUDE)
    app = App(tmp_path / "ws", config_overrides={"providers": {"claude": {
        "enabled": True,
        "command": [sys.executable, "-m", "aifos.adapters.claude_script",
                    "--claude", str(claude)]}}})
    try:
        result = app.router.call(
            "script", {"project_title": "万妖图录", "episode_number": 1},
            app.workspace.artifacts_dir)
        assert result.provider == "claude"
        assert result.data["scenes"]
    finally:
        app.close()


def test_claimed_director_review_rejects_abstract_unshootable_scene():
    payload = {"project_title": "导演逻辑门禁", "episode_number": 1}
    script = {
        "characters": [{"name": "甲", "role": "主角"}],
        "adaptation_review": {
            "source_to_screen_strategy": "把心理改成动作",
            "causal_chain": "触发到结果",
            "character_motivation": "目标驱动",
            "physical_reality": "检查重力接触",
            "spatial_continuity": "检查进出站位",
            "shootability": "镜头可见",
            "self_reviewed": True,
        },
        "scenes": [{
            "scene_no": 1, "location": "房间", "characters": ["甲"],
            "action": "甲意识到局势紧张",
            "lines": [{"character": "甲", "dialogue": "不好。"}],
        }],
    }
    error = validate_script(script, payload)
    assert "物理/空间/可拍摄性门禁失败" in error
    assert "导演改编字段仍由平台兜底" in error


# ---- say 配音桥 ----
def test_say_voice_generation(tmp_path):
    say = _make_bin(tmp_path, "say", FAKE_SAY)
    reply = _bridge("aifos.adapters.say_voice", {
        "capability": "voice",
        "payload": {"line_no": 3, "character": "甲", "text": "你好世界"},
        "out_dir": str(tmp_path / "out")}, ["--say", str(say)])
    assert reply["ok"], reply
    assert reply["uri"].endswith("line_003.wav")
    assert Path(reply["uri"]).exists()
    assert reply["data"]["duration"] == pytest.approx(1.0, abs=0.05)


def test_say_empty_text(tmp_path):
    say = _make_bin(tmp_path, "say", FAKE_SAY)
    reply = _bridge("aifos.adapters.say_voice", {
        "capability": "voice", "payload": {"line_no": 1, "text": ""},
        "out_dir": str(tmp_path)}, ["--say", str(say)])
    assert not reply["ok"]


def test_voice_routing_custom_cli_provider(tmp_path, monkeypatch):
    """say 已不在默认配置/路由里;手动声明完整 Provider 仍可接入。"""
    monkeypatch.setenv("PYTHONPATH", REPO_ROOT)
    say = _make_bin(tmp_path, "say", FAKE_SAY)
    app = App(tmp_path / "ws", config_overrides={
        "providers": {"say": {
            "type": "cli", "enabled": True, "capabilities": ["voice"],
            "command": [sys.executable, "-m", "aifos.adapters.say_voice",
                        "--say", str(say)],
            "cost_per_call": 0.0, "timeout": 120}},
        "routing": {"voice": ["say", "mock"]}})
    try:
        result = app.router.call(
            "voice", {"line_no": 1, "character": "甲", "text": "测试"},
            app.workspace.artifacts_dir)
        assert result.provider == "say"
        assert result.uri.endswith(".wav")
    finally:
        app.close()


def test_say_absent_from_defaults():
    """本地 say 配音已从默认产线移除(效果差):配音默认随视频/豆包。"""
    from aifos.config import DEFAULTS
    assert "say" not in DEFAULTS["providers"]
    assert DEFAULTS["routing"]["voice"] == ["doubao_tts", "api", "mock"]
    assert DEFAULTS["providers"]["jimeng"]["audio_in_video"] is True
    assert DEFAULTS["providers"]["ark"]["audio_in_video"] is True


# 假 codex(两遍):首遍剧本带无法本地归一的 phase="midway";
# 修复调用(提示词含"机器校验发现")返回改正后的完整 JSON
FAKE_CODEX_REPAIR = '''#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
out = args[args.index("--output-last-message") + 1]
prompt = args[-1]
counter = pathlib.Path(sys.argv[0]).parent / "invocations.txt"
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1))
registry_phase = "midway"
if "机器校验发现" in prompt:
    registry_phase = "start"
data = {"episode_title": "妖王之章", "logline": "一句话",
        "characters": [{"name": "甲", "role": "主角", "gender": "男",
                        "age_range": "25-30"}],
        "scenes": [{"scene_no": 1, "location": "古镇",
                    "characters": ["甲"], "action": "走",
                    "lines": [{"character": "甲", "dialogue": "你好"}]}],
        "prop_registry": [{
            "prop_id": "prop-letter", "name": "血书", "kind": "core",
            "instance_count": 1,
            "availability_start_event": {"event_id": "episode-start",
                                          "phase": registry_phase},
            "disclosure_policy": "explicit_frame_only"}]}
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(data, ensure_ascii=False))
'''


def test_codex_writer_repairs_invalid_enum_in_place(tmp_path):
    """内容性校验失败 → 同引擎就地修复复检,不丢弃整份剧本换产线。"""
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX_REPAIR)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {"project_title": "万妖图录", "episode_number": 15,
                    "premise": "", "style": ""},
        "out_dir": str(tmp_path / "out")},
        ["--engine", "codex", "--codex", str(codex)])
    assert reply["ok"], reply
    assert reply.get("repaired_fields") is True
    phase = reply["data"]["prop_registry"][0][
        "availability_start_event"]["phase"]
    assert phase == "start"
    # 恰好两次调用:初次生成 + 一次局部修复
    assert (codex.parent / "invocations.txt").read_text() == "2"


def test_codex_writer_alias_phase_needs_no_repair_call(tmp_path):
    """可本地归一的 phase 别名(开场→start)零成本通过,不触发修复调用。"""
    content = FAKE_CODEX_REPAIR.replace('"midway"', '"开场"')
    codex = _make_bin(tmp_path, "codex", content)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {"project_title": "万妖图录", "episode_number": 15,
                    "premise": "", "style": ""},
        "out_dir": str(tmp_path / "out")},
        ["--engine", "codex", "--codex", str(codex)])
    assert reply["ok"], reply
    assert not reply.get("repaired_fields")
    assert (codex.parent / "invocations.txt").read_text() == "1"


# 假 codex(卡住):打一行进度后彻底静默,模拟断流/挂起
FAKE_CODEX_STALLED = '''#!/usr/bin/env python3
import sys, time
print("[progress] started", flush=True)
time.sleep(30)
'''

# 假 codex(形象改写):校验 refine 提示词要素,返回改写 JSON
FAKE_CODEX_REFINE = '''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
out = args[args.index("--output-last-message") + 1]
prompt = args[-1]
assert "造型总监" in prompt, "缺少改写角色设定的提示词"
assert "头发再长" in prompt, "用户意见未进入提示词"
data = {"image_prompt": "国风少女,及腰黑长直,肤色白皙透亮,眼神清冷,"
                         "月白色襦裙,银质流苏耳饰,骨相与年龄感保持原设定",
        "changes": ["发型:齐肩短发改为及腰黑长直(落实\\"头发再长一点\\")",
                    "肤色:自然肤色提亮为白皙透亮(落实\\"皮肤更白\\")"],
        "conflict_notes": []}
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(data, ensure_ascii=False))
'''


def test_codex_writer_stall_watchdog_terminates(tmp_path):
    """连续无输出超过 stall 阈值 → 判定卡住终止,明确报错不回退。"""
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX_STALLED)
    import time as _time
    started = _time.monotonic()
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script", "payload": {},
        "out_dir": str(tmp_path)},
        ["--engine", "codex", "--codex", str(codex),
         "--timeout", "0", "--stall-timeout", "2"])
    elapsed = _time.monotonic() - started
    assert not reply["ok"]
    assert "卡住" in reply["error"]
    assert "无输出且无 CPU 活动" in reply["error"]
    assert elapsed < 20, f"看门狗未及时终止: {elapsed:.1f}s"


def test_prompt_refine_capability_roundtrip(tmp_path):
    """人物意见 → AI 改写形象提示词:要素齐全且通过校验。"""
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX_REFINE)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {
            "prompt_refine": True,
            "character_name": "小狐",
            "current_prompt": "国风少女,齐肩短发,自然肤色",
            "character_context": {"introduction": "山中狐仙化形的少女",
                                   "gender": "女", "age_range": "16-18"},
            "style": "国风水墨",
            "feedback": "头发再长一点,皮肤更白",
        },
        "out_dir": str(tmp_path / "out")},
        ["--engine", "codex", "--codex", str(codex)])
    assert reply["ok"], reply
    assert "及腰" in reply["data"]["image_prompt"]
    assert len(reply["data"]["changes"]) == 2


def test_validate_prompt_refine_rules():
    from aifos.adapters.claude_script import validate_prompt_refine
    assert validate_prompt_refine({"image_prompt": "短"}) is not None
    assert validate_prompt_refine(
        {"image_prompt": "x" * 30, "changes": []}) is not None   # 必须说明改动
    ok = {"image_prompt": "x" * 30, "changes": ["改了发型"]}
    assert validate_prompt_refine(ok) is None
    assert ok["conflict_notes"] == []   # 缺省字段被补齐


FAKE_CODEX_SILENT_BUSY = '''#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
out = args[args.index("--output-last-message") + 1]
t0 = time.time()
while time.time() - t0 < 8:        # 8 秒纯计算,零输出(模拟长思考)
    sum(i * i for i in range(20000))
data = {"episode_title": "妖王之章", "logline": "一句话",
        "characters": [{"name": "甲", "role": "主角", "gender": "男",
                        "age_range": "25-30"}],
        "scenes": [{"scene_no": 1, "location": "古镇",
                    "characters": ["甲"], "action": "走",
                    "lines": [{"character": "甲", "dialogue": "你好"}]}]}
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(data, ensure_ascii=False))
'''


def test_stall_watchdog_does_not_kill_silent_but_working_codex(tmp_path):
    """codex 长思考期几乎零输出;只要 CPU 在推进就必须判定存活。"""
    codex = _make_bin(tmp_path, "codex", FAKE_CODEX_SILENT_BUSY)
    reply = _bridge("aifos.adapters.claude_script", {
        "capability": "script",
        "payload": {"project_title": "万妖图录", "episode_number": 15,
                    "premise": "", "style": ""},
        "out_dir": str(tmp_path / "out")},
        ["--engine", "codex", "--codex", str(codex),
         "--timeout", "0", "--stall-timeout", "4"])
    assert reply["ok"], reply      # 静默 8s > 阈值 4s,但 CPU 在跑
    assert reply["data"]["scenes"]
