"""正式人物统一4选1定版、参考图硬门禁与视觉身份质检。"""

import json
from pathlib import Path

import pytest

from aifos.app import App
from aifos.adapters.codex_image import _ref_line, _style_line
from aifos.director import (
    CHARACTER_CANDIDATE_PROMPT_SCHEMA,
    character_candidate_target,
    character_production_readiness_error,
)
from aifos.adapters.claude_script import validate_script_bible
from aifos.errors import AifosError, ProviderUnavailable
from aifos.production.base import ProviderResult


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _to_cast_selection(app, title="人物定版测试"):
    first = app.director.produce(title, 1, pause_for_confirm=True)
    assert first["status"] == "awaiting_script"
    second = app.director.produce(title, 1, pause_for_confirm=True)
    assert second["status"] == "awaiting_cast"
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    return project, episode, script


def _lock_all(app, title, script, index=3):
    for character in script["characters"]:
        chosen = min(index, character_candidate_target(character))
        app.director.select_character_candidate(
            title, 1, character["name"], chosen)


def test_role_based_candidates_pause_before_downstream_images(app):
    project, episode, script = _to_cast_selection(app)
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    assert status["candidate_target"] == 4
    assert status["locked"] == 0
    assert all(item["candidate_count"] == character_candidate_target(
        next(c for c in script["characters"] if c["name"] == item["character"]))
               for item in status["characters"])
    for character in status["characters"]:
        target = character["candidate_target"]
        assert len({item["variant_id"] for item in character["candidates"]}) == target
        assert len({item["variant_label"] for item in character["candidates"]}) == target
        assert all(item["variant_source"] == "initial_state_same_prompt"
                   for item in character["candidates"])
        assert all(item["candidate_prompt_schema"]
                   == CHARACTER_CANDIDATE_PROMPT_SCHEMA
                   for item in character["candidates"])
        assert all(item["current_candidate_policy"]
                   for item in character["candidates"])
        assert all(set(item["look_variant"]) == {
            "hair", "makeup", "costume", "temperament"}
                   for item in character["candidates"])
        assert len({
            json.dumps(item["look_variant"], ensure_ascii=False,
                       sort_keys=True)
            for item in character["candidates"]
        }) == 1
        metas = [
            app.assets.meta(row)
            for row in app.assets.list(
                project["id"], "character_candidate")
            if app.assets.meta(row).get("character")
            == character["character"]
        ]
        assert len({meta["prompt"] for meta in metas}) == 1
    assert app.projects.latest_document(episode["id"], "storyboard")[0] is None
    assert app.assets.list(project["id"], "character_sheet") == []
    assert app.assets.list(project["id"], "scene_art") == []


def test_aifos_visual_qc_auto_selects_best_candidate(
        app, tmp_path, monkeypatch):
    project, _ = app.projects.get_or_create_project("自动候选选优")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    character = {
        "name": "顾明昭", "role": "主角", "gender": "女",
        "age_range": "27岁", "image_prompt": "晚明女官，单人全身正面",
    }
    script = {
        "characters": [character],
        "core_props": [],
        "scenes": [],
    }
    design = {
        "gender": "女", "age_range": "27岁",
        "costume": "晚明烟墨色窄袖长衫",
        "hair": "乌黑低圆髻", "appearance": "偏方鹅卵脸",
    }
    for index in range(1, 5):
        uri = tmp_path / f"candidate-{index}.png"
        uri.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]) * 32)
        app.assets.register(
            project["id"], "character_candidate",
            f"顾明昭:{index:02d}", uri=str(uri),
            meta={
                "character": "顾明昭", "role": "主角",
                "candidate_index": index,
                "candidate_prompt_schema":
                    CHARACTER_CANDIDATE_PROMPT_SCHEMA,
                "variant_id": f"sample-{index}",
                "variant_label": f"候选{index}",
                "variant_source": "initial_state_same_prompt",
                "look_variant": {
                    "hair": "乌黑低圆髻", "makeup": "淡妆",
                    "costume": "晚明烟墨色窄袖长衫",
                    "temperament": "克制",
                },
                "prompt": "晚明女官，单人全身正面，纯净背景",
                "image_quality": "high",
            })

    def qc_call(capability, payload, out_dir, cancel=None):
        assert capability == "image_qc"
        index = int(Path(payload["image_uri"]).stem.rsplit("-", 1)[1])
        passed = index == 3
        return ProviderResult(
            provider="codex", cost=0.01,
            data={
                "pass": passed,
                "visual_pass": passed,
                "input_contract_pass": True,
                "identity_checked": True,
                "identity_match": True,
                "gender_checked": True,
                "gender_match": True,
                "wardrobe_checked": True,
                "wardrobe_match": True,
                "count_checked": True,
                "count_match": True,
                "overlay_count_checked": True,
                "overlay_count_match": True,
                "physical_logic_checked": True,
                "physical_logic_match": True,
                "spatial_logic_checked": True,
                "spatial_logic_match": True,
                "detected_count": 1,
                "detected_overlay_count": 0,
                "issues": [] if passed else ["整体完成度低于候选3"],
            })

    monkeypatch.setattr(app.router, "call", qc_call)
    out_root = tmp_path / "episode"
    out_root.mkdir()
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "script": script, "out_root": out_root,
        "fresh_assets": True, "run_id": 77,
    }
    app.director._task_cost = 0.0
    app.director._task_providers = set()
    status = app.director._auto_select_asset_candidates(
        ctx, [character], {"顾明昭": design}, [])

    identity = app.assets.latest(
        project["id"], "character_identity", "顾明昭")
    meta = app.assets.meta(identity)
    assert status["passed"] is True
    assert meta["candidate_index"] == 3
    assert meta["auto_selected"] is True
    assert meta["selection_method"] == "aifos_visual_qc_rank"
    assert meta["selection_qc_passed"] is True
    assert meta["fresh_run_id"] == 77


