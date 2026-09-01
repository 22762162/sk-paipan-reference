"""提示词区(/api/episode/<id>/prompts)必须覆盖整集，而不是只覆盖镜头。

一集里非镜头资产(定角候选/人物立绘/设定图/道具/场景母版)的提示词条数通常
是镜头的两三倍，它们出错一样毁整集；视频这一栏也不能因为不进 render_plan
就结构性空白。空白必须可解释:还没做、做了没留痕、还是根本不进清单。
"""

import pytest

from aifos.app import App
from aifos.prompt_review import (
    ACTUAL_STATES,
    ASSET_CATEGORIES,
    FRAME_VARIANTS,
    _actual_state,
    _asset_prompt_rows,
    build_episode_prompt_review,
)


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="提示词区覆盖", number=1):
    app.director.produce(title, number, pause_for_confirm=True)
    summary = app.director.produce(title, number, pause_for_confirm=True)
    if summary["status"] == "awaiting_cast":
        project = app.projects.get_project(title)
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], number))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                title, number, character["name"], 1)
        app.director.produce(title, number, pause_for_confirm=True)
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    return project, episode


def test_actual_state_separates_pending_missing_and_untracked():
    """空白的三种含义必须分得开，否则用户读不懂一片空的提示词区。"""
    produced = {"prompt": "x", "state": "produced"}
    assert _actual_state({"status": "done"}, produced) == "produced"
    # 已生产却没有实际输入记录 = 无法追溯，和"还没做"是两回事。
    assert _actual_state({"status": "done"}, None) == "missing"
    assert _actual_state({"status": "reused"}, None) == "missing"
    assert _actual_state({"status": "pending"}, None) == "pending"
    assert _actual_state({"status": "queued"}, None) == "pending"
    # 这一类根本不进 render_plan(例如视频)。
    assert _actual_state(None, None) == "not_tracked"
    # 显式 state 优先，让"待提交"这类来源自己说了算。
    assert _actual_state(
        {"status": "pending"}, {"state": "planned"}) == "planned"
    for key in ("produced", "pending", "missing", "planned", "not_tracked"):
        assert ACTUAL_STATES[key]


def test_asset_prompt_rows_group_every_non_shot_category():
    plan = {"items": [
        {"id": "candidate:林川:1", "category": "character_candidate",
         "name": "林川", "label": "林川·候选1", "status": "done",
         "prompt": "编译稿", "prompt_used": "实际发送稿",
         "prompt_used_hash": "h1",
         "reference_inputs": {"items": [{"uri": "/a.png", "label": "风格基准"}]},
         "prompt_review": {"approved": True},
         "qc": {"passed": False, "issues": ["人数不符"]}},
        {"id": "scene:茶棚", "category": "scene_art", "name": "茶棚",
         "status": "pending", "prompt": "场景母版提示词"},
        # 镜头类不属于资产段，避免和镜头卡片重复。
        {"id": "shot:1", "category": "shot_image", "shot_no": 1,
         "prompt": "镜头提示词", "status": "done"},
        {"id": "frames:1", "category": "frames", "shot_no": 1,
         "status": "pending"},
    ]}
    groups = _asset_prompt_rows(plan)
    assert [group["category"] for group in groups] == [
        "character_candidate", "scene_art"]
    assert all(
        group["category"] in dict(ASSET_CATEGORIES) for group in groups)

    candidate = groups[0]["items"][0]
    assert candidate["prompt"] == "编译稿"
    assert candidate["actual_generation"]["prompt"] == "实际发送稿"
    assert candidate["actual_state"] == "produced"
    assert candidate["prompt_review_approved"] is True
    assert candidate["qc_passed"] is False
    assert candidate["qc_issues"] == ["人数不符"]
    assert candidate["references"][0]["uri"] == "/a.png"

    scene = groups[1]["items"][0]
    assert scene["actual_generation"] is None
    assert scene["actual_state"] == "pending"
    assert groups[1]["with_prompt"] == 1 and groups[1]["missing_prompt"] == 0


def test_asset_rows_flag_items_that_lost_their_prompt():
    plan = {"items": [
        {"id": "sheet:林川:front", "category": "character_sheet",
         "name": "林川", "status": "done", "prompt": ""},
    ]}
    groups = _asset_prompt_rows(plan)
    assert groups[0]["missing_prompt"] == 1
    row = groups[0]["items"][0]
    assert row["prompt"] == ""
    # 已生产却查不到提示词，必须报出来而不是显示成"还没做"。
    assert row["actual_state"] == "missing"


def test_prompt_review_page_covers_shots_and_non_shot_assets(app):
    project, episode = _preproduce(app)
    payload = build_episode_prompt_review(app, episode["id"])

    assert payload["shots"], "至少要有镜头提示词"
    groups = payload["assets"]
    assert groups, "非镜头资产提示词不能缺席提示词区"
    assets_total = sum(group["total"] for group in groups)
    assert payload["summary"]["assets_total"] == assets_total
    assert assets_total > 0
    # 人物类资产是每集必有的，缺了就说明整段没接上。
    assert any(
        group["category"].startswith("character") for group in groups)
    for group in groups:
        assert group["items"]
        for row in group["items"]:
            assert row["id"]
            assert row["actual_state"] in ACTUAL_STATES


def test_video_variant_is_not_structurally_blank(app):
    """视频不进 render_plan，但提示词区不能因此永远空白。"""
    project, episode = _preproduce(app)
    payload = build_episode_prompt_review(app, episode["id"])

    kinds = [kind for kind, _label, _mode in FRAME_VARIANTS]
    for shot in payload["shots"]:
        assert [item["kind"] for item in shot["variants"]] == kinds
        for variant in shot["variants"]:
            # 每一栏都要能解释自己为什么有/没有实际输入。
            assert variant["actual_state"] in ACTUAL_STATES
            assert isinstance(variant["submitted_references"], list)
        video = next(
            item for item in shot["variants"] if item["kind"] == "video")
        assert "reference_notes" in video
        actual = video["actual_generation"]
        if actual is not None:
            # 未拍时显示"将要提交"的分镜稿，而不是假装没有输入。
            assert actual["state"] in {"planned", "produced"}
            assert actual["prompt"].strip()
        else:
            assert video["actual_state"] == "not_tracked"
