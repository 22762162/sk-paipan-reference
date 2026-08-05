"""Pure comparative ranking for a frozen image-candidate wave.

Technical integrity and content QC remain authoritative upstream facts.  A
comparative model may explain which candidate is relatively best, but it may
not promote a technically unusable image, turn a failed QC verdict into a
pass, or choose a failed image while any passed candidate exists.

Every model response is bound to the candidate-set token, generation input,
reference selection, and a hash of the eligible candidate facts.  Invalid or
late responses fall back to a deterministic order and never block production.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


CANDIDATE_RANKING_SCHEMA = "aifos.candidate-ranking/v2"
CANDIDATE_COMPARISON_REQUEST_SCHEMA = \
    "aifos.candidate-comparison-request/v1"
CANDIDATE_COMPARISON_RESULT_SCHEMA = \
    "aifos.candidate-comparison-result/v1"
DEFAULT_RANKER_VERSION = "aifos-comparative-ranker-v1"

DEFAULT_CRITERIA = (
    {"key": "visible_facts", "weight": 1.0},
    {"key": "identity", "weight": 1.2},
    {"key": "spatial", "weight": 1.1},
    {"key": "prop_physics", "weight": 1.1},
    {"key": "text", "weight": 0.8},
    {"key": "continuity", "weight": 1.0},
    {"key": "composition", "weight": 0.8},
)


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _clean_text(value: Any, *, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _finite_number(value: Any, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _text_list(value: Any, *, limit: int = 16,
               item_limit: int = 800) -> list[str]:
    if isinstance(value, str):
        source = [value]
    elif isinstance(value, Sequence):
        source = value
    else:
        source = []
    output: list[str] = []
    for item in source:
        text = _clean_text(item, limit=item_limit)
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _technical_ok(raw: Mapping[str, Any]) -> bool:
    for key in ("technical_ok", "technical_usable", "media_usable"):
        if key in raw:
            return raw.get(key) is True
    probe = raw.get("technical_probe")
    if isinstance(probe, Mapping):
        if probe.get("synthetic") is True:
            return bool(probe.get("ok", probe.get("probe_ok", True)))
        if probe.get("error"):
            return False
        return bool(probe.get("ok", probe.get("probe_ok", False)))
    # Compatibility for already persisted legacy groups that predate the media
    # probe.  New callers should always supply an explicit technical fact.
    return bool(_clean_text(raw.get("uri"), limit=1600))


def _qc_passed(raw: Mapping[str, Any]) -> bool:
    if "qc_passed" in raw:
        return raw.get("qc_passed") is True
    return raw.get("passed") is True


def _candidate_index(raw: Mapping[str, Any], position: int) -> int:
    value = raw.get("candidate_index", raw.get("index", position))
    if isinstance(value, bool):
        return position
    try:
        number = int(value)
    except (TypeError, ValueError):
        return position
    return number if number > 0 else position


def _normalize_candidates(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, Sequence) \
        and not isinstance(value, (str, bytes)) else []
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for position, raw in enumerate(source, 1):
        if not isinstance(raw, Mapping):
            continue
        index = _candidate_index(raw, position)
        candidate_id = _clean_text(
            raw.get("candidate_id") or f"candidate:{index}", limit=500)
        if candidate_id in seen_ids:
            raise ValueError(f"候选ID重复:{candidate_id}")
        seen_ids.add(candidate_id)
        issues = _text_list(raw.get("issues"), limit=20)
        output.append({
            "candidate_id": candidate_id,
            "candidate_index": index,
            "uri": _clean_text(raw.get("uri"), limit=1600),
            "technical_ok": _technical_ok(raw),
            "qc_passed": _qc_passed(raw),
            "existing_score": _finite_number(
                raw.get("score", raw.get("ranking_score"))),
            "issues": issues,
            "input_hash": _clean_text(raw.get("input_hash"), limit=160),
            "candidate_set_token": _clean_text(
                raw.get("candidate_set_token"), limit=300),
            "stale": False,
            "stale_reason": "",
            "dimension_scores": {},
            "evidence": [],
            "fatal_issues": [],
            "soft_issues": issues,
            "total_score": _finite_number(
                raw.get("score", raw.get("ranking_score"))),
            "rank": None,
        })
    return output


def _normalize_criteria(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, Sequence) \
        and not isinstance(value, (str, bytes)) else DEFAULT_CRITERIA
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in source:
        if not isinstance(raw, Mapping):
            continue
        key = _clean_text(raw.get("key"), limit=80)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append({
            "key": key,
            "weight": max(0.0, _finite_number(raw.get("weight"), default=1.0)),
        })
    return output or [dict(item) for item in DEFAULT_CRITERIA]


def _ranking_input_document(
        candidates: Sequence[Mapping[str, Any]],
        *,
        candidate_set_token: str,
        target_input_hash: str,
        reference_selection_hash: str,
        criteria: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    usable = [
        row for row in candidates
        if row.get("technical_ok") and not row.get("stale")]
    passed = [row for row in usable if row.get("qc_passed")]
    eligible = passed or usable
    return {
        "schema": CANDIDATE_COMPARISON_REQUEST_SCHEMA,
        "candidate_set_token": candidate_set_token,
        "target_input_hash": target_input_hash,
        "reference_selection_hash": reference_selection_hash,
        "criteria": list(criteria),
        "eligible_candidates": [{
            "candidate_id": row["candidate_id"],
            "candidate_index": row["candidate_index"],
            "uri": row["uri"],
            "qc_passed": row["qc_passed"],
            "existing_score": row["existing_score"],
            "issues": row["issues"],
        } for row in eligible],
    }


def _normalize_dimension_scores(
        value: Any, allowed: set[str]) -> tuple[bool, dict[str, float]]:
    if not isinstance(value, Mapping):
        return True, {}
    output: dict[str, float] = {}
    for raw_key, raw_score in value.items():
        key = _clean_text(raw_key, limit=80)
        if key not in allowed:
            continue
        number = _finite_number(raw_score, default=float("nan"))
        if not math.isfinite(number) or number < 0.0 or number > 100.0:
            return False, {}
        output[key] = number
    return True, output


def _normalize_evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    output: list[dict[str, str]] = []
    for raw in value[:20]:
        if isinstance(raw, str):
            finding = _clean_text(raw, limit=800)
            if finding:
                output.append({
                    "dimension": "", "finding": finding,
                    "reference_asset_id": "",
                })
        elif isinstance(raw, Mapping):
            finding = _clean_text(raw.get("finding") or raw.get("evidence"),
                                  limit=800)
            if finding:
                output.append({
                    "dimension": _clean_text(
                        raw.get("dimension"), limit=80),
                    "finding": finding,
                    "reference_asset_id": _clean_text(
                        raw.get("reference_asset_id"), limit=300),
                })
    return output


def validate_candidate_comparison_result(
        result: Any,
        *,
        candidate_set_token: str,
        target_input_hash: str,
        reference_selection_hash: str,
        ranking_input_hash: str,
        eligible_candidate_ids: Sequence[str],
        criteria: Any = None,
) -> dict[str, Any]:
    """Validate an untrusted comparative response against frozen facts."""
    if not isinstance(result, Mapping):
        return {"valid": False, "stale": False,
                "reason": "comparison_result_missing", "rows": []}
    if result.get("schema") not in (None, CANDIDATE_COMPARISON_RESULT_SCHEMA):
        return {"valid": False, "stale": False,
                "reason": "comparison_result_schema_mismatch", "rows": []}
    expected_bindings = {
        "candidate_set_token": str(candidate_set_token or ""),
        "target_input_hash": str(target_input_hash or ""),
        "reference_selection_hash": str(reference_selection_hash or ""),
        "ranking_input_hash": str(ranking_input_hash or ""),
    }
    for key, expected in expected_bindings.items():
        if str(result.get(key) or "") != expected:
            return {"valid": False, "stale": True,
                    "reason": f"stale_{key}", "rows": []}
    eligible = [str(item) for item in eligible_candidate_ids]
    raw_rows = result.get("candidates")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        return {"valid": False, "stale": False,
                "reason": "comparison_rows_missing", "rows": []}
    allowed_dimensions = {
        row["key"] for row in _normalize_criteria(criteria)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            return {"valid": False, "stale": False,
                    "reason": "invalid_comparison_row", "rows": []}
        candidate_id = _clean_text(raw.get("candidate_id"), limit=500)
        if candidate_id not in eligible:
            return {"valid": False, "stale": False,
                    "reason": "ineligible_comparison_candidate", "rows": []}
        if candidate_id in seen:
            return {"valid": False, "stale": False,
                    "reason": "duplicate_comparison_candidate", "rows": []}
        seen.add(candidate_id)
        valid_scores, dimension_scores = _normalize_dimension_scores(
            raw.get("dimension_scores"), allowed_dimensions)
        if not valid_scores:
            return {"valid": False, "stale": False,
                    "reason": "invalid_dimension_score", "rows": []}
        if not dimension_scores:
            return {"valid": False, "stale": False,
                    "reason": "dimension_scores_missing", "rows": []}
        total_score = _finite_number(
            raw.get("total_score"), default=float("nan"))
        if (not math.isfinite(total_score)
                or total_score < 0.0 or total_score > 100.0):
            return {"valid": False, "stale": False,
                    "reason": "invalid_total_score", "rows": []}
        rows.append({
            "candidate_id": candidate_id,
            "dimension_scores": dimension_scores,
            "evidence": _normalize_evidence(raw.get("evidence")),
            "fatal_issues": _text_list(raw.get("fatal_issues"), limit=16),
            "soft_issues": _text_list(raw.get("soft_issues"), limit=16),
            "total_score": total_score,
        })
    if seen != set(eligible):
        return {"valid": False, "stale": False,
                "reason": "comparison_candidate_set_incomplete", "rows": []}
    winner = _clean_text(result.get("winner_candidate_id"), limit=500)
    if winner not in seen:
        return {"valid": False, "stale": False,
                "reason": "ineligible_comparison_winner", "rows": []}
    row_by_id = {row["candidate_id"]: row for row in rows}
    expected_winner = max(
        eligible,
        key=lambda candidate_id: (
            row_by_id[candidate_id]["total_score"],
            -eligible.index(candidate_id),
        ),
    )
    if winner != expected_winner:
        return {"valid": False, "stale": False,
                "reason": "winner_score_mismatch", "rows": []}
    confidence = _finite_number(
        result.get("confidence"), default=float("nan"))
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        return {"valid": False, "stale": False,
                "reason": "invalid_confidence", "rows": []}
    return {
        "valid": True,
        "stale": False,
        "reason": "accepted",
        "rows": rows,
        "winner_candidate_id": winner,
        "winner_reason": _clean_text(result.get("winner_reason"), limit=1200),
        "confidence": confidence,
    }


def build_candidate_ranking(
        candidates: Any,
        *,
        candidate_set_token: str,
        target_input_hash: str,
        reference_selection_hash: str = "",
        model_result: Any = None,
        ranker_version: str = DEFAULT_RANKER_VERSION,
        criteria: Any = None,
        model: str = "",
) -> dict[str, Any]:
    """Rank one frozen candidate wave with a safe deterministic fallback."""
    token = _clean_text(candidate_set_token, limit=300)
    target_hash = _clean_text(target_input_hash, limit=160)
    reference_hash = _clean_text(reference_selection_hash, limit=160)
    if not token:
        raise ValueError("candidate_set_token 不能为空")
    if not target_hash:
        raise ValueError("target_input_hash 不能为空")
    normalized = _normalize_candidates(candidates)
    for row in normalized:
        row_token = str(row.get("candidate_set_token") or "")
        row_input_hash = str(row.get("input_hash") or "")
        if row_token and row_token != token:
            row["stale"] = True
            row["stale_reason"] = "candidate_set_token_mismatch"
        elif row_input_hash and row_input_hash != target_hash:
            row["stale"] = True
            row["stale_reason"] = "target_input_hash_mismatch"
    criteria_rows = _normalize_criteria(criteria)
    usable = [
        row for row in normalized
        if row["technical_ok"] and not row["stale"]]
    passed = [row for row in usable if row["qc_passed"]]
    eligible = passed or usable
    input_document = _ranking_input_document(
        normalized,
        candidate_set_token=token,
        target_input_hash=target_hash,
        reference_selection_hash=reference_hash,
        criteria=criteria_rows,
    )
    ranking_input_hash = _stable_hash(input_document)
    if len(eligible) == 1 and model_result is None:
        validation = {
            "valid": False, "stale": False,
            "reason": "sole_eligible_candidate", "rows": [],
        }
    elif eligible:
        validation = validate_candidate_comparison_result(
            model_result,
            candidate_set_token=token,
            target_input_hash=target_hash,
            reference_selection_hash=reference_hash,
            ranking_input_hash=ranking_input_hash,
            eligible_candidate_ids=[row["candidate_id"] for row in eligible],
            criteria=criteria_rows,
        )
    else:
        validation = {
            "valid": False, "stale": False,
            "reason": "no_technical_candidate", "rows": [],
        }
    accepted = bool(validation["valid"])
    rows_by_id = {
        row["candidate_id"]: row for row in validation.get("rows", [])
    }
    if accepted:
        for row in normalized:
            compared = rows_by_id.get(row["candidate_id"])
            if not compared:
                continue
            row["dimension_scores"] = dict(compared["dimension_scores"])
            row["evidence"] = list(compared["evidence"])
            row["fatal_issues"] = list(compared["fatal_issues"])
            row["soft_issues"] = list(compared["soft_issues"])
            row["total_score"] = float(compared["total_score"])
        ordered = sorted(eligible, key=lambda row: (
            -float(row["total_score"]), int(row["candidate_index"])))
    else:
        ordered = sorted(eligible, key=lambda row: (
            -float(row["existing_score"]), int(row["candidate_index"])))
    for rank, row in enumerate(ordered, 1):
        row["rank"] = rank
    winner = ordered[0] if ordered else None
    scores = [float(row["total_score"] if accepted
                    else row["existing_score"]) for row in ordered]
    tie_margin = scores[0] - scores[1] if len(scores) > 1 else (
        scores[0] if scores else 0.0)
    confidence = float(validation.get("confidence") or 0.0) \
        if accepted else 0.0
    all_failed = bool(usable and not passed)
    winner_reason = (
        validation.get("winner_reason") or "comparative_visual_rank"
        if accepted else (
            "sole_eligible_candidate" if len(eligible) == 1 else
            "deterministic_existing_score_fallback"
            if winner else "no_technically_usable_candidate"))
    repair_basis = {
        "best_effort_candidate_id": (
            winner["candidate_id"] if all_failed and winner else ""),
        "issues": list(winner.get("fatal_issues") or [])
        + list(winner.get("soft_issues") or []) if all_failed and winner else [],
        "evidence": list(winner.get("evidence") or [])
        if all_failed and winner else [],
    }
    decision = {
        "schema": CANDIDATE_RANKING_SCHEMA,
        "ranker_version": str(ranker_version or DEFAULT_RANKER_VERSION),
        "candidate_set_token": token,
        "target_input_hash": target_hash,
        "reference_selection_hash": reference_hash,
        "ranking_input_hash": ranking_input_hash,
        "status": (
            "stale_candidate_set" if not usable and any(
                row["stale"] for row in normalized) else
            "technical_incomplete" if not usable else
            "best_effort_repair_required" if all_failed else "selected"),
        "ranking_unavailable": bool(not accepted and len(eligible) > 1),
        "best_effort": all_failed,
        "requires_repair": all_failed,
        "criteria": criteria_rows,
        "eligible_candidate_ids": [row["candidate_id"] for row in eligible],
        "candidates": normalized,
        "winner_candidate_id": winner["candidate_id"] if winner else "",
        "winner_candidate_index": winner["candidate_index"] if winner else None,
        "winner_reason": _clean_text(winner_reason, limit=1200),
        "confidence": confidence,
        "tie_margin": tie_margin,
        "repair_basis": repair_basis,
        "comparison_validation": {
            "status": "accepted" if accepted else (
                "not_required" if len(eligible) == 1
                and model_result is None else
                "not_requested" if model_result is None else "rejected"),
            "reason": validation["reason"],
            "stale": bool(validation["stale"]),
        },
        "model": str(model or ""),
    }
    decision["ranking_hash"] = _stable_hash({
        "schema": decision["schema"],
        "ranker_version": decision["ranker_version"],
        "candidate_set_token": token,
        "target_input_hash": target_hash,
        "reference_selection_hash": reference_hash,
        "ranking_input_hash": ranking_input_hash,
        "winner_candidate_id": decision["winner_candidate_id"],
        "eligible_candidate_ids": decision["eligible_candidate_ids"],
    })
    return decision


def build_candidate_comparison_request(
        ranking: Mapping[str, Any],
        *,
        reference_bindings: Any = None,
        target_facts: Any = None,
) -> dict[str, Any]:
    """Build the exact all-candidates-in-one-call comparison payload."""
    eligible = set(ranking.get("eligible_candidate_ids") or [])
    candidates = [
        row for row in (ranking.get("candidates") or [])
        if isinstance(row, Mapping) and row.get("candidate_id") in eligible
    ]
    bindings = reference_bindings if isinstance(
        reference_bindings, Sequence) and not isinstance(
            reference_bindings, (str, bytes)) else []
    return {
        "schema": CANDIDATE_COMPARISON_REQUEST_SCHEMA,
        "candidate_set_token": str(
            ranking.get("candidate_set_token") or ""),
        "target_input_hash": str(ranking.get("target_input_hash") or ""),
        "reference_selection_hash": str(
            ranking.get("reference_selection_hash") or ""),
        "ranking_input_hash": str(ranking.get("ranking_input_hash") or ""),
        "criteria": list(ranking.get("criteria") or []),
        "target_facts": dict(target_facts or {})
        if isinstance(target_facts, Mapping) else {},
        "reference_bindings": [dict(item) for item in bindings
                               if isinstance(item, Mapping)],
        "candidates": [{
            "candidate_id": row.get("candidate_id"),
            "candidate_index": row.get("candidate_index"),
            "uri": row.get("uri"),
            "qc_passed": row.get("qc_passed") is True,
            "issues": list(row.get("issues") or []),
        } for row in candidates],
    }


def candidate_ranking_is_current(
        ranking: Mapping[str, Any],
        *,
        candidate_set_token: str,
        target_input_hash: str,
        reference_selection_hash: str = "",
        ranking_input_hash: str = "",
) -> bool:
    """Return false when a late ranking targets an older frozen wave."""
    expected = {
        "candidate_set_token": str(candidate_set_token or ""),
        "target_input_hash": str(target_input_hash or ""),
        "reference_selection_hash": str(reference_selection_hash or ""),
    }
    if any(str(ranking.get(key) or "") != value
           for key, value in expected.items()):
        return False
    if ranking_input_hash and str(
            ranking.get("ranking_input_hash") or "") != str(
                ranking_input_hash):
        return False
    return ranking.get("schema") == CANDIDATE_RANKING_SCHEMA
