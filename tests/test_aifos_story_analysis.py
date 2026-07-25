"""剧本 AI 分析、制作圣经和提示词继承。"""

import pytest

from aifos.app import App
from aifos.director import MODERN_OTOME_STYLE
from aifos.errors import AifosError
from aifos.story_analysis import (
    STORY_ANALYSIS_SCHEMA,
    apply_story_analysis,
    build_story_analysis,
    infer_story_visual_context,
    reconcile_character_entities,
    unresolved_character_labels,
    validate_line_speaker_resolution,
    validate_story_analysis,
)


@pytest.fixture()
def script():
    return {
        "project_title": "心动合约",
        "episode_number": 1,
        "episode_title": "雨夜重逢",
        "logline": "现代设计师在雨夜遇见旧日恋人",
        "characters": [
            {"name": "苏念", "role": "主角", "gender": "女"},
            {"name": "顾屿", "role": "重要配角", "gender": "男"},
        ],
        "scenes": [{
            "scene_no": 1, "location": "现代城市设计事务所·夜",
            "characters": ["苏念", "顾屿"], "action": "窗外下雨，两人对峙",
            "lines": [{"character": "苏念", "dialogue": "你为什么回来？"}],
        }],
    }


def test_analysis_locks_user_style_and_builds_prompt_bible(script):
    analysis = build_story_analysis(script, MODERN_OTOME_STYLE)
    assert analysis["schema"] == STORY_ANALYSIS_SCHEMA
    assert analysis["visual"]["user_style_constraint"] == MODERN_OTOME_STYLE
    assert "古装" in analysis["visual"]["forbidden_visuals"]
    assert analysis["scenes"][0]["environment"]
    assert analysis["characters"][0]["candidate_count"] == 4
    assert analysis["characters"][1]["candidate_count"] == 4
    hero = analysis["characters"][0]
    assert hero["character_analysis"]["core_desire"]
    assert "苏念" in hero["image_prompt"]
    assert "脸部骨相" in hero["image_prompt"]
    assert hero["negative_prompt"]
    assert 3 <= len(hero["visual_dna"]["temperament_keywords"]) <= 8
    assert hero["cast_dedup"]["compared_with"] == ["顾屿"]
    assert analysis["production_rules"]["three_view_contract"][
        "canonical_individual_assets"] == [
            "face_closeup", "front", "profile", "back"]
    assert "15秒" in analysis["prompt_bible"]["seedance_prefix"]
    assert validate_story_analysis(analysis) is None


def test_history_story_builds_its_own_style_without_modern_or_2d_default():
    historical = {
        "project_title": "大明",
        "episode_number": 1,
        "episode_title": "乾清宫急报",
        "logline": "崇祯命太子守住京城",
        "characters": [
            {"name": "朱慈烺", "role": "主角"},
            {"name": "崇祯", "role": "重要配角"},
        ],
        "scenes": [{
            "scene_no": 1, "location": "乾清宫",
            "characters": ["朱慈烺", "崇祯"],
            "action": "王承恩捧着奏折奔入殿内",
            "lines": [
                {"character": "朱慈烺", "dialogue": "父皇，儿臣请战！"},
                {"character": "崇祯", "dialogue": "守住城门。"},
            ],
        }],
    }
    context = infer_story_visual_context(historical)
    analysis = build_story_analysis(historical)
    assert context["key"] == "ming_history"
    assert "明代" in analysis["world"]["era_and_location"]
    assert "明代历史题材" in analysis["visual"]["user_style_constraint"]
    assert analysis["visual"]["style_source"] == "ai_inferred"
    assert "2D" not in analysis["visual"]["user_style_constraint"]
    assert "现代乙女" not in analysis["visual"]["user_style_constraint"]
    assert "清代服饰" in analysis["visual"]["forbidden_visuals"]
    assert validate_story_analysis(analysis) is None


