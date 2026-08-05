"""High-value event contracts shared by script, storyboard and review.

The contract protects the moments an audience must *experience* rather than
merely be told about: a reveal, irreversible choice, power gain, awakening,
transformation, time travel, battle reversal, ritual, discovery or comparable
set piece.  Routine connective actions remain compressible; declared event
beats do not.
"""

from __future__ import annotations

import copy
import json


SCHEMA = "aifos.high-value-events/v1"
HIGH_VALUE_CUES = (
    "抽取", "抽卡", "天赋", "觉醒", "变身", "穿越", "重生", "升级",
    "晋升", "揭晓", "真相", "反转", "决战", "爆发", "仪式", "复活",
    "死亡", "牺牲", "SS级", "S级", "获得能力", "身份暴露", "伏笔兑现",
    "awakening", "transformation", "time travel", "reveal", "reversal",
)


def _text(value):
    return str(value or "").strip()


def _positive_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def normalize_high_value_events(script):
    """Normalize the optional script contract without inventing story beats."""
    if not isinstance(script, dict):
        return []
    source = script.get("high_value_events")
    if not isinstance(source, list):
        source = []
    events = []
    seen_events = set()
    for position, raw in enumerate(source, 1):
        if not isinstance(raw, dict):
            continue
        event_id = _text(
            raw.get("event_id") or raw.get("sequence_id")
            or raw.get("dramatic_sequence_id")
            or f"high-value-event:{position}")
        if event_id in seen_events:
            continue
        seen_events.add(event_id)
        beats = []
        seen_beats = set()
        beat_source = raw.get("required_beats") or raw.get("event_beats") or []
        if not isinstance(beat_source, list):
            beat_source = []
        for beat_position, beat in enumerate(beat_source, 1):
            if not isinstance(beat, dict):
                continue
            beat_id = _text(
                beat.get("beat_id") or beat.get("event_beat_id")
                or f"{event_id}:beat:{beat_position}")
            if beat_id in seen_beats:
                continue
            seen_beats.add(beat_id)
            beats.append({
                **copy.deepcopy(beat),
                "beat_id": beat_id,
                "role": _text(beat.get("role") or beat.get("event_role")),
                "visible_event": _text(
                    beat.get("visible_event") or beat.get("description")),
                "must_visualize": beat.get("must_visualize", True) is not False,
            })
        minimum = _positive_int(
            raw.get("minimum_independent_shots") or raw.get("minimum_shots"),
            3)
        events.append({
            **copy.deepcopy(raw),
            "event_id": event_id,
            "scene_no": _positive_int(raw.get("scene_no"), 1),
            "event_class": _text(raw.get("event_class") or "high_value"),
            "story_value": _text(raw.get("story_value")),
            "dramatic_question": _text(raw.get("dramatic_question")),
            "must_visualize": raw.get("must_visualize", True) is not False,
            "minimum_independent_shots": minimum,
            "routine_montage_allowed": raw.get(
                "routine_montage_allowed", True) is not False,
            "required_beats": beats,
        })
    script["high_value_event_schema"] = SCHEMA
    script["high_value_events"] = events
    return events


def shot_high_value_event_id(shot):
    if not isinstance(shot, dict):
        return ""
    return _text(
        shot.get("high_value_event_id")
        or shot.get("dramatic_sequence_id")
        or shot.get("special_sequence_id"))


def shot_event_beat_ids(shot):
    if not isinstance(shot, dict):
        return []
    values = shot.get("event_beat_ids")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = []
    singular = shot.get("event_beat_id") or shot.get("sequence_beat_id")
    if singular:
        values = [singular, *values]
    return list(dict.fromkeys(_text(value) for value in values if _text(value)))


def is_preserved_event_shot(shot):
    """Whether long-take folding must never consume this authored shot."""
    if not isinstance(shot, dict):
        return False
    if shot.get("must_visualize") is True or shot.get("must_preserve") is True:
        return True
    if shot_high_value_event_id(shot) or shot_event_beat_ids(shot):
        return True
    return _text(shot.get("event_class")).lower() == "high_value"


