"""Structured prompt/reference diagnosis and unchanged-retry guards."""

from aifos.generation_diagnostics import (
    GENERATION_DIAGNOSTICS_SCHEMA,
    generation_input_hash,
    normalize_generation_diagnostics,
    prompt_hash,
    reference_actions,
    reference_hash,
    retry_input_decision,
    targeted_prompt_patch,
)


def _complete_verdict():
    return {
        "pass": False,
        "issues": ["人物服装错误"],
        "image_error": {
            "summary": "人物服装错误",
            "categories": ["wardrobe"],
            "evidence": ["画面左侧人物穿了现代西装"],
        },
        "prompt_diagnosis": {
            "status": "needs_patch",
            "issues": ["服装要求不够明确"],
            "irrelevant_or_conflicting_sections": ["现代商务感"],
        },
        "reference_diagnosis": {
            "status": "needs_adjustment",
            "issues": ["图3是错误服装图"],
            "missing_roles": [],
        },
        "targeted_prompt_patch": {
            "instructions": ["李继周改穿明代圆领袍"],
            "preserve": ["人物脸", "站位", "机位"],
        },
        "reference_adjustments": [{
            "action": "replace",
            "target_index": 3,
            "role": "wardrobe",
            "character": "李继周",
            # Arbitrary model-provided URI must never survive normalization.
            "uri": "/tmp/invented.png",
            "replacement_selector": {
                "asset_id": 42,
                "role": "wardrobe",
                "character": "李继周",
                "uri": "/tmp/also-invented.png",
            },
            "reason": "换回本集锁定服装",
        }],
    }


def test_normalizes_complete_diagnosis_and_removes_arbitrary_reference_uris():
    result = normalize_generation_diagnostics(_complete_verdict())

    assert result["schema"] == GENERATION_DIAGNOSTICS_SCHEMA
    assert result["diagnosis_complete"] is True
    assert result["image_error"]["categories"] == ["wardrobe"]
    assert result["prompt_diagnosis"]["status"] == "needs_patch"
    assert result["reference_diagnosis"]["status"] == "needs_adjustment"
    action = reference_actions(result)[0]
    assert action["action"] == "replace"
    assert action["target_index"] == 3
    assert action["replacement_selector"]["asset_id"] == 42
    assert "uri" not in action
    assert "uri" not in action["replacement_selector"]


def test_legacy_failure_is_preserved_but_marked_incomplete():
    result = normalize_generation_diagnostics({
        "pass": False, "issues": ["多出一人"]})

    assert result["image_error"]["summary"] == "多出一人"
    assert result["image_error"]["evidence"] == ["多出一人"]
    assert result["diagnosis_complete"] is False


def test_hashes_are_stable_and_reference_order_is_significant():
    refs = [
        {"index": 1, "uri": "/a.png", "role": "identity",
         "character": "甲", "binding": "只锁脸"},
        {"index": 2, "uri": "/b.png", "role": "scene",
         "binding": "只锁场景"},
    ]
    assert prompt_hash("当前镜头动作") == prompt_hash("当前镜头动作")
    assert prompt_hash("当前镜头动作") != prompt_hash("当前镜头 动作")
    assert reference_hash(refs) == reference_hash([dict(row) for row in refs])
    assert reference_hash(refs) != reference_hash(list(reversed(refs)))
    assert generation_input_hash("当前镜头", refs) == generation_input_hash(
        "当前镜头", refs)


def test_reference_hash_changes_when_phase_or_reference_duty_changes():
    base = [{
        "index": 1, "uri": "/locked/role.png", "role": "wardrobe",
        "kind": "character_sheet", "character": "乔安", "phase": "start",
        "inherits": ["face", "hair"], "excludes": ["camera"],
        "binding": "锁定本镜起点服装",
    }]
    changed_phase = [dict(base[0], phase="end")]
    changed_duty = [dict(base[0], inherits=["face", "hair", "makeup"])]
    changed_exclusion = [dict(base[0], excludes=["camera", "pose"])]

    assert reference_hash(base) != reference_hash(changed_phase)
    assert reference_hash(base) != reference_hash(changed_duty)
    assert reference_hash(base) != reference_hash(changed_exclusion)


def test_retry_decision_blocks_incomplete_or_unchanged_input():
    refs = [{"index": 1, "uri": "/a.png", "role": "identity"}]
    legacy = normalize_generation_diagnostics({
        "pass": False, "issues": ["脸不一致"]})
    blocked = retry_input_decision("当前镜头", refs, legacy)
    assert blocked["retry_blocked"] is True
    assert "未完整分析" in blocked["retry_blocked_reason"]

    complete_but_no_change = _complete_verdict()
    complete_but_no_change["targeted_prompt_patch"] = {
        "instructions": [], "preserve": []}
    complete_but_no_change["reference_adjustments"] = [
        {"action": "keep", "target_index": 1}]
    blocked = retry_input_decision(
        "当前镜头", refs, complete_but_no_change)
    assert blocked["diagnosis_complete"] is True
    assert blocked["changes_input"] is False
    assert blocked["retry_blocked"] is True
    assert "均未变化" in blocked["retry_blocked_reason"]


def test_retry_decision_emits_current_shot_patch_and_change_signal():
    verdict = _complete_verdict()
    refs = [{"index": 3, "uri": "/old.png", "role": "wardrobe"}]
    decision = retry_input_decision("镜头12：李继周转身", refs, verdict)

    assert decision["retry_blocked"] is False
    assert decision["prompt_changed"] is True
    assert decision["references_changed"] is True
    assert "李继周改穿明代圆领袍" in decision["targeted_prompt_patch"]
    assert "只修改当前镜头" in decision["targeted_prompt_patch"]
    assert targeted_prompt_patch(verdict) == decision["targeted_prompt_patch"]