def test_episode_anachronism_whitelist_overrides_derived_era_bans():
    historical = {
        "project_title": "大明",
        "episode_number": 1,
        "logline": "皇太子带着穿越前的笔记本电脑醒来",
        "story_world": {
            "sanctioned_anachronisms": ["锦盒里的银色笔记本电脑"],
        },
        "characters": [{"name": "朱慈烺", "role": "主角"}],
        "scenes": [{
            "scene_no": 1,
            "location": "东宫寝殿",
            "characters": ["朱慈烺"],
            "action": "朱慈烺打开锦盒里的银色笔记本电脑",
            "lines": [],
        }],
    }
    raw = {
        "world": {
            "forbidden_drift": [
                "禁止现代物品进入明代寝殿",
                "禁止出现锦盒里的银色笔记本电脑",
                "禁止清代服饰",
            ],
        },
    }

    enriched = apply_story_analysis(
        historical, build_story_analysis(historical, raw=raw))
    forbidden = enriched["story_world"]["forbidden_drift"]

    assert "禁止出现锦盒里的银色笔记本电脑" not in forbidden
    assert "除剧情白名单（锦盒里的银色笔记本电脑）外，禁止现代物品进入明代寝殿" in forbidden
    assert "禁止清代服饰" in forbidden
    resolved = enriched["production_analysis"]["rule_conflicts_resolved"]
    assert {item["resolution"] for item in resolved} == {
        "discarded_lower_priority_forbidden_rule",
        "qualified_by_episode_whitelist",
    }


def test_character_image_prompt_is_compact_visual_only_and_idempotent():
    historical = {
        "project_title": "大明",
        "episode_number": 1,
        "logline": "皇太子穿越首夜准备翻盘",
        "characters": [{"name": "朱慈烺", "role": "主角"}],
        "scenes": [{
            "scene_no": 1, "location": "东宫寝殿",
            "characters": ["朱慈烺"], "action": "太子伤后醒来",
            "lines": [],
        }],
    }
    compact = (
        "电影级半写实精品漫剧，明代皇太子朱慈烺，男，约十五岁清瘦"
        "少年，白皙清秀、骨相分明，乌黑长发束发，绝无辫发；明代交领"
        "寝居中衣外覆貂皮大氅；全身正面自然站姿，纯净中性棚拍背景，"
        "无字幕、无文字、无Logo、无水印")
    generated = (
        "单人角色定妆母图：朱慈烺；身份：大明皇太子，父崇祯帝、"
        "母周皇后；人物处境：距亡国不足108天，谋划说服父皇；"
        "掌握历史先知与防疫常识、过度依赖残电电脑；"
        "全身正面自然站姿，纯净中性棚拍背景")
    raw = {
        "characters": [{
            "name": "朱慈烺", "gender": "男",
            "age_range": "约15岁少年（身体）／附成年现代意识",
            "identity_facts": "大明皇太子（储君），父崇祯帝，母周皇后",
            "image_prompt": f"{compact}；{generated}；{generated}",
            "character_analysis": {
                "identity_and_class": "大明皇太子（储君）",
                "current_situation": "距亡国不足108天，谋划说服父皇",
                "strengths": ["历史先知", "冷静算计"],
                "flaws": ["依赖残电电脑"],
            },
            "visual_dna": {
                "face_structure": "少年清秀脸型，白皙肤色，骨相分明",
                "hair_silhouette": "乌黑长发束发，绝无清代辫发",
                "body_or_occupation_marks": "清瘦少年体格，手指纤细",
                "clothing_structure": "明代交领寝居中衣外覆貂皮大氅",
                "clothing_wear_state": "整洁但略显伤后松散",
                "story_visual_symbol": "笔记本电脑与防疫奏疏",
                "story_visual_symbol_origin": "穿越与救国计划",
                "signature_accessory": "貂皮大氅",
                "temperament_keywords": ["沉郁", "孤勇", "决绝"],
            },
        }],
    }
    first = build_story_analysis(historical, raw=raw)
    prompt = first["characters"][0]["image_prompt"]
    assert prompt == compact
    assert prompt.count("全身正面自然站姿") == 1
    assert "父崇祯帝" not in prompt
    assert "108天" not in prompt
    assert "说服父皇" not in prompt
    assert "历史先知" not in prompt
    assert "残电电脑" not in prompt
    assert len(prompt) < 700

    second = build_story_analysis(historical, raw=first)
    assert second["characters"][0]["image_prompt"] == prompt