def test_fresh_assets_regenerates_all_candidates_without_old_references(
        app, tmp_path, monkeypatch):
    project, _ = app.projects.get_or_create_project("全新资产隔离")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    character = {
        "name": "沈砚舟", "role": "主角", "gender": "男",
        "age_range": "24岁", "image_prompt": "晚明青年文官",
    }
    design = {
        "gender": "男", "age_range": "24岁",
        "costume": "晚明靛青直裰", "hair": "束发",
    }
    old = tmp_path / "old.png"
    old.write_bytes(b"\x89PNG\r\n\x1a\nold")
    for index in range(1, 5):
        app.assets.register(
            project["id"], "character_candidate",
            f"沈砚舟:{index:02d}", uri=str(old),
            meta={
                "character": "沈砚舟", "candidate_index": index,
                "candidate_prompt_schema":
                    CHARACTER_CANDIDATE_PROMPT_SCHEMA,
                "variant_label": f"旧候选{index}",
                "look_variant": dict(design),
                "prompt": "旧提示词", "image_quality": "high",
            })
    app.assets.register(
        project["id"], "character_identity", "沈砚舟", uri=str(old),
        meta={"character": "沈砚舟", "locked": True,
              "image_quality": "high"})

    captured = []

    def fake_parallel(ctx, tasks, line="", **kwargs):
        captured.extend(tasks)
        results = {}
        for task in tasks:
            fresh = tmp_path / f"fresh-{task['tag'][1]}.png"
            fresh.write_bytes(
                b"\x89PNG\r\n\x1a\n" + bytes([task["tag"][1]]) * 32)
            results[task["tag"]] = ProviderResult(
                provider="image_api", cost=1.0, uri=str(fresh), data={})
        return results

    monkeypatch.setattr(app.director, "_run_parallel", fake_parallel)
    out_root = tmp_path / "fresh-run"
    out_root.mkdir()
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "script": {"characters": [character], "scenes": []},
        "out_root": out_root, "aspect": "9:16",
        "dims": {"width": 1080, "height": 1920},
        "quality_policy": {}, "fresh_assets": True, "run_id": 99,
    }
    status = app.director._ensure_character_candidates(
        ctx, [character], {"沈砚舟": design}, "超写实晚明短剧")

    assert len(captured) == 4
    assert all(
        task["payload"]["allow_text_to_image_bootstrap"] is True
        and task["payload"]["reference_images"] == []
        and task["payload"]["asset_matches"] == []
        for task in captured)
    assert status["characters"][0]["candidate_count"] == 4
    for index in range(1, 5):
        row = app.assets.latest(
            project["id"], "character_candidate",
            f"沈砚舟:{index:02d}")
        assert row["version"] == 2
        assert row["uri"] != str(old)
        assert app.assets.meta(row)["fresh_run_id"] == 99


