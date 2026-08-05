"""Pure, auditable reference selection for image and video generation.

The selector deliberately separates two responsibilities:

* deterministic policy decides which references are mandatory, incompatible,
  redundant, or eligible for an optional slot; and
* an optional multimodal comparison may reorder only that eligible shortlist.

Identity anchors, core props, spatial blocking references, and references
explicitly marked mandatory are never silently removed.  When they exceed the
provider budget the decision is returned as ``mandatory_budget_overflow`` so a
caller can split the request instead of weakening the contract.

This module has no provider, database, filesystem, or director dependency.  It
is safe to use while building a prompt review and again when validating a late
model response.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


REFERENCE_SELECTION_SCHEMA = "aifos.reference-selection/v1"
REFERENCE_SELECTION_REQUEST_SCHEMA = \
    "aifos.reference-selection-request/v1"
REFERENCE_VISUAL_RESULT_SCHEMA = \
    "aifos.reference-visual-ranking/v1"
DEFAULT_SELECTOR_VERSION = "aifos-reference-selector-v1"

_ALWAYS_MANDATORY_ROLES = frozenset(("identity", "spatial", "core_prop"))
_PROP_ROLES = frozenset(("prop", "core_prop", "prop_identity"))
_PHASE_SENSITIVE_ROLES = frozenset((
    "continuity", "keyframe", "composition", "revision_base",
))


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


def _normalized_name(value: Any) -> str:
    return _clean_text(value, limit=160).casefold()


def _finite_number(value: Any, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _positive_budget(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("max_references 必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_references 必须是非负整数") from exc
    if number < 0 or str(value).strip() != str(number):
        raise ValueError("max_references 必须是非负整数")
    return number


def _reference_key(raw: Mapping[str, Any], position: int) -> str:
    asset_id = raw.get("asset_id")
    if asset_id not in (None, ""):
        return f"asset:{asset_id}"
    content_hash = _clean_text(raw.get("content_hash"), limit=160)
    if content_hash:
        return f"content:{content_hash}"
    uri = _clean_text(raw.get("uri"), limit=1600)
    if uri:
        return f"uri:{uri}"
    return f"anonymous:{position}:{_stable_hash(dict(raw))[:16]}"


def _is_mandatory(raw: Mapping[str, Any], role: str) -> tuple[bool, str]:
    if raw.get("mandatory") is True or raw.get("required") is True:
        return True, "explicit_mandatory"
    if role in _ALWAYS_MANDATORY_ROLES:
        return True, f"mandatory_role:{role}"
    # Current AIFOS prop references are registered from the core-prop asset
    # chain.  Treating role=prop as mandatory is intentionally conservative;
    # an integration can mark a decorative prop role as prop_optional.
    if role in _PROP_ROLES:
        return True, "core_prop_contract"
    if raw.get("core_prop") is True or raw.get("is_core") is True:
        return True, "core_prop_contract"
    importance = _normalized_name(raw.get("importance"))
    if importance in {"critical", "core", "required", "核心", "关键"}:
        return True, "critical_importance"
    return False, ""


def _normalize_target_facts(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    characters = source.get("visible_characters")
    if isinstance(characters, str):
        characters = [characters]
    elif not isinstance(characters, Sequence):
        characters = []
    return {
        "visible_characters": [
            _clean_text(item, limit=160) for item in characters
            if _clean_text(item, limit=160)
        ],
        "frame_phase": _clean_text(
            source.get("frame_phase") or source.get("phase"), limit=80),
        "scene_id": _clean_text(source.get("scene_id"), limit=160),
        "camera_id": _clean_text(source.get("camera_id"), limit=160),
        "view": _clean_text(
            source.get("view") or source.get("camera_view"), limit=120),
        "story_phase": _clean_text(source.get("story_phase"), limit=160),
        "era_context": _clean_text(
            source.get("era_context") or source.get("era"), limit=160),
        "allow_cross_era": bool(source.get("allow_cross_era")),
        "allow_cross_scene": bool(source.get("allow_cross_scene")),
    }


def _normalize_reference(
        raw: Mapping[str, Any], position: int) -> dict[str, Any]:
    role = _normalized_name(raw.get("role") or raw.get("reference_role")
                            or raw.get("kind")) or "manual"
    mandatory, mandatory_reason = _is_mandatory(raw, role)
    key = _reference_key(raw, position)
    asset_id = raw.get("asset_id")
    if asset_id in (None, ""):
        asset_id = key
    uri = _clean_text(raw.get("uri"), limit=1600)
    content_hash = _clean_text(raw.get("content_hash"), limit=160)
    if not content_hash:
        content_hash = _stable_hash({
            "asset_id": asset_id,
            "uri": uri,
            "role": role,
            "target": raw.get("target") or raw.get("attach_to")
            or raw.get("character"),
            "binding": raw.get("binding"),
        })
    return {
        "pool_index": position,
        "selection_key": key,
        "asset_id": asset_id,
        "uri": uri,
        "label": _clean_text(raw.get("label") or raw.get("name"),
                             limit=300),
        "role": role,
        "kind": _normalized_name(raw.get("kind")) or role,
        "target": _clean_text(
            raw.get("target") or raw.get("attach_to")
            or raw.get("character"), limit=160),
        "phase": _clean_text(
            raw.get("phase") or raw.get("reference_phase"), limit=80),
        "era_context": _clean_text(
            raw.get("era_context") or raw.get("era"), limit=160),
        "story_phase": _clean_text(raw.get("story_phase"), limit=160),
        "scene_id": _clean_text(raw.get("scene_id"), limit=160),
        "camera_id": _clean_text(raw.get("camera_id"), limit=160),
        "view": _clean_text(
            raw.get("view") or raw.get("camera_view"), limit=120),
        "binding": _clean_text(raw.get("binding"), limit=1200),
        "content_hash": content_hash,
        "forbidden": raw.get("forbidden") is True,
        "mandatory": mandatory,
        "mandatory_reason": mandatory_reason,
        "relevance_score": _finite_number(raw.get("relevance_score")),
        "recency_score": _finite_number(raw.get("recency_score")),
        "visual_match_score": None,
        "selected": False,
        "reason": "",
        "reject_reason": "",
    }


def _hard_rejection(
        row: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    if row.get("mandatory"):
        return ""
    if row.get("forbidden") is True:
        return "explicitly_forbidden"
    row_era = _normalized_name(row.get("era_context"))
    target_era = _normalized_name(target.get("era_context"))
    if (row_era and target_era and row_era != target_era
            and not target.get("allow_cross_era")):
        return "era_mismatch"
    role = _normalized_name(row.get("role"))
    row_phase = _normalized_name(row.get("phase"))
    target_phase = _normalized_name(target.get("frame_phase"))
    if (role in _PHASE_SENSITIVE_ROLES and row_phase and target_phase
            and row_phase != target_phase):
        return "phase_mismatch"
    row_story_phase = _normalized_name(row.get("story_phase"))
    target_story_phase = _normalized_name(target.get("story_phase"))
    if (role in _PHASE_SENSITIVE_ROLES and row_story_phase
            and target_story_phase and row_story_phase != target_story_phase):
        return "story_phase_mismatch"
    row_scene = _normalized_name(row.get("scene_id"))
    target_scene = _normalized_name(target.get("scene_id"))
    if (role in {"continuity", "keyframe", "revision_base"}
            and row_scene and target_scene and row_scene != target_scene
            and not target.get("allow_cross_scene")):
        return "scene_mismatch"
    return ""


def _metadata_score(
        row: Mapping[str, Any], target: Mapping[str, Any]) -> float:
    score = _finite_number(row.get("relevance_score"))
    comparisons = (
        ("phase", "frame_phase", 20.0),
        ("era_context", "era_context", 25.0),
        ("story_phase", "story_phase", 15.0),
        ("scene_id", "scene_id", 18.0),
        ("camera_id", "camera_id", 12.0),
        ("view", "view", 10.0),
    )
    for row_key, target_key, weight in comparisons:
        actual = _normalized_name(row.get(row_key))
        expected = _normalized_name(target.get(target_key))
        if actual and expected and actual == expected:
            score += weight
    visible = {
        _normalized_name(item) for item in target.get("visible_characters", [])
        if _normalized_name(item)
    }
    if _normalized_name(row.get("target")) in visible:
        score += 30.0
    # Recency is only a tie-break contribution after incompatible era/phase
    # references have already been rejected.
    score += max(-5.0, min(5.0, _finite_number(row.get("recency_score"))))
    return score


def _visual_result_payload(
        result: Any,
        *,
        expected_input_hash: str,
        allowed_keys: Sequence[str],
) -> tuple[bool, str, list[str], dict[str, float], dict[str, str]]:
    if not isinstance(result, Mapping):
        return False, "visual_result_missing", [], {}, {}
    if result.get("schema") not in (None, REFERENCE_VISUAL_RESULT_SCHEMA):
        return False, "visual_result_schema_mismatch", [], {}, {}
    if str(result.get("input_hash") or "") != expected_input_hash:
        return False, "stale_visual_result", [], {}, {}
    order = result.get("ranked_selection_keys")
    if not isinstance(order, Sequence) or isinstance(order, (str, bytes)):
        return False, "ranked_selection_keys_missing", [], {}, {}
    normalized_order = [str(item) for item in order]
    if allowed_keys and not normalized_order:
        return False, "visual_rank_empty", [], {}, {}
    if len(normalized_order) != len(set(normalized_order)):
        return False, "duplicate_visual_rank", [], {}, {}
    allowed = set(allowed_keys)
    if any(item not in allowed for item in normalized_order):
        return False, "unknown_visual_reference", [], {}, {}
    scores_raw = result.get("scores")
    scores_raw = scores_raw if isinstance(scores_raw, Mapping) else {}
    reasons_raw = result.get("reasons")
    reasons_raw = reasons_raw if isinstance(reasons_raw, Mapping) else {}
    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for key in normalized_order:
        if key in scores_raw:
            number = _finite_number(scores_raw.get(key), default=float("nan"))
            if (not math.isfinite(number)
                    or number < 0.0 or number > 100.0):
                return False, "invalid_visual_score", [], {}, {}
            scores[key] = number
        reasons[key] = _clean_text(reasons_raw.get(key), limit=600)
    return True, "accepted", normalized_order, scores, reasons


def validate_reference_visual_ranking(
        result: Any,
        *,
        expected_input_hash: str,
        allowed_selection_keys: Sequence[str],
) -> dict[str, Any]:
    """Validate an untrusted visual ranking against the frozen shortlist."""
    valid, reason, order, scores, reasons = _visual_result_payload(
        result,
        expected_input_hash=str(expected_input_hash or ""),
        allowed_keys=[str(item) for item in allowed_selection_keys],
    )
    return {
        "valid": valid,
        "stale": reason == "stale_visual_result",
        "reason": reason,
        "ranked_selection_keys": order,
        "scores": scores,
        "reasons": reasons,
    }


def build_reference_selection_decision(
        reference_pool: Any,
        *,
        max_references: Any,
        target_facts: Any = None,
        visual_result: Any = None,
        provider: str = "",
        selector_version: str = DEFAULT_SELECTOR_VERSION,
        model: str = "",
        metadata_shortlist_size: int | None = None,
) -> dict[str, Any]:
    """Select references without allowing an AI to weaken hard requirements.

    Calling this function without ``visual_result`` produces both a stable
    deterministic decision and the frozen ``input_hash`` needed by a later
    visual ranking call.  Calling it again with the model response applies the
    response only when that hash and every ranked key validate.
    """
    budget = _positive_budget(max_references)
    target = _normalize_target_facts(target_facts)
    raw_pool = reference_pool if isinstance(reference_pool, Sequence) \
        and not isinstance(reference_pool, (str, bytes)) else []
    pool = [
        _normalize_reference(item, position)
        for position, item in enumerate(raw_pool, 1)
        if isinstance(item, Mapping)
    ]
    pool_hash = _stable_hash(pool)
    seen_optional_content: set[str] = set()
    mandatory_content = {
        str(row.get("content_hash") or "") for row in pool
        if row.get("mandatory") and row.get("content_hash")
    }
    optional: list[dict[str, Any]] = []
    for row in pool:
        if row["mandatory"]:
            row["metadata_score"] = _metadata_score(row, target)
            continue
        rejection = _hard_rejection(row, target)
        content_hash = str(row.get("content_hash") or "")
        if not rejection and content_hash in mandatory_content:
            rejection = "redundant_with_mandatory"
        if not rejection and content_hash in seen_optional_content:
            rejection = "redundant_optional"
        if rejection:
            row["reject_reason"] = rejection
            row["metadata_score"] = _metadata_score(row, target)
            continue
        seen_optional_content.add(content_hash)
        row["metadata_score"] = _metadata_score(row, target)
        optional.append(row)

    mandatory = [row for row in pool if row["mandatory"]]
    residual = max(0, budget - len(mandatory))
    optional.sort(key=lambda row: (
        -float(row["metadata_score"]), int(row["pool_index"])))
    if metadata_shortlist_size is None:
        shortlist_limit = min(len(optional), max(residual, residual * 3))
    else:
        shortlist_limit = _positive_budget(metadata_shortlist_size)
        shortlist_limit = min(len(optional), max(residual, shortlist_limit))
    shortlist = optional[:shortlist_limit]
    shortlist_keys = [str(row["selection_key"]) for row in shortlist]
    input_document = {
        "schema": REFERENCE_SELECTION_REQUEST_SCHEMA,
        "selector_version": str(selector_version or DEFAULT_SELECTOR_VERSION),
        "target_facts": target,
        "budget": {"provider": str(provider or ""), "max_refs": budget},
        "mandatory": [{
            "selection_key": row["selection_key"],
            "content_hash": row["content_hash"],
            "role": row["role"],
            "target": row["target"],
        } for row in mandatory],
        "optional_shortlist": [{
            "selection_key": row["selection_key"],
            "content_hash": row["content_hash"],
            "role": row["role"],
            "target": row["target"],
            "phase": row["phase"],
            "era_context": row["era_context"],
            "scene_id": row["scene_id"],
            "camera_id": row["camera_id"],
            "view": row["view"],
            "metadata_score": row["metadata_score"],
        } for row in shortlist],
    }
    input_hash = _stable_hash(input_document)
    validation = validate_reference_visual_ranking(
        visual_result,
        expected_input_hash=input_hash,
        allowed_selection_keys=shortlist_keys,
    )
    visual_accepted = bool(validation["valid"])
    rank_position = {
        key: position for position, key in enumerate(
            validation["ranked_selection_keys"])
    }
    # A partial valid ranking is allowed: ranked references come first and the
    # remaining frozen shortlist keeps its deterministic metadata order.
    optional_order = sorted(shortlist, key=lambda row: (
        0 if row["selection_key"] in rank_position else 1,
        rank_position.get(row["selection_key"], len(shortlist)),
        -float(row["metadata_score"]),
        int(row["pool_index"]),
    )) if visual_accepted else list(shortlist)

    selected_optional = optional_order[:residual]
    selected_keys = {
        str(row["selection_key"]) for row in mandatory + selected_optional
    }
    for row in pool:
        key = str(row["selection_key"])
        if key in validation["scores"]:
            row["visual_match_score"] = validation["scores"][key]
        if key in selected_keys:
            row["selected"] = True
            if row["mandatory"]:
                row["reason"] = row["mandatory_reason"]
            elif visual_accepted and key in rank_position:
                reason = validation["reasons"].get(key)
                row["reason"] = reason or "visual_rank"
            else:
                row["reason"] = "deterministic_metadata_fallback"
        elif not row["reject_reason"]:
            row["reject_reason"] = (
                "metadata_shortlist_limit"
                if row not in shortlist else "optional_budget_limit")

    selected_rows = mandatory + selected_optional
    bindings = [{
        "image_index": index,
        "selection_key": row["selection_key"],
        "asset_id": row["asset_id"],
        "uri": row["uri"],
        "role": row["role"],
        "target": row["target"],
        "instruction": row["binding"],
    } for index, row in enumerate(selected_rows, 1)]
    overflow = len(mandatory) > budget
    status = "mandatory_budget_overflow" if overflow else "ready"
    needed_visual_choices = min(residual, len(shortlist))
    fallback_used = bool(
        needed_visual_choices
        and (not visual_accepted
             or len(rank_position) < needed_visual_choices))
    decision = {
        "schema": REFERENCE_SELECTION_SCHEMA,
        "selector_version": str(
            selector_version or DEFAULT_SELECTOR_VERSION),
        "status": status,
        "ready": not overflow,
        "pool_hash": pool_hash,
        "input_hash": input_hash,
        "target_facts": target,
        "budget": {
            "provider": str(provider or ""),
            "max_refs": budget,
            "mandatory_count": len(mandatory),
            "optional_slots": residual,
            "selected_count": len(selected_rows),
            "mandatory_overflow": max(0, len(mandatory) - budget),
        },
        "pool": pool,
        "metadata_shortlist_keys": shortlist_keys,
        "selected_asset_ids": [row["asset_id"] for row in selected_rows],
        "selected_selection_keys": [
            row["selection_key"] for row in selected_rows],
        "bindings": bindings,
        "fallback_used": fallback_used,
        "selector_validation": {
            "status": "accepted" if visual_accepted else (
                "not_requested" if visual_result is None else "rejected"),
            "reason": validation["reason"],
            "stale": validation["stale"],
        },
        "model": str(model or ""),
    }
    decision["selection_hash"] = _stable_hash({
        "schema": decision["schema"],
        "selector_version": decision["selector_version"],
        "input_hash": input_hash,
        "pool_hash": pool_hash,
        "status": status,
        "selected_selection_keys": decision["selected_selection_keys"],
        "bindings": bindings,
        "fallback_used": fallback_used,
    })
    return decision


def build_reference_selection_request(
        decision: Mapping[str, Any]) -> dict[str, Any]:
    """Return the frozen, optional-only payload for a visual selector."""
    allowed = set(decision.get("metadata_shortlist_keys") or [])
    rows = [
        row for row in (decision.get("pool") or [])
        if isinstance(row, Mapping)
        and row.get("selection_key") in allowed
    ]
    return {
        "schema": REFERENCE_SELECTION_REQUEST_SCHEMA,
        "input_hash": str(decision.get("input_hash") or ""),
        "target_facts": dict(decision.get("target_facts") or {}),
        "optional_slots": int(
            (decision.get("budget") or {}).get("optional_slots") or 0),
        "references": [{
            "selection_key": row.get("selection_key"),
            "asset_id": row.get("asset_id"),
            "uri": row.get("uri"),
            "label": row.get("label"),
            "role": row.get("role"),
            "target": row.get("target"),
            "binding": row.get("binding"),
        } for row in rows],
    }


def materialize_reference_upload_manifest(
        reference_pool: Any,
        decision: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Materialize the exact upload order frozen by ``decision.bindings``.

    ``image_index`` is an upload-position contract, not descriptive metadata.
    Callers must therefore build the provider manifest from bindings in their
    frozen order instead of filtering the original pool with an unordered set.
    The returned binding text is also copied from the decision so the audit
    document and the actual provider instruction cannot drift apart.
    """
    if decision.get("schema") != REFERENCE_SELECTION_SCHEMA:
        raise ValueError("参考图选择决策 schema 无效")
    if decision.get("ready") is not True:
        raise ValueError("参考图选择决策尚未 ready")
    raw_pool = reference_pool if isinstance(reference_pool, Sequence) \
        and not isinstance(reference_pool, (str, bytes)) else []
    source_by_key = {
        _reference_key(raw, position): dict(raw)
        for position, raw in enumerate(raw_pool, 1)
        if isinstance(raw, Mapping)
    }
    bindings = decision.get("bindings")
    if not isinstance(bindings, Sequence) or isinstance(
            bindings, (str, bytes)):
        raise ValueError("参考图选择决策 bindings 无效")
    manifest: list[dict[str, Any]] = []
    for image_index, raw_binding in enumerate(bindings, 1):
        if not isinstance(raw_binding, Mapping):
            raise ValueError("参考图选择 binding 必须是对象")
        try:
            declared_index = int(raw_binding.get("image_index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("参考图选择 binding 缺少 image_index") from exc
        if declared_index != image_index:
            raise ValueError(
                "参考图选择 image_index 与冻结上传顺序不一致")
        selection_key = str(raw_binding.get("selection_key") or "")
        source = source_by_key.get(selection_key)
        if source is None:
            raise ValueError(
                f"参考图选择 binding 找不到原始条目:{selection_key}")
        source_uri = str(source.get("uri") or "").strip()
        binding_uri = str(raw_binding.get("uri") or "").strip()
        if not source_uri or source_uri != binding_uri:
            raise ValueError(
                f"参考图选择 binding URI 与原始条目不一致:{selection_key}")
        source.update({
            "index": image_index,
            "image_index": image_index,
            "selection_key": selection_key,
            "uri": binding_uri,
            "role": str(raw_binding.get("role") or source.get("role") or ""),
            "binding": str(raw_binding.get("instruction") or ""),
        })
        if raw_binding.get("target") not in (None, ""):
            source["character"] = str(raw_binding.get("target"))
        manifest.append(source)
    expected_count = int(
        (decision.get("budget") or {}).get("selected_count") or 0)
    if len(manifest) != expected_count:
        raise ValueError("参考图选择数量与冻结上传清单不一致")
    return manifest


def reference_selection_is_current(
        decision: Mapping[str, Any],
        *,
        expected_input_hash: str,
        expected_selection_hash: str = "",
) -> bool:
    """Reject a late decision after the reference input has changed."""
    if str(decision.get("input_hash") or "") != str(expected_input_hash or ""):
        return False
    if expected_selection_hash and str(
            decision.get("selection_hash") or "") != str(
                expected_selection_hash):
        return False
    return decision.get("schema") == REFERENCE_SELECTION_SCHEMA
