"""Comparative four-image ranking remains subordinate to technical/QC facts."""

from copy import deepcopy

from aifos.candidate_ranking import (
    CANDIDATE_COMPARISON_RESULT_SCHEMA,
    build_candidate_comparison_request,
    build_candidate_ranking,
    candidate_ranking_is_current,
)


def _candidate(index, *, passed=False, technical=True, score=0, issues=None):
    return {
        "candidate_id": f"wave#candidate-{index}",
        "candidate_index": index,
        "candidate_set_token": "cset-v1:test",
        "input_hash": "generation-input",
        "uri": f"/candidate-{index}.png",
        "passed": passed,
        "score": score,
        "issues": list(issues or []),
        "technical_probe": {"ok": technical},
    }


def _baseline(candidates):
    return build_candidate_ranking(
        candidates,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
    )


def _comparison(baseline, score_by_id, *, winner=None, confidence=0.8,
                issues_by_id=None):
    issues_by_id = issues_by_id or {}
    winner = winner or max(score_by_id, key=score_by_id.get)
    return {
        "schema": CANDIDATE_COMPARISON_RESULT_SCHEMA,
        "candidate_set_token": baseline["candidate_set_token"],
        "target_input_hash": baseline["target_input_hash"],
        "reference_selection_hash": baseline["reference_selection_hash"],
        "ranking_input_hash": baseline["ranking_input_hash"],
        "winner_candidate_id": winner,
        "winner_reason": "空间关系最准确",
        "confidence": confidence,
        "candidates": [{
            "candidate_id": candidate_id,
            "total_score": score,
            "dimension_scores": {
                "identity": min(100, score),
                "spatial": min(100, score),
            },
            "evidence": [{
                "dimension": "spatial",
                "finding": "人物和家具保持正确遮挡",
                "reference_asset_id": "space-1",
            }],
            "fatal_issues": issues_by_id.get(candidate_id, []),
            "soft_issues": [],
        } for candidate_id, score in score_by_id.items()],
    }


def test_technical_bad_candidate_never_enters_comparison_or_wins():
    candidates = [
        _candidate(1, passed=True, technical=False, score=9999),
        _candidate(2, passed=False, technical=True, score=10),
        _candidate(3, passed=False, technical=True, score=20),
    ]
    ranking = _baseline(candidates)
    request = build_candidate_comparison_request(ranking)

    assert ranking["eligible_candidate_ids"] == [
        "wave#candidate-2", "wave#candidate-3"]
    assert {row["candidate_id"] for row in request["candidates"]} == {
        "wave#candidate-2", "wave#candidate-3"}
    assert ranking["winner_candidate_id"] == "wave#candidate-3"
    assert ranking["best_effort"] is True


def test_when_any_candidate_passes_failed_candidates_are_ineligible():
    candidates = [
        _candidate(1, passed=False, score=5000),
        _candidate(2, passed=True, score=2),
        _candidate(3, passed=True, score=3),
        _candidate(4, passed=False, score=9999),
    ]
    baseline = _baseline(candidates)

    assert baseline["eligible_candidate_ids"] == [
        "wave#candidate-2", "wave#candidate-3"]
    assert baseline["winner_candidate_id"] == "wave#candidate-3"
    assert baseline["best_effort"] is False
    assert baseline["requires_repair"] is False


def test_stale_candidate_row_cannot_enter_a_current_comparison():
    stale = _candidate(1, passed=True, score=999)
    stale["candidate_set_token"] = "cset-v1:old"
    current = _candidate(2, passed=True, score=1)
    ranking = _baseline([stale, current])

    assert ranking["eligible_candidate_ids"] == ["wave#candidate-2"]
    assert ranking["winner_candidate_id"] == "wave#candidate-2"
    stale_row = next(row for row in ranking["candidates"]
                     if row["candidate_id"] == "wave#candidate-1")
    assert stale_row["stale"] is True
    assert stale_row["stale_reason"] == "candidate_set_token_mismatch"
    assert ranking["comparison_validation"]["status"] == "not_required"
    assert ranking["ranking_unavailable"] is False


def test_one_multimodal_comparison_selects_between_passed_candidates():
    candidates = [
        _candidate(1, passed=True, score=50),
        _candidate(2, passed=True, score=60),
        _candidate(3, passed=False, score=999),
        _candidate(4, passed=False, score=998),
    ]
    baseline = _baseline(candidates)
    result = _comparison(baseline, {
        "wave#candidate-1": 95,
        "wave#candidate-2": 70,
    }, winner="wave#candidate-1")
    ranking = build_candidate_ranking(
        candidates,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
        model_result=result,
        model="comparative-test",
    )

    assert ranking["comparison_validation"]["status"] == "accepted"
    assert ranking["ranking_unavailable"] is False
    assert ranking["winner_candidate_id"] == "wave#candidate-1"
    assert ranking["winner_reason"] == "空间关系最准确"
    first = next(row for row in ranking["candidates"]
                 if row["candidate_id"] == "wave#candidate-1")
    failed = next(row for row in ranking["candidates"]
                  if row["candidate_id"] == "wave#candidate-3")
    assert first["rank"] == 1
    assert first["dimension_scores"]["spatial"] == 95
    assert first["evidence"][0]["reference_asset_id"] == "space-1"
    assert failed["rank"] is None
    assert failed["qc_passed"] is False