def test_candidate_count_is_four_for_every_formal_role():
    assert character_candidate_target({"role": "主角"}) == 4
    assert character_candidate_target({"role": "重要配角"}) == 4
    assert character_candidate_target({"role": "配角"}) == 4
    assert character_candidate_target({"role": "非重要配角"}) == 4
    assert character_candidate_target({"role": "背景路人"}) == 0
    assert character_candidate_target({"role": "跑龙套"}) == 0
    assert character_candidate_target({
        "name": "哑着嗓子", "role": "主角"}) == 0
    assert character_candidate_target({
        "name": "待确认说话人", "role": "待确认说话人"}) == 0


def test_unresolved_action_label_blocks_character_generation():
    script = {
        "characters": [{"name": "温声", "role": "配角"}],
        "scenes": [{"scene_no": 1, "location": "东宫",
                    "characters": ["温声"],
                    "lines": [{"character": "温声", "dialogue": "拿来。"}]}],
    }
    error = character_production_readiness_error(script, {})
    assert "人物实体尚未确认" in error
    assert "AI 重新分析" in error


def test_placeholder_gender_and_age_block_candidate_generation():
    script = {
        "characters": [{
            "name": "林川", "role": "主角",
            "gender": "未指定（人物定版后以参考图为准）",
            "age_range": "待确认",
            "image_prompt": "林川单人角色定妆母图",
        }],
        "scenes": [],
    }
    analysis = {
        "characters": [{
            "name": "林川",
            "gender": "以参考图为准",
            "age_range": "未指定",
            "image_prompt": "林川单人角色定妆母图",
        }],
    }

    error = character_production_readiness_error(script, analysis)
    assert "性别、年龄段尚未明确" in error
    assert "不会生成候选图" in error


def test_script_bible_allows_identity_draft_but_strict_gate_rejects_it():
    script = {
        "story_world": {
            "name": "测试世界", "overview": "测试",
            "era_and_location": "当代城市", "social_order": "现实社会",
            "hard_rules": "现实物理", "visual_baseline": "现实材质",
            "forbidden_drift": ["禁止身份漂移"],
        },
        "story_background": {
            "prior_events": "无", "current_situation": "开场",
            "core_conflict": "冲突", "episode_goal": "推进",
            "continuity_hooks": "承接",
        },
        "characters": [{
            "name": "林川", "role": "主角",
            "introduction": "本剧主角", "gender": "未指定",
            "age_range": "以参考图为准", "identity": "学生",
            "personality": "谨慎",
        }],
        "core_props": [],
        "scenes": [],
        "script_logic_audit": {"passed": True, "issues": []},
    }

    assert validate_script_bible(
        script, require_resolved_identity=False) is None
    assert "性别、年龄段必须明确" in validate_script_bible(script)


def test_portrait_prompt_prioritizes_identity_over_reference_clothing(app):
    prompt = app.director._portrait_prompt(
        "林昭", "主角", "现代都市3D半写实",
        design={"hair": "长直发", "makeup": "清透妆", "costume": "通勤装"})
    assert "发型轮廓" in prompt and "眉眼妆" in prompt
    assert "允许与参考图服装不同" in prompt


def test_candidate_prompts_reuse_one_initial_state_and_prompt(app):
    design = {
        "species": "人类", "appearance": "暖白肤色鹅蛋脸，约25岁",
        "eyes": "深棕杏眼", "personality": "认真但有亲和力",
        "hair": "齐肩内扣短发", "makeup": "清透妆",
        "costume": "浅灰通勤套装", "temperament": "温柔克制",
    }
    variants = [app.director._candidate_variant(i, design) for i in range(1, 5)]
    prompts = [app.director._candidate_portrait_prompt(
        "林昭", "主角", "现代都市半写实", design, variant)
        for variant in variants]
    contexts = [app.director._candidate_prompt_review_context(
        "林昭", "主角", "现代都市半写实", design, variant)
        for variant in variants]
    assert len({item["variant_id"] for item in variants}) == 4
    assert len({tuple(item["look_variant"].values()) for item in variants}) == 1
    assert len(set(prompts)) == 1
    assert len({
        json.dumps(context, ensure_ascii=False, sort_keys=True)
        for context in contexts
    }) == 1
    assert all("【单次输出硬约束】" in prompt for prompt in prompts)
    assert all("本次只输出1张独立竖幅图片" in prompt for prompt in prompts)
    assert all("四宫格" in prompt and "候选合集" in prompt
               for prompt in prompts)
    assert all("四张候选" not in prompt and "四图同词" not in prompt
               for prompt in prompts)
    assert all("人物立绘必须是纯净、无文字的单人物资产背景" in prompt
               for prompt in prompts)
    assert all("无参考图时" in prompt for prompt in prompts)
    assert all("PROJECT STYLE LOCK" in prompt for prompt in prompts)
    assert all("当前图片不得切换画风" in prompt for prompt in prompts)
    assert "齐肩内扣短发" in prompts[0]
    assert all(item["candidate_prompt_schema"]
               == CHARACTER_CANDIDATE_PROMPT_SCHEMA
               for item in variants)


