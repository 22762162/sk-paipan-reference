"""Two-stage reference selection keeps hard assets and audits soft choices."""

from aifos.reference_selection import (
    REFERENCE_VISUAL_RESULT_SCHEMA,
    build_reference_selection_decision,
    build_reference_selection_request,
    materialize_reference_upload_manifest,
    reference_selection_is_current,
)


def _pool():
    return [
        {
            "asset_id": 1, "uri": "/hero.png", "role": "identity",
            "character": "虞寻歌", "binding": "只锁人物身份",
        },
        {
            "asset_id": 2, "uri": "/phone.png", "role": "prop",
            "label": "核心道具手机", "binding": "只锁手机结构",
        },
        {
            "asset_id": 3, "uri": "/space.png", "role": "spatial",
            "binding": "只锁空间和机位",
        },
        {
            "asset_id": 4, "uri": "/modern-end.png",
            "role": "continuity", "phase": "end", "era": "现代",
            "scene_id": "hotel", "camera_id": "cam-a",
            "relevance_score": 2,
        },
        {
            "asset_id": 5, "uri": "/modern-scene.png", "role": "scene",
            "era": "现代", "scene_id": "hotel", "relevance_score": 10,
        },
        {
            "asset_id": 6, "uri": "/ming-recent.png",
            "role": "continuity", "phase": "end", "era": "明代",
            "scene_id": "palace", "recency_score": 999,
        },
        {
            "asset_id": 7, "uri": "/modern-start.png",
            "role": "continuity", "phase": "start", "era": "现代",
            "scene_id": "hotel", "recency_score": 5,
        },
    ]


def _target():
    return {
        "visible_characters": ["虞寻歌"],
        "frame_phase": "end",
        "scene_id": "hotel",
        "camera_id": "cam-a",
        "era_context": "现代",
    }


def test_mandatory_identity_core_prop_and_spatial_are_never_soft_ranked_out():
    decision = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target())

    assert decision["status"] == "ready"
    assert decision["selected_asset_ids"][:3] == [1, 2, 3]
    assert [row["role"] for row in decision["pool"][:3]] == [
        "identity", "prop", "spatial"]
    assert all(row["mandatory"] for row in decision["pool"][:3])
    assert decision["budget"]["mandatory_count"] == 3
    assert decision["budget"]["optional_slots"] == 1
    assert [row["image_index"] for row in decision["bindings"]] == [1, 2, 3, 4]


def test_mandatory_budget_overflow_is_explicit_instead_of_deleting_assets():
    decision = build_reference_selection_decision(
        _pool()[:3], max_references=2, target_facts=_target())

    assert decision["status"] == "mandatory_budget_overflow"
    assert decision["ready"] is False
    assert decision["selected_asset_ids"] == [1, 2, 3]
    assert decision["budget"]["mandatory_overflow"] == 1


def test_wrong_era_and_wrong_frame_phase_lose_before_recency_is_considered():
    decision = build_reference_selection_decision(
        _pool(), max_references=5, target_facts=_target())
    by_id = {row["asset_id"]: row for row in decision["pool"]}

    assert by_id[6]["selected"] is False
    assert by_id[6]["reject_reason"] == "era_mismatch"
    assert by_id[7]["selected"] is False
    assert by_id[7]["reject_reason"] == "phase_mismatch"
    assert set(decision["selected_asset_ids"]) == {1, 2, 3, 4, 5}


def test_visual_selector_can_reorder_only_the_frozen_optional_shortlist():
    baseline = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target())
    request = build_reference_selection_request(baseline)
    assert {row["asset_id"] for row in request["references"]} == {4, 5}

    visual_result = {
        "schema": REFERENCE_VISUAL_RESULT_SCHEMA,
        "input_hash": baseline["input_hash"],
        "ranked_selection_keys": ["asset:4", "asset:5"],
        "scores": {"asset:4": 98, "asset:5": 60},
        "reasons": {"asset:4": "人物站位与本镜终点一致"},
    }
    decision = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target(),
        visual_result=visual_result, model="visual-test")

    assert decision["selector_validation"]["status"] == "accepted"
    assert decision["fallback_used"] is False
    assert decision["selected_asset_ids"] == [1, 2, 3, 4]
    selected = next(row for row in decision["pool"] if row["asset_id"] == 4)
    assert selected["visual_match_score"] == 98
    assert selected["reason"] == "人物站位与本镜终点一致"