def test_blank_project_style_is_inferred_from_full_imported_script(tmp_path):
    from aifos.script_import import parse_text_script

    imported = parse_text_script(
        "朱慈烺说道：“父皇，儿臣请战！”\n"
        "“守住大明城门。”崇祯沉声道。",
        "大明", 1)
    app = App(tmp_path / "ws-auto-style")
    try:
        summary = app.director.produce(
            "大明", 1, script=imported, pause_for_confirm=True)
        assert summary["status"] == "awaiting_script"
        project = app.projects.get_project("大明")
        # 自动结果不伪装成人工项目锁定风格；本集制作圣经才是事实源。
        assert project["style"] == ""
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        analysis, _ = app.projects.latest_document(
            episode["id"], "story_analysis")
        assert analysis["visual"]["style_source"] == "ai_inferred"
        assert "明代历史题材" in analysis["visual"][
            "user_style_constraint"]
        assert analysis["project_style"] == ""
    finally:
        app.close()


def test_analysis_is_injected_into_all_downstream_prompt_context(script):
    analysis = build_story_analysis(script, MODERN_OTOME_STYLE)
    enriched = apply_story_analysis(script, analysis)
    assert enriched["production_analysis"]["prompt_bible"][
        "global_image_prefix"]
    assert "现代城市" in enriched["scenes"][0]["prompt_prefix"]
    assert enriched["characters"][0]["prompt_prefix"]
    assert enriched["characters"][0]["visual_dna"]["hair_silhouette"]
    assert enriched["characters"][0]["cast_dedup"]["overlap_threshold"] == 2
    assert enriched["story_world"]["visual_baseline"].startswith(
        MODERN_OTOME_STYLE)


def test_character_image_prompt_is_compact_and_idempotent(script):
    raw = {
        "characters": [{
            "name": "苏念",
            "gender": "女",
            "age_range": "二十六岁",
            "identity_facts": "现代城市设计师",
            "image_prompt": (
                "电影级现代乙女半写实漫剧，苏念，女，二十六岁，"
                "清晰自然骨相，黑色锁骨发，身着利落设计师通勤装；"
                "全身正面自然站姿，纯净棚拍背景，无文字无水印"
            ),
        }],
    }
    first = build_story_analysis(script, MODERN_OTOME_STYLE, raw=raw)
    first_prompt = first["characters"][0]["image_prompt"]
    assert first_prompt.startswith("电影级现代乙女半写实漫剧")
    assert len(first_prompt) <= 720

    second = build_story_analysis(
        script, MODERN_OTOME_STYLE, raw=first, source="saved")
    second_prompt = second["characters"][0]["image_prompt"]
    assert second_prompt == first_prompt
    assert second_prompt.count("单人角色定妆母图：苏念") <= 1

    enriched = apply_story_analysis(script, second)
    assert enriched["characters"][0]["image_prompt"] == first_prompt