def audit_high_value_event_coverage(script, storyboard):
    """Compare declared must-see beats with independent storyboard shots."""
    script_copy = copy.deepcopy(script) if isinstance(script, dict) else {}
    events = normalize_high_value_events(script_copy)
    shots = [shot for shot in (storyboard or {}).get("shots") or []
             if isinstance(shot, dict)]
    issues = []
    rows = []
    for event in events:
        if not event.get("must_visualize"):
            continue
        event_id = event["event_id"]
        matched = [shot for shot in shots
                   if shot_high_value_event_id(shot) == event_id]
        covered_beats = list(dict.fromkeys(
            beat_id for shot in matched for beat_id in shot_event_beat_ids(shot)))
        required_beats = [
            beat["beat_id"] for beat in event.get("required_beats") or []
            if beat.get("must_visualize")]
        missing = [beat_id for beat_id in required_beats
                   if beat_id not in covered_beats]
        minimum = event["minimum_independent_shots"]
        event_issues = []
        if len(matched) < minimum:
            event_issues.append(
                f"高价值事件{event_id}至少需要{minimum}个独立镜头，"
                f"当前只有{len(matched)}个")
        if missing:
            event_issues.append(
                f"高价值事件{event_id}缺少必须可见节拍:"
                + "、".join(missing))
        if any(shot.get("folded_into_long_take") for shot in matched):
            event_issues.append(f"高价值事件{event_id}被错误折入普通长镜头")
        issues.extend(event_issues)
        rows.append({
            "event_id": event_id,
            "scene_no": event.get("scene_no"),
            "minimum_independent_shots": minimum,
            "shot_nos": [shot.get("shot_no") for shot in matched],
            "required_beats": required_beats,
            "covered_beats": covered_beats,
            "missing_beats": missing,
            "passed": not event_issues,
        })
    return {
        "schema": SCHEMA,
        "passed": not issues,
        "declared_event_count": len(events),
        "events": rows,
        "issues": issues,
    }


def validate_high_value_event_contract(script):
    """Return authoring errors for confidently detectable set-piece scenes."""
    if not isinstance(script, dict):
        return ["剧本不是对象"]
    events = normalize_high_value_events(script)
    issues = []
    declared_scenes = {event["scene_no"] for event in events}
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        logic = scene.get("director_logic")
        if not isinstance(logic, dict):
            logic = {}
        # Only scan what happens *in this scene*.  entry/information state and
        # immutable continuity facts routinely mention an earlier awakening,
        # SS-rank result or reveal; treating those recap facts as a new set
        # piece created phantom contracts and rejected otherwise valid scripts.
        text = json.dumps({
            "action": scene.get("action"),
            "dramatic_function": logic.get("dramatic_function"),
            "physical_actions": logic.get("physical_actions"),
            "director_intent": logic.get("director_intent"),
            "exit_state": logic.get("exit_state"),
        }, ensure_ascii=False, default=str).lower()
        if (any(cue.lower() in text for cue in HIGH_VALUE_CUES)
                and _positive_int(scene.get("scene_no"), 0)
                not in declared_scenes):
            issues.append(
                f"场{scene.get('scene_no')}含高价值事件信号但未建立"
                " high_value_events 合同")
    for event in events:
        if not event.get("must_visualize"):
            continue
        beats = [beat for beat in event.get("required_beats") or []
                 if beat.get("must_visualize")]
        minimum = event["minimum_independent_shots"]
        if len(beats) < minimum:
            issues.append(
                f"高价值事件{event['event_id']}至少需要{minimum}个"
                f"必看节拍，当前只有{len(beats)}个")
        if not event.get("dramatic_question"):
            issues.append(
                f"高价值事件{event['event_id']}缺少 dramatic_question")
    return issues
