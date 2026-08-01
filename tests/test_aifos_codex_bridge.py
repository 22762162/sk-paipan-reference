"""Codex 图片适配桥测试:协议转换、文件校验、路由集成(假 codex 二进制)。"""

import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aifos.app import App

# 假 codex:记录完整参数,从指令文本中提取目标 png 路径并创建之
FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, re, struct, sys, zlib

def write_test_png(path, width=9, height=16):
    """Write a genuinely decodable 9:16 PNG for production probe tests."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    scanline = b"\\x00" + (b"\\x28\\x78\\xc8" * width)
    payload = b"\\x89PNG\\r\\n\\x1a\\n"
    payload += chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(scanline * height))
    payload += chunk(b"IEND", b"")
    with open(path, "wb") as image_file:
        image_file.write(payload)

instruction = sys.argv[-1]
log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "codex_argv.jsonl")
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
if "你是AIFOS图片生成前的提示词审核员" in instruction:
    source = instruction.split("【AIFOS原始提示词】\\n", 1)[1].split(
        "\\n【冲突裁决规则】", 1)[0]
    print(json.dumps({
        "schema": "aifos.codex-prompt-review/v1",
        "approved": True,
        "optimized_prompt": source,
        "issues_found": [], "changes_made": [], "blocking_reason": "",
    }, ensure_ascii=False))
    sys.exit(0)
if "你是漫剧图片质检员" in instruction:
    print(json.dumps({
        "pass": True,
        "identity_checked": True, "identity_match": True,
        "gender_checked": True, "gender_match": True,
        "count_checked": True, "count_match": True,
        "detected_count": 1, "issues": [],
    }, ensure_ascii=False))
    sys.exit(0)
paths = re.findall(r"(/\\S+?\\.png)", instruction)
for p in paths:
    write_test_png(p)
print("codex done:", len(paths), "files")
'''

# 假旧版 codex:不认识 --sandbox,报 unexpected argument;去掉后才成功
FAKE_OLD_CODEX = '''#!/usr/bin/env python3
import re, sys
args = sys.argv[1:]
if "--sandbox" in args or "--skip-git-repo-check" in args:
    print("error: unexpected argument '--sandbox' found", file=sys.stderr)
    sys.exit(2)
instruction = args[-1]
for p in re.findall(r"(/\\S+?\\.png)", instruction):
    open(p, "wb").write(b"fake-png")
print("old codex ok")
'''

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture()
def fake_codex(tmp_path):
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text(FAKE_CODEX, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _bridge(request, codex):
    proc = subprocess.run(
        [sys.executable, "-m", "aifos.adapters.codex_image",
         "--codex", str(codex)],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_image_capability(tmp_path, fake_codex):
    out = tmp_path / "out"
    reply = _bridge({
        "capability": "image",
        "payload": {"shot_no": 1, "prompt": "古镇长街", "characters": ["林昭"]},
        "out_dir": str(out)}, fake_codex)
    assert reply["ok"], reply
    assert reply["uri"].endswith("shot_001.keyframe.png")
    assert Path(reply["uri"]).exists()


def test_existing_target_must_be_updated_by_current_codex_call(tmp_path):
    binary = tmp_path / "bin" / "codex-noop"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\nprint('ok but no image written')\n",
        encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "shot_001.keyframe.png"
    target.write_bytes(b"old-failed-image")

    reply = _bridge({
        "capability": "image",
        "payload": {
            "shot_no": 1, "prompt": "必须重新生成",
            "characters": ["林昭"],
        },
        "out_dir": str(out),
    }, binary)

    assert reply["ok"] is False
    assert "拒绝把断点旧图冒充新结果" in reply["error"]
    assert target.read_bytes() == b"old-failed-image"


def test_frames_capability(tmp_path, fake_codex):
    out = tmp_path / "out"
    reply = _bridge({
        "capability": "frames",
        "payload": {"shot_no": 2, "image_uri": "/x.png", "prompt": "p"},
        "out_dir": str(out)}, fake_codex)
    assert reply["ok"]
    assert Path(reply["data"]["first"]).exists()
    assert Path(reply["data"]["last"]).exists()


def test_missing_codex_reports_error(tmp_path):
    reply = _bridge({
        "capability": "image",
        "payload": {"shot_no": 1},
        "out_dir": str(tmp_path / "out")}, "/missing/codex")
    assert not reply["ok"]
    assert "不存在" in reply["error"]


def test_unknown_capability(tmp_path, fake_codex):
    reply = _bridge({
        "capability": "video", "payload": {},
        "out_dir": str(tmp_path / "out")}, fake_codex)
    assert not reply["ok"]


def test_instruction_quality(tmp_path, fake_codex):
    """出图指令:真实出图指令 + 画风统一 + 参考图 + 默认非交互参数。"""
    out = tmp_path / "out"
    ref = tmp_path / "portrait_林昭.png"
    ref.write_bytes(b"x")
    scene = tmp_path / "scene_古镇.png"
    scene.write_bytes(b"x")
    reply = _bridge({
        "capability": "image",
        "payload": {"shot_no": 1, "prompt": "古镇长街夜景",
                    "characters": ["林昭"], "camera": "远景推近",
                    "style": "水墨国风",
                    "character_refs": [str(ref)],
                    "scene_ref": str(scene)},
        "out_dir": str(out)}, fake_codex)
    assert reply["ok"], reply
    argv = [json.loads(line) for line in
            (fake_codex.parent / "codex_argv.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    args = argv[-1]
    # 默认非交互参数
    assert "--sandbox" in args and "workspace-write" in args
    assert "--skip-git-repo-check" in args
    instruction = args[-1]
    # 禁止代码画图充数,必须真实出图
    assert "禁止用 Pillow" in instruction
    assert "图像生成能力" in instruction
    # 画风统一 + 参考图一致性
    assert "水墨国风" in instruction
    assert str(ref) in instruction and str(scene) in instruction
    assert "人物设定图" in instruction and "场景概念图" in instruction


def test_old_codex_flags_fallback(tmp_path):
    """旧版 codex 不认默认参数 → 自动去掉重试,依然成功。"""
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text(FAKE_OLD_CODEX, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    reply = _bridge({
        "capability": "image",
        "payload": {"shot_no": 3, "prompt": "x", "characters": []},
        "out_dir": str(tmp_path / "out")}, binary)
    assert reply["ok"], reply
    assert Path(reply["uri"]).exists()


def test_produce_passes_reference_art(tmp_path, fake_codex, monkeypatch):
    """整集制作时,镜头出图指令必须带上人物立绘/场景图做一致性参考。"""
    monkeypatch.setenv("PYTHONPATH", REPO_ROOT)
    app = App(tmp_path / "ws", config_overrides={"providers": {"codex": {
        "enabled": True,
        "command": [sys.executable, "-m", "aifos.adapters.codex_image",
                    "--codex", str(fake_codex)],
    }}})
    try:
        summary = app.director.produce("参考图验证", 1)
        assert summary["status"] == "awaiting_cast"
        project = app.projects.get_project("参考图验证")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                "参考图验证", 1, character["name"], 1)
        app.director.produce("参考图验证", 1)
    finally:
        app.close()
    log = fake_codex.parent / "codex_argv.jsonl"
    instructions = [json.loads(line)[-1] for line in
                    log.read_text(encoding="utf-8").splitlines()]
    keyframe_calls = [i for i in instructions if "keyframe" in i]
    assert keyframe_calls
    # 对白镜头的指令按参考图对照表引用人工锁定立绘(portrait_*.png 真实路径)
    assert any("最终立绘" in i and "portrait_" in i
               for i in keyframe_calls)
    # 场景图同样进对照表(编号绑定,标签为「场景…基准图」)
    assert any("基准图" in i and "scene_" in i for i in keyframe_calls)
    # 对照表编号与用途绑定进入指令
    assert any("参考图对照表" in i and "图1=" in i for i in keyframe_calls)


def test_router_integration(tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", REPO_ROOT)
    app = App(tmp_path / "ws", config_overrides={"providers": {"codex": {
        "enabled": True,
        "command": [sys.executable, "-m", "aifos.adapters.codex_image",
                    "--codex", str(fake_codex)],
    }}})
    try:
        result = app.router.call(
            "image", {"shot_no": 5, "prompt": "测试", "characters": []},
            app.workspace.artifacts_dir)
        assert result.provider == "codex"
        assert Path(result.uri).exists()
    finally:
        app.close()