def test_legacy_appended_character_prompt_keeps_only_authoritative_card(script):
    concise = (
        "电影级现代乙女半写实漫剧，苏念，女，二十六岁，"
        "清晰自然骨相，黑色锁骨发，身着利落设计师通勤装；"
        "全身正面自然站姿，纯净棚拍背景，无文字无水印"
    )
    legacy = {
        "characters": [{
            "name": "苏念",
            "gender": "女",
            "age_range": "二十六岁",
            "image_prompt": (
                concise
                + "；单人角色定妆母图：苏念；身份：旧版重复母版；"
                + "脸部骨相：旧版冲突模板"
            ),
        }],
    }
    analysis = build_story_analysis(
        script, MODERN_OTOME_STYLE, raw=legacy, source="saved")
    prompt = analysis["characters"][0]["image_prompt"]
    assert prompt == concise
    assert "旧版重复母版" not in prompt
    assert "旧版冲突模板" not in prompt


def test_legacy_analysis_without_character_fields_is_upgraded(script):
    analysis = build_story_analysis(script, MODERN_OTOME_STYLE)
    # 旧版本曾把角色分析保存成不完整对象；详情页和分镜生产表仍必须可打开。
    for character in analysis["characters"]:
        character.pop("character_analysis", None)
        character.pop("visual_dna", None)
        character.pop("cast_dedup", None)
    enriched = apply_story_analysis(script, analysis)
    assert enriched["characters"][0]["character_analysis"]["core_desire"]
    assert enriched["characters"][0]["visual_dna"]["hair_silhouette"]
    assert enriched["characters"][0]["cast_dedup"]["compared_with"] == ["顾屿"]


def test_ai_resolution_merges_misparsed_states_into_real_people():
    imported = {
        "project_title": "大明", "episode_number": 1,
        "logline": "太子试探内侍",
        "characters": [
            {"name": "哑着嗓子", "role": "主角"},
            {"name": "小心翼翼地", "role": "配角"},
            {"name": "朱慈烺试探着", "role": "配角"},
        ],
        "scenes": [{
            "scene_no": 1, "location": "东宫",
            "characters": ["哑着嗓子", "小心翼翼地", "朱慈烺试探着"],
            "action": "",
            "lines": [
                {"character": "哑着嗓子", "dialogue": "锦盒里是什么？"},
                {"character": "小心翼翼地", "dialogue": "是西洋奇物。"},
                {"character": "朱慈烺试探着", "dialogue": "拿来。"},
            ],
        }],
        "import_analysis": {"dialogue_count": 3, "character_count": 3},
    }
    raw = {
        "speaker_resolution": [
            {"raw_label": "哑着嗓子", "canonical_name": "朱慈烺",
             "classification": "performance_cue",
             "performance": "哑着嗓子", "confidence": 0.99},
            {"raw_label": "朱慈烺试探着", "canonical_name": "朱慈烺",
             "classification": "misparsed_label",
             "performance": "试探着", "confidence": 0.99},
            {"raw_label": "小心翼翼地", "canonical_name": "李继周",
             "classification": "performance_cue",
             "performance": "小心翼翼地", "confidence": 0.97},
        ],
        "characters": [
            {"name": "朱慈烺", "gender": "男", "age_range": "约十五岁",
             "identity_facts": "明末皇太子",
             "visual_direction": "明代太子常服，黑发无辫"},
            {"name": "李继周", "gender": "男", "age_range": "三十余岁",
             "identity_facts": "东宫内侍",
             "visual_direction": "明代内官帽服"},
        ],
    }
    assert reconcile_character_entities(imported, raw) is True
    assert [item["name"] for item in imported["characters"]] == [
        "朱慈烺", "李继周"]
    assert [line["character"] for line in imported["scenes"][0]["lines"]] == [
        "朱慈烺", "李继周", "朱慈烺"]
    assert imported["scenes"][0]["lines"][0]["performance"] == "哑着嗓子"
    assert unresolved_character_labels(imported) == []
    analysis = build_story_analysis(imported, raw=raw)
    prince = next(
        item for item in analysis["characters"] if item["name"] == "朱慈烺")
    assert prince["gender"] == "男"
    assert prince["age_range"] == "约十五岁"
    assert "明末皇太子" in prince["image_prompt"]
    assert "最终人物出图卡" not in prince["image_prompt"]


