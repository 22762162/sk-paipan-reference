"""Cross-episode facts must reach the actual script prompt, compactly."""

from aifos.adapters.claude_script import build_prompt


def test_script_prompt_includes_only_compact_previous_episode_facts():
    payload = {
        "project_title": "承接测试",
        "episode_number": 2,
        "premise": "继续密诏危机",
        "previous_episode_continuity": {
            "schema": "aifos.episode-continuity-input/v1",
            "previous_episode_id": "episode-1",
            "previous_episode_number": 1,
            "previous_exit_state": "林昭右臂受伤，门半开。",
            "unresolved_hooks": ["门外人影是谁", "密诏落款来源"],
            "states": [{
                "domain": "character",
                "entity_id": "林昭",
                "state": "右臂受伤",
                "evidence": "第9镜",
            }],
            "instructions": ["internal-only-instruction"],
            "source_versions": {"script": 4},
            "irrelevant_full_previous_script": "SHOULD_NOT_LEAK",
        },
    }

    prompt = build_prompt("script", payload)

    assert "【紧邻前集连续性硬约束】" in prompt
    assert "林昭右臂受伤，门半开。" in prompt
    assert "门外人影是谁" in prompt
    assert "不得复活已毁坏道具" in prompt
    assert "SHOULD_NOT_LEAK" not in prompt
    assert "internal-only-instruction" not in prompt
    assert "source_versions" not in prompt


def test_first_episode_script_prompt_has_no_previous_episode_section():
    prompt = build_prompt("script", {
        "project_title": "首集",
        "episode_number": 1,
        "premise": "故事开始",
    })

    assert "【紧邻前集连续性硬约束】" not in prompt
