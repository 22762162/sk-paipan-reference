"""剧本 AI 分析、制作圣经和提示词继承。"""

import pytest

from aifos.app import App
from aifos.director import MODERN_OTOME_STYLE
from aifos.errors import AifosError
from aifos.story_analysis import (
    STORY_ANALYSIS_SCHEMA,
    apply_story_analysis,
    build_story_analysis,
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
    assert analysis["characters"][0]["candidate_count"] == 5
    assert analysis["characters"][1]["candidate_count"] == 3
    hero = analysis["characters"][0]
    assert hero["character_analysis"]["core_desire"]
    assert 3 <= len(hero["visual_dna"]["temperament_keywords"]) <= 8
    assert hero["cast_dedup"]["compared_with"] == ["顾屿"]
    assert analysis["production_rules"]["three_view_contract"][
        "canonical_individual_assets"] == [
            "face_closeup", "front", "profile", "back"]
    assert "15秒" in analysis["prompt_bible"]["seedance_prefix"]
    assert validate_story_analysis(analysis) is None


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
