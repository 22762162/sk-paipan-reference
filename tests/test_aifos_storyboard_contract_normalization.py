"""Regression coverage for deterministic five-dimensional storyboard repair.

These cases mirror the failures that stopped ``游戏入侵`` at storyboard
generation: temporal-beat phase names leaked into static frame targets, props
were visible before their registry disclosure boundary, state changes omitted
their transition, and a model repair returned a malformed ``shots`` value.
All four are deterministic contract-shape problems and must not require a
second creative-model pass.
"""

from __future__ import annotations

import copy
import json

import pytest

from aifos.adapters import claude_script
from aifos.adapters.claude_script import (
    _merge_storyboard_full_repair,
    _merge_storyboard_shot_repairs,
    _repair_with_engine,
    _storyboard_error_fields,
    validate_storyboard,
)
from aifos.production.api_providers import OpenAIChatProvider
from aifos.story_logic import (
    audit_storyboard_prop_contract,
    normalize_storyboard_contract,
    reconcile_storyboard_prop_registry,
)


def _frame_targets(*, keyframe="freeze", first="start", last="end"):
    return {
        "keyframe": {
            "phase": keyframe,
            "state": "朱漆密函在画面中完成状态定格",
            "fallback": False,
        },
        "first_frame": {
            "phase": first,
            "state": "朱漆密函合拢放在案面",
            "fallback": False,
        },
        "last_frame": {
            "phase": last,
            "state": "朱漆密函已打开并由陆沉右手托住",
            "fallback": False,
        },
    }


def _shot(number, *, targets=None, frame_props=None, transitions=None):
    shot = {
        "shot_no": number,
        "scene_no": 1,
        "scene_event_id": "scene:1",
        "event_id": f"shot-{number:03d}",
        "duration": 8,
        "description": "陆沉从案面拿起朱漆密函并打开",
        "prompt": f"第{number}镜",
        "characters": ["陆沉"],
        "frame_targets": targets or _frame_targets(),
        "frame_props": frame_props or [],
        "prop_transitions": transitions or [],
    }
    return shot


def _prop_row(phase, *, state, holder, location, visibility="visible"):
    return {
        "prop_id": "prop-red-case",
        "phase": phase,
        "visibility": visibility,
        "representation": "physical",
        "physical_state": state,
        "holder": holder,
        "location": location,
        "support": "案面" if holder == "none" else "陆沉右手",
    }


def _registry(start_event="shot-003", start_phase="end"):
    start = {"event_id": start_event, "phase": start_phase}
    return [{
        "prop_id": "prop-red-case",
        "name": "朱漆密函",
        "kind": "core",
        "instance_count": 1,
        "introduced_at": copy.deepcopy(start),
        "availability_start_event": copy.deepcopy(start),
        "retired_at": None,
        "availability_end_event": None,
        "disclosure_policy": "explicit_frame_only",
    }]


def test_storyboard_contract_normalizes_phase_aliases_disclosure_and_transition():
    """Known model slips become a valid, auditable contract without another LLM."""
    storyboard = {
        "episode_title": "游戏入侵",
        "prop_registry": _registry(),
        "shots": [
            _shot(
                1,
                targets=_frame_targets(
                    keyframe="main", first="setup", last="settle"),
                frame_props=[
                    _prop_row(
                        "setup", state="完好合拢", holder="none",
                        location="书案中央"),
                    _prop_row(
                        "main", state="已打开", holder="陆沉",
                        location="陆沉身前"),
                    _prop_row(
                        "settle", state="已打开", holder="陆沉",
                        location="陆沉身前"),
                ],
            ),
            _shot(2),
            _shot(3),
        ],
    }

    source_script = {
        "scenes": [{"scene_no": 1, "event_id": "scene:1"}],
        "prop_registry": _registry("scene:1", "end"),
    }
    returned = reconcile_storyboard_prop_registry(storyboard, source_script)

    assert returned is storyboard
    targets = storyboard["shots"][0]["frame_targets"]
    assert targets["first_frame"]["phase"] == "start"
    assert targets["keyframe"]["phase"] == "freeze"
    assert targets["last_frame"]["phase"] == "end"
    assert {
        row["phase"] for row in storyboard["shots"][0]["frame_props"]
    } == {"start", "freeze", "end"}

    # The registry claimed shot 3, but the exact same-scene prop is already
    # visibly disclosed at shot 1 start. Both lifecycle aliases must agree.
    expected_start = {"event_id": "shot-001", "phase": "start"}
    prop = storyboard["prop_registry"][0]
    assert prop["availability_start_event"] == expected_start
    assert prop["introduced_at"] == expected_start

    transitions = storyboard["shots"][0]["prop_transitions"]
    assert len(transitions) == 1
    assert transitions[0]["prop_id"] == "prop-red-case"
    assert transitions[0]["from_phase"] == "start"
    assert transitions[0]["to_phase"] == "end"
    assert transitions[0]["action"].strip()

    report = audit_storyboard_prop_contract(storyboard)
    assert report["passed"] is True, report["issues"]
    assert validate_storyboard(storyboard) is None

    # Normalization runs on initial output and repaired output, so it must not
    # keep moving boundaries or append duplicate transitions on later passes.
    snapshot = copy.deepcopy(storyboard)
    reconcile_storyboard_prop_registry(storyboard, source_script)
    assert storyboard == snapshot


