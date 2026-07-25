"""核心道具四选一、人工锁定与下游真实参考图绑定。"""

from pathlib import Path

import pytest

from aifos.app import App
from aifos.adapters.codex_image import build_instruction


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _to_asset_selection(app, title="道具四选一"):
    first = app.director.produce(title, 1, pause_for_confirm=True)
    assert first["status"] == "awaiting_script"
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    script["core_props"] = [{
        "name": "铜制机关钥匙",
        "story_function": "跨三镜开启密室并证明主人身份",
        "visual_design": "掌心长，燕尾齿与圆环握柄",
        "era_material": "旧铜锻造，边缘有长期摩擦包浆",
        "owner": "林昭",
        "continuity_states": "桌面→林昭右手→插入锁孔→留在门锁",
        "candidate_count": 4,
    }]
    app.projects.save_document(episode["id"], "script", script)
    second = app.director.produce(title, 1, pause_for_confirm=True)
    assert second["status"] == "awaiting_cast"
    return project, episode, script


def test_core_prop_gets_four_candidates_and_blocks_until_selected(app):
    project, _episode, script = _to_asset_selection(app)
    selection = app.director.production_asset_selection_status(
        project["id"], script)
    assert selection["prop_total"] == 1
    assert selection["props"][0]["candidate_target"] == 4
    assert len(selection["props"][0]["candidates"]) == 4

    for character in script["characters"]:
        if character.get("candidate_count") == 0:
            continue
        app.director.select_character_candidate(
            project["title"], 1, character["name"], 1)
    selection = app.director.production_asset_selection_status(
        project["id"], script)
    assert selection["passed"] is False
    assert selection["asset_locked"] + 1 == selection["asset_total"]

    selected = app.director.select_prop_candidate(
        project["title"], 1, "铜制机关钥匙", 2)
    assert selected["passed"] is True
    assert selected["props"][0]["locked"] is True

    character_versions = {
        item["character"]: item["identity_version"]
        for item in selected["characters"]}
    regenerated = app.director.regenerate_character_candidates(
        project["title"], 1, prop_name="铜制机关钥匙")
    assert regenerated["status"] == "awaiting_cast"
    refreshed = app.director.production_asset_selection_status(
        project["id"], script)
    assert refreshed["props"][0]["locked"] is False
    assert len(refreshed["props"][0]["candidates"]) == 4
    assert {
        item["character"]: item["identity_version"]
        for item in refreshed["characters"]
    } == character_versions


def test_locked_prop_is_a_real_shot_reference_with_single_duty(app):
    project, episode, script = _to_asset_selection(app, "道具参考绑定")
    app.director.select_prop_candidate(
        project["title"], 1, "铜制机关钥匙", 1)
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "script": script,
        "out_root": app.workspace.artifacts_dir / "prop-ref-test",
        "character_asset_policy": {"generate_sheets": False},
    }
    refs = app.director._art_refs(
        ctx, [], "", shot_no=1, prop_names=["铜制机关钥匙"])
    payload = {
        **refs,
        "require_reference_images": True,
        "prompt": "林昭用铜制机关钥匙打开密室门",
    }
    app.director._attach_reference_manifest(payload)

    assert len(payload["prop_refs"]) == 1
    assert Path(payload["prop_refs"][0]).is_file()
    prop_ref = next(
        item for item in payload["reference_manifest"]
        if item["kind"] == "prop_identity")
    assert prop_ref["role"] == "prop"
    assert "只锁定核心道具" in prop_ref["binding"]

    ctx["storyboard"] = {"shots": [{
        "shot_no": 1, "scene_no": 1, "characters": [],
        "description": "铜制机关钥匙插入锁孔并转动",
    }]}
    video_rows = app.director._auto_video_reference_rows(ctx, 1)
    assert any(row["kind"] == "prop_identity" for row in video_rows)


def test_codex_adapter_has_dedicated_prop_candidate_output(tmp_path):
    instruction, targets, data = build_instruction(
        "image", {
            "prop_candidate": True,
            "prop_name": "铜制机关钥匙",
            "art_name": "铜制机关钥匙_candidate_01",
            "prompt": "单件旧铜钥匙，纯背景",
            "prompt_contract_complete": True,
            "aspect": "1:1", "width": 1024, "height": 1024,
        }, tmp_path)
    assert "供人工四选一" in instruction
    assert targets[0].name.startswith("prop_")
    assert data["prop"] == "铜制机关钥匙"
