"""预生产确认流测试:暂停 → 确认 → 自动完成;人物/场景图资产。"""

from pathlib import Path

import pytest

from aifos.app import App
from aifos.cli import main


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def test_review_pauses_before_video(app):
    summary = app.director.produce("万妖图录", 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    stages = [s["stage"] for s in summary["stages"]]
    assert stages == ["script", "cast", "storyboard", "images", "frames"]
    # 预生产阶段不消耗视频产线
    project = app.projects.get_project("万妖图录")
    assert app.assets.latest(project["id"], "video", "e001_shot001") is None
    # 但人物立绘/场景概念图与首尾帧已就绪
    assert app.assets.latest(project["id"], "character_art",
                             app.assets.list(project["id"],
                                             "character")[0]["name"])
    assert app.assets.list(project["id"], "scene_art")
    assert app.assets.latest(project["id"], "first_frame", "e001_shot001")


def test_confirm_continues_to_done(app):
    app.director.produce("万妖图录", 2, pause_for_confirm=True)
    summary = app.director.produce("万妖图录", 2)  # 确认 = 无暂停再次调用
    assert summary["status"] == "done"
    # 预生产产物全部复用,只补视频之后的部分
    for stage in ("images", "frames"):
        report = next(s for s in summary["stages"] if s["stage"] == stage)
        assert report["cost"] == 0
    videos = next(s for s in summary["stages"] if s["stage"] == "videos")
    assert videos["cost"] > 0
    project = app.projects.get_project("万妖图录")
    assert Path(app.assets.latest(
        project["id"], "video", "e002_shot001")["uri"]).exists()


def test_cast_art_reused_across_episodes(app):
    app.director.produce("万妖图录", 1)
    project = app.projects.get_project("万妖图录")
    first_arts = {r["name"]: r["uri"]
                  for r in app.assets.list(project["id"], "character_art")}
    summary = app.director.produce("万妖图录", 3)
    cast = next(s for s in summary["stages"] if s["stage"] == "cast")
    # 第二集与第一集若出现相同角色/场景则复用其立绘
    arts = {r["name"]: r["uri"]
            for r in app.assets.list(project["id"], "character_art")}
    for name, uri in first_arts.items():
        assert arts[name] == uri
    assert cast["status"] == "done"


def test_cli_review_and_confirm(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    code = main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "1", "--review"])
    out = capsys.readouterr().out
    assert code == 0
    assert "待确认" in out and "confirm" in out
    code = main(["--workspace", ws, "confirm", "--project", "万妖图录",
                 "--episode", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "完成" in out
