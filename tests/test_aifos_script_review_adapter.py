"""Independent script-review transport contract."""

from aifos.adapters.claude_script import (
    build_prompt,
    validate_independent_script_review,
)


def _reviews():
    return {
        name: {
            "score": 4,
            "evidence": [f"{name} 的逐场证据"],
            "directed_revision": [f"定向优化 {name}"],
        }
        for name in (
            "causal_chain", "conflict_density", "character_arc",
            "dialogue_quality", "hook_strength")
    }


def test_review_prompt_is_review_only_and_contains_frozen_script():
    prompt = build_prompt("script", {
        "independent_script_review": True,
        "script_version": "7",
        "script": {"scenes": [{"action": "虞寻歌抽中盗神"}]},
    })
    assert "独立剧本评审" in prompt
    assert "虞寻歌抽中盗神" in prompt
    assert "不续写、不改写" in prompt
    assert "dimension_reviews" in prompt


def test_review_validator_requires_exact_five_dimensions_with_evidence():
    assert validate_independent_script_review({
        "dimension_reviews": _reviews()}) is None
    broken = _reviews()
    broken.pop("hook_strength")
    assert "完整返回固定五维" in validate_independent_script_review({
        "dimension_reviews": broken})
    broken = _reviews()
    broken["causal_chain"]["evidence"] = []
    assert "evidence" in validate_independent_script_review({
        "dimension_reviews": broken})