def test_hidden_presence_does_not_move_disclosure_before_visible_reveal():
    """A concealed physical prop exists, but its first disclosure is still later."""
    storyboard = {
        "prop_registry": _registry("shot-002", "end"),
        "shots": [
            _shot(1, frame_props=[
                _prop_row(
                    phase, state="完好合拢", holder="陆沉",
                    location="陆沉衣袍内侧", visibility="hidden")
                for phase in ("start", "freeze", "end")
            ]),
            _shot(
                2,
                frame_props=[
                    _prop_row(
                        "start", state="完好合拢", holder="陆沉",
                        location="陆沉衣袍内侧", visibility="hidden"),
                    _prop_row(
                        "freeze", state="已取出", holder="陆沉",
                        location="陆沉身前", visibility="visible"),
                    _prop_row(
                        "end", state="已取出", holder="陆沉",
                        location="陆沉身前", visibility="visible"),
                ],
                transitions=[{
                    "prop_id": "prop-red-case",
                    "from_phase": "start",
                    "to_phase": "end",
                    "action": "陆沉从衣袍内侧取出朱漆密函",
                }],
            ),
        ],
    }
    source_script = {
        "scenes": [{"scene_no": 1, "event_id": "scene:1"}],
        "prop_registry": _registry("scene:1", "end"),
    }

    reconcile_storyboard_prop_registry(storyboard, source_script)

    expected = {"event_id": "shot-002", "phase": "end"}
    assert storyboard["prop_registry"][0][
        "availability_start_event"] == expected
    assert storyboard["prop_registry"][0]["introduced_at"] == expected
    report = audit_storyboard_prop_contract(storyboard)
    assert report["passed"] is True, report["issues"]


def test_state_delta_without_authored_action_is_not_laundered_into_transition():
    shot = _shot(1, frame_props=[
        _prop_row(
            "start", state="完好合拢", holder="none",
            location="书案中央"),
        _prop_row(
            "end", state="已打开", holder="陆沉",
            location="陆沉身前"),
    ])
    shot["description"] = "陆沉凝视前方"
    shot["prompt"] = "人物近景"
    storyboard = {"prop_registry": _registry("shot-001", "start"),
                  "shots": [shot]}

    normalize_storyboard_contract(storyboard)

    assert storyboard["shots"][0]["prop_transitions"] == []
    report = audit_storyboard_prop_contract(storyboard)
    assert any(
        "缺少 start→end prop_transitions" in issue
        for issue in report["issues"])