def test_visual_dna_is_compiled_without_dumping_internal_audit_json(app):
    design = {
        "species": "人类", "appearance": "长期熬夜形成的清瘦骨相",
        "personality": "谨慎、目标明确", "costume": "旧工装夹克",
        "character_analysis": {
            "current_situation": "在追查失踪同伴",
            "core_desire": "找到同伴",
            "greatest_fear": "再次失去重要的人",
        },
        "visual_dna": {
            "face_structure": "清瘦长脸和轻微眼下疲态",
            "hair_silhouette": "自行剪短的不齐耳短发",
            "body_or_occupation_marks": "右手虎口工具磨痕",
            "clothing_structure": "多口袋旧工装",
            "story_visual_symbol": "修过三次的旧怀表",
            "signature_accessory": "旧怀表",
            "temperament_keywords": ["警觉", "克制", "疲惫但坚定"],
        },
        "cast_dedup": {
            "status": "passed", "compared_with": ["周鹿"],
            "overlap_threshold": 2, "conflicts": [],
        },
    }
    prompt = app.director._candidate_portrait_prompt(
        "林昭", "主角", "电影级半写实", design,
        app.director._candidate_variant(1, design))
    assert "人物视觉DNA" in prompt
    assert "右手虎口工具磨痕" in prompt
    assert "旧怀表" not in prompt
    assert "共同初始造型" in prompt
    assert "全剧角色去重" not in prompt
    assert '"overlap_threshold"' not in prompt
    assert "模板网红脸" in prompt


def test_story_candidates_all_use_clean_initial_look_without_later_states(app):
    design = {
        "species": "人类",
        "gender": "男",
        "age_range": "24岁",
        "appearance": "清瘦长脸，眉眼清秀",
        "eyes": "深棕眼，警觉",
        "hair": "束发无冠，湿碎发贴额",
        "makeup": "自然素面；雨水与泥点；后脑有肿伤及少量血丝",
        "costume": "旧靛青举人袍",
        "signature_props": "吏部札付",
        "image_prompt": (
            "林川，旧靛青举人袍，手持吏部札付，作为嫁祸关键定版态"),
        "visual_dna": {
            "face_structure": "清瘦长脸",
            "hair_silhouette": "束发无冠",
            "clothing_structure": "旧靛青举人袍",
            "story_visual_symbol": "吏部札付",
            "signature_accessory": "吏部札付",
            "temperament_keywords": ["警觉", "克制"],
        },
        "visual_variants": [
            {
                "label": "进京谋生日常态",
                "occasion": "雨后进城",
                "hair": "束发无冠",
                "makeup": "雨水打湿的自然素面",
                "costume": (
                    "青灰粗布圆领长衫沾黄泥水；"
                    "灰褐麻布短褐；衣料半干半湿"),
                "palette": "灰褐与泥黄",
                "props": "湿旧蓝布包袱",
                "temperament": "疲惫而警觉",
            },
            {
                "label": "过渡态",
                "costume": "洗旧青灰长衫",
                "props": "旧蓝布包袱",
            },
            {
                "label": "嫁祸关键定版态",
                "costume": "旧靛青举人袍",
                "props": "吏部札付",
                "temperament": "强压惊疑",
            },
        ],
    }
    variants = [app.director._candidate_variant(i, design)
                for i in range(1, 5)]
    prompts = [app.director._candidate_portrait_prompt(
        "林川", "主角", "明初电影级半写实", design, variant)
        for variant in variants]
    contexts = [app.director._candidate_prompt_review_context(
        "林川", "主角", "明初电影级半写实", design, variant)
        for variant in variants]

    assert len(set(prompts)) == 1
    assert len({
        json.dumps(context, ensure_ascii=False, sort_keys=True)
        for context in contexts
    }) == 1
    prompt = prompts[0]
    review_context = contexts[0]
    assert "青灰粗布圆领长衫" in prompt
    assert "灰褐麻布短褐" in prompt
    assert "青灰粗布圆领长衫沾黄泥水" not in prompt
    assert "黄泥水" not in prompt
    assert "旧蓝布包袱" in prompt
    assert "湿旧蓝布包袱" not in prompt
    assert "雨水打湿" not in prompt
    assert "衣料半干半湿" not in prompt
    assert "后脑有肿伤" not in prompt
    assert "少量血丝" not in prompt
    assert "旧靛青举人袍" not in prompt
    assert "吏部札付" not in prompt
    assert "全局服装/标志道具" in prompt
    assert review_context["initial_character_state"]["wardrobe"] \
        == "青灰粗布圆领长衫；灰褐麻布短褐"
    assert review_context["initial_character_state"][
        "accessories_and_props"] == "旧蓝布包袱"
    serialized_context = str(review_context)
    assert "旧靛青举人袍" not in serialized_context
    assert "吏部札付" not in serialized_context


