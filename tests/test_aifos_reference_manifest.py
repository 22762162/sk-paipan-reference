"""参考图对照表:出图前就写死"谁参考哪张图",编号与提交顺序一致。"""

import json

import pytest

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.errors import AifosError
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
    wardrobe_entries = [
        item for item in manifest if item.get("role") == "wardrobe"]
    combined_entries = [
        item for item in manifest
        if "兼当前服装参考" in item.get("label", "")]
    identity_entries = [
        item for item in manifest if item.get("role") == "identity"]
    assert not combined_entries, "身份图不得再兼任服装参考"
    assert identity_entries
    assert all("identity" in item["inherits"] for item in identity_entries)
    assert all(
        {"wardrobe", "props", "prop_position"} <= set(item["excludes"])
        for item in identity_entries)
    assert all("不得用此图覆盖脸" in item["binding"]
               for item in wardrobe_entries)
    assert all("wardrobe" in item["inherits"]
               and "face" in item["excludes"]
               for item in wardrobe_entries)


def test_shot_payload_preserves_functional_population_without_identity_refs(
        app):
    project, episode, _ = _preproduce(app, title="功能人物外围链")
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    script, _ = app.projects.latest_document(episode["id"], "script")
    shot = next(item for item in storyboard["shots"]
                if item.get("characters"))
    shot["functional_figures"] = [{
        "name": "蒙面杀手", "count": 2, "function": "封锁出口",
    }]
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": app.workspace.artifacts_dir
        / f"p{project['id']:03d}" / "e001",
        "script": script, "storyboard": storyboard,
        "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
    }

    payload = app.director._shot_payload(ctx, shot)

    assert payload["functional_figures"] == shot["functional_figures"]
    assert payload["visible_figure_count"] == len(shot["characters"]) + 2
    assert "蒙面杀手" not in payload["identity_characters"]
    assert payload["composition_contract"][
        "expected_visible_figure_count"] == payload["visible_figure_count"]


def test_joint_frames_keep_phase_prompts_and_drop_cross_phase_wardrobe_ref(
        app, tmp_path):
    """换装镜头的首尾合同与视觉参考必须按边界隔离。"""
    project, episode, _ = _preproduce(app, title="首尾换装隔离")
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    script, _ = app.projects.latest_document(episode["id"], "script")
    shot = next(item for item in storyboard["shots"]
                if item.get("characters"))
    hero = shot["characters"][0]
    shot["start_state"] = {hero: {
        "position": "床中央仰躺", "wardrobe": "象牙白衬衫，未穿鞋",
        "hair_makeup": "长发散在枕面", "emotion": "闭眼梦魇",
    }}
    shot["end_state"] = {hero: {
        "position": "走廊远端", "wardrobe": "浅卡其风衣、米色平底鞋",
        "hair_makeup": "长发随步伐轻动", "emotion": "急迫离开",
    }}
    shot["frame_targets"] = {
        "keyframe": {
            "phase": "end", "state": f"{hero}穿风衣走向走廊远端",
            "characters": [hero], "visible_figure_count": 1,
            "fallback": False,
        },
        "first_frame": {
            "phase": "start", "state": f"{hero}在床中央仰躺",
            "characters": [hero], "visible_figure_count": 1,
            "fallback": False,
        },
        "last_frame": {
            "phase": "end", "state": f"{hero}穿风衣走向走廊远端",
            "characters": [hero], "visible_figure_count": 1,
            "fallback": False,
        },
    }
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": app.workspace.artifacts_dir
        / f"p{project['id']:03d}" / "e001",
        "script": script, "storyboard": storyboard,
        "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
    }

    payload = app.director._shot_payload(ctx, shot, frame_kind="frames")
    first = payload["frame_prompt_compacts"]["first_frame"]
    last = payload["frame_prompt_compacts"]["last_frame"]

    assert "象牙白衬衫" in first and "浅卡其风衣" not in first
    assert "浅卡其风衣" in last and "象牙白衬衫" not in last
    assert not any(
        item.get("role") == "wardrobe" and item.get("character") == hero
        for item in payload["reference_manifest"])

    keyframe = tmp_path / "authored-end.png"
    keyframe.write_bytes(PNG)
    row = app.assets.register(
        project["id"], "image", "authored-end", uri=str(keyframe),
        meta={"image_quality": "high"})
    assert app.director._bind_keyframe_for_frames(payload, shot, row) == "end"
    first_uris = {
        item["uri"] for item in
        payload["frame_reference_manifests"]["first_frame"]}
    last_uris = {
        item["uri"] for item in
        payload["frame_reference_manifests"]["last_frame"]}
    assert str(keyframe) not in first_uris
    assert str(keyframe) in last_uris


def test_later_qc_prefers_immutable_generation_snapshot(app):
    snapshot = {
        "schema": "aifos.image-generation-input/v1",
        "scope": {"item_id": "shot:4", "shot_no": 4,
                  "frame_kind": "keyframe"},
        "prompt": "镜头4冻结提示词",
        "prompt_hash": "prompt-hash",
        "reference_manifest": [
            {"index": 1, "uri": "/frozen-a.png", "label": "人物A",
             "role": "identity", "character": "A", "binding": "只锁A"},
            {"index": 2, "uri": "/frozen-space.png", "label": "空间图",
             "role": "spatial", "character": "", "binding": "只锁站位"},
        ],
        "reference_hash": "reference-hash",
        "input_hash": "immutable-input-hash",
    }
    item = {
        "id": "shot:4", "shot_no": 4, "category": "shot_image",
        "generation_input": snapshot,
        "prompt_used": "会造成漂移的旧提示词",
        "reference_inputs": {"items": [
            {"uri": "/wrong-order.png", "label": "错误重建"}]},
    }
    recovered = app.director._plan_generation_input(
        item, {"prompt": "当前重建提示词"}, {})

    assert recovered["input_hash"] == "immutable-input-hash"
    assert recovered["prompt"] == "镜头4冻结提示词"
    assert [row["uri"] for row in recovered["reference_manifest"]] == [
        "/frozen-a.png", "/frozen-space.png"]