def test_body_attached_prop_uses_explicit_wrist_action_as_transition_evidence():
    """Episode 30: wrist action need not repeat the wrist-cord display name."""
    from aifos.director import Director

    prop_id = "prop_yxh_black_wristcord_01"
    prop = {
        "prop_id": prop_id,
        "name": "虞寻欢黑色腕绳",
        "kind": "core",
        "instance_count": 1,
        "introduced_at": {"event_id": "shot-001", "phase": "start"},
        "availability_start_event": {
            "event_id": "shot-001", "phase": "start"},
        "retired_at": None,
        "availability_end_event": None,
        "disclosure_policy": "explicit_frame_only",
    }
    shot = {
        "shot_no": 1,
        "scene_no": 1,
        "scene_event_id": "scene:1",
        "event_id": "shot-001",
        "description": "第二次脉搏到来，暗金细线从右腕内侧闭合成一圈。",
        "video_action": "暗金环痕在右腕闭合。",
        "characters": ["虞寻欢"],
        "frame_props": [{
            "prop_id": prop_id,
            "phase": "start",
            "physical_state": "偏移，腕内侧为正常皮肤",
            "holder": "虞寻欢",
            "location": "右腕腕骨上方",
            "support": "右腕皮肤",
            "visibility": "visible",
            "representation": "physical",
        }, {
            "prop_id": prop_id,
            "phase": "end",
            "physical_state": "偏移，暗金环痕闭合",
            "holder": "虞寻欢",
            "location": "右腕腕骨上方",
            "support": "右腕皮肤",
            "visibility": "visible",
            "representation": "physical",
        }],
        "prop_transitions": [],
    }
    storyboard = {"prop_registry": [copy.deepcopy(prop)], "shots": [shot]}
    script = {
        "scenes": [{"scene_no": 1, "event_id": "scene:1"}],
        "prop_registry": [copy.deepcopy(prop)],
    }

    reconcile_storyboard_prop_registry(storyboard, script)

    transitions = storyboard["shots"][0]["prop_transitions"]
    assert len(transitions) == 1
    assert transitions[0]["prop_id"] == prop_id
    assert transitions[0]["transition_backfilled"] is True
    assert "右腕" in transitions[0]["action"]
    report = audit_storyboard_prop_contract(storyboard)
    assert report["passed"] is True, report["issues"]

    director = Director.__new__(Director)
    ctx = {"script": script, "storyboard": storyboard}
    director._require_valid_storyboard_prop_contract(ctx)
    contract = director._shot_prop_contract(ctx, storyboard["shots"][0])
    assert contract["prop_transitions"][0]["prop_id"] == prop_id


@pytest.mark.parametrize("description", [
    "朱漆密函保持不动，陆沉推开房门离开",
    "禁止打开朱漆密函",
    "朱漆密函不得打开",
    "朱漆密函旁的房门被陆沉打开",
])
def test_unrelated_or_negated_action_cannot_authorize_prop_transition(
        description):
    shot = _shot(1, frame_props=[
        _prop_row(
            "start", state="完好合拢", holder="none",
            location="书案中央"),
        _prop_row(
            "end", state="完好合拢", holder="陆沉",
            location="门外"),
    ])
    shot["description"] = description
    storyboard = {"prop_registry": _registry("shot-001", "start"),
                  "shots": [shot]}

    normalize_storyboard_contract(storyboard)

    assert storyboard["shots"][0]["prop_transitions"] == []
    report = audit_storyboard_prop_contract(storyboard)
    assert any(
        "缺少 start→end prop_transitions" in issue
        for issue in report["issues"])


def test_exact_disclosure_boundary_is_not_widened_by_normalization():
    """An authored exact reveal stays strict; an early visible row is a leak."""
    storyboard = {
        "prop_registry": _registry("shot-002", "end"),
        "shots": [
            _shot(1, frame_props=[
                _prop_row(
                    phase, state="提前露出", holder="陆沉",
                    location="陆沉手中", visibility="visible")
                for phase in ("start", "freeze", "end")
            ]),
            _shot(2, frame_props=[
                _prop_row(
                    phase, state="正式揭示", holder="陆沉",
                    location="陆沉手中", visibility="visible")
                for phase in ("start", "freeze", "end")
            ]),
        ],
    }
    source_script = {
        "scenes": [{"scene_no": 1, "event_id": "scene:1"}],
        "prop_registry": _registry("shot-002", "end"),
    }

    reconcile_storyboard_prop_registry(storyboard, source_script)

    assert storyboard["prop_registry"][0][
        "availability_start_event"] == {
            "event_id": "shot-002", "phase": "end"}
    report = audit_storyboard_prop_contract(storyboard)
    assert any(
        "在首次可披露事件之前出现" in issue
        for issue in report["issues"])


def test_coarse_disclosure_boundary_never_moves_across_scenes():
    storyboard = {
        "prop_registry": _registry("scene:2", "start"),
        "shots": [
            _shot(1, frame_props=[
                _prop_row(
                    phase, state="提前露出", holder="陆沉",
                    location="陆沉手中", visibility="visible")
                for phase in ("start", "freeze", "end")
            ]),
            {**_shot(2), "scene_no": 2, "scene_event_id": "scene:2"},
        ],
    }
    source_script = {
        "scenes": [
            {"scene_no": 1, "event_id": "scene:1"},
            {"scene_no": 2, "event_id": "scene:2"},
        ],
        "prop_registry": _registry("scene:2", "start"),
    }

    reconcile_storyboard_prop_registry(storyboard, source_script)

    assert storyboard["prop_registry"][0][
        "availability_start_event"] == {
            "event_id": "scene:2", "phase": "start"}
    report = audit_storyboard_prop_contract(storyboard)
    assert any(
        "在首次可披露事件之前出现" in issue
        for issue in report["issues"])


