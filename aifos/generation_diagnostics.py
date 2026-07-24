"""Pure helpers for diagnosing the inputs that produced a failed image.

Visual QC has two different jobs:

* describe what is wrong in the rendered image; and
* decide whether the prompt or one of the submitted references caused it.

The second job must use the exact inputs from the current shot.  These helpers
normalize model output into a small, stable contract and derive an auditable
retry decision without touching assets, databases, or provider state.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


GENERATION_DIAGNOSTICS_SCHEMA = "aifos.visual-input-diagnosis/v1"

_DIAGNOSIS_STATUSES = {
    "correct", "needs_patch", "needs_adjustment", "conflicting",
    "insufficient", "missing", "uncertain", "unknown",
}
_REFERENCE_ACTIONS = {
    "keep", "remove", "rebind", "replace", "add", "drop_revision_base",
}
_ACTIONABLE_REFERENCE_ACTIONS = _REFERENCE_ACTIONS - {"keep"}


def _clean_text(value: Any, *, limit: int = 1200) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def _text_list(value: Any, *, limit: int = 12,
               item_limit: int = 600) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (tuple, list)):
        values = value
    else:
        values = []
    output: list[str] = []
    for item in values:
        text = _clean_text(item, limit=item_limit)
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def prompt_hash(prompt: Any) -> str:
    """Hash the exact prompt text submitted to a generation provider."""
    return _stable_hash(str(prompt or ""))


def normalize_reference_manifest(references: Any) -> list[dict[str, Any]]:
    """Keep only reference facts that affect model input, in submission order."""
    if not isinstance(references, list):
        return []
    output: list[dict[str, Any]] = []
    for position, item in enumerate(references, 1):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index") or position)
        except (TypeError, ValueError):
            index = position
        output.append({
            "index": index,
            "asset_id": item.get("asset_id"),
            "uri": str(item.get("uri") or ""),
            "label": _clean_text(item.get("label") or item.get("name"),
                                 limit=300),
            "role": _clean_text(item.get("role") or item.get("kind"),
                                limit=80),
            "character": _clean_text(item.get("character"), limit=120),
            "binding": _clean_text(item.get("binding"), limit=1200),
        })
    return output


def reference_hash(references: Any) -> str:
    """Hash reference ordering, identity, role and binding."""
    return _stable_hash(normalize_reference_manifest(references))


def generation_input_hash(prompt: Any, references: Any) -> str:
    """Hash the complete prompt/reference pair for unchanged-retry detection."""
    return _stable_hash({
        "prompt_hash": prompt_hash(prompt),
        "reference_hash": reference_hash(references),
    })


def _normalize_image_error(value: Any, fallback_issues: list[str]) -> dict:
    if isinstance(value, str):
        value = {"summary": value}
    value = value if isinstance(value, dict) else {}
    summary = _clean_text(value.get("summary"))
    categories = _text_list(value.get("categories"), limit=12,
                            item_limit=80)
    evidence = _text_list(value.get("evidence"), limit=12)
    if not summary and fallback_issues:
        summary = fallback_issues[0]
    if not evidence and fallback_issues:
        evidence = fallback_issues
    return {
        "summary": summary,
        "categories": categories,
        "evidence": evidence,
    }


def _normalize_diagnosis(value: Any, *, adjustment=False) -> dict:
    if isinstance(value, str):
        value = {"status": "needs_adjustment" if adjustment else "needs_patch",
                 "issues": [value]}
    value = value if isinstance(value, dict) else {}
    status = _clean_text(value.get("status"), limit=40).lower() or "unknown"
    if status not in _DIAGNOSIS_STATUSES:
        status = "unknown"
    output = {
        "status": status,
        "issues": _text_list(value.get("issues")),
    }
    if adjustment:
        missing = value.get("missing_roles")
        output["missing_roles"] = [
            {
                "role": _clean_text(item.get("role"), limit=80),
                "character": _clean_text(item.get("character"), limit=120),
                "reason": _clean_text(item.get("reason")),
            }
            for item in (missing if isinstance(missing, list) else [])
            if isinstance(item, dict)
        ][:12]
    else:
        output["irrelevant_or_conflicting_sections"] = _text_list(
            value.get("irrelevant_or_conflicting_sections"))
    return output


def _normalize_prompt_patch(value: Any) -> dict:
    if isinstance(value, str):
        value = {"instructions": [value]}
    elif isinstance(value, list):
        value = {"instructions": value}
    value = value if isinstance(value, dict) else {}
    return {
        "instructions": _text_list(value.get("instructions"), limit=6,
                                   item_limit=800),
        "preserve": _text_list(value.get("preserve"), limit=12,
                               item_limit=160),
        "max_scope": "current_shot_only",
    }


def _normalize_reference_adjustments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in value[:16]:
        if not isinstance(raw, dict):
            continue
        action = _clean_text(raw.get("action"), limit=40).lower()
        if action not in _REFERENCE_ACTIONS:
            continue
        row: dict[str, Any] = {
            "action": action,
            "role": _clean_text(raw.get("role"), limit=80),
            "character": _clean_text(raw.get("character"), limit=120),
            "reason": _clean_text(raw.get("reason")),
        }
        target = raw.get("target_index", raw.get("index"))
        if target is not None:
            try:
                row["target_index"] = int(target)
            except (TypeError, ValueError):
                pass
        selector = raw.get("replacement_selector")
        if isinstance(selector, dict):
            # Model-provided arbitrary URIs are intentionally ignored.  The
            # director must resolve an existing asset id / semantic selector.
            row["replacement_selector"] = {
                "asset_id": selector.get("asset_id"),
                "role": _clean_text(selector.get("role"), limit=80),
                "character": _clean_text(
                    selector.get("character"), limit=120),
            }
        output.append(row)
    return output


def normalize_generation_diagnostics(
        verdict: Any, *, issues: Any = None) -> dict[str, Any]:
    """Normalize new and legacy QC output without pretending legacy is complete.

    A passing legacy verdict remains valid.  A failing legacy verdict receives
    useful fallback ``image_error`` evidence, but ``diagnosis_complete`` stays
    false so the caller can avoid spending money on an unchanged blind retry.
    """
    verdict = verdict if isinstance(verdict, dict) else {}
    fallback_issues = _text_list(
        verdict.get("issues") if issues is None else issues)
    raw_keys = {
        "image_error": isinstance(
            verdict.get("image_error"), (dict, str)),
        "prompt_diagnosis": isinstance(
            verdict.get("prompt_diagnosis"), dict),
        "reference_diagnosis": isinstance(
            verdict.get("reference_diagnosis"), dict),
        "targeted_prompt_patch": isinstance(
            verdict.get("targeted_prompt_patch"), (dict, list, str)),
        "reference_adjustments": isinstance(
            verdict.get("reference_adjustments"), list),
    }
    prompt_diagnosis = _normalize_diagnosis(
        verdict.get("prompt_diagnosis"))
    reference_diagnosis = _normalize_diagnosis(
        verdict.get("reference_diagnosis"), adjustment=True)
    patch = _normalize_prompt_patch(verdict.get("targeted_prompt_patch"))
    adjustments = _normalize_reference_adjustments(
        verdict.get("reference_adjustments"))
    passed = bool(verdict.get("pass"))
    image_error = _normalize_image_error(
        verdict.get("image_error"), fallback_issues)
    diagnosis_complete = (
        passed
        or (
            all(raw_keys.values())
            and bool(image_error.get("summary")
                     or image_error.get("evidence"))
            and prompt_diagnosis.get("status") != "unknown"
            and reference_diagnosis.get("status") != "unknown"
        )
    )
    return {
        "schema": GENERATION_DIAGNOSTICS_SCHEMA,
        "image_error": image_error,
        "prompt_diagnosis": prompt_diagnosis,
        "reference_diagnosis": reference_diagnosis,
        "targeted_prompt_patch": patch,
        "reference_adjustments": adjustments,
        "diagnosis_complete": diagnosis_complete,
    }


def targeted_prompt_patch(diagnostics: Any) -> str:
    """Render only the concise patch intended for the generation model."""
    normalized = (
        diagnostics
        if isinstance(diagnostics, dict)
        and diagnostics.get("schema") == GENERATION_DIAGNOSTICS_SCHEMA
        else normalize_generation_diagnostics(diagnostics))
    patch = normalized.get("targeted_prompt_patch") or {}
    instructions = _text_list(patch.get("instructions"), limit=6,
                              item_limit=800)
    preserve = _text_list(patch.get("preserve"), limit=12, item_limit=160)
    parts = []
    if instructions:
        parts.append("【本镜定向修正】" + "；".join(instructions))
    if preserve:
        parts.append("【保持不变】" + "、".join(preserve))
    if parts:
        parts.append("【范围】只修改当前镜头，不补写整集剧情或其他镜头")
    return "\n".join(parts)


def reference_actions(diagnostics: Any) -> list[dict[str, Any]]:
    """Return normalized, URI-free reference adjustment proposals."""
    normalized = (
        diagnostics
        if isinstance(diagnostics, dict)
        and diagnostics.get("schema") == GENERATION_DIAGNOSTICS_SCHEMA
        else normalize_generation_diagnostics(diagnostics))
    return list(normalized.get("reference_adjustments") or [])


def retry_input_decision(prompt: Any, references: Any, diagnostics: Any,
                         *, previous_input_hash: str = "") -> dict[str, Any]:
    """Describe whether a retry would materially change prompt or references.

    This does not apply reference changes.  It supplies the fail-closed signal
    used by the director before it launches another billable generation call.
    """
    normalized = (
        diagnostics
        if isinstance(diagnostics, dict)
        and diagnostics.get("schema") == GENERATION_DIAGNOSTICS_SCHEMA
        else normalize_generation_diagnostics(diagnostics))
    prompt_text = str(prompt or "")
    current_prompt_hash = prompt_hash(prompt_text)
    current_reference_hash = reference_hash(references)
    current_input_hash = generation_input_hash(prompt_text, references)
    patch = targeted_prompt_patch(normalized)
    revised_prompt = (
        prompt_text if not patch or patch in prompt_text
        else f"{prompt_text.rstrip()}\n{patch}")
    revised_prompt_hash = prompt_hash(revised_prompt)
    actions = reference_actions(normalized)
    actionable = [
        item for item in actions
        if item.get("action") in _ACTIONABLE_REFERENCE_ACTIONS]
    prompt_changed = revised_prompt_hash != current_prompt_hash
    references_changed = bool(actionable)
    same_as_previous_attempt = bool(
        previous_input_hash and previous_input_hash == current_input_hash)
    changes_input = prompt_changed or references_changed
    blocked = not normalized.get("diagnosis_complete") or not changes_input
    if not normalized.get("diagnosis_complete"):
        reason = "质检未完整分析提示词和参考图，禁止盲目原样重试"
    elif not changes_input:
        reason = "修订后提示词与参考图均未变化，禁止原样重试"
    else:
        reason = ""
    return {
        "diagnosis_complete": bool(normalized.get("diagnosis_complete")),
        "prompt_hash": current_prompt_hash,
        "reference_hash": current_reference_hash,
        "input_hash": current_input_hash,
        "revised_prompt_hash": revised_prompt_hash,
        "targeted_prompt_patch": patch,
        "reference_actions": actions,
        "prompt_changed": prompt_changed,
        "references_changed": references_changed,
        "changes_input": changes_input,
        "same_as_previous_attempt": same_as_previous_attempt,
        "retry_blocked": blocked,
        "retry_blocked_reason": reason,
    }
