"""AI 编剧/导演纯策略只给建议，不得重新变成生产闸门。"""

from dataclasses import FrozenInstanceError

import pytest

from aifos.story_intelligence import (
    MAX_GRID_CELLS,
    ContinuityDomain,
    ReviewDimension,
    build_draft_fusion_decision,
    build_episode_continuity_input,
    build_nine_grid_pages,
    build_script_review_court,
    summarize_episode_directing,
)


def _five_dimensions(**updates):
    values = {
        ReviewDimension.CAUSAL_CHAIN: {
            "score": 4,
            "evidence": ["主角查史料后触发穿越。"],
            "directed_revision": ["补明触发物的前置伏笔。"],
        },
        ReviewDimension.CONFLICT_DENSITY: {
            "score": 3,
            "evidence": ["中段连续三镜只有查阅动作。"],
            "directed_revision": ["在中段加入身份暴露风险。"],
        },
        ReviewDimension.CHARACTER_ARC: {
            "score": 4,
            "evidence": ["主角从犹豫转为主动救人。"],
            "directed_revision": ["补一次选择代价。"],
        },
        ReviewDimension.DIALOGUE_QUALITY: {
            "score": 5,
            "evidence": ["台词短且有人物身份差异。"],
            "directed_revision": ["保留现有口吻。"],
        },
        ReviewDimension.HOOK_STRENGTH: {
            "score": 4,
            "evidence": ["结尾暴露密诏。"],
            "directed_revision": ["把密诏后果落到下一集。"],
        },
    }
    values.update(updates)
    return values


def _review(**updates):
    facts = {
        "script_version": "e001-v3",
        "generator_run_id": "script-run-3",
        "reviewer_run_id": "review-run-1",
        "reviewer_source": "independent-codex-review",
        "dimension_reviews": _five_dimensions(),
    }
    facts.update(updates)
    return build_script_review_court(**facts)


def test_independent_script_court_has_exact_five_dimensions_and_evidence():
    review = _review()

    assert [item.dimension for item in review.dimensions] == list(
        ReviewDimension)
    assert all(item.evidence for item in review.dimensions)
    assert all(item.directed_revision for item in review.dimensions)
    assert "冲突密度：在中段加入身份暴露风险。" in review.advice
    assert "台词质感：保留现有口吻。" not in review.advice


def test_generator_cannot_use_same_run_as_independent_reviewer():
    with pytest.raises(ValueError, match="不能自报评分"):
        _review(reviewer_run_id="script-run-3")


@pytest.mark.parametrize("source", [
    "generator", "self", "self_report", "生成器", "自评",
])
def test_generator_self_report_source_is_rejected(source):
    with pytest.raises(ValueError, match="独立评审来源"):
        _review(reviewer_source=source)


def test_script_court_requires_all_five_dimensions():
    dimensions = _five_dimensions()
    dimensions.pop(ReviewDimension.HOOK_STRENGTH)

    with pytest.raises(ValueError, match="钩子强度"):
        _review(dimension_reviews=dimensions)


def test_each_review_dimension_requires_evidence_and_directed_revision():
    dimensions = _five_dimensions()
    dimensions[ReviewDimension.CAUSAL_CHAIN] = {
        "score": 2,
        "evidence": [],
        "directed_revision": ["修正因果。"],
    }
    with pytest.raises(ValueError, match="因果链证据"):
        _review(dimension_reviews=dimensions)


def test_low_creative_score_is_advice_not_production_block():
    dimensions = _five_dimensions()
    dimensions[ReviewDimension.CAUSAL_CHAIN]["score"] = 1
    review = _review(dimension_reviews=dimensions)

    assert review.kind == "review"
    assert review.production_blocking is False
    assert review.advice


def test_review_contract_is_immutable():
    with pytest.raises(FrozenInstanceError):
        _review().production_blocking = True


def _draft_sources():
    return [
        {
            "source_id": "draft-a",
            "engine": "claude",
            "document_ref": "script/a/v2",
            "generator_run_id": "run-a",
        },
        {
            "source_id": "draft-b",
            "engine": "codex",
            "document_ref": "script/b/v1",
            "generator_run_id": "run-b",
        },
    ]


def _fusion(**updates):
    facts = {
        "decision_id": "fusion-e001-v1",
        "sources": _draft_sources(),
        "preferred_source_id": "draft-a",
        "output_document_ref": "script/fused/v1",
        "contributions": [
            {
                "source_id": "draft-a",
                "aspect": "因果链",
                "retained_value": "保留穿越触发链。",
                "reason": "前后动作因果完整。",
            },
            {
                "source_id": "draft-b",
                "aspect": "结尾钩子",
                "retained_value": "保留密诏揭示。",
                "reason": "下集驱动力更强。",
            },
        ],
        "fusion_reasons": ["A稿作骨架，B稿增强结尾。"],
    }
    facts.update(updates)
    return build_draft_fusion_decision(**facts)


def test_dual_draft_decision_preserves_sources_and_fusion_reasons():
    decision = _fusion()

    assert [source.engine for source in decision.sources] == [
        "claude", "codex"]
    assert {item.source_id for item in decision.contributions} == {
        "draft-a", "draft-b"}
    assert decision.fusion_reasons == ("A稿作骨架，B稿增强结尾。",)
    assert decision.kind == "review"
    assert decision.production_blocking is False


def test_dual_draft_contract_rejects_one_or_three_sources():
    with pytest.raises(ValueError, match="两个来源"):
        _fusion(sources=_draft_sources()[:1])
    with pytest.raises(ValueError, match="两个来源"):
        _fusion(sources=_draft_sources() + [_draft_sources()[0]])