def test_reconciled_registry_survives_enrichment_and_spend_preflight(tmp_path):
    """The exact lifecycle must not be overwritten again below the writer."""
    from aifos.app import App
    from aifos.director import Director
    from aifos.workflow import (
        build_continuity_bible,
        enrich_storyboard,
        production_profile,
    )

    script = {
        "project_title": "道具连续性测试",
        "episode_number": 1,
        "episode_title": "递函",
        "logline": "陆沉取得密函",
        "characters": [{"name": "陆沉", "role": "主角", "gender": "男"}],
        "scenes": [{
            "scene_no": 1,
            "event_id": "scene:1",
            "location": "书房",
            "characters": ["陆沉"],
            "action": "陆沉取得密函",
            "lines": [],
        }],
        "prop_registry": _registry("scene:1", "end"),
    }
    raw = {
        "prop_registry": _registry("shot-003", "end"),
        "shots": [_shot(1, frame_props=[
            _prop_row(
                "start", state="完好合拢", holder="none",
                location="书案中央"),
            _prop_row(
                "end", state="已打开", holder="陆沉",
                location="陆沉身前"),
        ])],
    }
    app = App(tmp_path / "ws")
    try:
        profile = production_profile(app.config, app.standards.active())
    finally:
        app.close()
    continuity = build_continuity_bible(
        {"title": "道具连续性测试", "style": ""}, script, profile)

    enriched = enrich_storyboard(script, raw, continuity, profile)

    expected = {"event_id": "shot-001", "phase": "start"}
    assert enriched["prop_registry"][0][
        "availability_start_event"] == expected
    assert enriched["shots"][0]["prop_registry"][0][
        "availability_start_event"] == expected
    # This is the final guard immediately before any paid generation API.
    director = Director.__new__(Director)
    ctx = {
        "script": script,
        "storyboard": enriched,
    }
    director._require_valid_storyboard_prop_contract(ctx)
    # A stale or malformed per-shot copy cannot bypass the audited top-level
    # registry that is passed to image/video prompt contracts.
    enriched["shots"][0]["prop_registry"][0][
        "availability_start_event"] = {
            "event_id": "episode-end", "phase": "end"}
    enriched["prop_registry"][0]["availability_start_event"] = {
        "event_id": "episode-end", "phase": "end"}
    contract = director._shot_prop_contract(ctx, enriched["shots"][0])
    assert contract["prop_registry"][0][
        "availability_start_event"] == expected


@pytest.mark.parametrize("malformed", [
    {"repair_summary": "已修复"},
    {"shots": "not-an-array"},
    {"shots": None},
])
def test_malformed_cli_repair_cannot_overwrite_original_shots(malformed):
    source = {
        "episode_title": "原始标题",
        "shots": [
            {"shot_no": 1, "prompt": "keep-1"},
            {"shot_no": 2, "prompt": "keep-2"},
        ],
    }
    before = copy.deepcopy(source)

    merged = _merge_storyboard_shot_repairs(source, malformed, positions=[2])

    assert merged is None
    assert source == before


def test_sparse_cli_shot_repair_merges_fields_without_replacing_whole_shot():
    """A valid patch may change one contract field, never erase the whole shot."""
    source = {
        "shots": [
            {
                "shot_no": 1,
                "duration": 8,
                "prompt": "保留原镜头提示词",
                "characters": ["陆沉"],
                "frame_targets": _frame_targets(),
            },
        ],
    }
    patch = {"shots": [{
        "_position": 1,
        "frame_targets": _frame_targets(keyframe="main"),
    }]}

    merged = _merge_storyboard_shot_repairs(source, patch, positions=[1])

    assert merged["shots"][0]["duration"] == 8
    assert merged["shots"][0]["prompt"] == "保留原镜头提示词"
    assert merged["shots"][0]["characters"] == ["陆沉"]
    assert merged["shots"][0]["frame_targets"]["keyframe"]["phase"] == "main"
    assert source["shots"][0]["frame_targets"]["keyframe"]["phase"] == "freeze"