def test_legacy_character_design_is_upgraded_without_losing_fields(app):
    legacy = {
        "appearance": "方脸、肩背挺直",
        "hair": "利落短发",
        "costume": "深蓝维修工装",
        "occupation": "设备维修师",
        "signature_props": "磨旧的扳手",
        "personality": "沉稳但固执",
    }
    upgraded = app.director._upgrade_character_visual_dna(
        legacy, {"name": "陈工", "role": "重要配角"})
    assert upgraded["appearance"] == legacy["appearance"]
    assert upgraded["visual_dna"]["face_structure"] == legacy["appearance"]
    assert upgraded["visual_dna"]["hair_silhouette"] == legacy["hair"]
    assert upgraded["visual_dna"]["signature_accessory"] == "磨旧的扳手"
    assert 3 <= len(upgraded["visual_dna"]["temperament_keywords"]) <= 8
    assert upgraded["character_analysis"]["identity_and_class"] == "设备维修师"


def test_shot_uses_individual_canonical_view_not_review_board(app):
    assert app.director._shot_reference_sheet_keys(
        {"description": "她背对镜头离场"}) == ("back",)
    assert app.director._shot_reference_sheet_keys(
        {"description": "严格侧面转头观察"}) == ("profile",)
    assert app.director._shot_reference_sheet_keys(
        {"camera": "面部大特写"}) == ("closeup",)
    assert app.director._shot_reference_sheet_keys(
        {"description": "正面走入房间"}) == ("front",)


def test_over_shoulder_uses_front_sheet_for_subject_and_back_for_counterpart(
        app):
    shot = {
        "characters": ["朱慈烺", "李继周"],
        "description": "李继周半个背影作过肩前景，朱慈烺正面对他说话",
        "camera": "中近景过肩机位",
        "dialogue": {"character": "朱慈烺", "dialogue": "照此办理。"},
    }
    keys = app.director._shot_reference_sheet_keys_by_character(shot)
    assert keys["朱慈烺"] in (("front", "closeup"),
                              ("closeup", "front"))
    assert keys["李继周"] == ("back",)


def test_reference_portrait_locks_face_hair_makeup_and_workwear(app):
    design = {"appearance": "鹅蛋脸", "hair": "齐肩短发", "costume": "外卖制服"}
    variant = app.director._candidate_variant(1, design)
    prompt = app.director._candidate_portrait_prompt(
        "外卖小哥", "非重要配角", "现代半写实", design, variant,
        has_reference=True)
    assert "只锁人物脸型、五官骨相" in prompt
    assert "严格保持参考图发型轮廓" in prompt
    assert "不得更换发型身份" in prompt
    assert "不得改变妆造体系" in prompt
    assert "禁止换脸" in prompt
    assert "外卖小哥" in prompt and "外卖制服" in prompt
    assert "职业服装:" not in prompt
    assert "纯净、无文字的单人物资产背景" in prompt


