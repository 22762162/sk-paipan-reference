"""制作标准中心：不可变版本、激活指针、校验与交换包。"""

import copy
import sqlite3

import pytest

from aifos.app import App
from aifos.standard_center import (
    DEFAULT_STANDARD,
    REQUIRED_GATE_IDS,
    StandardConflictError,
    StandardValidationError,
)


def test_default_standard_is_complete_and_active(tmp_path):
    app = App(tmp_path / "ws")
    try:
        active = app.standards.active()
        assert active["active"] is True
        assert active["profile_key"] == "sk-manju-v5"
        assert active["version"] == 1
        assert len(app.standards.history("sk-manju-v5")) == 1

        content = active["content"]
        assert content["source_skill"]["id"] == "sk-manju-storyboard-skill"
        rules = content["rules"]
        production = rules["production"]
        story_analysis = rules["story_analysis"]
        assert story_analysis["required_before_images"] is True
        assert story_analysis["auto_analyze_uploaded_script"] is True
        assert story_analysis["user_style_is_hard_constraint"] is True
        assert story_analysis[
            "resolve_character_entities_before_images"] is True
        assert story_analysis["performance_cues_are_not_characters"] is True
        assert story_analysis[
            "final_character_image_prompt_required"] is True
        assert story_analysis["compact_prompt_compilation"] is True
        contract = production["prompt_contract"]
        assert contract["schema"] == "aifos.shot-prompt/v1"
        assert contract["single_primary_action"] is True
        assert contract["single_camera_move"] is True
        assert contract["compact_prompt_sent_to_model"] is True
        assert contract["full_prompt_kept_for_audit"] is True
        assert contract["order"][:4] == [
            "subject", "scene", "start", "single_action"]
        assert story_analysis["downstream_consumers"] == [
            "character", "scene", "storyboard", "keyframe", "seedance",
        ]
        assert production["video_model"] == "seedance2.0fast_vip"
        assert production["resolution"] == "720p"
        assert production["voice"] == "jimeng_builtin"
        assert production["lip_sync"] is True
        assert production["burn_subtitles"] is False
        assert production["preferred_segment_seconds"] == [5, 8]
        assert production["max_segment_seconds"] == 15
        assert production["time_precision_seconds"] == 0.5
        assert len(rules["storyboard"]["five_dimensions"]) == 5
        assert len(rules["storyboard"]["required_columns"]) == 17
        assert len(rules["storyboard"]["scene_type_words"]) == 8
        candidate_targets = rules["character_assets"]["candidate_targets"]
        assert candidate_targets == {
            "main": 5,
            "important_supporting": 3,
            "non_main": 1,
            "non_main_max": 1,
            "background": 0,
        }
        assert rules["character_assets"]["visual_dna_required"] is True
        assert rules["character_assets"]["cast_dedup_overlap_threshold"] == 2
        assert rules["character_assets"]["canonical_views"] == [
            "face_closeup", "front", "profile", "back"]
        assert rules["character_assets"][
            "turnaround_review_board_only"] is True
        assert rules["character_assets"][
            "seedance_may_redesign_character"] is False
        assert [gate["id"] for gate in rules["quality_gates"]] == REQUIRED_GATE_IDS
        assert app.db.query_one("PRAGMA busy_timeout")[0] == 5000
    finally:
        app.close()


def test_existing_standard_is_upgraded_with_story_analysis_rules(tmp_path):
    app = App(tmp_path / "ws")
    try:
        active = app.standards.active()
        legacy = copy.deepcopy(active)
        legacy["content"]["rules"].pop("story_analysis")
        upgraded = app.standards._upgrade_spatial_standard(legacy)
        assert upgraded["version"] == active["version"] + 1
        assert upgraded["content"]["rules"]["story_analysis"] == (
            DEFAULT_STANDARD["rules"]["story_analysis"])
        assert "剧本分析" in upgraded["change_note"]
        assert len(app.standards.history("sk-manju-v5")) == 2
    finally:
        app.close()