def test_line_level_resolution_overrides_reused_wrong_label_per_dialogue():
    imported = {
        "project_title": "大明", "episode_number": 1,
        "characters": [{"name": "哑着嗓子", "role": "主角"}],
        "scenes": [{
            "scene_no": 1, "location": "东宫",
            "characters": ["哑着嗓子"], "action": "",
            "lines": [
                {"character": "哑着嗓子", "dialogue": "殿下醒了？"},
                {"character": "哑着嗓子", "dialogue": "今夕是何年何月？"},
                {"character": "哑着嗓子", "dialogue": "回殿下，是崇祯十六年。"},
            ],
        }],
        "import_analysis": {"dialogue_count": 3, "character_count": 1},
    }
    raw = {
        "line_speakers": [
            {"scene_no": 1, "line_index": 1, "dialogue": "殿下醒了？",
             "raw_label": "哑着嗓子", "canonical_name": "李继周",
             "performance": "小心问候", "confidence": 0.99},
            {"scene_no": 1, "line_index": 2, "dialogue": "今夕是何年何月？",
             "raw_label": "哑着嗓子", "canonical_name": "朱慈烺",
             "performance": "哑声试探", "confidence": 0.99},
            {"scene_no": 1, "line_index": 3,
             "dialogue": "回殿下，是崇祯十六年。",
             "raw_label": "哑着嗓子", "canonical_name": "李继周",
             "performance": "躬身回话", "confidence": 0.99},
        ],
        "characters": [
            {"name": "朱慈烺", "importance": "主角",
             "gender": "男", "age_range": "十五岁"},
            {"name": "李继周", "importance": "非重要配角",
             "gender": "男（宦官）", "age_range": "三十余岁"},
        ],
    }
    assert validate_line_speaker_resolution(imported, raw) is None
    assert reconcile_character_entities(imported, raw) is True
    assert [line["character"] for line
            in imported["scenes"][0]["lines"]] == [
                "李继周", "朱慈烺", "李继周"]
    assert {item["name"] for item in imported["characters"]} == {
        "朱慈烺", "李继周"}
    assert imported["import_analysis"]["line_speaker_corrections"][0][
        "dialogue"] == "殿下醒了？"


def test_low_confidence_background_voice_does_not_remain_unresolved_cast():
    imported = {
        "project_title": "冒名入仕", "episode_number": 1,
        "characters": [{
            "name": "待确认说话人", "role": "待确认说话人",
            "asset_policy": "unresolved_no_generation",
        }],
        "scenes": [{
            "scene_no": 1, "location": "胭谷山",
            "characters": ["待确认说话人"],
            "action": "现代青年林川二十四岁，官差骑马赶到现场。",
            "lines": [
                {"character": "待确认说话人",
                 "dialogue": "我只是路过，被打晕后莫名换了衣服。"},
                {"character": "待确认说话人",
                 "dialogue": "快！前面有马车！"},
                {"character": "待确认说话人", "dialogue": "在那里！"},
            ],
        }],
        "import_analysis": {"dialogue_count": 3, "character_count": 1},
    }
    raw = {
        "line_speakers": [
            {"scene_no": 1, "line_index": 1,
             "dialogue": "我只是路过，被打晕后莫名换了衣服。",
             "raw_label": "待确认说话人", "canonical_name": "林川",
             "confidence": 0.78},
            {"scene_no": 1, "line_index": 2,
             "dialogue": "快！前面有马车！",
             "raw_label": "待确认说话人",
             "canonical_name": "官差（画外群体声）",
             "confidence": 0.55},
            {"scene_no": 1, "line_index": 3, "dialogue": "在那里！",
             "raw_label": "待确认说话人",
             "canonical_name": "官差（画外群体声）",
             "confidence": 0.55},
        ],
        "characters": [{
            "name": "林川", "importance": "主角",
            "gender": "男", "age_range": "24岁",
            "identity_facts": "现代清华高材生、国考选调生，穿越成明初穷秀才",
        }],
    }

    assert reconcile_character_entities(imported, raw) is True
    assert unresolved_character_labels(imported) == []
    assert [line["character"] for line
            in imported["scenes"][0]["lines"]] == [
                "林川", "官差（画外群体声）", "官差（画外群体声）"]
    hero = next(item for item in imported["characters"]
                if item["name"] == "林川")
    background = next(item for item in imported["characters"]
                      if item["name"] == "官差（画外群体声）")
    assert hero["role"] == "主角"
    assert hero["gender"] == "男" and hero["age_range"] == "24岁"
    assert background["role"] == "背景人物"
    assert background["asset_policy"] == "scene_only_no_individual_asset"

    analysis = build_story_analysis(imported, raw=raw)
    background_card = next(
        item for item in analysis["characters"]
        if item["name"] == "官差（画外群体声）")
    assert background_card["importance"] == "背景路人"
    assert background_card["candidate_count"] == 0
    assert "image_prompt" not in background_card