def test_legacy_qc_recovers_provider_reference_order(app):
    payload = {
        "shot_no": 8,
        "prompt": "镜头8",
        "identity_references": [{
            "character": "甲", "uri": "/identity.png", "asset_id": 1,
        }],
        "spatial_ref": "/space.png",
        "character_refs": ["/wardrobe.png"],
        "asset_matches": [{
            "uri": "/wardrobe.png", "name": "甲:costume",
            "label": "甲服装", "reference_role": "wardrobe",
            "attach_to": "甲",
        }],
    }
    # v1 mobile display order was identity→character→spatial.
    legacy_item = {
        "id": "shot:8", "shot_no": 8, "category": "shot_image",
        "prompt_used": "镜头8冻结提示词",
        "reference_inputs": {"items": [
            {"kind": "identity", "uri": "/identity.png",
             "label": "甲最终立绘", "attach_to": "甲"},
            {"kind": "character", "uri": "/wardrobe.png",
             "label": "甲服装", "attach_to": "甲"},
            {"kind": "spatial", "uri": "/space.png",
             "label": "空间调度图"},
        ]},
    }
    recovered = app.director._plan_generation_input(
        legacy_item, payload, {})

    assert [row["uri"] for row in recovered["reference_manifest"]] == [
        "/identity.png", "/space.png", "/wardrobe.png"]
    assert recovered["reference_manifest"][1]["role"] == "spatial"
    assert "只读取人物编号" in recovered["reference_manifest"][1]["binding"]


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
    current = payload["character_background"][hero].get("costume")
    assert current and current in prompt
    assert "服装:" in prompt
    # 人物总设定可能包含后续场次的服装细节/配色；当前镜头只允许当前
    # wardrobe_state，不能把多套造型重新拍扁进提示词。
    for key in ("costume_detail", "palette"):
        value = str(design.get(key) or "").strip()
        if value and value != current:
            assert value not in prompt
    # 整场剧情动作可能覆盖多个镜头，不能重复塞进当前关键帧提示词。
    scene = next(s for s in script["scenes"]
                 if s["scene_no"] == shot["scene_no"])
    if str(scene.get("action") or "").strip():
        assert "本场情境" not in prompt


def test_selected_later_outfit_cannot_pollute_current_shot(app, monkeypatch):
    monkeypatch.setattr(
        app.director, "_character_design",
        lambda _project_id, _name: {
            "species": "人类", "gender": "男",
            "costume": "青官袍乌纱", "temperament": "谨慎",
        })
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "脱官袍披旧月白直裰，去乌纱仅留网巾",
            "temperament": "夜探书房相",
        })
    shot = {
        "characters": ["沈砚"],
        "start_state": {"沈砚": {
            "wardrobe": "青官袍乌纱", "headwear": "乌纱帽",
            "emotion": "镇定内紧", "prop": "鱼符藏左袖",
        }},
        "end_state": {"沈砚": {
            "wardrobe": "青官袍乌纱", "headwear": "乌纱帽",
            "emotion": "镇定内紧", "prop": "鱼符藏左袖",
        }},
    }
    ctx = {
        "project": {"id": 1},
        "script": {"characters": [{"name": "沈砚", "occupation": "知县"}]},
    }

    facts = app.director._shot_character_facts(ctx, shot)["沈砚"]
    assert facts["costume"] == "青官袍乌纱"
    assert facts["accessories"] == "乌纱帽"
    assert facts["temperament"] == "镇定内紧"
    assert "旧月白直裰" not in json.dumps(facts, ensure_ascii=False)
    assert "夜探书房相" not in json.dumps(facts, ensure_ascii=False)


def test_static_frame_character_facts_follow_selected_phase(app, monkeypatch):
    """First-frame subject styling must not inherit the tail appearance."""
    monkeypatch.setattr(
        app.director, "_character_design",
        lambda _project_id, _name: {
            "species": "人类", "gender": "女", "costume": "身份图旧服装",
        })
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {})
    shot = {
        "characters": ["虞寻歌"],
        "start_state": {"虞寻歌": {
            "wardrobe": "象牙白衬衫、深灰西裤，未穿风衣与鞋",
            "hair_makeup": "长卷发散在枕面",
            "emotion": "惊惧梦魇",
        }},
        "end_state": {"虞寻歌": {
            "wardrobe": "浅卡其风衣、米色平底鞋",
            "hair_makeup": "长卷发随步伐轻动",
            "emotion": "表面镇定、步伐急迫",
        }},
    }
    ctx = {"project": {"id": 1}, "script": {"characters": []}}

    first = app.director._shot_character_facts(
        ctx, {**shot, "frame_target": {"phase": "start"}})["虞寻歌"]
    last = app.director._shot_character_facts(
        ctx, {**shot, "frame_target": {"phase": "end"}})["虞寻歌"]

    assert first["costume"] == "象牙白衬衫、深灰西裤，未穿风衣与鞋"
    assert first["hair"] == "长卷发散在枕面"
    assert first["temperament"] == "惊惧梦魇"
    assert "浅卡其风衣" not in first["costume"]
    assert "米色平底鞋" not in first["costume"]
    assert last["costume"] == "浅卡其风衣、米色平底鞋"
    assert last["hair"] == "长卷发随步伐轻动"
    assert last["temperament"] == "表面镇定、步伐急迫"


