from aifos.high_value_events import audit_high_value_event_coverage
from aifos.high_value_events import validate_high_value_event_contract
from aifos.adapters.claude_script import build_prompt
from aifos.storyboard_preflight import preflight_storyboard


def _script():
    return {
        "high_value_events": [{
            "event_id": "game_draw",
            "scene_no": 4,
            "minimum_independent_shots": 4,
            "required_beats": [
                {"beat_id": "open", "role": "rule_setup"},
                {"beat_id": "a_draw", "role": "attempt"},
                {"beat_id": "retry", "role": "escalation"},
                {"beat_id": "ss_draw", "role": "payoff"},
            ],
        }],
    }


def _shot(number, beat):
    return {
        "shot_no": number,
        "scene_no": 4,
        "high_value_event_id": "game_draw",
        "event_beat_ids": [beat],
        "must_visualize": True,
    }


def test_high_value_event_requires_process_not_only_payoff():
    report = audit_high_value_event_coverage(
        _script(), {"shots": [_shot(1, "ss_draw")]})

    assert report["passed"] is False
    assert report["events"][0]["missing_beats"] == [
        "open", "a_draw", "retry"]
    assert "至少需要4个独立镜头" in "；".join(report["issues"])


def test_high_value_event_four_beat_process_passes():
    storyboard = {"shots": [
        _shot(1, "open"), _shot(2, "a_draw"),
        _shot(3, "retry"), _shot(4, "ss_draw"),
    ]}

    assert audit_high_value_event_coverage(_script(), storyboard)["passed"]


def test_storyboard_preflight_exposes_missing_high_value_event_without_shot():
    report = preflight_storyboard(_script(), {"shots": []})

    assert report["passed"] is False
    assert report["high_value_event_coverage"]["passed"] is False
    assert any(item["kind"] == "high_value_event"
               for item in report["issues"])


def test_legacy_script_without_declared_event_remains_compatible():
    report = audit_high_value_event_coverage({}, {"shots": []})

    assert report["passed"] is True
    assert report["declared_event_count"] == 0


def test_game_draw_signal_requires_script_contract():
    issues = validate_high_value_event_contract({
        "scenes": [{"scene_no": 4, "action": "连续抽取后获得SS级天赋"}],
    })

    assert issues == ["场4含高价值事件信号但未建立 high_value_events 合同"]


def test_script_and_storyboard_prompts_both_carry_highest_rule():
    script_prompt = build_prompt("script", {
        "project_title": "游戏入侵", "episode_number": 1})
    storyboard_prompt = build_prompt("storyboard", {"script": {}})

    assert "AIFOS最高通用创作规则：高价值事件必须展开" in script_prompt
    assert "最高规则优先于长镜头" in storyboard_prompt
    assert "foldable_into_long_take:false" in storyboard_prompt
