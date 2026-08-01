"""历史失败经验必须变成相关、可执行且非阻断的质量规则。"""

from aifos.generation_quality import (
    MAX_CONTEXT_RULES,
    QUALITY_CATEGORIES,
    PostGenerationIssue,
    evaluate_post_generation,
    infer_quality_categories,
    preflight_episode_contracts,
    preflight_shot_contract,
    quality_rule_lines,
    select_quality_rules,
)


EXPECTED_CATEGORIES = {
    "identity", "gender_age", "count", "wardrobe", "era", "text",
    "camera_contract", "spatial_logic", "prop_physics", "lighting",
    "video_state_chain", "video_camera_motion",
    "video_identity_continuity", "video_prop_state",
    "reference_budget", "audio_lipsync", "technical_provider",
    "technical_encoding",
}


def test_catalog_covers_every_required_failure_category_once():
    assert set(QUALITY_CATEGORIES) == EXPECTED_CATEGORIES
    assert len(QUALITY_CATEGORIES) == len(EXPECTED_CATEGORIES)


def test_context_selection_is_capped_at_five_and_lines_are_short_direct():
    rules = select_quality_rules(
        categories=QUALITY_CATEGORIES,
        context="人物在明代书房拿道具，镜头运镜并说对白",
        modality="video",
        historical_failures={category: 100 for category in QUALITY_CATEGORIES},
        limit=999,
    )

    assert len(rules) == MAX_CONTEXT_RULES == 5
    assert all(len(rule.instruction) <= 40 for rule in rules)
    assert all("此前" not in rule.instruction for rule in rules)


def test_irrelevant_historical_failures_cannot_pollute_current_shot():
    rules = select_quality_rules(
        categories=["text"],
        context="屏幕显示合同给定原文",
        modality="image",
        historical_failures={"wardrobe": 999, "lighting": 999},
    )

    assert [rule.category for rule in rules] == ["text"]


def test_history_alone_cannot_activate_unrelated_rules():
    assert select_quality_rules(
        context="空镜，无人物无文字",
        modality="image",
        historical_failures={"identity": 999},
    ) == ()


def test_context_can_infer_only_matching_image_categories():
    categories = infer_quality_categories(
        "明代夜晚书房，人物手持书册，主光来自右侧",
        modality="image",
    )

    assert {"era", "identity", "prop_physics", "lighting"} <= set(categories)
    assert "audio_lipsync" not in categories
    assert "technical_encoding" not in categories


def test_quality_rule_lines_are_directly_injectable_without_metadata():
    lines = quality_rule_lines(
        categories=["identity", "count"], modality="image")

    assert lines == (
        "人脸、发型和妆造严格沿用已锁定人物母图。",
        "画面人数严格等于镜头合同，不增人、不复制人。",
    )


def test_preflight_detects_each_mutually_exclusive_camera_dimension():
    issues = preflight_shot_contract({
        "shot_no": 5,
        "camera": {
            "shot_scale": ["特写", "全景"],
            "angle": ["俯拍", "仰拍"],
            "lens": ["广角", "长焦"],
            "movement": ["固定镜头", "跟拍"],
        },
    })

    assert {issue.code for issue in issues} == {
        "camera_contract.shot_scale_conflict",
        "camera_contract.angle_conflict",
        "camera_contract.lens_conflict",
        "camera_contract.movement_conflict",
    }
    assert all(issue.shot_id == "5" and issue.blocking for issue in issues)


def test_preflight_detects_camera_conflict_inside_prompt_text():
    issues = preflight_shot_contract({
        "shot_id": "s01",
        "prompt": "固定镜头跟拍人物，从特写切成全景。",
    })

    assert {issue.code for issue in issues} == {
        "camera_contract.shot_scale_conflict",
        "camera_contract.movement_conflict",
    }


def test_preflight_detects_visible_people_count_mismatch():
    issues = preflight_shot_contract({
        "shot_no": 2,
        "expected_people_count": 1,
        "visible_characters": ["乔安", "路人"],
    })

    assert [issue.code for issue in issues] == [
        "count.visible_characters_mismatch"]


def test_preflight_detects_modern_prop_in_ancient_scene():
    issues = preflight_shot_contract({
        "shot_no": 3,
        "era": "明代",
        "props": ["木桌", "笔记本电脑"],
    })

    assert [issue.code for issue in issues] == [
        "era.modern_prop_in_ancient_scene"]


def test_explicit_cross_era_story_basis_allows_modern_prop():
    assert preflight_shot_contract({
        "shot_no": 3,
        "era": "明代",
        "props": ["笔记本电脑"],
        "allow_anachronism": True,
    }) == ()


def test_sanctioned_cross_era_item_does_not_waive_other_modern_props():
    issues = preflight_shot_contract({
        "shot_no": 3,
        "era_context": "明代京城",
        "props": ["主角随身智能手机", "无剧情依据的笔记本电脑"],
        "sanctioned_anachronisms": ["主角随身的智能手机"],
    })

    assert [issue.code for issue in issues] == [
        "era.modern_prop_in_ancient_scene"]
    assert "笔记本电脑" in issues[0].message
    assert "智能手机" not in issues[0].message


def test_declared_spatial_and_physical_contracts_must_be_present():
    issues = preflight_shot_contract({
        "shot_no": 12,
        "spatial_required": True,
        "spatial_blocking": {"actors": [{"name": "甲"}]},
        "physical_contract_required": True,
        "physical_contract": {"rules": []},
    })

    assert {issue.code for issue in issues} == {
        "spatial.required_contract_missing",
        "physics.required_contract_missing",
    }