def test_conflicting_full_body_identity_uses_face_only_anchor(
        app, monkeypatch):
    project, episode, script = _preproduce(
        app, title="身份服装职责隔离")
    name = script["characters"][0]["name"]
    locked = app.director._locked_identity(project["id"], name)
    closeup = app.assets.latest(
        project["id"], "character_sheet", f"{name}:closeup")
    assert locked is not None and closeup is not None
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "脱去青官袍、穿旧月白直裰，摘乌纱仅留网巾",
        })

    refs = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode)},
        [name], "", wardrobe_states={name: "青官袍乌纱"})

    identity = refs["identity_references"][0]
    assert identity["identity_anchor_type"] == "face_only_derived"
    assert identity["uri"] == closeup["uri"]
    assert identity["source_identity_uri"] == locked["uri"]
    assert locked["uri"] not in refs["character_refs"]
    manifest = app.director._reference_manifest(refs)
    identity_entry = next(
        item for item in manifest if item["role"] == "identity")
    assert "最终立绘面部锚" in identity_entry["label"]
    assert "不得推断或继承图外服装" in identity_entry["binding"]
    assert "服装只服从本镜服装参考" in identity_entry["binding"]


def test_face_only_anchor_keeps_matching_signature_hair_ornament(
        app, monkeypatch):
    project, episode, script = _preproduce(
        app, title="发饰形制锚点")
    name = script["characters"][0]["name"]
    hairpin = {
        "presence": "worn", "kind": "hair_ornament",
        "name": "横向断纹旧银簪",
        "shape": "横穿发髻的长直簪杆",
        "material": "氧化旧银",
        "color": "暗旧银色",
        "placement": "横向穿过中高发髻",
        "signature_details": "右端大号镂刻叶形端头、左端小尖饰",
        "forbidden_variants": ["短发夹", "垂坠金钗"],
    }
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "另一场礼服",
            "accessories": hairpin,
        })

    refs = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode)},
        [name], "", wardrobe_states={name: "当前场常服"},
        headwear_states={name: hairpin})

    identity = refs["identity_references"][0]
    assert identity["identity_anchor_type"] == "face_only_derived"
    assert identity["headwear_anchor"] == hairpin
    manifest = app.director._reference_manifest(refs)
    entry = next(item for item in manifest if item["role"] == "identity")
    assert "横向断纹旧银簪" in entry["binding"]
    assert "不得简化、换款、改色或移位" in entry["binding"]
    assert "横穿发髻的长直簪杆" in entry["binding"]
    assert "右端大号镂刻叶形端头" in entry["binding"]
    assert "短发夹、垂坠金钗" in entry["binding"]
    assert "headwear_signature_details" in entry["inherits"]

    # A wardrobe-only face crop may use the exact dedicated closeup already
    # attached in its identity slot.  When tolerant text compatibility misses
    # an inserted material adjective, scoped visual proof on the source locked
    # portrait still establishes the ordinary front-view headwear.
    soft_hat = {
        "presence": "worn", "kind": "soft_hat",
        "name": "低矮软角吏巾",
    }
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "另一场礼服，戴低矮软角吏巾",
            "hair": "灰发束入低矮软角布质吏巾",
        })
    assert not app.director._headwear_states_compatible(
        soft_hat, app.director._look_headwear_text(
            app.director._locked_look_variant(project["id"], name)))
    locked = app.director._locked_identity(project["id"], name)
    locked_meta = app.assets.meta(locked)
    app.assets.register(
        project["id"], locked["kind"], locked["name"], uri=locked["uri"],
        meta={
            **locked_meta,
            "selection_qc_passed": True,
            "selection_qc": {
                "passed": True,
                "identity_checked": True,
                "identity_match": True,
                "identity_checks": [{
                    "character": name,
                    "basis": ["低矮软角吏巾清晰可见"],
                    "checked": True,
                    "match": True,
                }],
                "image_error": {"categories": [], "evidence": []},
            },
        },
        new_version=False)

    scoped = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode)},
        [name], "", wardrobe_states={name: "当前场常服"},
        headwear_states={name: soft_hat})

    scoped_identity = scoped["identity_references"][0]
    assert scoped_identity["identity_anchor_type"] == "face_only_derived"
    assert scoped_identity["headwear_anchor_verified"]["attachment"] == (
        "dedicated_face_closeup")

    original_latest = app.assets.latest

    def latest_without_closeup(project_id, kind, asset_name, *args, **kwargs):
        if kind == "character_sheet" and asset_name == f"{name}:closeup":
            return None
        return original_latest(project_id, kind, asset_name, *args, **kwargs)

    # Even a legacy source without scoped visual QC must not let a features or
    # makeup sheet masquerade as the missing dedicated headwear closeup.
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "另一场礼服",
            "accessories": soft_hat,
        })
    app.assets.register(
        project["id"], locked["kind"], locked["name"], uri=locked["uri"],
        meta={
            **app.assets.meta(locked),
            "selection_qc_passed": None,
            "selection_qc": {},
        },
        new_version=False)
    monkeypatch.setattr(app.assets, "latest", latest_without_closeup)
    with pytest.raises(AifosError, match="不是有效的专用closeup结构图"):
        app.director._art_refs(
            {"project": dict(project), "episode": dict(episode)},
            [name], "", wardrobe_states={name: "当前场常服"},
            headwear_states={name: soft_hat})


