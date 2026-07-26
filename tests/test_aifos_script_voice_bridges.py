"""Claude 编剧桥与 macOS say 配音桥测试(假二进制)。"""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aifos.adapters.claude_script import (
    extract_json, validate_script, validate_storyboard)
from aifos.app import App

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


def test_extract_json_tolerates_noise():
    assert extract_json('前缀 {"a": 1} 后缀')["a"] == 1
    assert extract_json("{broken} {\"b\": 2}")["b"] == 2
    assert extract_json("没有对象") is None


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
