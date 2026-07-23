"""Seedance 参考图自动选入 + 已锁定角色缺失候选不再永远待生成。"""

import json
from pathlib import Path

import pytest

from aifos.app import App


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _lock_all(app, title, number=1):
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    script, _ = app.projects.latest_document(episode["id"], "script")
    for character in script["characters"]:
        app.director.select_character_candidate(
            title, number, character["name"], 1)
    return project, episode


def _preproduce(app, title):
    app.director.produce(title, 1, pause_for_confirm=True)   # 剧本停
    app.director.produce(title, 1, pause_for_confirm=True)   # 选角停
    project, episode = _lock_all(app, title)
    app.director.produce(title, 1, pause_for_confirm=True)   # 预生产停
    out = (app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001")
    return project, episode, out


def test_auto_video_references_include_necessary_images(app):
    """人工未选择时:分镜图/人物最终立绘/场景图自动选入交给 Seedance。"""
    project, episode, out = _preproduce(app, "万妖图录")
    effective = app.director.effective_video_references(episode["id"])
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    assert effective["shots"], "没有任何镜头的参考图集合"
    for shot in storyboard["shots"]:
        entry = effective["shots"][str(shot["shot_no"])]
        assert entry["mode"] == "auto"
        kinds = [item["kind"] for item in entry["items"]]
        assert "image" in kinds, f"镜头{shot['shot_no']}缺分镜图"
        if shot.get("characters"):
            assert "character_identity" in kinds, \
                f"镜头{shot['shot_no']}缺人物最终立绘"
        assert len(entry["items"]) <= 7


def test_manual_selection_overrides_auto(app):
    """人工选定(含清空)优先于自动集合。"""
    project, episode, out = _preproduce(app, "万妖图录")
    row = app.assets.latest(project["id"], "image", "e001_shot002")
    app.director.set_video_references(episode["id"], 1, [row["id"]])
    effective = app.director.effective_video_references(episode["id"])
    entry = effective["shots"]["1"]
    assert entry["mode"] == "manual"
    assert [item["asset_id"] for item in entry["items"]] == [row["id"]]
    # 清空 = 明确只用首尾帧,不回落自动
    app.director.set_video_references(episode["id"], 1, [])
    effective = app.director.effective_video_references(episode["id"])
    assert effective["shots"]["1"]["mode"] == "manual"
    assert effective["shots"]["1"]["items"] == []
    # 其他镜头仍是自动
    assert effective["shots"]["2"]["mode"] == "auto"


def test_episode_payload_carries_effective_references(app):
    from aifos.web.server import _episode_payload
    project, episode, out = _preproduce(app, "万妖图录")
    payload = _episode_payload(app, episode["id"])
    shots = payload["video_references_effective"]["shots"]
    first = shots["1"]["items"]
    assert first and all("url" in item for item in first)


def test_locked_character_missing_candidates_not_stuck_pending(app):
    """已锁定角色缺失的候选图不再挂在清单里永远"待生成"。"""
    title = "万妖图录"
    app.director.produce(title, 1, pause_for_confirm=True)
    app.director.produce(title, 1, pause_for_confirm=True)   # 候选已生成
    project, episode = _lock_all(app, title)
    # 模拟历史断点:删掉某角色 2 张候选文件(相当于当年没画完就锁定)
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    removed = 0
    for index in (4, 5):
        row = app.assets.latest(project["id"], "character_candidate",
                                f"{hero}:{index:02d}")
        if row and row["uri"] and Path(row["uri"]).exists():
            Path(row["uri"]).unlink()
            removed += 1
    assert removed == 2
    app.director.produce(title, 1, pause_for_confirm=True)   # 续跑预生产
    out = (app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out / "render_plan.json").read_text(encoding="utf-8"))
    candidates = [i for i in plan["items"]
                  if i["category"] == "character_candidate"]
    stuck = [i for i in candidates if i["status"] == "pending"]
    assert not stuck, f"仍有候选图卡在待生成: {[i['id'] for i in stuck]}"
    # 已锁定角色缺失的槽位直接不在清单里(不算进总进度)
    ids = {i["id"] for i in candidates}
    assert f"candidate:{hero}:4" not in ids
    assert f"candidate:{hero}:5" not in ids


def test_shot_example_image_always_submitted_even_low_quality(app):
    """镜头示例图有则必交:即使被归为低质量试错图也自动选入。"""
    project, episode, out = _preproduce(app, "万妖图录")
    row = app.assets.latest(project["id"], "image", "e001_shot001")
    assert row is not None
    # 模拟该分镜图被质量策略归为低质量(试错级)
    app.assets.register(
        project["id"], "image", "e001_shot001", uri=row["uri"],
        meta={"image_quality": "low"}, new_version=True)
    effective = app.director.effective_video_references(episode["id"])
    kinds = [item["kind"] for item in effective["shots"]["1"]["items"]]
    assert "image" in kinds, "低质量分镜示例图被静默丢弃(应仍提交)"


def test_reset_manual_selection_falls_back_to_auto(app):
    """人工选择可一键撤销,回落自动选入。"""
    project, episode, out = _preproduce(app, "万妖图录")
    app.director.set_video_references(episode["id"], 1, [])   # 人工清空
    assert app.director.effective_video_references(
        episode["id"])["shots"]["1"]["mode"] == "manual"
    app.director.set_video_references(episode["id"], 1, [], reset=True)
    effective = app.director.effective_video_references(episode["id"])
    assert effective["shots"]["1"]["mode"] == "auto"
    assert any(item["kind"] == "image"
               for item in effective["shots"]["1"]["items"])