def test_repair_empty_contract_arrays_cannot_erase_prop_rows():
    frame_rows = [
        _prop_row(
            "start", state="完好合拢", holder="none",
            location="书案中央"),
        _prop_row(
            "end", state="已打开", holder="陆沉",
            location="陆沉手中"),
    ]
    transitions = [{
        "prop_id": "prop-red-case",
        "from_phase": "start",
        "to_phase": "end",
        "action": "陆沉打开朱漆密函",
    }]
    source = {"shots": [_shot(
        1, frame_props=frame_rows, transitions=transitions)]}
    patch = {"shots": [{
        "_position": 1,
        "frame_props": [],
        "prop_transitions": [],
    }]}

    merged = _merge_storyboard_shot_repairs(
        source, patch, positions=[1],
        allowed_fields={"frame_props", "prop_transitions"})

    assert merged["shots"][0]["frame_props"] == frame_rows
    assert merged["shots"][0]["prop_transitions"] == transitions
    assert source["shots"][0]["frame_props"] == frame_rows


def test_repair_contract_arrays_merge_rows_by_stable_identity():
    source = {"shots": [_shot(1, frame_props=[
        _prop_row(
            "start", state="完好合拢", holder="none",
            location="书案中央"),
        _prop_row(
            "end", state="已打开", holder="陆沉",
            location="陆沉手中"),
    ], transitions=[{
        "prop_id": "prop-red-case",
        "from_phase": "start",
        "to_phase": "end",
        "action": "旧动作证据",
    }])]}
    patch = {"shots": [{
        "_position": 1,
        "frame_props": [{
            "prop_id": "prop-red-case",
            "phase": "end",
            "representation": "physical",
            "location": "陆沉身前",
        }],
        "prop_transitions": [{
            "prop_id": "prop-red-case",
            "from_phase": "start",
            "to_phase": "end",
            "action": "陆沉打开朱漆密函",
        }],
    }]}

    merged = _merge_storyboard_shot_repairs(
        source, patch, positions=[1],
        allowed_fields={"frame_props", "prop_transitions"})

    rows = merged["shots"][0]["frame_props"]
    assert len(rows) == 2
    assert rows[0]["location"] == "书案中央"
    assert rows[1]["location"] == "陆沉身前"
    assert merged["shots"][0]["prop_transitions"] == [{
        "prop_id": "prop-red-case",
        "from_phase": "start",
        "to_phase": "end",
        "action": "陆沉打开朱漆密函",
    }]


def test_shortened_full_repair_cannot_drop_unreturned_shots():
    source = {"shots": [
        {"shot_no": 1, "prompt": "keep-1"},
        {"shot_no": 2, "prompt": "keep-2"},
        {"shot_no": 3, "prompt": "keep-3"},
    ]}
    shortened = {"shots": [{
        "shot_no": 1,
        "frame_targets": {"keyframe": {"phase": "main"}},
        "prompt": "恶意改写",
    }, {
        "shot_no": 2,
        "frame_targets": {"keyframe": {"phase": "main"}},
        "prompt": "恶意改写",
        "characters": ["陌生人"],
        "scene_no": 99,
    }]}

    merged = _merge_storyboard_full_repair(
        source, shortened, positions=[2], allowed_fields={"frame_targets"})

    assert len(merged["shots"]) == 3
    assert merged["shots"][0] == {"shot_no": 1, "prompt": "keep-1"}
    assert merged["shots"][1] == {
        "shot_no": 2,
        "prompt": "keep-2",
        "frame_targets": {"keyframe": {"phase": "main"}},
    }
    assert merged["shots"][2] == {"shot_no": 3, "prompt": "keep-3"}
    assert source["shots"][1] == {"shot_no": 2, "prompt": "keep-2"}


@pytest.mark.parametrize("malformed", [
    {"repair_summary": "已修复"},
    {"shots": "not-an-array"},
    {"shots": []},
])
def test_full_repair_requires_safe_shot_patch(malformed):
    source = {"shots": [{"shot_no": 1, "prompt": "keep"}]}
    assert _merge_storyboard_full_repair(
        source, malformed, positions=[1],
        allowed_fields={"frame_targets"}) is None
    assert source == {"shots": [{"shot_no": 1, "prompt": "keep"}]}


