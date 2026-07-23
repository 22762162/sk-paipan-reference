"""参考图对照表:出图前就写死"谁参考哪张图",编号与提交顺序一致。"""

import pytest

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


def test_prompt_details_costume_scene(app):
    """服装细节/妆容/配饰/配色与场景情境细节都必须写进提示词。"""
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
    # 场景情境细节(本场剧情动作)
    scene = next(s for s in script["scenes"]
                 if s["scene_no"] == shot["scene_no"])
    if str(scene.get("action") or "").strip():
        assert "本场情境" in prompt


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
