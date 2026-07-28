"""可披露窗口:「藏而未露」不是披露,定格按尾态对齐。

《长夏记事》实案——银铃缝在衣襟内侧(hidden),编剧把可披露起点设在
铃铛掉出的第5镜;旧校验把「实体存在但隐藏」也当披露,前4镜的隐藏行
全被判「在首次可披露事件之前出现」,两条编剧产线同一错误全灭整集卡死。
伏笔类道具(信物/暗器/藏物)在旧规则下必然无解:设在揭示镜→前面被拒,
设在第一镜→与揭示语义打架。
"""

from aifos.story_logic import (
    audit_storyboard_prop_contract,
    normalize_storyboard_frame_phase_pairs,
)


_REVEAL_TRANSITION = [{
    "prop_id": "prop_bell_001", "from_phase": "start", "to_phase": "end",
    "action": "别针松脱，银铃连同红绳滑落至掌心，由隐藏转为可见",
}]


def _bell_storyboard(bell_rows, transitions_at=5):
    """一场五镜;银铃在第5镜尾态才掉出来变可见。"""
    shots = []
    for index in range(1, 6):
        rows = bell_rows.get(index, [])
        shot = {
            "shot_no": index, "scene_no": 1,
            "scene_event_id": "event_001",
            "event_id": f"sh{index:02d}",
            "frame_props": rows,
        }
        # 起止可见性变化的镜头必须登记 transition(独立既有规则)。
        if rows and index == transitions_at:
            visibilities = {row["visibility"] for row in rows}
            if len(visibilities) > 1:
                shot["prop_transitions"] = [
                    dict(item) for item in _REVEAL_TRANSITION]
        shots.append(shot)
    return {
        "shots": shots,
        "prop_registry": [{
            "prop_id": "prop_bell_001", "name": "旧银铃",
            "kind": "core", "instance_count": 1,
            "introduced_at": {"event_id": "sh05", "phase": "end"},
            "availability_start_event": {
                "event_id": "sh05", "phase": "end"},
            "retired_at": None, "availability_end_event": None,
            "disclosure_policy": "explicit_frame_only",
        }],
    }


def _row(phase, visibility):
    return {
        "prop_id": "prop_bell_001", "phase": phase,
        "visibility": visibility, "representation": "physical",
        "physical_state": "完好", "holder": "沈眉",
        "location": "衣襟内侧", "support": "衣料",
    }


def _issues(storyboard):
    normalize_storyboard_frame_phase_pairs(storyboard)
    report = audit_storyboard_prop_contract(storyboard)
    return report.get("issues") if isinstance(report, dict) else report


def test_hidden_prop_before_disclosure_is_legal():
    """伏笔:道具实体在场但 hidden,不受可披露窗口约束。"""
    storyboard = _bell_storyboard({
        1: [_row("start", "hidden"), _row("end", "hidden")],
        3: [_row("start", "hidden"), _row("end", "hidden")],
        5: [_row("start", "hidden"), _row("end", "visible")],
    })
    assert _issues(storyboard) == []


def test_visible_prop_before_disclosure_still_rejected():
    """真违规仍要拦:揭示前就让观众看见,是穿帮。"""
    storyboard = _bell_storyboard({
        1: [_row("start", "visible"), _row("end", "visible")],
        5: [_row("start", "hidden"), _row("end", "visible")],
    })
    issues = _issues(storyboard)
    assert any("在首次可披露事件之前出现" in issue for issue in issues), issues


def test_occluded_before_disclosure_still_rejected():
    """occluded=在画面里但被挡住,仍属已披露,窗口照管。"""
    storyboard = _bell_storyboard({
        2: [_row("start", "occluded"), _row("end", "occluded")],
        5: [_row("start", "hidden"), _row("end", "visible")],
    })
    issues = _issues(storyboard)
    assert any("在首次可披露事件之前出现" in issue for issue in issues), issues


def test_freeze_row_aligns_with_end_state():
    """定格=本镜尾态(既定裁决):尾态才可披露的道具,其定格行不算越界。

    时间轴上 freeze 排在 end 之前,不按裁决对齐就会把回填出来的定格行
    判成「早于可披露点」——纯排序假象。
    """
    storyboard = _bell_storyboard({
        5: [_row("start", "hidden"), _row("end", "visible")],
    })
    # 回填会按尾态补出 freeze=visible 行。
    normalize_storyboard_frame_phase_pairs(storyboard)
    freeze_rows = [
        row for row in storyboard["shots"][4]["frame_props"]
        if row["phase"] == "freeze"]
    assert freeze_rows and freeze_rows[0]["visibility"] == "visible"
    report = audit_storyboard_prop_contract(storyboard)
    issues = report.get("issues") if isinstance(report, dict) else report
    assert issues == []


def test_absent_prop_needs_no_availability():
    storyboard = _bell_storyboard({
        1: [_row("start", "absent"), _row("end", "absent")],
        5: [_row("start", "hidden"), _row("end", "visible")],
    })
    assert _issues(storyboard) == []


# ---------- 分镜就地修复:镜头级增量,不重发整份 ----------

def test_error_shot_positions_parsed():
    from aifos.production.api_providers import OpenAIChatProvider
    error = ("镜头1.frame_props[2] 在首次可披露事件之前出现；"
             "镜头12的 prop_x 起止状态变化但缺少 start→end prop_transitions；"
             "镜头1.frame_props[5] 重复")
    assert OpenAIChatProvider._error_shot_positions(error) == [1, 12]
    assert OpenAIChatProvider._error_shot_positions("") == []
    assert OpenAIChatProvider._error_shot_positions(None) == []


def test_repair_shots_sends_only_broken_shots_and_merges():
    """整集分镜远超输出上限;只回传坏镜头再本地合并。"""
    from aifos.production.api_providers import OpenAIChatProvider

    provider = OpenAIChatProvider.__new__(OpenAIChatProvider)
    sent = {}

    def fake_chat(messages):
        sent["payload"] = messages[-1]["content"]
        return ('{"shots":[{"_position":2,"shot_no":2,"fixed":true}]}')

    provider._chat = fake_chat
    provider._postprocess = lambda capability, value: value
    provider._validate = lambda capability, payload, value: ""

    data = {"shots": [
        {"shot_no": 1, "keep": "a"},
        {"shot_no": 2, "keep": "b"},
        {"shot_no": 3, "keep": "c"},
    ]}
    merged, note = provider._repair_shots(
        {}, data, "镜头2.frame_props[1] 在首次可披露事件之前出现")

    assert merged["shots"][1] == {"shot_no": 2, "fixed": True}
    # 未点名的镜头一字不动。
    assert merged["shots"][0] == {"shot_no": 1, "keep": "a"}
    assert merged["shots"][2] == {"shot_no": 3, "keep": "c"}
    assert "合并 1 个镜头" in note
    # 只把坏镜头发出去,省掉整份重发。
    assert '"shot_no": 2' in sent["payload"]
    assert '"keep": "a"' not in sent["payload"]


def test_repair_shots_declines_when_everything_is_broken():
    """整份都坏就没有省的余地,交回整份重发路径。"""
    from aifos.production.api_providers import OpenAIChatProvider
    provider = OpenAIChatProvider.__new__(OpenAIChatProvider)
    data = {"shots": [{"shot_no": 1}, {"shot_no": 2}]}
    merged, note = provider._repair_shots({}, data, "镜头1.x；镜头2.y")
    assert merged is None and note == ""