def test_line_level_resolution_requires_every_dialogue():
    script = {
        "scenes": [{"scene_no": 1, "lines": [
            {"character": "旧标签", "dialogue": "第一句"},
            {"character": "旧标签", "dialogue": "第二句"},
        ]}],
    }
    error = validate_line_speaker_resolution(script, {
        "line_speakers": [{
            "scene_no": 1, "line_index": 1, "dialogue": "第一句",
            "canonical_name": "甲",
        }],
    })
    assert "场1第2句" in error


def test_saved_analysis_is_versioned_and_rejects_stale_edit(tmp_path):
    app = App(tmp_path / "ws")
    try:
        summary = app.director.produce(
            "心动合约", 1, premise="现代乙女雨夜重逢",
            style=MODERN_OTOME_STYLE, pause_for_confirm=True)
        assert summary["status"] == "awaiting_script"
        project = app.projects.get_project("心动合约")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        analysis, version = app.projects.latest_document(
            episode["id"], "story_analysis")
        assert version == 1
        analysis["visual"]["lighting"] = "雨夜冷蓝主光，室内暖光勾边"
        saved = app.director.save_story_analysis(
            episode["id"], analysis, expected_version=version)
        assert saved["version"] == 2
        assert saved["analysis"]["visual"]["lighting"].startswith("雨夜冷蓝")
        with pytest.raises(AifosError, match="其他页面更新"):
            app.director.save_story_analysis(
                episode["id"], analysis, expected_version=version)
    finally:
        app.close()


def test_story_analysis_edit_invalidates_continuity_snapshot(tmp_path):
    app = App(tmp_path / "ws")
    try:
        app.director.produce(
            "制作圣经联动", 1, pause_for_confirm=True)
        project = app.projects.get_project("制作圣经联动")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        app.director.produce(
            "制作圣经联动", 1, pause_for_confirm=True)
        continuity_before, continuity_version = app.projects.latest_document(
            episode["id"], "continuity")
        analysis, analysis_version = app.projects.latest_document(
            episode["id"], "story_analysis")
        analysis["world"]["overview"] = "调整后的世界观将改变所有后续画面。"
        saved = app.director.save_story_analysis(
            episode["id"], analysis, expected_version=analysis_version)

        app.director.produce(
            "制作圣经联动", 1, pause_for_confirm=True)
        continuity_after, new_version = app.projects.latest_document(
            episode["id"], "continuity")
        assert new_version == continuity_version + 1
        assert continuity_after["story_analysis_version"] == saved["version"]
        assert continuity_after["story_world"]["overview"] == (
            "调整后的世界观将改变所有后续画面。")
        assert continuity_before["story_world"]["overview"] != (
            continuity_after["story_world"]["overview"])
    finally:
        app.close()