def test_rear_headwear_contract_adds_scoped_back_structure_anchor(
        app, monkeypatch):
    project, episode, script = _preproduce(
        app, title="后脑发饰结构锚")
    name = script["characters"][0]["name"]
    back = app.assets.latest(
        project["id"], "character_sheet", f"{name}:back")
    closeup = app.assets.latest(
        project["id"], "character_sheet", f"{name}:closeup")
    profile = app.assets.latest(
        project["id"], "character_sheet", f"{name}:profile")
    assert back is not None and closeup is not None and profile is not None
    headwear = {
        "presence": "worn", "kind": "soft_hat", "name": "素黑网巾",
        "shape": "网巾贴合头顶，后脑圆形开口露出低髻",
        "material": "细密黑色网纱，棕黑布边",
        "color": "烟墨黑",
        "placement": "前缘沿发际线，低髻从后脑圆孔露出",
        "signature_details": "后脑交叉束带，低髻中央短横固定簪",
        "forbidden_variants": ["完全包住低髻", "顶髻突出在网巾上方"],
    }
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "深靛圆领袍", "accessories": headwear,
        })
    original_conflict_check = (
        app.director._locked_identity_has_headwear_qc_conflict)
    monkeypatch.setattr(
        app.director, "_locked_identity_has_headwear_qc_conflict",
        lambda _project_id, _name: True)

    refs = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode)},
        [name], "", wardrobe_states={name: "深靛圆领袍"},
        headwear_states={name: headwear},
        sheet_keys_by_character={name: ("profile",)})

    assert len(refs["headwear_references"]) == 1
    assert refs["headwear_references"][0]["uri"] == back["uri"]
    assert refs["identity_references"][0]["uri"] == closeup["uri"]
    assert refs["identity_references"][0]["headwear_anchor"] == headwear
    assert profile["uri"] not in refs["character_refs"]
    assert not [
        item for item in refs["asset_matches"]
        if item.get("uri") == profile["uri"]]
    manifest = app.director._reference_manifest(refs)
    headwear_entry = next(
        item for item in manifest if item["role"] == "headwear")
    identity_entry = next(
        item for item in manifest if item["role"] == "identity")
    assert headwear_entry["uri"] == back["uri"]
    assert headwear_entry["index"] == identity_entry["index"] + 1
    assert "正面身份图看不见或与本图冲突" in headwear_entry["binding"]
    assert "后脑圆形开口露出低髻" in headwear_entry["binding"]
    assert "顶髻突出在网巾上方" in headwear_entry["binding"]
    assert "headwear_signature_details" in headwear_entry["inherits"]
    assert "face_identity_override" in headwear_entry["excludes"]

    # When immutable hard anchors fill the attachment budget, only an ordinary
    # closeup may defer to an already matching identity/headwear anchor.
    # Rear/profile morphology must remain fail-closed.
    monkeypatch.setattr(
        "aifos.director.SHOT_BASE_REFERENCE_LIMIT", 1)
    monkeypatch.setattr(
        app.director, "_director_autonomy_enabled", lambda: True)
    ordinary_headwear = {
        "presence": "worn", "kind": "official_hat",
        "name": "黑纱官帽与网巾", "shape": "标准正面官帽轮廓",
        "material": "黑纱与布边", "color": "烟墨黑",
        "placement": "贴合发际线佩戴",
        "forbidden_variants": ["无帽", "金冠"],
    }
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "深靛圆领袍", "accessories": ordinary_headwear,
        })
    monkeypatch.setattr(
        app.director, "_locked_identity_has_headwear_qc_conflict",
        original_conflict_check)
    locked_identity = app.director._locked_identity(project["id"], name)
    locked_meta = app.assets.meta(locked_identity)
    app.assets.register(
        project["id"], locked_identity["kind"], locked_identity["name"],
        uri=locked_identity["uri"],
        meta={
            **locked_meta,
            # The portrait has an unrelated belt defect, but its scoped
            # identity/headwear evidence is still independently usable.
            "selection_qc_passed": False,
            "selection_qc": {
                "passed": False,
                "visual_pass": False,
                "image_passed": False,
                "critical_failures": ["腰带金属扣样式不符合设定"],
                "identity_checked": True,
                "identity_match": True,
                "wardrobe_checked": True,
                "wardrobe_match": False,
                "identity_checks": [{
                    "character": name,
                    "view": "front_or_three_quarter",
                    "basis": ["正面身份、年龄与脸型匹配"],
                    "checked": True,
                    "match": True,
                }],
                "image_error": {
                    "categories": ["wardrobe"],
                    "evidence": ["黑纱官帽与网巾清晰可见"],
                },
            },
        },
        new_version=False)
    assert original_conflict_check(project["id"], name) is False
    constrained = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode)},
        [name], "", shot_no=11,
        wardrobe_states={name: "深靛圆领袍"},
        headwear_states={name: ordinary_headwear})
    assert constrained["identity_references"][0][
        "headwear_anchor"] == ordinary_headwear
    assert constrained["identity_references"][0][
        "headwear_anchor_verified"]["source"] == (
            "locked_identity_visual_qc")
    assert constrained["identity_references"][0][
        "headwear_anchor_verified"]["source_qc_passed"] is False
    assert constrained["headwear_references"] == []
    assert any(
        item.get("character") == name and item.get("sheet") == "closeup"
        for item in constrained["appearance_reference_warnings"])

    identity = constrained["identity_references"][0]
    negative_evidence = (
        "黑纱官帽未佩戴",
        "黑纱官帽与设定不符",
        "应为黑纱官帽，实际无帽",
        "佩戴朱红帽而非黑纱官帽",
        "应佩戴黑纱官帽",
    )
    for evidence in negative_evidence:
        negative_qc = {
            **app.assets.meta(locked_identity),
            "selection_qc": {
                "passed": True,
                "identity_checked": True,
                "identity_match": True,
                "identity_checks": [{
                    "character": name,
                    "basis": [evidence],
                    "checked": True,
                    "match": True,
                }],
                "image_error": {"categories": [], "evidence": []},
            },
        }
        app.assets.register(
            project["id"], locked_identity["kind"],
            locked_identity["name"], uri=locked_identity["uri"],
            meta=negative_qc, new_version=False)
        assert app.director._locked_identity_headwear_verification(
            project["id"], name, identity, ordinary_headwear) is None

    # Structured belt evidence must ignore the synthesized umbrella issue,
    # but a concrete contradictory hat issue must still fail closed.
    monkeypatch.setattr(
        app.director, "_locked_identity_has_headwear_qc_conflict",
        original_conflict_check)
    scoped_qc = {
        "passed": False,
        "identity_checked": True,
        "identity_match": True,
        "identity_checks": [{
            "character": name,
            "basis": ["黑纱官帽清晰可见"],
            "checked": True,
            "match": True,
        }],
        "image_error": {
            "categories": ["wardrobe"],
            "evidence": ["腰带金属扣错误"],
        },
        "issues": ["画面服装、头饰或妆发与本镜继承状态不一致"],
    }
    scoped_meta = {
        **app.assets.meta(locked_identity),
        "selection_qc_passed": False,
        "selection_qc": scoped_qc,
    }
    app.assets.register(
        project["id"], locked_identity["kind"], locked_identity["name"],
        uri=locked_identity["uri"], meta=scoped_meta,
        new_version=False)
    assert original_conflict_check(project["id"], name) is False
    assert app.director._locked_identity_headwear_verification(
        project["id"], name, identity, ordinary_headwear)
    for issue in (
            "黑纱官帽形制错误",
            "黑纱官帽与设定不一致",
            "画面服装、头饰或妆发与本镜继承状态不一致，且黑纱官帽戴反"):
        scoped_qc["issues"] = [issue]
        app.assets.register(
            project["id"], locked_identity["kind"],
            locked_identity["name"], uri=locked_identity["uri"],
            meta=scoped_meta, new_version=False)
        assert original_conflict_check(project["id"], name) is True
        assert app.director._locked_identity_headwear_verification(
            project["id"], name, identity, ordinary_headwear) is None

    monkeypatch.setattr(
        app.director, "_director_autonomy_enabled", lambda: False)
    with pytest.raises(AifosError, match="closeup发饰结构"):
        app.director._art_refs(
            {"project": dict(project), "episode": dict(episode)},
            [name], "", shot_no=11,
            wardrobe_states={name: "深靛圆领袍"},
            headwear_states={name: ordinary_headwear})

    monkeypatch.setattr(
        app.director, "_director_autonomy_enabled", lambda: True)
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "深靛圆领袍", "accessories": headwear,
        })
    original_latest = app.assets.latest

    def latest_without_back(project_id, kind, asset_name):
        if kind == "character_sheet" and asset_name == f"{name}:back":
            return None
        return original_latest(project_id, kind, asset_name)

    monkeypatch.setattr(app.assets, "latest", latest_without_back)
    with pytest.raises(AifosError, match="closeup发饰结构"):
        app.director._art_refs(
            {"project": dict(project), "episode": dict(episode)},
            [name], "", shot_no=11,
            wardrobe_states={name: "深靛圆领袍"},
            headwear_states={name: headwear})

    monkeypatch.setattr(app.assets, "latest", original_latest)
    mismatched_headwear = {
        **ordinary_headwear, "name": "朱红软角官帽",
    }
    with pytest.raises(AifosError, match="closeup发饰结构"):
        app.director._art_refs(
            {"project": dict(project), "episode": dict(episode)},
            [name], "", shot_no=11,
            wardrobe_states={name: "深靛圆领袍"},
            headwear_states={name: mismatched_headwear})


