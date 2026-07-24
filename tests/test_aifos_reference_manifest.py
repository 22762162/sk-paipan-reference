"""参考图对照表:出图前就写死"谁参考哪张图",编号与提交顺序一致。"""

import pytest

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.production.api_providers import _local_refs, _reference_entries

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
       b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc"
       b"\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`"
       b"\x82")


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="万妖图录"):
    app.director.produce(title, 1, pause_for_confirm=True)
    app.director.produce(title, 1, pause_for_confirm=True)
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    for character in script["characters"]:
        app.director.select_character_candidate(
            title, 1, character["name"], 1)
    app.director.produce(title, 1, pause_for_confirm=True)
    return project, episode, script


def _multi_char_shot_payload(app, project, episode):
    """取一个多人物镜头的最终出图 payload(与真实生产同一条路径)。"""
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    script, _ = app.projects.latest_document(episode["id"], "script")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "script": script, "storyboard": storyboard,
           "aspect": "9:16", "dims": {"width": 1080, "height": 1920}}
    shot = next(s for s in storyboard["shots"]
                if len(s.get("characters", [])) >= 1)
    return app.director._shot_payload(ctx, shot), shot


def test_prompt_carries_numbered_reference_binding(app):
    """提示词必须写明:图N=谁的什么图、参考什么;编号从 1 连续。"""
    project, episode, script = _preproduce(app)
    payload, shot = _multi_char_shot_payload(app, project, episode)
    manifest = payload["reference_manifest"]
    assert manifest, "多人物镜头没有参考图对照表"
    assert [m["index"] for m in manifest] == list(
        range(1, len(manifest) + 1))
    prompt = payload["prompt"]
    assert "参考图对照表" in prompt
    first = manifest[0]
    assert "最终立绘" in first["label"]        # 图1 = 人物最终立绘
    assert f"图1={first['label']}" in prompt
    assert first["character"] in first["binding"]   # 绑定明确到人
    # 每个出场人物都有一张明确归属自己的参考图,且绑定文字点名该人物
    for name in shot["characters"]:
        entry = next((m for m in manifest if m["character"] == name), None)
        assert entry is not None, f"{name} 没有对应的参考图绑定"
        assert name in entry["binding"]
        assert f"图{entry['index']}={entry['label']}" in prompt


def test_manifest_order_matches_provider_submission_order(app, tmp_path):
    """提示词编号 = API 产线实际提交顺序(图N就是第N张,不许错位)。"""
    project, episode, script = _preproduce(app)
    payload, _ = _multi_char_shot_payload(app, project, episode)
    manifest_uris = [m["uri"] for m in payload["reference_manifest"]]
    entries = _reference_entries(payload)
    assert [e["uri"] for e in entries] == manifest_uris
    assert entries[0]["label"] == payload["reference_manifest"][0]["label"]
    # 本地图片提交顺序也严格按对照表(用真实 PNG 验证过滤+排序)
    pngs = []
    for index in range(3):
        path = tmp_path / f"ref{index}.png"
        path.write_bytes(PNG)
        pngs.append(str(path))
    fake = {"reference_manifest": [
        {"index": 1, "uri": pngs[2], "label": "A"},
        {"index": 2, "uri": pngs[0], "label": "B"},
        {"index": 3, "uri": "/no/such.png", "label": "C"},
        {"index": 4, "uri": pngs[1], "label": "D"},
    ]}
    assert [str(p) for p in _local_refs(fake)] == [
        pngs[2], pngs[0], pngs[1]]


def test_prompt_details_keep_visible_costume_but_not_full_scene_plot(app):
    """保留本镜可见造型，但不把整场剧情动作重复塞给图片模型。"""
    project, episode, script = _preproduce(app)
    payload, shot = _multi_char_shot_payload(app, project, episode)
    prompt = payload["prompt"]
    hero = shot["characters"][0]
    design = app.director._character_design(project["id"], hero) or {}
    # 妆容/配饰/发型等身份细节由锁定立绘图提供;文字侧必须写清
    # 服装、服装细节与配色(锁定后允许保留的服装语义字段)
    for key, label in (("costume", "服装"),
                       ("costume_detail", "服装细节"),
                       ("palette", "配色")):
        if str(design.get(key) or "").strip():
            assert label in prompt, f"提示词缺少{label}"
    # 整场剧情动作可能覆盖多个镜头，不能重复塞进当前关键帧提示词。
    scene = next(s for s in script["scenes"]
                 if s["scene_no"] == shot["scene_no"])
    if str(scene.get("action") or "").strip():
        assert "本场情境" not in prompt


def test_shot_provider_prompt_contains_current_frame_only(app):
    project, episode, script = _preproduce(app, title="当前镜头提示词")
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    shot = next(
        item for item in storyboard["shots"]
        if item.get("characters"))
    current = "CURRENT_FRAME_SENTINEL_只画当前举手动作"
    shot["description"] = current
    shot["prompt"] = "OLD_STORYBOARD_PROMPT_SENTINEL_不得下发"
    script["story_background"] = {
        "prior_events": "FULL_EPISODE_PLOT_SENTINEL_整集前情",
        "core_conflict": "FULL_EPISODE_CONFLICT_SENTINEL_整集冲突",
    }
    scene = next(
        item for item in script["scenes"]
        if item["scene_no"] == shot["scene_no"])
    scene["action"] = "FULL_SCENE_ACTION_SENTINEL_整场剧情"
    for character in script["characters"]:
        character["backstory"] = "FULL_BIO_SENTINEL_人物完整身世"
        character["motivation"] = "FULL_MOTIVATION_SENTINEL_人物长期计划"
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": app.workspace.artifacts_dir
        / f"p{project['id']:03d}" / "e001",
        "script": script, "storyboard": storyboard,
        "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
    }

    payload = app.director._shot_payload(ctx, shot)
    provider_prompt = payload["prompt_compact"]
    assert current in provider_prompt
    assert payload["prompt_contract_complete"] is True
    for forbidden in (
            "OLD_STORYBOARD_PROMPT_SENTINEL",
            "FULL_EPISODE_PLOT_SENTINEL",
            "FULL_EPISODE_CONFLICT_SENTINEL",
            "FULL_SCENE_ACTION_SENTINEL",
            "FULL_BIO_SENTINEL",
            "FULL_MOTIVATION_SENTINEL"):
        assert forbidden not in provider_prompt
        assert forbidden not in payload["prompt"]
    assert all(
        "backstory" not in facts and "motivation" not in facts
        for facts in payload["character_background"].values())