def test_all_failed_comparison_produces_repair_basis_without_faking_pass():
    candidates = [
        _candidate(1, score=40, issues=["手持手机方向错误"]),
        _candidate(2, score=30, issues=["车内座椅缺失"]),
        _candidate(3, score=20),
        _candidate(4, score=10),
    ]
    baseline = _baseline(candidates)
    result = _comparison(
        baseline,
        {f"wave#candidate-{index}": score
         for index, score in enumerate((50, 90, 40, 30), 1)},
        winner="wave#candidate-2",
        issues_by_id={"wave#candidate-2": ["车内座椅缺失"]},
    )
    ranking = build_candidate_ranking(
        candidates,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
        model_result=result,
    )

    assert ranking["status"] == "best_effort_repair_required"
    assert ranking["best_effort"] is True
    assert ranking["requires_repair"] is True
    assert ranking["winner_candidate_id"] == "wave#candidate-2"
    assert ranking["repair_basis"]["best_effort_candidate_id"] == \
        "wave#candidate-2"
    assert "车内座椅缺失" in ranking["repair_basis"]["issues"]
    assert not any(row["qc_passed"] for row in ranking["candidates"])


def test_stale_model_result_is_rejected_with_stable_nonblocking_fallback():
    candidates = [
        _candidate(1, passed=True, score=10),
        _candidate(2, passed=True, score=20),
    ]
    baseline = _baseline(candidates)
    stale = _comparison(baseline, {
        "wave#candidate-1": 99, "wave#candidate-2": 1,
    }, winner="wave#candidate-1")
    stale["reference_selection_hash"] = "old-references"
    first = build_candidate_ranking(
        candidates,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
        model_result=stale,
    )
    second = build_candidate_ranking(
        deepcopy(candidates),
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
        model_result=stale,
    )

    assert first["comparison_validation"] == {
        "status": "rejected",
        "reason": "stale_reference_selection_hash",
        "stale": True,
    }
    assert first["ranking_unavailable"] is True
    assert first["winner_candidate_id"] == "wave#candidate-2"
    assert first["ranking_hash"] == second["ranking_hash"]


def test_model_cannot_name_failed_or_omitted_candidate_as_winner():
    candidates = [
        _candidate(1, passed=True, score=10),
        _candidate(2, passed=True, score=20),
        _candidate(3, passed=False, score=999),
    ]
    baseline = _baseline(candidates)
    invalid = _comparison(baseline, {
        "wave#candidate-1": 90,
        "wave#candidate-2": 80,
    }, winner="wave#candidate-3")
    ranking = build_candidate_ranking(
        candidates,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
        model_result=invalid,
    )

    assert ranking["comparison_validation"]["status"] == "rejected"
    assert ranking["comparison_validation"]["reason"] == \
        "ineligible_comparison_winner"
    assert ranking["winner_candidate_id"] == "wave#candidate-2"


def test_no_technically_usable_candidate_has_no_winner():
    candidates = [
        _candidate(1, passed=True, technical=False, score=999),
        _candidate(2, passed=False, technical=False, score=1),
    ]
    ranking = _baseline(candidates)

    assert ranking["status"] == "technical_incomplete"
    assert ranking["eligible_candidate_ids"] == []
    assert ranking["winner_candidate_id"] == ""
    assert ranking["winner_candidate_index"] is None
    assert ranking["requires_repair"] is False


def test_current_guard_checks_token_generation_reference_and_ranking_hashes():
    ranking = _baseline([
        _candidate(1, passed=True, score=10),
        _candidate(2, passed=True, score=20),
    ])
    assert candidate_ranking_is_current(
        ranking,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
        ranking_input_hash=ranking["ranking_input_hash"],
    )
    assert not candidate_ranking_is_current(
        ranking,
        candidate_set_token="cset-v1:new",
        target_input_hash="generation-input",
        reference_selection_hash="reference-choice",
    )
    assert not candidate_ranking_is_current(
        ranking,
        candidate_set_token="cset-v1:test",
        target_input_hash="generation-input",
        reference_selection_hash="new-reference-choice",
    )