def test_fusion_must_name_each_sources_retained_strength():
    with pytest.raises(ValueError, match="两个来源各自"):
        _fusion(contributions=[_fusion().contributions[0]])


def test_continuity_input_carries_exit_hooks_and_four_state_domains():
    continuity = build_episode_continuity_input(
        previous_episode_id="e001",
        previous_exit_state="主角已拿到密诏，站在东宫书案前。",
        unresolved_hooks=["密诏落款是谁", "门外脚步声是谁"],
        character_states={
            "林昭": {"state": "右臂受伤", "evidence": "第13镜"},
        },
        prop_states={"密诏": "在林昭左手"},
        wardrobe_states={"林昭": "青色常服沾血"},
        scene_states={"东宫书房": "烛台倒地，门半开"},
    )

    assert continuity.previous_exit_state.startswith("主角已拿到")
    assert continuity.unresolved_hooks == ("密诏落款是谁", "门外脚步声是谁")
    assert {state.domain for state in continuity.states} == set(
        ContinuityDomain)
    assert continuity.instructions == (
        "承接前集出口，不擅自重置。",
        "未回收钩子按剧情推进。",
        "人物、道具、服装、场景状态须连续。",
    )
    assert continuity.production_blocking is False


def test_empty_optional_continuity_facts_remain_nonblocking():
    continuity = build_episode_continuity_input(
        previous_episode_id="e001",
        previous_exit_state="上一集黑场。",
    )

    assert continuity.unresolved_hooks == ()
    assert continuity.states == ()
    assert continuity.production_blocking is False


def _shot(number, scene=1, scale="中景", movement="固定", **updates):
    result = {
        "shot_no": number,
        "scene_no": scene,
        "shot_scale": scale,
        "camera_movement": movement,
        "dialogue": f"第{number}镜台词",
        "beat": "0-2秒动作",
        "lighting": "暖烛光",
        "dramatic_function": "逼近真相",
        "keyframe_uri": f"asset://shot-{number}.png",
    }
    result.update(updates)
    return result


def test_episode_director_summary_counts_scales_moves_and_scene_functions():
    plan = summarize_episode_directing(
        episode_id="e001",
        shots=[
            _shot(1, scale="全景", movement="推进"),
            _shot(2, scale="中景", movement="固定"),
            _shot(3, scene=2, scale="中景", movement="固定",
                  dramatic_function="揭示密诏"),
        ],
    )

    assert plan.shot_count == 3
    assert plan.shot_scale_distribution == (("中景", 2), ("全景", 1))
    assert plan.camera_movement_distribution == (("固定", 2), ("推进", 1))
    assert [(scene.scene_no, scene.dramatic_function) for scene in plan.scenes] == [
        (1, "逼近真相"), (2, "揭示密诏")]
    assert plan.production_blocking is False


def test_episode_director_marks_adjacent_duplicate_as_advice_only():
    plan = summarize_episode_directing(
        episode_id="e001",
        shots=[_shot(1), _shot(2), _shot(3, movement="推进")],
    )

    assert len(plan.adjacent_repetitions) == 1
    repeated = plan.adjacent_repetitions[0]
    assert (repeated.previous_shot_no, repeated.current_shot_no) == (1, 2)
    assert "确认重复服务情绪" in repeated.advice
    assert plan.production_blocking is False


def test_episode_director_reports_missing_dialogue_beat_lighting_inputs():
    shot = _shot(7)
    shot.update({"dialogue": "", "beat": "", "lighting": ""})
    plan = summarize_episode_directing(episode_id="e001", shots=[shot])

    completeness = plan.input_completeness
    assert completeness.dialogue_missing == (7,)
    assert completeness.beat_missing == (7,)
    assert completeness.lighting_missing == (7,)
    assert completeness.advice == (
        "补齐镜头台词输入。",
        "补齐动作节拍输入。",
        "补齐光影输入。",
    )
    assert plan.production_blocking is False


def test_complete_director_inputs_still_return_review_not_pass_gate():
    plan = summarize_episode_directing(
        episode_id="e001", shots=[_shot(1, scale="全景")])

    assert plan.kind == "review"
    assert plan.production_blocking is False
    assert plan.advice == ("整集导演输入已具备，可继续人工审片。",)


def test_nine_grid_groups_by_scene_and_splits_after_nine_shots():
    shots = [_shot(number, scene=1) for number in range(1, 12)]
    shots += [_shot(20, scene=2)]
    pages = build_nine_grid_pages(shots)

    assert [(page.scene_no, page.page_no, len(page.cells)) for page in pages] == [
        (1, 1, 9), (1, 2, 2), (2, 1, 1)]
    assert all(len(page.cells) <= MAX_GRID_CELLS for page in pages)
    assert all(
        len({cell.scene_no for cell in page.cells}) == 1 for page in pages)


def test_nine_grid_cell_is_one_independent_shot_not_multiframe_asset():
    pages = build_nine_grid_pages([_shot(1), _shot(2)])
    page = pages[0]

    assert [cell.shot_no for cell in page.cells] == [1, 2]
    assert page.render_mode == "independent_shot_cells"
    assert page.generates_reference_asset is False
    assert page.single_image_multi_panel is False
    assert page.production_blocking is False
    assert page.advice == ("每格只看一镜。", "只审节奏，不作生成参考图。")


def test_all_public_reports_are_nonblocking_reviews():
    reports = [
        _review(),
        _fusion(),
        build_episode_continuity_input(
            previous_episode_id="e001", previous_exit_state="黑场。"),
        summarize_episode_directing(
            episode_id="e001", shots=[_shot(1)]),
        build_nine_grid_pages([_shot(1)])[0],
    ]

    assert all(report.kind == "review" for report in reports)
    assert all(report.production_blocking is False for report in reports)