def test_headwear_conflict_cannot_reenter_through_wardrobe_reference(
        app, monkeypatch):
    project, episode, script = _preproduce(
        app, title="头饰与服装参考职责隔离")
    name = script["characters"][0]["name"]
    closeup = app.assets.latest(
        project["id"], "character_sheet", f"{name}:closeup")
    assert closeup is not None
    monkeypatch.setattr(
        app.director, "_locked_look_variant",
        lambda _project_id, _name: {
            "costume": "深青官袍、乌纱帽、革带",
            "hair": "束发压于乌纱帽下",
        })

    refs = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode)},
        [name], "", wardrobe_states={name: "深青官袍"},
        headwear_states={name: "无"})

    identity = refs["identity_references"][0]
    assert identity["identity_anchor_type"] == "face_only_derived"
    assert identity["uri"] == closeup["uri"]
    assert not [
        item for item in refs["asset_matches"]
        if item.get("kind") == "wardrobe_reference"]
    warning = refs["appearance_reference_warnings"][0]
    assert warning["headwear"] == "无"
    assert "未上传冲突服装图" in warning["reason"]


def test_continuity_reference_must_match_headwear_as_well_as_robe(
        app, tmp_path):
    project, _ = app.projects.get_or_create_project("头饰连续性参考测试")
    with_hat = tmp_path / "official-with-hat.png"
    no_hat = tmp_path / "official-no-hat.png"
    with_hat.write_bytes(PNG)
    no_hat.write_bytes(PNG)
    with_hat_row = app.assets.register(
        project["id"], "image", "with-hat",
        uri=str(with_hat), meta={
            "characters": ["赵德昌"], "location": "县衙公堂",
            "image_quality": "medium",
            "appearance_state": {
                "赵德昌": {"wardrobe": "深青官袍", "headwear": "乌纱帽"},
            },
        })
    no_hat_row = app.assets.register(
        project["id"], "image", "no-hat",
        uri=str(no_hat), meta={
            "characters": ["赵德昌"], "location": "县衙公堂",
            "image_quality": "medium",
            "appearance_state": {
                "赵德昌": {"wardrobe": "深青官袍", "headwear": "无"},
            },
        })

    matched = app.director._matching_produced_image_rows(
        project["id"], ["赵德昌"], "县衙公堂",
        wardrobe_states={"赵德昌": "深青官袍"},
        headwear_states={"赵德昌": "无"})
    ids = {row["id"] for row in matched}
    assert no_hat_row["id"] in ids
    assert with_hat_row["id"] not in ids


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
    assert current not in provider_prompt
    assert payload["prompt_contract"]["frame_target_source"] == \
        "frame_targets.keyframe"
    assert provider_prompt.count("【定格状态】") == 1
    assert all(
        label not in provider_prompt
        for label in ("【起点】", "【单一主动作】", "【终点】"))
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