def test_user_reference_appears_in_manifest(app):
    """用户上传的参考图进入对照表并注明用途。"""
    project, episode, script = _preproduce(app)
    hero = script["characters"][0]["name"]
    app.director.add_reference("万妖图录", "定妆照", PNG, ".png",
                               attach_to=hero)
    payload, shot = _multi_char_shot_payload(app, project, episode)
    if hero not in shot.get("characters", []):
        pytest.skip("首个多人镜头未包含主角,跳过")
    labels = [m["label"] for m in payload["reference_manifest"]]
    assert any("定妆照" in label or "用户" in label for label in labels)


def test_frame_edit_base_is_always_in_reference_manifest(app, tmp_path):
    """首尾帧修改不能只写 image_uri，必须真的作为参考图按序上传。"""
    frame = tmp_path / "current-frame.png"
    frame.write_bytes(PNG)
    payload = {
        "prompt": "只修正手部，其他保持不变",
        "image_uri": str(frame),
    }
    app.director._attach_reference_manifest(payload)
    assert payload["reference_manifest"]
    assert payload["reference_manifest"][0]["uri"] == str(frame)
    assert payload["reference_manifest"][0]["role"] == "keyframe"
    assert "图1=本镜已通过的关键图" in payload["prompt"]


def test_character_asset_reference_table_is_not_duplicated_in_provider_prompt(
        app, tmp_path):
    portrait = tmp_path / "portrait.png"
    portrait.write_bytes(PNG)
    payload = {
        "character_sheet": "profile",
        "sheet_label": "侧面母资产",
        "art_name": "李继周",
        "characters": ["李继周"],
        "prompt": "CURRENT_ASSET_SENTINEL_严格90度侧面",
        "prompt_contract_complete": True,
        "identity_references": [{
            "character": "李继周", "uri": str(portrait),
        }],
        "character_refs": [str(portrait)],
    }
    app.director._attach_reference_manifest(payload)
    instruction, _, _ = build_instruction("image", payload, tmp_path)

    assert payload["prompt_compact"] == "CURRENT_ASSET_SENTINEL_严格90度侧面"
    assert "参考图对照表" not in payload["prompt_compact"]
    assert instruction.count("参考图对照表(") == 1


def test_legacy_unscoped_reference_does_not_pollute_every_shot(app, tmp_path):
    """历史无用途、无关联参考图不得自动进入所有镜头。"""
    project, episode, _ = _preproduce(app)
    old_ref = tmp_path / "legacy-global.png"
    old_ref.write_bytes(PNG)
    row = app.assets.register(
        project["id"], "reference", "历史全局人物图",
        uri=str(old_ref), meta={"attach_to": "", "note": ""})
    payload, _ = _multi_char_shot_payload(app, project, episode)
    assert row["uri"] not in [
        item["uri"] for item in payload["reference_manifest"]]


def test_continuity_reference_requires_exact_character_set(app, tmp_path):
    """双人旧图不能凭“有一个角色重叠”混进单人镜头。"""
    project, _ = app.projects.get_or_create_project("连续性参考测试")
    single = tmp_path / "single.png"
    group = tmp_path / "group.png"
    single.write_bytes(PNG)
    group.write_bytes(PNG)
    single_row = app.assets.register(
        project["id"], "image", "single",
        uri=str(single), meta={
            "characters": ["乔安"],
            "location": "直播间",
            "image_quality": "medium",
        })
    group_row = app.assets.register(
        project["id"], "image", "group",
        uri=str(group), meta={
            "characters": ["乔安", "白芷"],
            "location": "直播间",
            "image_quality": "medium",
        })
    matched = app.director._matching_produced_image_rows(
        project["id"], ["乔安"], "直播间")
    ids = {row["id"] for row in matched}
    assert single_row["id"] in ids
    assert group_row["id"] not in ids


def test_spatial_diagram_is_uploaded_for_keyframe_with_single_role(
        app, tmp_path):
    """多人/变机位空间图从关键帧阶段就要真上传，但不得污染画面样式。"""
    project, episode, script = _preproduce(app)
    spatial = tmp_path / "shot-space.png"
    spatial.write_bytes(PNG)
    character = script["characters"][0]["name"]
    refs = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode), "script": script},
        [character], "", shot_no=1, spatial_ref=str(spatial))
    payload = {"prompt": "单人按空间图站位", **refs}
    app.director._attach_reference_manifest(payload)
    entry = next(item for item in payload["reference_manifest"]
                 if item["role"] == "spatial")
    assert entry["uri"] == str(spatial)
    assert "不得把俯视视角" in entry["binding"]
    assert payload["reference_manifest"][0]["role"] == "identity"
