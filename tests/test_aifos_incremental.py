"""增量生产测试:断点续产、只补缺失、--force 全量重制。"""

import json
from pathlib import Path

import pytest

from aifos.app import App


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _stage(summary, name):
    return next(s for s in summary["stages"] if s["stage"] == name)


def _finish(app, title, number):
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


def test_second_run_reuses_everything(app):
    first = _finish(app, "万妖图录", 1)
    assert first["status"] == "done"
    videos_cost_1 = _stage(first, "videos")["cost"]
    assert videos_cost_1 > 0

    second = app.director.produce("万妖图录", 1)
    assert second["status"] == "done"
    # 剧本/分镜复用,版本不增
    assert _stage(second, "script")["detail"]["reused"] is True
    assert _stage(second, "storyboard")["detail"]["reused"] is True
    project = app.projects.get_project("万妖图录")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    _, version = app.projects.latest_document(episode["id"], "script")
    assert version == 1
    # 图片/首尾帧/视频全部复用,零生成成本
    for stage in ("images", "frames", "videos"):
        report = _stage(second, stage)
        assert report["cost"] == 0
        assert report["detail"]["reused"] == report["detail"]["count"]
    voice = _stage(second, "voices")
    assert voice["cost"] == 0
    assert voice["detail"]["integrated_in_video"] is True


def test_missing_artifact_regenerated_only(app):
    first = _finish(app, "万妖图录", 2)
    videos = _stage(first, "videos")["detail"]["count"]
    project = app.projects.get_project("万妖图录")
    image_versions = {}
    for shot_no in range(1, videos + 1):
        row = app.assets.latest(
            project["id"], "image", f"e002_shot{shot_no:03d}")
        meta = json.loads(row["meta"] or "{}")
        image_versions[shot_no] = {
            "id": row["id"], "version": row["version"],
            "file_sha256": meta.get("file_sha256"),
            "shot_content_hash": meta.get("shot_content_hash"),
            "continuity_token": (
                meta.get("continuity_dependency") or {}).get("token"),
        }
    # 删除一个镜头视频,模拟真实产线中断/损坏
    victim = app.assets.latest(project["id"], "video", "e002_shot001")
    Path(victim["uri"]).unlink()

    second = app.director.produce("万妖图录", 2)
    report = _stage(second, "videos")
    assert report["detail"]["reused"] == videos - 1
    assert report["cost"] == pytest.approx(0.1)  # 仅补一个镜头
    assert _stage(second, "images")["detail"]["reused"] == videos
    assert _stage(second, "frames")["detail"]["reused"] == videos
    for shot_no, expected in image_versions.items():
        row = app.assets.latest(
            project["id"], "image", f"e002_shot{shot_no:03d}")
        meta = json.loads(row["meta"] or "{}")
        assert {
            "id": row["id"], "version": row["version"],
            "file_sha256": meta.get("file_sha256"),
            "shot_content_hash": meta.get("shot_content_hash"),
            "continuity_token": (
                meta.get("continuity_dependency") or {}).get("token"),
        } == expected
    assert Path(app.assets.latest(
        project["id"], "video", "e002_shot001")["uri"]).exists()
    assert second["status"] == "done"


def test_force_regenerates_all(app):
    _finish(app, "万妖图录", 3)
    forced = app.director.produce("万妖图录", 3, force=True)
    for stage in ("images", "frames", "videos"):
        report = _stage(forced, stage)
        assert report["detail"].get("reused", 0) == 0
        assert report["cost"] > 0
    voice = _stage(forced, "voices")
    assert voice["cost"] == 0
    assert voice["detail"]["mode"] == "jimeng_builtin"
    assert forced["status"] == "done"