def test_selected_binding_order_is_the_actual_provider_upload_order(
        app, tmp_path):
    keyframe = tmp_path / "current-keyframe.png"
    scene = tmp_path / "scene-master.png"
    keyframe.write_bytes(PNG)
    scene.write_bytes(PNG)
    payload = {
        "prompt": "保持场景结构并修正当前镜头",
        # keyframe enters the raw pool first but is optional; the scene master
        # enters later and is mandatory, so the selector intentionally moves
        # it to frozen upload position 1.
        "image_uri": str(keyframe),
        "scene_ref": str(scene),
        "location": "酒店套房",
    }

    app.director._attach_reference_manifest(payload)

    manifest = payload["reference_manifest"]
    bindings = payload["reference_selection_decision"]["bindings"]
    assert [row["role"] for row in manifest] == ["scene", "keyframe"]
    assert [row["index"] for row in manifest] == [1, 2]
    assert [row["uri"] for row in manifest] == [
        row["uri"] for row in bindings]
    assert [row["binding"] for row in manifest] == [
        row["instruction"] for row in bindings]
    assert [row["image_index"] for row in bindings] == [1, 2]
    assert [row["uri"] for row in _reference_entries(payload)] == [
        str(scene), str(keyframe)]
    assert "图1=场景「酒店套房」基准图" in payload["prompt"]
    assert "图2=本镜已通过的关键图" in payload["prompt"]


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


def test_rebuilt_manifest_removes_stale_numbered_reference_duties(
        app, tmp_path):
    portrait = tmp_path / "portrait.png"
    spatial = tmp_path / "spatial.png"
    portrait.write_bytes(PNG)
    spatial.write_bytes(PNG)
    payload = {
        "prompt": (
            "冻结当前状态。参考图2只继承空间站位与机位。"
            "参考图4只锁定门窗材质和主光。"),
        "action": "人物停住；参考图2仅继承blocking与occlusion。",
        "camera": "平视中景。参考图4只锁定场景光线。",
        "prompt_contract": {
            "action": "静态定格。图2只负责空间，不负责人物身份。",
        },
        "identity_references": [{
            "character": "李继周", "uri": str(portrait),
        }],
        "character_refs": [str(portrait)],
        "spatial_ref": str(spatial),
    }

    app.director._attach_reference_manifest(payload)

    assert "参考图2只" not in payload["prompt"]
    assert "参考图4只" not in payload["prompt"]
    assert "参考图2" not in payload["action"]
    assert "参考图4" not in payload["camera"]
    assert "图2只负责" not in json.dumps(
        payload.get("prompt_contract") or {}, ensure_ascii=False)
    assert "图1=P01·李继周最终立绘" in payload["prompt"]
    assert "图2=本镜空间调度图" in payload["prompt"]


def test_shot_plan_hash_invalidates_when_reference_manifest_changes(
        app, monkeypatch):
    project, _ = app.projects.get_or_create_project("参考图变更作废旧关键帧")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    shot = {
        "shot_no": 1, "scene_no": 1, "characters": ["沈砚舟"],
        "description": "同一静态镜头",
    }
    state = {"reference_manifest": [{
        "index": 1, "uri": "/old-profile.png",
        "role": "identity_detail", "character": "沈砚舟",
    }]}

    def payload_for_shot(_ctx, _shot, **_kwargs):
        return {
            "prompt_compact": "同一镜头提示词",
            "prompt_contract": {},
            "reference_manifest": state["reference_manifest"],
        }

    monkeypatch.setattr(app.director, "_shot_payload", payload_for_shot)
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": app.workspace.artifacts_dir
        / f"p{project['id']:03d}" / "e001",
        "storyboard": {"shots": [shot]},
    }

    app.director._plan_seed_shots(ctx)
    first = next(
        item for item in app.director._plan_read(ctx)["items"]
        if item["id"] == "shot:1")
    first_hash = first["content_hash"]
    app.director._plan_mark(
        ctx, "shot:1", "done", extra={"output_uri": "/old.png"})

    state["reference_manifest"] = [{
        "index": 1, "uri": "/face-only.png",
        "role": "identity", "character": "沈砚舟",
    }, {
        "index": 2, "uri": "/correct-back.png",
        "role": "headwear", "character": "沈砚舟",
    }]
    app.director._plan_seed_shots(ctx)

    current = next(
        item for item in app.director._plan_read(ctx)["items"]
        if item["id"] == "shot:1")
    assert current["content_hash"] != first_hash
    assert current["status"] == "pending"
    assert current["invalidated_previous_output"] is True
    assert "参考图合同已变化" in current["invalidation_reason"]


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


def test_legacy_continuity_image_cannot_pollute_current_wardrobe(
        app, tmp_path):
    project, _ = app.projects.get_or_create_project("服装连续性参考测试")
    legacy = tmp_path / "legacy-white.png"
    official = tmp_path / "official.png"
    legacy.write_bytes(PNG)
    official.write_bytes(PNG)
    legacy_row = app.assets.register(
        project["id"], "image", "legacy",
        uri=str(legacy), meta={
            "characters": ["沈砚"], "location": "县衙公堂",
            "image_quality": "medium",
        })
    official_row = app.assets.register(
        project["id"], "image", "official",
        uri=str(official), meta={
            "characters": ["沈砚"], "location": "县衙公堂",
            "image_quality": "medium",
            "appearance_state": {
                "沈砚": {"wardrobe": "青官袍乌纱", "headwear": "乌纱"},
            },
        })

    matched = app.director._matching_produced_image_rows(
        project["id"], ["沈砚"], "县衙公堂",
        wardrobe_states={"沈砚": "官袍"})
    ids = {row["id"] for row in matched}
    assert official_row["id"] in ids
    assert legacy_row["id"] not in ids


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
    assert "不得把3D示意视角" in entry["binding"]
    assert payload["reference_manifest"][0]["role"] == "identity"
    identity = payload["reference_manifest"][0]
    assert {"face", "hair_silhouette", "makeup", "stable_makeup"} <= set(
        identity["inherits"])


