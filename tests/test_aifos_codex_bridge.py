"""Codex 图片适配桥测试:协议转换、文件校验、路由集成(假 codex 二进制)。"""

import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aifos.app import App

# 假 codex:从指令文本中提取目标 png 路径并创建之
FAKE_CODEX = '''#!/usr/bin/env python3
import re, sys
instruction = sys.argv[-1]
paths = re.findall(r"(/\\S+?\\.png)", instruction)
for p in paths:
    open(p, "wb").write(b"fake-png")
print("codex done:", len(paths), "files")
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