def test_candidate_reference_semantics_lock_project_style_and_identity():
    payload = {
        "portrait_candidate": True,
        "style": "现代都市半写实",
        "reference_images": ["/tmp/identity.png"],
    }
    ref_line = _ref_line(payload)
    assert "脸是最高标准" in ref_line
    assert "发型轮廓、发量、发色家族、妆造" in ref_line
    assert "不得改脸、换发型、换妆造、换服装、换动作或换画风" in ref_line
    style_line = _style_line(payload)
    assert "不存在候选画风选项" in style_line
    assert "完全相同的初始状态提示词" in style_line
    assert "只靠图片模型随机采样" in style_line
    assert "不得换装、换妆、换动作" in style_line
    normal = _ref_line({"reference_images": ["/tmp/identity.png"]})
    assert "禁止换脸或换发型" in normal


def test_legacy_candidate_is_not_given_a_fake_look_label(app):
    project, _episode, script = _to_cast_selection(app, "历史候选兼容")
    name = script["characters"][0]["name"]
    old = app.assets.latest(project["id"], "character_candidate", f"{name}:01")
    app.assets.register(
        project["id"], "character_candidate", f"{name}:01",
        uri=old["uri"],
        meta={"character": name, "candidate_index": 1},
        new_version=True,
    )
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    candidate = status["characters"][0]["candidates"][0]
    assert candidate["variant_source"] == "legacy"
    assert candidate["variant_label"] == ""
    assert candidate["look_variant"] is None
    assert candidate["current_candidate_policy"] is False
    with pytest.raises(AifosError, match="旧版"):
        app.director.select_character_candidate(
            project["title"], 1, name, 1)


def test_old_story_state_candidate_is_kept_as_history_but_not_reused(app):
    title = "旧剧情状态候选迁移"
    first = app.director.produce(
        title, 1, pause_for_confirm=True)
    assert first["status"] == "awaiting_script"
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    old_uri = app.workspace.artifacts_dir / "old-official-state.png"
    old_uri.write_bytes(b"\x89PNG\r\n\x1a\nold")
    app.assets.register(
        project["id"], "character_candidate", f"{name}:01",
        uri=str(old_uri),
        meta={
            "character": name,
            "candidate_index": 1,
            "variant_id": "story_final",
            "variant_label": "官服关键态",
            "variant_source": "generated",
            "look_variant": {
                "hair": "束发",
                "makeup": "后脑伤",
                "costume": "青色官袍",
                "temperament": "眩晕",
            },
            "prompt": "青色官袍、后脑伤与泥水状态",
        })

    second = app.director.produce(
        title, 1, pause_for_confirm=True)

    assert second["status"] == "awaiting_cast"
    history = app.assets.history(
        project["id"], "character_candidate", f"{name}:01")
    assert len(history) == 2
    assert app.assets.meta(history[0])["variant_label"] == "官服关键态"
    latest_meta = app.assets.meta(history[-1])
    assert latest_meta["candidate_prompt_schema"] \
        == CHARACTER_CANDIDATE_PROMPT_SCHEMA
    assert latest_meta["variant_source"] == "initial_state_same_prompt"
    assert "青色官袍、后脑伤与泥水状态" != latest_meta["prompt"]


def test_locked_candidate_becomes_only_identity_reference(app):
    title = "最终立绘锁定测试"
    project, episode, script = _to_cast_selection(app, title)
    _lock_all(app, title, script, index=4)
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    assert status["passed"]
    for character in status["characters"]:
        selected = next(c for c in character["candidates"] if c["selected"])
        identity = app.assets.latest(
            project["id"], "character_identity", character["character"])
        portrait = app.assets.latest(
            project["id"], "character_art", character["character"])
        assert identity["uri"] == selected["uri"] == portrait["uri"]

    summary = app.director.produce(title, 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "script": script, "storyboard": storyboard,
           "aspect": "9:16", "dims": {"width": 1080, "height": 1920}}
    payload = app.director._shot_payload(ctx, storyboard["shots"][0])
    assert payload["require_reference_images"] is True
    assert len(payload["identity_references"]) \
        == len(storyboard["shots"][0]["characters"])
    locked_uris = {ref["uri"] for ref in payload["identity_references"]}
    assert locked_uris.issubset(set(payload["character_refs"]))
    # 最终候选只锁身份。服装/头饰/妆发必须服从当前镜头状态，避免把
    # 候选图中属于其他场次的造型错误扩散到每个镜头。
    for name in storyboard["shots"][0]["characters"]:
        look = app.director._locked_look_variant(project["id"], name)
        state = (
            storyboard["shots"][0]["end_state"].get(name)
            or storyboard["shots"][0]["start_state"].get(name)
            or {})
        if state.get("wardrobe"):
            assert str(state["wardrobe"]) in payload["prompt"]
            assert payload["character_background"][name]["costume"] \
                == str(state["wardrobe"])
            if (look.get("costume")
                    and str(look["costume"]) != str(state["wardrobe"])):
                assert str(look["costume"]) not in payload["prompt"]


