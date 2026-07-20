"""打磨系统测试:剧本意见重写、单张图片附意见重画。"""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aifos.app import App
from aifos.cli import main

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _script_of(app, title, number):
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    return app.projects.latest_document(episode["id"], "script")


def test_revise_script_regenerates(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    before, v1 = _script_of(app, "万妖图录", 1)
    summary = app.director.revise_script(
        "万妖图录", 1, "节奏太慢,加一场打斗")
    assert summary["status"] == "awaiting_confirm"
    after, v2 = _script_of(app, "万妖图录", 1)
    assert v2 == v1 + 1
    assert after != before  # 意见进入 payload → 确定性变化
    # 意见沉淀到数据中心
    cases = app.data.cases(label="failure", kind="case")
    assert any("节奏太慢" in row["prompt"] for row in cases)


def test_regen_character_art(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    project = app.projects.get_project("万妖图录")
    name = app.assets.list(project["id"], "character_art")[0]["name"]
    v1 = app.assets.latest(project["id"], "character_art", name)["version"]
    result = app.director.regen_image(
        "万妖图录", 1, {"kind": "character_art", "name": name},
        feedback="表情更凶")
    assert Path(result["uri"]).exists()
    v2 = app.assets.latest(project["id"], "character_art", name)["version"]
    assert v2 == v1 + 1


def test_regen_shot_invalidates_video(app):
    app.director.produce("万妖图录", 2)  # 完整制作,含视频
    project = app.projects.get_project("万妖图录")
    assert app.assets.latest(project["id"], "video", "e002_shot001")
    app.director.regen_image(
        "万妖图录", 2, {"kind": "shot", "shot_no": 1}, feedback="改成夜晚")
    # 旧视频作废,首尾帧已重生成
    assert app.assets.latest(project["id"], "video", "e002_shot001") is None
    assert app.assets.latest(
        project["id"], "image", "e002_shot001")["version"] >= 2
    # 增量补齐:只重拍该镜头视频
    summary = app.director.produce("万妖图录", 2)
    assert summary["status"] == "done"
    videos = next(s for s in summary["stages"] if s["stage"] == "videos")
    assert videos["detail"]["reused"] == videos["detail"]["count"] - 1


def test_regen_scene_art_and_errors(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    project = app.projects.get_project("万妖图录")
    scene = app.assets.list(project["id"], "scene_art")[0]["name"]
    result = app.director.regen_image(
        "万妖图录", 1, {"kind": "scene_art", "name": scene})
    assert Path(result["uri"]).exists()
    from aifos.errors import AifosError
    with pytest.raises(AifosError):
        app.director.regen_image("万妖图录", 1, {"kind": "shot",
                                                 "shot_no": 999})
    with pytest.raises(AifosError):
        app.director.regen_image("不存在", 1, {"kind": "shot", "shot_no": 1})


def test_claude_bridge_receives_feedback(tmp_path):
    """修改意见与上一版剧本必须进入 claude 提示词。"""
    dump = tmp_path / "prompt.txt"
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(dump)!r}, 'w').write(sys.argv[-1])\n"
        "print(json.dumps({'episode_title': 'T', 'logline': 'L',"
        "'characters': [{'name': '甲', 'role': '主角'}],"
        "'scenes': [{'scene_no': 1, 'location': 'x', 'characters': ['甲'],"
        "'action': '', 'lines': [{'character': '甲', 'dialogue': 'hi'}]}]},"
        " ensure_ascii=False))\n",
        encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    request = {
        "capability": "script",
        "payload": {"project_title": "T", "episode_number": 1,
                    "feedback": "加一场雨戏",
                    "previous_script": {"logline": "旧版梗概"}},
        "out_dir": str(tmp_path / "out"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "aifos.adapters.claude_script",
         "--claude", str(fake)],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT)
    reply = json.loads(proc.stdout.strip().splitlines()[-1])
    assert reply["ok"], reply
    prompt = dump.read_text(encoding="utf-8")
    assert "加一场雨戏" in prompt
    assert "旧版梗概" in prompt


def test_cli_revise_and_regen(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "1", "--review"]) == 0
    capsys.readouterr()
    assert main(["--workspace", ws, "revise", "--project", "万妖图录",
                 "--episode", "1", "--feedback", "更热血"]) == 0
    assert "重写" in capsys.readouterr().out
    assert main(["--workspace", ws, "regen", "--project", "万妖图录",
                 "--episode", "1", "--shot", "1",
                 "--feedback", "夜晚"]) == 0
    assert "已重画" in capsys.readouterr().out