def test_full_repair_can_replace_null_shots_when_nothing_is_preservable(
        monkeypatch):
    repaired = {"shots": [{
        "shot_no": 1,
        "scene_no": 1,
        "duration": 8,
        "prompt": "现代酒店房间建立镜头",
        "characters": [],
        "frame_targets": {
            "keyframe": {
                "phase": "freeze", "state": "房间稳定建立",
                "fallback": False,
            },
            "first_frame": {
                "phase": "start", "state": "房间进入画面",
                "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "房间稳定收束",
                "fallback": False,
            },
        },
    }]}

    monkeypatch.setattr(
        "aifos.adapters.claude_script._invoke_engine",
        lambda *_args, **_kwargs: (
            True, json.dumps(repaired, ensure_ascii=False)))

    fixed, note = _repair_with_engine(
        "codex", "codex", "storyboard", {}, {"shots": None},
        "缺少 shots", 60)

    assert note == ""
    assert fixed is not None
    assert len(fixed["shots"]) == 1
    assert validate_storyboard(fixed) is None


@pytest.mark.parametrize("malformed", [
    {},
    {"shots": None},
    {"shots": "bad"},
    {"shots": []},
])
def test_full_repair_with_invalid_source_shots_never_uses_len_on_none(
        monkeypatch, malformed):
    calls = []

    def invoke(*_args, **_kwargs):
        calls.append(True)
        return True, '{"shots":null}'

    monkeypatch.setattr(
        "aifos.adapters.claude_script._invoke_engine", invoke)

    fixed, note = _repair_with_engine(
        "codex", "codex", "storyboard", {}, malformed,
        "缺少 shots", 60)

    assert fixed is None
    assert "缺少有效 shots" in note
    assert len(calls) == 1


def test_adapter_repair_exception_still_persists_storyboard_evidence(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        claude_script, "_invoke_engine",
        lambda *_args, **_kwargs: (True, '{"shots":null}'))

    def fail_repair(*_args, **_kwargs):
        raise TypeError("object of type 'NoneType' has no len()")

    monkeypatch.setattr(claude_script, "_repair_with_engine", fail_repair)

    reply = claude_script.run(
        {"capability": "storyboard", "payload": {},
         "out_dir": str(tmp_path)},
        "claude", 60, engine="codex", codex="/bin/echo")

    assert reply["ok"] is False
    assert "就地修复结构异常: TypeError" in reply["error"]
    evidence = tmp_path / "writer_failure_storyboard.txt"
    assert evidence.exists()
    assert '"shots":null' in evidence.read_text(encoding="utf-8")


def test_repair_permissions_ignore_keys_in_diagnostic_shot_dump():
    error = (
        "镜头2 frame_targets.keyframe.phase 非法: "
        "{'characters':['甲'],'prompt':'不得改','frame_props':[]}")
    assert _storyboard_error_fields(error) == {"frame_targets"}


@pytest.mark.parametrize("reply", [
    '{"repair_summary":"已修复"}',
    '{"shots":"not-an-array"}',
])
def test_malformed_openai_repair_cannot_overwrite_original_shots(reply):
    provider = OpenAIChatProvider.__new__(OpenAIChatProvider)
    provider._chat = lambda _messages: reply
    data = {
        "shots": [
            {"shot_no": 1, "prompt": "keep-1"},
            {"shot_no": 2, "prompt": "keep-2"},
        ],
    }
    before = copy.deepcopy(data)

    merged, _note = provider._repair_shots(
        {}, data, "镜头2.frame_targets.keyframe.phase 非法")

    assert merged is None
    assert data == before


def test_openai_storyboard_postprocess_uses_source_scene_boundary():
    """DeepSeek/OpenAI-compatible writer gets the same repair as CLI writers."""
    provider = OpenAIChatProvider.__new__(OpenAIChatProvider)
    storyboard = {
        "prop_registry": _registry("shot-003", "end"),
        "shots": [_shot(1, frame_props=[
            _prop_row(
                "start", state="完好合拢", holder="none",
                location="书案中央"),
            _prop_row(
                "end", state="已打开", holder="陆沉",
                location="陆沉身前"),
        ])],
    }
    payload = {"script": {
        "scenes": [{"scene_no": 1, "event_id": "scene:1"}],
        "prop_registry": _registry("scene:1", "end"),
    }}

    fixed = provider._postprocess("storyboard", storyboard, payload)

    assert fixed["prop_registry"][0]["availability_start_event"] == {
        "event_id": "shot-001", "phase": "start"}
    assert validate_storyboard(fixed) is None
