"""账号矩阵测试:项目类型/账号/画幅、偶像流水线、发布包。"""

import json
from pathlib import Path

import pytest

from aifos.app import App
from aifos.cli import main


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _finish(app, title, number, **kwargs):
    summary = app.director.produce(title, number, **kwargs)
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


def test_project_matrix_fields(app):
    project, created = app.projects.get_or_create_project(
        "小澜同学", kind="idol", account="xiaolan_ai", aspect="9:16")
    assert created
    assert (project["kind"], project["account"]) == ("idol", "xiaolan_ai")
    updated = app.projects.update_project("小澜同学", aspect="16:9")
    assert updated["aspect"] == "16:9"


def test_default_aspect_is_portrait(app):
    summary = _finish(app, "万妖图录", 1)
    assert summary["aspect"] == "9:16"
    # 关键图按 1080x1920 竖屏产出
    project = app.projects.get_project("万妖图录")
    image = app.assets.latest(project["id"], "image", "e001_shot001")
    content = Path(image["uri"]).read_text(encoding="utf-8")
    assert 'width="1080"' in content and 'height="1920"' in content


def test_landscape_project_override(app):
    app.projects.get_or_create_project("横屏剧", aspect="16:9")
    summary = _finish(app, "横屏剧", 1)
    assert summary["aspect"] == "16:9"
    project = app.projects.get_project("横屏剧")
    image = app.assets.latest(project["id"], "image", "e001_shot001")
    content = Path(image["uri"]).read_text(encoding="utf-8")
    assert 'width="1920"' in content and 'height="1080"' in content


def test_idol_pipeline(app):
    app.projects.get_or_create_project(
        "小澜同学", kind="idol", account="xiaolan_ai")
    summary = _finish(app, "小澜同学", 1, premise="新歌翻唱")
    assert summary["status"] == "done"
    project = app.projects.get_project("小澜同学")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    # 偶像模板:单一人设口播,人设名=项目名
    assert [c["name"] for c in script["characters"]] == ["小澜同学"]
    for scene in script["scenes"]:
        assert all(line["character"] == "小澜同学"
                   for line in scene["lines"])
    assert "新歌翻唱" in script["logline"]
    # 引导关注收尾
    assert any("关注" in line["dialogue"]
               for scene in script["scenes"] for line in scene["lines"])


def test_publish_kit(app):
    app.projects.get_or_create_project(
        "小澜同学", kind="idol", account="xiaolan_ai")
    summary = _finish(app, "小澜同学", 2)
    kit_path = summary["outputs"]["publish"]
    assert kit_path and Path(kit_path).exists()
    kit = json.loads(Path(kit_path).read_text(encoding="utf-8"))
    assert kit["account"] == "xiaolan_ai"
    assert kit["kind"] == "idol"
    assert kit["aspect"] == "9:16"
    assert "#AI虚拟偶像" in kit["hashtags"]
    assert kit["titles"] and kit["video"] and kit["cover"]
    assert kit["duration"] > 0


def test_publish_kit_drama_hashtags(app):
    summary = _finish(app, "万妖图录", 5)
    kit = json.loads(
        Path(summary["outputs"]["publish"]).read_text(encoding="utf-8"))
    assert "#漫剧" in kit["hashtags"]
    assert "#第5集" in kit["hashtags"]


def test_cli_project_and_publish(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "project", "create", "--title", "小澜同学",
                 "--kind", "idol", "--account", "xiaolan_ai"]) == 0
    assert "AI虚拟偶像" in capsys.readouterr().out
    assert main(["--workspace", ws, "produce", "--title", "小澜同学",
                 "--episode", "1"]) == 0
    boot = App(ws)
    try:
        _finish(boot, "小澜同学", 1)
    finally:
        boot.close()
    capsys.readouterr()
    assert main(["--workspace", ws, "publish", "--project", "小澜同学",
                 "--episode", "1"]) == 0
    out = capsys.readouterr().out
    assert "xiaolan_ai" in out and "#AI虚拟偶像" in out
    # 项目列表展示矩阵信息
    assert main(["--workspace", ws, "project", "list"]) == 0
    assert "偶像" in capsys.readouterr().out


def test_db_migration_adds_columns(tmp_path):
    """旧库(无 kind/account/aspect 列)打开后自动迁移。"""
    import sqlite3
    db_path = tmp_path / "ws" / "aifos.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', "
        "style TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT "
        "'active', created_at REAL NOT NULL)")
    conn.execute(
        "INSERT INTO projects(title, created_at) VALUES('老项目', 0)")
    conn.commit()
    conn.close()
    app = App(tmp_path / "ws")
    try:
        row = app.projects.get_project("老项目")
        assert row["kind"] == "drama"
        assert row["account"] == ""
    finally:
        app.close()
