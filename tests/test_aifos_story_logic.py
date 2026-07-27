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


def test_prop_phase_aliases_normalize_before_audit():
    """模型写 begin/开场/scene_start 等同义 phase → 本地归一,不丢弃剧本。"""
    from aifos.story_logic import audit_prop_contract, normalize_prop_contract

    def registry_with(phase):
        return {
            "scenes": [{"scene_no": 1, "event_id": "scene:1"}],
            "prop_registry": [{
                "prop_id": "prop-letter", "name": "血书", "kind": "core",
                "instance_count": 1,
                "availability_start_event": {
                    "event_id": "episode-start", "phase": phase},
                "disclosure_policy": "explicit_frame_only"}]}

    for alias, expected in (("begin", "start"), ("开场", "start"),
                            ("scene_start", "start"), ("Retired", "end"),
                            ("尾帧", "end"), ("定格", "freeze")):
        script = registry_with(alias)
        normalize_prop_contract(script)
        got = script["prop_registry"][0][
            "availability_start_event"]["phase"]
        assert got == expected, (alias, got)
        assert not [issue for issue
                    in audit_prop_contract(script)["issues"]
                    if "phase" in issue]

    # 语义不明的值保留原样,交给校验(继而就地修复),不得瞎猜
    script = registry_with("midway")
    normalize_prop_contract(script)
    assert script["prop_registry"][0][
        "availability_start_event"]["phase"] == "midway"
    assert [issue for issue in audit_prop_contract(script)["issues"]
            if "phase" in issue]


def test_frame_phase_pairs_backfilled_locally():
    """道具时间线缺失端按"无状态变化"克隆回填;真矛盾仍留给 audit。"""
    from aifos.story_logic import normalize_storyboard_frame_phase_pairs
    sb = {"shots": [
        {"shot_no": 2, "frame_props": [
            {"prop_id": "P1", "phase": "start", "visibility": "visible",
             "holder": "阿砚", "physical_state": "完好"}]},
        {"shot_no": 3, "frame_props": [
            {"prop_id": "P1", "phase": "freeze", "visibility": "absent",
             "physical_state": "完好"}]},
        {"shot_no": 4, "frame_props": [          # 成对的不动
            {"prop_id": "P1", "phase": "start", "visibility": "visible"},
            {"prop_id": "P1", "phase": "end", "visibility": "visible"}]},
    ]}
    normalize_storyboard_frame_phase_pairs(sb)
    s2 = sb["shots"][0]["frame_props"]
    assert any(r["phase"] == "end" and r.get("phase_backfilled")
               and r["holder"] == "阿砚" for r in s2)
    s3 = sb["shots"][1]["frame_props"]
    assert {r["phase"] for r in s3} == {"freeze", "start", "end"}
    assert len(sb["shots"][2]["frame_props"]) == 2   # 未回填


def test_synthesized_beat_shots_inherit_scene_event_id():
    """节拍镜是 workflow 合成的,必须继承剧本场次稳定事件号。

    否则前置校验以「scene_event_id 与剧本稳定事件不一致」拦下整份
    分镜(《雨夜凶杀》镜头5/13/19 的真实事故)。
    """
    from aifos.workflow import _append_performance_beats
    script = {"scenes": [{
        "scene_no": 1, "event_id": "YYXS-E01-S01-SHELTER",
        "characters": ["林川"], "location": "屋檐下",
        "lines": [{"character": "林川", "dialogue": "雨太大了。"}]}]}
    shots = [{"shot_no": 1, "scene_no": 1, "kind": "dialogue",
              "description": "林川躲雨", "camera": "中景",
              "duration": 3.0, "characters": ["林川"],
              "dialogue": {"character": "林川", "dialogue": "雨太大了。"},
              "prompt": "p"}]
    out = _append_performance_beats(shots, script)
    beats = [s for s in out if s.get("kind") == "beat"]
    assert beats, "应生成场末节拍镜"
    for beat in beats:
        assert beat.get("scene_event_id") == "YYXS-E01-S01-SHELTER"