def test_crowded_keyframe_refs_cannot_evict_scene_or_spatial_anchors(
        app, tmp_path, monkeypatch):
    """The five-image ceiling must drop props, never room/blocking truth."""
    project, episode, script = _preproduce(app, title="场景硬槽位")
    characters = [row["name"] for row in script["characters"][:2]]
    scene = tmp_path / "scene-slice.png"
    spatial = tmp_path / "shot-space.png"
    scene.write_bytes(PNG)
    spatial.write_bytes(PNG)
    monkeypatch.setattr(
        app.director, "_scene_slice_for_shot",
        lambda *_args, **_kwargs: str(scene))

    prop_rows = {}
    for index in range(4):
        uri = tmp_path / f"prop-{index}.png"
        uri.write_bytes(PNG)
        prop_rows[f"道具{index}"] = {
            "id": 9000 + index, "kind": "prop_identity",
            "name": f"道具{index}", "uri": str(uri), "version": 1,
            "meta": "{}",
        }
    monkeypatch.setattr(
        app.director, "_locked_prop",
        lambda _project_id, name: prop_rows.get(name))

    refs = app.director._art_refs(
        {"project": dict(project), "episode": dict(episode),
         "script": script,
         "character_asset_policy": {"generate_sheets": True}},
        characters, "同一卧室", shot_no=1,
        spatial_ref=str(spatial), prop_names=list(prop_rows))

    assert refs["scene_ref"] == str(scene)
    assert refs["spatial_ref"] == str(spatial)
    assert len({item["uri"] for item in refs["asset_matches"]}) == 5
    roles = {item.get("reference_role")
             for item in refs["asset_matches"]}
    assert {"scene", "spatial"} <= roles


def test_codex_five_reference_budget_never_drops_scene_master(
        app, tmp_path):
    """Prompt/UI attachments are frozen before Codex can improvise drops."""
    paths = []
    for name in ("person-a", "person-b", "scene", "space", "prop-a",
                 "prop-b", "detail"):
        uri = tmp_path / f"{name}.png"
        uri.write_bytes(PNG)
        paths.append(uri)
    person_a, person_b, scene, space, prop_a, prop_b, detail = paths
    payload = {
        "prompt": "同一卧室内的双人静态关键帧",
        "characters": ["甲", "乙"],
        "identity_references": [
            {"character": "甲", "uri": str(person_a)},
            {"character": "乙", "uri": str(person_b)},
        ],
        "character_refs": [str(person_a), str(person_b), str(detail)],
        "scene_ref": str(scene),
        "spatial_ref": str(space),
        "prop_refs": [str(prop_a), str(prop_b)],
        "asset_matches": [
            {"uri": str(scene), "label": "统一卧室母版",
             "reference_role": "scene"},
            {"uri": str(space), "label": "本镜空间调度图",
             "reference_role": "spatial"},
            {"uri": str(prop_a), "label": "核心道具A",
             "reference_role": "prop"},
            {"uri": str(prop_b), "label": "核心道具B",
             "reference_role": "prop"},
        ],
    }

    app.director._attach_reference_manifest(payload)

    assert len(payload["reference_manifest"]) == 5
    uris = [row["uri"] for row in payload["reference_manifest"]]
    assert str(scene) in uris
    assert str(space) in uris
    assert str(person_a) in uris and str(person_b) in uris
    assert payload["reference_budget"]["scene_anchor_preserved"] is True
    assert payload["reference_budget"]["dropped_count"] == 2
    assert "共5张" in payload["prompt"]
    assert "图6=" not in payload["prompt"]


def test_three_person_keyframe_reserves_spatial_before_clean_camera_view(
        app, tmp_path):
    """三人镜不能让重复的干净机位图挤掉空间调度硬锚。"""
    paths = {}
    for name in (
            "person-a", "person-b", "person-c", "scene", "space",
            "clean"):
        uri = tmp_path / f"{name}.png"
        uri.write_bytes(PNG)
        paths[name] = str(uri)
    payload = {
        "prompt": "同一值房内三人静态关键帧",
        "characters": ["甲", "乙", "丙"],
        "identity_references": [
            {"character": "甲", "uri": paths["person-a"]},
            {"character": "乙", "uri": paths["person-b"]},
            {"character": "丙", "uri": paths["person-c"]},
        ],
        "character_refs": [
            paths["person-a"], paths["person-b"], paths["person-c"]],
        "scene_ref": paths["scene"],
        "spatial_ref": paths["space"],
        "spatial_scene_clean_ref": paths["clean"],
        "asset_matches": [
            {"uri": paths["scene"], "label": "统一物理母场景",
             "reference_role": "scene", "mandatory": True},
            {"uri": paths["space"], "label": "本镜空间调度图",
             "reference_role": "spatial", "mandatory": True},
            {"uri": paths["clean"], "label": "本镜干净机位空间图",
             "reference_role": "spatial_scene_clean", "mandatory": True},
        ],
    }

    app.director._attach_reference_manifest(payload)

    roles = [row["role"] for row in payload["reference_manifest"]]
    assert roles.count("identity") == 3
    assert {"scene", "spatial"} <= set(roles)
    assert "spatial_scene_clean" not in roles
    assert "spatial_scene_clean_ref" not in payload
    assert payload["reference_budget"]["spatial_anchor_preserved"] is True
    assert payload["reference_budget"][
        "clean_scene_anchor_preserved"] is False


