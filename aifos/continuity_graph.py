"""Deterministic dependency groups for cross-shot visual continuity.

Independent locations/realms may render in parallel.  Shots that share one
continuous world state form an ordered chain so a downstream image cannot be
generated before the immediately preceding selected image exists.
"""

from __future__ import annotations

from collections import OrderedDict


_BREAK_FIELDS = (
    "continuity_break",
    "time_jump",
    "realm_transition",
    "era_transition",
    "reset_visual_continuity",
)


def _text(value):
    return str(value or "").strip()


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() not in {
            "", "0", "false", "off", "no", "none",
        }
    return bool(value)


def _continuity_key(shot, scene_locations):
    """Return the authored world-state key, not a camera/cut key."""
    scene_no = shot.get("scene_no")
    explicit = _text(
        shot.get("continuity_group")
        or shot.get("continuity_group_id")
        or shot.get("sequence_id"))
    location = _text(
        shot.get("location") or scene_locations.get(scene_no))
    realm = _text(
        shot.get("active_realm_id") or shot.get("realm_id")
        or shot.get("world_id"))
    era = _text(
        shot.get("era_context") or shot.get("era")
        or shot.get("time_period"))
    # A normal hard cut/reverse angle is still the same world state.  Only an
    # explicit continuity group or a change in place/realm/era creates a new
    # dependency chain.
    return (explicit or f"scene:{scene_no}", location, realm, era)


def build_keyframe_continuity_plan(shots, scene_locations=None):
    """Build ordered continuity chains and an exact predecessor map.

    Group order follows storyboard order.  A truthy break flag starts a fresh
    group before that shot while preserving other independent chains.
    """
    scene_locations = dict(scene_locations or {})
    groups = OrderedDict()
    generation = {}
    last_key = None
    break_index = 0

    for position, raw in enumerate(shots or []):
        if not isinstance(raw, dict):
            continue
        shot_no = int(raw.get("shot_no") or 0)
        if shot_no <= 0:
            continue
        base = _continuity_key(raw, scene_locations)
        breaks = any(_truthy(raw.get(field)) for field in _BREAK_FIELDS)
        if breaks:
            break_index += 1
            generation[base] = break_index
        suffix = generation.get(base, 0)
        key = (*base, suffix)
        # Re-entering the same location after another chain is a new temporal
        # segment unless the writer explicitly supplied continuity_group.
        if (last_key is not None and key != last_key and key in groups
                and not _text(raw.get("continuity_group")
                              or raw.get("continuity_group_id"))):
            break_index += 1
            generation[base] = break_index
            key = (*base, break_index)
        groups.setdefault(key, []).append({
            "shot_no": shot_no,
            "position": position,
            "scene_no": raw.get("scene_no"),
        })
        last_key = key

    chains = []
    predecessor_by_shot = {}
    group_by_shot = {}
    for index, (key, members) in enumerate(groups.items(), 1):
        shot_nos = [member["shot_no"] for member in members]
        group_id = f"continuity:{index:03d}"
        chains.append({
            "group_id": group_id,
            "world_key": list(key),
            "shot_nos": shot_nos,
        })
        previous = None
        for shot_no in shot_nos:
            predecessor_by_shot[shot_no] = previous
            group_by_shot[shot_no] = group_id
            previous = shot_no

    return {
        "schema": "aifos.keyframe-continuity-plan/v1",
        "groups": chains,
        "predecessor_by_shot": predecessor_by_shot,
        "group_by_shot": group_by_shot,
    }

