from aifos.prop_policy import (
    build_prop_asset_plan,
    build_prop_contract,
    validate_prop_contract,
)
from aifos.prompt_contract import compile_shot_prompt
from aifos.standard_center import DEFAULT_STANDARD, StandardCenter


def test_prop_plan_promotes_text_interaction_and_repeated_props():
    script = {
        "characters": [{"name": "乔安", "signature_props": ["银色笔记本电脑"]}],
        "scenes": [
            {
                "scene_no": 1,
                "location": "书房",
                "props": [
                    {"name": "银色笔记本电脑", "text_required": True},
                    {"name": "普通茶杯"},
                ],
            }
        ],
    }
    storyboard = {
        "shots": [
            {"shot_no": 1, "description": "乔安打开银色笔记本电脑"},
            {"shot_no": 2, "description": "乔安合上银色笔记本电脑"},
        ]
    }
    plan = build_prop_asset_plan(
        script, storyboard, DEFAULT_STANDARD["rules"]["props"])
    by_name = {item["name"]: item for item in plan["items"]}

    assert by_name["银色笔记本电脑"]["tier"] == "A"
    assert by_name["银色笔记本电脑"]["requires_separate_asset"] is True
    assert by_name["银色笔记本电脑"]["asset_outputs"]
    assert by_name["普通茶杯"]["tier"] == "C"
    assert by_name["普通茶杯"]["asset_outputs"] == []


def test_prop_contract_preflight_reports_missing_state_position_and_asset():
    shot = {
        "shot_no": 5,
        "props": [{"name": "银色笔记本电脑", "interaction": True, "text_required": True}],
        "readable_text": {"required": True, "carrier": "电脑屏幕", "whitelist": ["《明季北略》"]},
    }
    plan = {
        "items": [{
            "name": "银色笔记本电脑",
            "tier": "A",
            "text_required": True,
            "interaction": True,
            "requires_separate_asset": True,
        }]
    }
    codes = {item["code"] for item in validate_prop_contract(shot, plan)}
    assert {"prop_contract_missing", "interaction_state_missing",
            "interaction_position_missing", "asset_reference_missing"} <= codes


def test_prop_contract_is_rendered_before_physical_and_text_rules():
    shot = {
        "shot_no": 1,
        "props": [{
            "name": "银色笔记本电脑",
            "state": "屏幕打开",
            "owner": "乔安",
            "position": "桌面中央",
            "action": "阅读",
        }],
        "readable_text": {
            "required": True,
            "carrier": "电脑屏幕",
            "whitelist": ["《明季北略》"],
        },
    }
    contract = build_prop_contract(shot)
    assert contract["schema"] == "aifos.prop-contract/v1"
    _, prompt = compile_shot_prompt(shot, location="现代书房")
    assert "【道具合同】" in prompt
    assert "《明季北略》" in prompt
    assert prompt.index("【道具合同】") < prompt.index("【文字】")


def test_default_standard_contains_prop_policy_without_new_gate_id():
    issues = StandardCenter.validate(DEFAULT_STANDARD)
    assert not [issue for issue in issues if issue["path"].startswith("rules.props")]
    assert "props" not in [
        gate["id"] for gate in DEFAULT_STANDARD["rules"]["quality_gates"]
    ]