def test_moving_carriage_contract_requires_complete_power_chain():
    issues = preflight_shot_contract({
        "shot_no": 13,
        "description": "车夫赶着马车疾驰入城，车轮滚动",
        "physical_contract_required": True,
        "physical_contract": {
            "rules": ["道具服从重力"],
            "objects": ["马车车厢"],
        },
    })

    assert [issue.code for issue in issues] == [
        "physics.carriage_power_chain_missing"]


def test_moving_carriage_without_live_horse_is_contract_conflict():
    issues = preflight_shot_contract({
        "shot_no": 14,
        "description": "马已经死了，无马的马车仍然疾驰",
        "physical_contract_required": True,
        "physical_contract": {
            "rules": ["道具服从重力"],
            "objects": ["移动马车：马匹↔挽具/辕杆↔车体↔车夫缰绳"],
        },
    })

    assert [issue.code for issue in issues] == [
        "physics.carriage_power_conflict"]


def test_reference_must_have_one_non_conflicting_responsibility():
    issues = preflight_shot_contract({
        "shot_no": 4,
        "references": [{
            "label": "乔安母图",
            "responsibilities": ["identity", "wardrobe"],
            "include": ["黑色长发", "银耳环"],
            "exclude": ["银耳环"],
        }],
    })

    assert {issue.code for issue in issues} == {
        "reference.multiple_responsibilities",
        "reference.include_exclude_conflict",
    }


def test_two_primary_anchors_for_same_target_are_a_conflict():
    issues = preflight_shot_contract({
        "shot_no": 4,
        "references": [
            {"asset_id": 1, "role": "identity", "target": "乔安", "primary": True},
            {"asset_id": 2, "role": "identity", "target": "乔安", "primary": True},
        ],
    })

    assert any(issue.code == "reference.competing_primary_anchors"
               for issue in issues)


def test_seedance_20_and_25_reference_budgets_are_versioned():
    old = preflight_shot_contract({
        "shot_no": 6,
        "model": "Seedance 2.0",
        "total_reference_count": 10,
        "asset_reference_count": 8,
    })
    new_ok = preflight_shot_contract({
        "shot_no": 7,
        "model": "Seedance 2.5",
        "total_reference_count": 50,
        "asset_reference_count": 40,
    })
    new_bad = preflight_shot_contract({
        "shot_no": 8,
        "model": "Seedance 2.5",
        "total_reference_count": 51,
        "asset_reference_count": 41,
    })

    assert {issue.code for issue in old} == {
        "reference.total_budget_exceeded", "reference.asset_budget_exceeded"}
    assert new_ok == ()
    assert {issue.code for issue in new_bad} == {
        "reference.total_budget_exceeded", "reference.asset_budget_exceeded"}


def test_single_action_cannot_hide_then_or_two_phases():
    for prompt in (
        "人物抬手，然后转身离开。",
        "The actor raises the cup, then walks away.",
        "第一阶段开门，第二阶段坐下。",
    ):
        issues = preflight_shot_contract({
            "shot_no": 9, "single_action": True, "prompt": prompt})
        assert any(
            issue.code == "motion.single_action_has_multiple_phases"
            for issue in issues)


def test_complex_action_requires_at_least_three_states():
    bad = preflight_shot_contract({
        "shot_no": 10,
        "complex_action": True,
        "states": ["起始：手未接触杯子", "结束：杯子在手中"],
    })
    good = preflight_shot_contract({
        "shot_no": 10,
        "complex_action": True,
        "states": ["起始：手未接触", "过渡：手接触杯柄", "结束：杯子离桌"],
    })

    assert any(
        issue.code == "motion.complex_action_missing_three_states"
        for issue in bad)
    assert good == ()


def test_episode_preflight_only_returns_bad_shots_and_keeps_others_independent():
    result = preflight_episode_contracts([
        {"shot_no": 1, "camera": {"shot_scale": "中景"}},
        {"shot_no": 2, "expected_people_count": 1,
         "visible_characters": ["甲", "乙"]},
        {"shot_no": 3, "single_action": True,
         "prompt": "抬手，然后转身"},
    ])

    assert set(result) == {"2", "3"}
    assert "1" not in result


def test_content_failure_is_advisory_and_never_retries():
    decision = evaluate_post_generation([
        {"category": "identity", "message": "人脸略有漂移"},
        {"category": "prop_physics", "message": "杯子漂浮"},
    ], attempts_remaining=3)

    assert len(decision.advisory_issues) == 2
    assert decision.technical_errors == ()
    assert decision.retry_allowed is False
    assert decision.retry_reason == "只有内容建议，不自动重试。"


def test_only_provider_or_encoding_errors_can_retry():
    decision = evaluate_post_generation([
        PostGenerationIssue("technical_provider", "供应商超时"),
        {"category": "technical_encoding", "message": "视频无法解码"},
        {"category": "lighting", "message": "光比不理想"},
    ], attempts_remaining=1)

    assert len(decision.technical_errors) == 2
    assert len(decision.advisory_issues) == 1
    assert decision.retry_allowed is True


def test_unknown_post_issue_fails_safe_as_advisory():
    decision = evaluate_post_generation([
        {"category": "looks_bad", "message": "审美不喜欢"},
    ], attempts_remaining=9)

    assert decision.retry_allowed is False
    assert decision.advisory_issues[0].category == "content_unknown"


def test_technical_error_cannot_retry_after_budget_is_exhausted():
    decision = evaluate_post_generation([
        {"category": "technical_provider", "message": "限流"},
    ], attempts_remaining=0)

    assert decision.retry_allowed is False
    assert "次数已用完" in decision.retry_reason


def test_invalid_contract_returns_one_local_issue_instead_of_raising():
    issues = preflight_shot_contract("not-a-contract")

    assert len(issues) == 1
    assert issues[0].code == "contract.invalid"
