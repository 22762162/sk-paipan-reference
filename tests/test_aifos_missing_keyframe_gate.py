"""Regression coverage for missing keyframes before the frame-chain stage."""

import pytest

from aifos.app import App
from aifos.errors import AifosError


def test_produce_pauses_after_technical_keyframe_gap(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    frames_called = []

    monkeypatch.setattr(
        "aifos.director.STAGES",
        [("images", "关键帧"), ("frames", "首尾帧")])

    def incomplete_images(ctx):
        ctx["images"] = [{"shot_no": 2, "uri": "kept.png"}]
        ctx["shot_candidate_repair_required"] = True
        ctx["shot_candidate_repair_shots"] = [1, 3]
        return {
            "count": 1,
            "technical_incomplete": True,
            "technical_incomplete_shots": [1, 3],
        }

    def frames_must_not_run(_ctx):
        frames_called.append(True)
        raise AssertionError("missing keyframes must pause before frames")

    monkeypatch.setattr(app.director, "_stage_images", incomplete_images)
    monkeypatch.setattr(app.director, "_stage_frames", frames_must_not_run)
    try:
        summary = app.director.produce("关键帧缺图保护", 1)

        assert summary["status"] == "paused"
        assert [row["stage"] for row in summary["stages"]] == ["images"]
        assert summary["stages"][0]["detail"][
            "technical_incomplete_shots"] == [1, 3]
        assert frames_called == []
    finally:
        app.close()


def test_frames_reports_all_missing_keyframes_instead_of_key_error(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    project, _ = app.projects.get_or_create_project("首尾帧缺图保护")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    marked = []
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "images": [{"shot_no": 2, "uri": "kept.png"}],
        "storyboard": {"shots": [
            {"shot_no": 1, "scene_no": 1},
            {"shot_no": 2, "scene_no": 1},
            {"shot_no": 3, "scene_no": 2},
        ]},
    }
    monkeypatch.setattr(
        app.director, "_plan_mark",
        lambda _ctx, item_id, status, **extra:
        marked.append((item_id, status, extra.get("error", ""))))
    try:
        with pytest.raises(AifosError) as captured:
            app.director._stage_frames_impl(ctx)

        message = str(captured.value)
        assert "镜头1、3" in message
        assert "KeyError" not in message
        assert "已有资产全部保留" in message
        assert [row[:2] for row in marked] == [
            ("frames:1", "pending"), ("frames:3", "pending")]
        assert all("尚未调用生图模型" in row[2] for row in marked)
    finally:
        app.close()