def test_missing_identity_blocks_character_generation_and_qc(app):
    project, _episode, script = _to_cast_selection(app, "缺锚点阻断")
    name = script["characters"][0]["name"]
    with pytest.raises(AifosError, match="尚未锁定最终立绘"):
        app.director._art_refs({"project": dict(project)}, [name], "")
    with pytest.raises(AifosError, match="尚未锁定最终立绘"):
        app.director._qc_spec(project["id"], [name])


def test_router_rejects_text_only_provider_for_people(app, tmp_path):
    router = app.router
    # 临时把通用 API 放到链首且声明不支持参考图；必须跳过它而走 mock。
    api = router.providers["api"]
    api.enabled = True
    api.reference_images = False
    api.conf["endpoint"] = "http://127.0.0.1:1"
    api.conf["api_key"] = "x"
    app.config.data["routing"]["image"] = ["api", "mock"]
    ref = tmp_path / "identity.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    result = router.call("image", {
        "portrait": True, "art_name": "测试", "prompt": "x",
        "character_refs": [str(ref)], "require_reference_images": True,
    }, tmp_path)
    assert result.provider == "mock"
    assert any("不支持真实参考图" in item["reason"]
               for item in result.fallbacks)
    with pytest.raises(ProviderUnavailable, match="未携带任何图片"):
        router.call("image", {
            "portrait": True, "art_name": "测试", "prompt": "x",
            "require_reference_images": True,
        }, tmp_path)


def test_visual_qc_requires_identity_ack_and_reuses_signature(app, tmp_path):
    title = "视觉身份质检"
    project, episode, script = _to_cast_selection(app, title)
    _lock_all(app, title, script)
    summary = app.director.produce(title, 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    name = script["characters"][0]["name"]
    shot = next(item for item in storyboard["shots"]
                if name in item.get("characters", []))
    target = tmp_path / "target.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    calls = []

    class Router:
        def call(self, capability, payload, out_dir, cancel=None):
            calls.append(payload)
            return ProviderResult(
                provider="vision", cost=0.2,
                        data={"pass": True, "identity_checked": True,
                              "identity_match": True,
                              "gender_checked": True, "gender_match": True,
                              "wardrobe_checked": True,
                              "wardrobe_match": True,
                              "count_checked": True, "count_match": True,
                          "physical_logic_checked": True,
                          "physical_logic_match": True,
                          "spatial_logic_checked": True,
                          "spatial_logic_match": True,
                          "issues": []})

    app.director.router = Router()
    app.assets.register(
        project["id"], "image", f"e001_shot{shot['shot_no']:03d}",
        uri=str(target), meta={"shot_no": shot["shot_no"]}, new_version=True)
    report1 = app.director.qc_item(title, 1, f"shot:{shot['shot_no']}")
    report2 = app.director.qc_item(title, 1, f"shot:{shot['shot_no']}")
    assert report1["passed"] and report1["identity_checked"]
    assert report2["cached"] is True
    assert len(calls) == 1
    assert calls[0]["identity_required"] is True
    assert calls[0]["gender_required"] is True
    assert calls[0]["identity_references"][0]["character"] == name


def test_systemic_identity_failures_trip_redraw_circuit_breaker(app):
    title = "人物漂移熔断"
    project, episode, script = _to_cast_selection(app, title)
    ctx = {"episode": dict(episode),
           "out_root": app.director._episode_dir(project, episode)}
    items = []
    for index in range(1, 4):
        items.append({"id": f"shot:{index}", "category": "shot_image",
                      "shot_no": index, "label": f"镜头{index}",
                      "prompt": "x", "status": "done",
                      "qc": {"passed": False,
                             "issues": ["发型和脸与最终立绘不一致"]}})
    app.director._plan_write(ctx, {"items": items})
    result = app.director.redo_items(title, 1, only_failed=True)
    assert result["status"] == "blocked"
    assert result["reason"] == "systemic_identity_failure"
    assert result["redone"] == 0