def test_visual_selector_cannot_rank_a_mandatory_or_rejected_reference():
    baseline = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target())
    malicious = {
        "schema": REFERENCE_VISUAL_RESULT_SCHEMA,
        "input_hash": baseline["input_hash"],
        # identity is mandatory and the Ming image was rejected before this
        # stage; neither belongs to the optional shortlist.
        "ranked_selection_keys": ["asset:1", "asset:6"],
        "scores": {"asset:1": 100, "asset:6": 100},
    }
    decision = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target(),
        visual_result=malicious)

    assert decision["selector_validation"]["status"] == "rejected"
    assert decision["selector_validation"]["reason"] == \
        "unknown_visual_reference"
    assert decision["fallback_used"] is True
    assert decision["selected_asset_ids"][:3] == [1, 2, 3]


def test_stale_visual_result_falls_back_deterministically_and_is_audited():
    baseline = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target())
    stale = {
        "schema": REFERENCE_VISUAL_RESULT_SCHEMA,
        "input_hash": "old-reference-input",
        "ranked_selection_keys": ["asset:4"],
        "scores": {"asset:4": 100},
    }
    first = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target(),
        visual_result=stale)
    second = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target(),
        visual_result=stale)

    assert first["selector_validation"] == {
        "status": "rejected", "reason": "stale_visual_result",
        "stale": True,
    }
    assert first["selected_asset_ids"] == second["selected_asset_ids"]
    assert first["selection_hash"] == second["selection_hash"]
    assert reference_selection_is_current(
        first, expected_input_hash=baseline["input_hash"],
        expected_selection_hash=first["selection_hash"])
    assert not reference_selection_is_current(
        first, expected_input_hash="new-input")


def test_duplicate_optional_content_is_recorded_not_submitted_twice():
    pool = _pool()[:3] + [
        {
            "asset_id": 20, "uri": "/scene-a.png", "role": "scene",
            "content_hash": "same-scene", "relevance_score": 10,
        },
        {
            "asset_id": 21, "uri": "/scene-a-copy.png", "role": "scene",
            "content_hash": "same-scene", "relevance_score": 99,
        },
    ]
    decision = build_reference_selection_decision(
        pool, max_references=5, target_facts=_target())
    by_id = {row["asset_id"]: row for row in decision["pool"]}

    assert by_id[20]["selected"] is True
    assert by_id[21]["selected"] is False
    assert by_id[21]["reject_reason"] == "redundant_optional"


def test_upload_manifest_follows_frozen_binding_order_and_instructions():
    baseline = build_reference_selection_decision(
        _pool(), max_references=5, target_facts=_target())
    decision = build_reference_selection_decision(
        _pool(), max_references=5, target_facts=_target(),
        visual_result={
            "schema": REFERENCE_VISUAL_RESULT_SCHEMA,
            "input_hash": baseline["input_hash"],
            "ranked_selection_keys": ["asset:5", "asset:4"],
            "scores": {"asset:5": 96, "asset:4": 88},
        })

    # A valid visual comparison puts scene asset 5 before continuity asset 4
    # even though the original pool contains 4 first.  Upload order must
    # follow the frozen bindings, not a set-filtered walk over the pool.
    assert decision["selected_asset_ids"] == [1, 2, 3, 5, 4]
    manifest = materialize_reference_upload_manifest(_pool(), decision)

    assert [row["asset_id"] for row in manifest] == [1, 2, 3, 5, 4]
    assert [row["index"] for row in manifest] == [1, 2, 3, 4, 5]
    assert [row["image_index"] for row in manifest] == [1, 2, 3, 4, 5]
    assert [row["uri"] for row in manifest] == [
        binding["uri"] for binding in decision["bindings"]]
    assert [row["binding"] for row in manifest] == [
        binding["instruction"] for binding in decision["bindings"]]


def test_upload_manifest_rejects_tampered_binding_position():
    decision = build_reference_selection_decision(
        _pool(), max_references=4, target_facts=_target())
    decision["bindings"][0]["image_index"] = 2

    try:
        materialize_reference_upload_manifest(_pool(), decision)
    except ValueError as exc:
        assert "image_index" in str(exc)
    else:
        raise AssertionError("篡改后的 image_index 不得静默进入上传清单")
