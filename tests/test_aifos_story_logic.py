"""剧本第一道总闸门：细节补全、道具生命周期与局部返编边界。"""

from aifos.adapters.claude_script import build_prompt
from aifos.story_logic import normalize_script_logic


def _minimal_script():
    return {
        "characters": [{"name": "甲", "role": "主角"}],
        "scenes": [{
            "scene_no": 1,
            "location": "书房",
            "characters": ["甲"],
            "action": "甲从桌上拿起钥匙，走到门前开锁。",
            "lines": [{"character": "甲", "dialogue": "找到了。"}],
        }],
    }


def test_legacy_script_gets_auditable_prop_and_rewrite_contract():
    script = _minimal_script()
    normalize_script_logic(script)
    logic = script["scenes"][0]["director_logic"]
    contract = logic["continuity_contract"]

    assert logic["information_state"]
    assert logic["time_continuity"]
    assert logic["missing_details_completed"]
    assert contract["entry_boundary"]
    assert contract["exit_boundary"]
    assert contract["prop_ledger"]
    assert contract["knowledge_state"]
    assert contract["time_state"]
    assert "只修改本场" in contract["local_rewrite_scope"]
    assert script["script_logic_audit"]["schema"] == "aifos.script-logic/v2"
    assert script["script_logic_audit"]["passed"] is True
    assert "道具生命周期" in script["script_logic_audit"]["summary"]


def test_claimed_writer_review_cannot_leave_new_fields_to_fallback():
    script = _minimal_script()
    script["adaptation_review"] = {
        "source_to_screen_strategy": "把叙述改成可见动作",
        "causal_chain": "触发到结果",
        "character_motivation": "目标驱动行动",
        "physical_reality": "检查支撑与接触",
        "spatial_continuity": "检查入口出口",
        "shootability": "核心事件可被拍到",
        "self_reviewed": True,
    }
    script["scenes"][0]["director_logic"] = {
        "dramatic_function": "找到开门工具",
        "entry_state": "甲站在桌边，空手",
        "physical_actions": "甲拿钥匙后走到门前开锁",
        "prop_continuity": "钥匙来自桌面，由甲拿走",
        "spatial_logic": "桌在左侧，门在右侧，甲沿直线路径走过去",
        "exit_state": "甲站在打开的门前，右手持钥匙",
        "director_intent": "用拿钥匙和开门动作拍出发现",
    }

    normalize_script_logic(script)

    report = script["script_logic_audit"]
    assert report["passed"] is False
    assert any(
        "仍由平台兜底" in issue for issue in report["issues"])
    assert any(
        "information_state" in issue for issue in report["issues"])
    assert any(
        "continuity_contract" in issue for issue in report["issues"])


def test_source_adaptation_prompt_makes_props_and_local_rewrite_explicit():
    prompt = build_prompt("script", {
        "project_title": "测试剧",
        "episode_number": 1,
        "previous_script": _minimal_script(),
        "feedback": "完善为可拍剧本",
        "source_material_adaptation": True,
    })

    assert "不是已锁定正式剧本" in prompt
    assert "关键道具建立完整生命周期" in prompt
    assert "人物信息状态" in prompt
    assert "局部返编问题场" in prompt
    assert '"continuity_contract"' in prompt