def test_save_creates_immutable_version_and_persists(tmp_path):
    root = tmp_path / "ws"
    app = App(root)
    first = app.standards.active()
    content = copy.deepcopy(first["content"])
    content["name"] = "SK 精品漫剧·高密度版"
    content["description"] = "面向高密度情绪叙事的可调版本。"
    content["rules"]["production"]["preferred_segment_seconds"] = [6, 8]
    second = app.standards.save(
        content, change_note="调整优选分段", expected_active_id=first["version_id"])
    assert second["version"] == 2
    assert second["active"] is True
    assert second["fingerprint"] != first["fingerprint"]
    assert app.standards.get(first["version_id"])["active"] is False
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        app.db.conn.execute(
            "UPDATE production_standard_versions SET name='tampered' WHERE id=?",
            (first["version_id"],))
    app.db.conn.rollback()
    app.close()

    reopened = App(root)
    try:
        active = reopened.standards.active()
        assert active["version_id"] == second["version_id"]
        assert active["content"]["rules"]["production"][
            "preferred_segment_seconds"] == [6, 8]
        assert len(reopened.standards.history("sk-manju-v5")) == 2
    finally:
        reopened.close()


def test_invalid_save_is_atomic_and_returns_structured_issues(tmp_path):
    app = App(tmp_path / "ws")
    try:
        before = app.standards.active()
        bad = copy.deepcopy(before["content"])
        bad["rules"]["production"]["video_model"] = "seedance2.0_vip"
        bad["rules"]["production"]["preferred_segment_seconds"] = [9, 6]
        bad["rules"]["dialogue"]["max_chars_per_shot"] = 40
        bad["rules"]["performance"]["listener_duration_ratio"] = 0.2
        with pytest.raises(StandardValidationError) as caught:
            app.standards.save(bad, change_note="非法修改")
        issues = caught.value.issues
        assert all(set(item) == {"path", "message"} for item in issues)
        paths = {item["path"] for item in issues}
        assert "rules.production.video_model" in paths
        assert "rules.dialogue.max_chars_per_shot" in paths
        assert "rules.performance.listener_duration_ratio" in paths
        assert app.standards.active()["version_id"] == before["version_id"]
        assert len(app.standards.history("sk-manju-v5")) == 1
    finally:
        app.close()


def test_expected_active_id_detects_conflict_atomically(tmp_path):
    app = App(tmp_path / "ws")
    try:
        first = app.standards.active()
        content = copy.deepcopy(first["content"])
        content["description"] = "版本二"
        second = app.standards.save(
            content, expected_active_id=first["version_id"])
        stale = copy.deepcopy(content)
        stale["description"] = "基于旧版本的并发保存"
        with pytest.raises(StandardConflictError) as caught:
            app.standards.save(
                stale, expected_active_id=first["version_id"])
        assert caught.value.actual_active_id == second["version_id"]
        assert len(app.standards.history("sk-manju-v5")) == 2
    finally:
        app.close()


def test_activate_reset_and_import_export(tmp_path):
    source = App(tmp_path / "source")
    try:
        first = source.standards.active()
        custom = copy.deepcopy(first["content"])
        custom["name"] = "导演工作室标准"
        custom["rules"]["dialogue"]["max_chars_per_shot"] = 20
        draft = source.standards.save(
            custom, change_note="先存草稿", activate=False)
        assert source.standards.active()["version_id"] == first["version_id"]
        assert draft["active"] is False

        activated = source.standards.activate(draft["version_id"])
        assert activated["active"] is True
        bundle = source.standards.export_bundle()
        assert bundle["standard"]["fingerprint"] == draft["fingerprint"]

        reset = source.standards.reset("回到厂标")
        assert reset["version"] == 3
        assert reset["content"] == DEFAULT_STANDARD
    finally:
        source.close()

    target = App(tmp_path / "target")
    try:
        imported = target.standards.import_bundle(bundle, change_note="跨工作区导入")
        assert imported["version"] == 2
        assert imported["content"]["name"] == "导演工作室标准"
        assert imported["content"]["rules"]["dialogue"][
            "max_chars_per_shot"] == 20
        exported = target.standards.export_bundle(imported["version_id"])
        assert exported["standard"]["fingerprint"] == imported["fingerprint"]

        tampered = copy.deepcopy(bundle)
        tampered["standard"]["content"]["name"] = "被篡改"
        with pytest.raises(StandardValidationError) as caught:
            target.standards.import_bundle(tampered)
        assert caught.value.issues[0]["path"] == "standard.fingerprint"
        assert len(target.standards.history("sk-manju-v5")) == 2
    finally:
        target.close()