def test_three_person_revision_switches_to_clean_redraw_when_slots_are_full(
        app, tmp_path):
    """三人返修自动丢弃编辑基底，保留三身份、场景和空间硬锚。"""
    paths = {}
    for name in (
            "person-a", "person-b", "person-c", "scene", "space",
            "clean", "revision"):
        uri = tmp_path / f"{name}.png"
        uri.write_bytes(PNG)
        paths[name] = str(uri)
    payload = {
        "prompt": "只修正三人镜头中被质检指出的问题",
        "shot_no": 16,
        "physical_scene_id": "都察院东值房",
        "canonical_scene_reference_uri": paths["scene"],
        "characters": ["甲", "乙", "丙"],
        "identity_references": [
            {"character": "甲", "uri": paths["person-a"]},
            {"character": "乙", "uri": paths["person-b"]},
            {"character": "丙", "uri": paths["person-c"]},
        ],
        "character_refs": [
            paths["person-a"], paths["person-b"], paths["person-c"]],
        "scene_ref": paths["scene"],
        "spatial_ref": paths["space"],
        "spatial_scene_clean_ref": paths["clean"],
        "reference_images": [paths["revision"]],
        "asset_matches": [
            {"uri": paths["scene"], "label": "统一物理母场景",
             "reference_role": "scene", "mandatory": True},
            {"uri": paths["space"], "label": "本镜空间调度图",
             "reference_role": "spatial", "mandatory": True},
            {"uri": paths["clean"], "label": "本镜干净机位空间图",
             "reference_role": "spatial_scene_clean", "mandatory": True},
            {"uri": paths["revision"], "label": "上一轮相对最优失败图（待修改基底）",
             "reference_role": "revision_base",
             "scene_id": "都察院东值房", "shot_no": 16},
        ],
    }

    app.director._attach_reference_manifest(payload)

    roles = [row["role"] for row in payload["reference_manifest"]]
    assert roles.count("identity") == 3
    assert set(roles) == {"identity", "scene", "spatial"}
    assert len(roles) == 5
    assert payload["scene_ref"] == paths["scene"]
    assert "spatial_scene_clean_ref" not in payload
    assert payload["reference_budget"]["spatial_anchor_preserved"] is True
    assert payload["reference_budget"][
        "revision_base_preserved"] is False
    assert payload["reference_budget"]["scene_anchor_preserved"] is True
    assert payload["revision_mode"] == "regenerate_clean"
    assert payload["revision_base_budget_dropped"] is True
    assert paths["revision"] not in payload.get("reference_images", [])
    assert "图6=" not in payload["prompt"]


def test_frame_budget_keeps_room_keyframe_and_adjacent_tail(
        app, tmp_path):
    paths = {}
    for name in ("person-a", "person-b", "scene", "space", "key", "tail"):
        uri = tmp_path / f"{name}.png"
        uri.write_bytes(PNG)
        paths[name] = str(uri)
    payload = {
        "prompt": "同一卧室的连续首尾帧",
        "frame_kind": "frames",
        "characters": ["甲", "乙"],
        "identity_references": [
            {"character": "甲", "uri": paths["person-a"]},
            {"character": "乙", "uri": paths["person-b"]},
        ],
        "character_refs": [paths["person-a"], paths["person-b"]],
        "scene_ref": paths["scene"],
        "spatial_ref": paths["space"],
        "keyframe_reference_uri": paths["key"],
        "image_uri": paths["key"],
        "chain_first_uri": paths["tail"],
        "previous_shot_reference_required": True,
    }

    app.director._attach_reference_manifest(payload)

    uris = {row["uri"] for row in payload["reference_manifest"]}
    assert uris == {
        paths["person-a"], paths["person-b"], paths["scene"],
        paths["key"], paths["tail"],
    }
    assert "spatial_ref" not in payload
    assert payload["reference_budget"]["scene_anchor_preserved"] is True


def test_three_person_frame_drops_adjacent_tail_without_blocking(
        app, tmp_path):
    paths = {}
    for name in ("person-a", "person-b", "person-c", "scene", "key", "tail"):
        uri = tmp_path / f"{name}.png"
        uri.write_bytes(PNG)
        paths[name] = str(uri)
    payload = {
        "prompt": "同一房间的三人连续首尾帧",
        "frame_kind": "frames",
        "characters": ["甲", "乙", "丙"],
        "identity_references": [
            {"character": "甲", "uri": paths["person-a"]},
            {"character": "乙", "uri": paths["person-b"]},
            {"character": "丙", "uri": paths["person-c"]},
        ],
        "character_refs": [
            paths["person-a"], paths["person-b"], paths["person-c"]],
        "scene_ref": paths["scene"],
        "image_uri": paths["key"],
        "chain_first_uri": paths["tail"],
        "previous_shot_reference_required": True,
    }

    app.director._attach_reference_manifest(payload)

    uris = {row["uri"] for row in payload["reference_manifest"]}
    assert uris == {
        paths["person-a"], paths["person-b"], paths["person-c"],
        paths["scene"], paths["key"],
    }
    assert "chain_first_uri" not in payload
    assert payload["previous_shot_reference_required"] is False
    assert payload["continuity_reference_budget_dropped"] is True
    assert payload["reference_budget"]["scene_anchor_preserved"] is True


def test_unvalidated_wide_scene_master_is_never_v360_sliced(
        app, tmp_path, monkeypatch):
    """A 2:1 establishing image must stay whole instead of becoming seams."""
    project, episode, script = _preproduce(
        app, title="非全景母版禁止切片")
    location = "同一卧室"
    wide = tmp_path / "wide-master.png"
    wide.write_bytes(PNG)
    panorama = app.assets.register(
        project["id"], "scene_art",
        app.director._scene_view_asset_name(location, "panorama"),
        uri=str(wide), meta={
            "image_quality": "high",
            "view": "panorama",
            "aspect": "2:1",
            # Legacy scene masters only recorded their 2:1 aspect.  That is
            # not proof of an equirectangular projection and must fail closed.
        })
    sliced = []
    monkeypatch.setattr(
        "aifos.director.slice_for_block",
        lambda *_args, **_kwargs: sliced.append(True) or "bad-slice.png")
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "script": script,
        "blocking": {"shot_index": {"1": {"camera": {}}}},
    }

    assert app.director._scene_slice_for_shot(ctx, location, 1) == ""
    assert sliced == []
    row, label = app.director._scene_view_reference(
        project["id"], location, {}, script=script)
    assert row["id"] == panorama["id"]
    assert "禁止按360全景切片" in label
