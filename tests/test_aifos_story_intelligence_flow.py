"""Actual script/storyboard adapters for the non-blocking story brain."""

import copy
import json

from aifos.app import App
from aifos.story_intelligence import (
    ContinuityDomain,
    ReviewDimension,
    build_script_review_court,
    build_storyboard_review_documents,
    derive_episode_continuity_input,
    review_document,
)


def _formal_shot(number, *, scene=1, duration=5.5):
    return {
        "shot_no": number,
        "scene_no": scene,
        "unit_id": f"U{number:02d}",
        "pipeline_version": "v-test",
        "duration": duration,
        "kind": "dialogue",
        "dialogue": {"character": "林昭", "dialogue": "密诏是真的。"},
        "shot_contract": {"景别": "中景", "运镜": "缓慢推进"},
        "shot_function": "揭示信息",
        "script_reference": "林昭展开密诏，确认落款。",
        "visual_hook": "门外人影压住门缝。",
        "start_state": {"林昭": {"pose": "坐在书案前"}},
        "end_state": {"林昭": {"pose": "起身看向门口"}},
        "performance": {"beat": "从确认转为警觉"},
        "five_dimensions": {
            "subject_motion": "林昭展开密诏",
            "environment_light": "烛火从画左稳定照入",
            "camera_design": {
                "shot_scale": "中景",
                "movement": "缓慢推进",
            },
            "time_state": {"start": "坐", "end": "起身"},
            "aesthetics": {"lighting": "暖烛光"},
        },
    }


def _five_dimension_review():
    return {
        dimension: {
            "score": 4,
            "evidence": [f"{dimension.value} 有逐场证据。"],
            "directed_revision": [f"定向修正 {dimension.value}。"],
        }
        for dimension in ReviewDimension
    }


def test_independent_script_review_is_json_document_and_stays_advisory():
    report = build_script_review_court(
        script_version="e002-v4",
        generator_run_id="writer-run-4",
        reviewer_run_id="review-run-9",
        reviewer_source="independent-codex-review",
        dimension_reviews=_five_dimension_review(),
    )
    document = review_document(report)

    assert document["schema"] == "aifos.story-review/v1"
    assert document["kind"] == "review"
    assert document["production_blocking"] is False
    assert document["generator_run_id"] != document["reviewer_run_id"]
    assert len(document["dimensions"]) == 5
    json.dumps(document, ensure_ascii=False)


def test_cross_episode_input_is_derived_from_saved_exit_facts_only():
    previous_script = {
        "story_background": {
            "narrative": {
                "continuity_hooks": ["门外人影是谁", "密诏落款来源"],
            },
        },
        "scenes": [{
            "scene_no": 2,
            "location": "东宫书房",
            "director_logic": {
                "exit_state": "门半开，倒地烛台仍在燃烧。",
            },
        }],
    }
    previous_storyboard = {"shots": [{
        "shot_no": 12,
        "scene_no": 2,
        "end_state": {
            "林昭": {
                "pose": "贴墙站立",
                "position": "门内右侧",
                "injury": "右臂受伤",
                "wardrobe": "青色常服沾血",
                "headwear": {"presence": "none"},
            },
        },
        "frame_props": [{
            "prop_id": "secret-edict",
            "name": "密诏",
            "phase": "end",
            "holder": "林昭左手",
            "location": "门内右侧",
            "physical_state": "展开",
        }],
    }]}
    original_script = copy.deepcopy(previous_script)
    original_storyboard = copy.deepcopy(previous_storyboard)

    continuity = derive_episode_continuity_input(
        previous_episode_id="e001",
        previous_script=previous_script,
        previous_storyboard=previous_storyboard,
    )

    assert continuity.production_blocking is False
    assert continuity.previous_exit_state.startswith("前集分镜第12镜结尾")
    assert continuity.unresolved_hooks == (
        "门外人影是谁", "密诏落款来源")
    assert {item.domain for item in continuity.states} == {
        ContinuityDomain.CHARACTER,
        ContinuityDomain.PROP,
        ContinuityDomain.WARDROBE,
        ContinuityDomain.SCENE,
    }
    assert any(
        item.entity_id == "密诏" and "林昭左手" in item.state
        for item in continuity.states)
    assert previous_script == original_script
    assert previous_storyboard == original_storyboard


def test_missing_previous_exit_returns_review_text_instead_of_blocking():
    continuity = derive_episode_continuity_input(
        previous_episode_id="e001",
        previous_script={},
        previous_storyboard={},
    )

    assert continuity.production_blocking is False
    assert "请编剧人工核对" in continuity.previous_exit_state
    assert continuity.states == ()


def test_formal_storyboard_builds_director_summary_and_view_only_grid():
    storyboard = {
        "version": 7,
        "shots": [_formal_shot(1), _formal_shot(2, duration=6.0)],
    }
    original = copy.deepcopy(storyboard)
    documents = build_storyboard_review_documents(
        episode_id="e002",
        storyboard=storyboard,
        keyframes=[
            {"shot_no": 1, "uri": "/assets/shot-001.png"},
            {"shot_no": 2, "uri": "/assets/shot-002.png"},
        ],
        storyboard_version=7,
    )

    director = documents["director_review"]
    assert director["production_blocking"] is False
    assert director["shot_count"] == 2
    assert director["total_duration_seconds"] == 11.5
    assert director["shot_scale_distribution"] == [["中景", 2]]
    assert director["camera_movement_distribution"] == [["缓慢推进", 2]]
    assert director["shot_function_distribution"] == [["揭示信息", 2]]
    assert director["input_completeness"]["advice"] == []

    browser = documents["nine_grid_browser"]
    assert browser["production_blocking"] is False
    assert browser["view_only"] is True
    assert browser["reference_chain_eligible"] is False
    assert browser["generates_reference_asset"] is False
    assert browser["single_image_multi_panel"] is False
    assert browser["render_mode"] == "independent_shot_cells"
    assert browser["page_count"] == 1
    cells = browser["pages"][0]["cells"]
    assert [cell["shot_no"] for cell in cells] == [1, 2]
    assert [cell["keyframe_status"] for cell in cells] == ["ready", "ready"]
    assert cells[0]["script_reference"] == "林昭展开密诏，确认落款。"
    assert storyboard == original

    serialized = json.dumps(documents, ensure_ascii=False)
    assert "reference_images" not in serialized
    assert "reference_manifest" not in serialized
    assert "reference_assets" not in serialized
    assert "composite_uri" not in serialized


def test_review_documents_can_be_saved_as_real_project_documents(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("审片接入测试")
        episode, _ = app.projects.get_or_create_episode(
            project["id"], 2, premise="承接密诏悬念")
        documents = build_storyboard_review_documents(
            episode_id=str(episode["id"]),
            storyboard={"shots": [_formal_shot(1)]},
            keyframes={1: "/assets/shot-001.png"},
            storyboard_version=3,
        )

        director_version = app.projects.save_document(
            episode["id"], "episode_director_review",
            documents["director_review"])
        grid_version = app.projects.save_document(
            episode["id"], "nine_grid_browser",
            documents["nine_grid_browser"])

        saved_director, _ = app.projects.latest_document(
            episode["id"], "episode_director_review")
        saved_grid, _ = app.projects.latest_document(
            episode["id"], "nine_grid_browser")
        assert (director_version, grid_version) == (1, 1)
        assert saved_director["kind"] == "review"
        assert saved_grid["view_only"] is True
        assert saved_grid["reference_chain_eligible"] is False
    finally:
        app.close()
