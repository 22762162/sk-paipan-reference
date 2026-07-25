"""Deterministic rules for non-diegetic chibi inner-persona overlays.

An inner persona is a narrative device, not a second physical actor.  Keeping
that distinction in one module prevents it from leaking into cast counts,
blocking, continuity state, identity references, or listener reactions.
"""

from __future__ import annotations

import copy
import re


INNER_PERSONA_SCHEMA = "aifos.inner-persona/v1"
INNER_OVERLAY_SCHEMA = "aifos.narrative-overlay/v1"

DEFAULT_INNER_PERSONA_RULES = {
    "schema": INNER_PERSONA_SCHEMA,
    "mode": "chibi_overlay",
    "physical_presence": False,
    "counts_as_real_character": False,
    "included_in_spatial_blocking": False,
    "visible_to": "host_only",
    "historical_characters_may_react": False,
    "real_form_forbidden_after_transition": True,
    "wardrobe_source": "locked_current_look",
    "inherit_signature_props": False,
    "allowed_functions": [
        "inner_monologue", "inner_commentary",
        "comic_reaction", "decision_conflict",
    ],
    "auto_after_every_dialogue": False,
    "expression_style": "exaggerated",
    "proportion_style": "oversized_head_tiny_body",
    "total_height_in_heads": 1.8,
    "head_height_ratio": 0.58,
    "body_smaller_than_head": True,
    "max_overlays_per_shot": 1,
    "host_mouth_closed_for_inner_voice": True,
    "forbid_burned_subtitles": True,
}

_MODERN_CUES = (
    "现代书房", "现代办公室", "现代都市", "现代场景", "现代书桌",
    "穿越前", "前世画面", "前世镜头", "现实世界",
)
_POST_TRANSITION_CUES = (
    "穿越后", "画面切换至明", "明代", "明末", "东宫", "太子寝殿",
    "宫殿", "紫禁城", "古代场景", "历史场景",
)
_INNER_CUES = (
    "内心", "心声", "心想", "吐槽", "腹诽", "意识", "脑海",
    "心理活动", "灵魂",
)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _script_character(script, name):
    return next((
        item for item in (script or {}).get("characters", [])
        if isinstance(item, dict) and item.get("name") == name
    ), {})


def _infer_source_and_host(script):
    """Infer only from explicit time-travel/inner-consciousness evidence."""
    for character in (script or {}).get("characters", []):
        if not isinstance(character, dict):
            continue
        name = _text(character.get("name"))
        evidence = " ".join(_text(character.get(key)) for key in (
            "name", "role", "introduction", "identity", "backstory",
            "relationships",
        ))
        if not (
                "现代之魂" in evidence
                or "穿越后" in evidence and "意识" in evidence
                or "前世" in name and ("穿越" in evidence or "灵魂" in evidence)):
            continue
        host = ""
        match = re.search(r"([^/（(·，,；;\s]+)前世", name)
        if match:
            host = match.group(1)
        if not host:
            match = re.search(
                r"(?:成为|进入|寄于|附于|宿主为)([^，。；/\s]+)",
                evidence)
            if match:
                host = match.group(1)
        cast_names = {
            item.get("name") for item in (script or {}).get("characters", [])
            if isinstance(item, dict)
        }
        if host in cast_names:
            return name, host
    return "", ""


def normalize_inner_persona_policy(script, rules=None):
    """Return a strict policy or a disabled record when evidence is absent."""
    source = (
        (script or {}).get("inner_persona_policy")
        if isinstance((script or {}).get("inner_persona_policy"), dict)
        else {})
    explicit_source = _text(
        source.get("source_character") or source.get("persona_character"))
    explicit_host = _text(source.get("host_character"))
    inferred_source, inferred_host = _infer_source_and_host(script)
    source_name = explicit_source or inferred_source
    host_name = explicit_host or inferred_host
    enabled = bool(
        source.get("enabled", bool(source_name and host_name))
        and source_name and host_name)
    policy = {
        **copy.deepcopy(DEFAULT_INNER_PERSONA_RULES),
        **copy.deepcopy(rules or {}),
        **copy.deepcopy(source),
        "schema": INNER_PERSONA_SCHEMA,
        "enabled": enabled,
        "source_character": source_name,
        "host_character": host_name,
    }
    policy["physical_presence"] = False
    policy["counts_as_real_character"] = False
    policy["included_in_spatial_blocking"] = False
    policy["historical_characters_may_react"] = False
    policy["real_form_forbidden_after_transition"] = True
    policy["inherit_signature_props"] = False
    policy["auto_after_every_dialogue"] = False
    policy["expression_style"] = "exaggerated"
    policy["proportion_style"] = "oversized_head_tiny_body"
    policy["total_height_in_heads"] = 1.8
    policy["head_height_ratio"] = 0.58
    policy["body_smaller_than_head"] = True
    policy["max_overlays_per_shot"] = 1
    policy["host_mouth_closed_for_inner_voice"] = True
    policy["forbid_burned_subtitles"] = True
    policy["asset_name"] = _text(
        policy.get("asset_name")
        or (f"{source_name}:chibi-a" if source_name else ""))
    policy["asset_uri"] = _text(policy.get("asset_uri"))
    return policy


def shot_timeline_state(raw, previous="unknown"):
    """Resolve modern-before / historical-after from shot-local evidence."""
    raw = raw or {}
    dialogue = raw.get("dialogue")
    dialogue_text = (
        _text(dialogue.get("dialogue")) if isinstance(dialogue, dict) else "")
    text = " ".join(_text(value) for value in (
        raw.get("location"), raw.get("scene_location"),
        raw.get("world_state"), raw.get("era"), raw.get("description"),
        raw.get("action"), raw.get("prompt"), raw.get("script_reference"),
        dialogue_text,
    ) if _text(value))
    if any(cue in text for cue in _POST_TRANSITION_CUES):
        return "post_transition"
    if any(cue in text for cue in _MODERN_CUES):
        return "pre_transition"
    return previous


def physical_scene_characters(scene_people, timeline, policy):
    """Return only people physically available in the current timeline."""
    people = list(dict.fromkeys(scene_people or []))
    if not policy.get("enabled"):
        return people
    source = policy.get("source_character")
    host = policy.get("host_character")
    if timeline == "pre_transition":
        # The future host and historical cast do not enter a modern flashback
        # merely because a legacy one-scene script listed every entity.
        return [name for name in people if name == source]
    if timeline == "post_transition":
        return [name for name in people if name != source]
    return people


def normalize_narrative_overlay(value, policy, *, fallback_text=""):
    """Normalize one overlay without allowing it to become physical."""
    value = value if isinstance(value, dict) else {}
    function = _text(value.get("function") or value.get("purpose"))
    if function not in policy.get("allowed_functions", []):
        function = "inner_commentary"
    expression = _text(value.get("expression") or value.get("performance"))
    action = _text(value.get("action"))
    if not expression:
        expression = (
            "夸张但清晰可读的Q版表情：眉眼、嘴形、手势和身体弹性服务当下情绪")
    if not action:
        action = _text(fallback_text) or "在宿主肩旁以夸张Q版动作完成内心反应"
    return {
        "schema": INNER_OVERLAY_SCHEMA,
        "kind": "inner_persona_chibi",
        "function": function,
        "name": policy.get("source_character", ""),
        "host_character": policy.get("host_character", ""),
        "asset_name": policy.get("asset_name", ""),
        "asset_uri": policy.get("asset_uri", ""),
        "wardrobe_source": "locked_current_look",
        "physical_presence": False,
        "counts_as_real_character": False,
        "included_in_spatial_blocking": False,
        "visible_to": "host_only",
        "historical_characters_may_react": False,
        "inherit_signature_props": False,
        "expression_style": "exaggerated",
        "proportion_style": "oversized_head_tiny_body",
        "total_height_in_heads": 1.8,
        "head_height_ratio": 0.58,
        "body_smaller_than_head": True,
        "expression": expression,
        "action": action,
        "dialogue": _text(
            value.get("dialogue") or value.get("inner_dialogue")),
        "host_mouth_closed": True,
        "forbid_burned_subtitles": True,
    }


def apply_inner_persona_to_shots(script, raw_shots, rules=None):
    """Remove post-transition physical duplicates and normalize Q overlays."""
    policy = normalize_inner_persona_policy(script, rules)
    if not policy.get("enabled"):
        return [copy.deepcopy(item) for item in raw_shots], policy
    source = policy["source_character"]
    host = policy["host_character"]
    timeline = "unknown"
    out = []
    for raw in raw_shots:
        shot = copy.deepcopy(raw)
        timeline = shot_timeline_state(shot, timeline)
        shot["timeline_state"] = timeline
        characters = list(dict.fromkeys(shot.get("characters") or []))
        explicit = shot.get("narrative_overlays")
        if isinstance(explicit, dict):
            explicit = [explicit]
        overlays = [
            normalize_narrative_overlay(item, policy)
            for item in (explicit or []) if isinstance(item, dict)
        ]
        text = " ".join(_text(value) for value in (
            shot.get("description"), shot.get("prompt"),
            shot.get("script_reference"),
        ))
        source_was_physical = source in characters
        if timeline == "post_transition":
            characters = [name for name in characters if name != source]
            explicit_inner = any(cue in text for cue in _INNER_CUES)
            mechanical_reaction = (
                shot.get("kind") == "reaction"
                and "消化" in text and "刚说的话" in text)
            should_convert = (
                source_was_physical
                and explicit_inner and not mechanical_reaction)
            if should_convert and not overlays:
                overlays = [normalize_narrative_overlay(
                    {}, policy, fallback_text=shot.get("description", ""))]
            if (source_was_physical and not characters
                    and shot.get("kind") == "beat" and not overlays):
                characters = [host]
                for field in ("description", "prompt"):
                    if shot.get(field):
                        shot[field] = str(shot[field]).replace(source, host)
            if source_was_physical and mechanical_reaction and characters:
                for field in ("description", "prompt"):
                    if shot.get(field):
                        shot[field] = str(shot[field]).replace(
                            source, characters[0])
            if overlays and host not in characters:
                characters.insert(0, host)
            dialogue = shot.get("dialogue")
            if isinstance(dialogue, dict) and dialogue.get("character") == source:
                if not overlays:
                    overlays = [normalize_narrative_overlay({
                        "function": "inner_monologue",
                        "dialogue": dialogue.get("dialogue", ""),
                    }, policy, fallback_text=shot.get("description", ""))]
                shot["dialogue"] = {
                    **dialogue,
                    "inner_voice": True,
                    "performed_by": source,
                    "host_character": host,
                    "host_mouth_closed": True,
                }
                shot["kind"] = "inner_monologue"
            elif (isinstance(dialogue, dict)
                  and dialogue.get("character") == host
                  and explicit_inner):
                if not overlays:
                    overlays = [normalize_narrative_overlay({
                        "function": "inner_monologue",
                        "dialogue": dialogue.get("dialogue", ""),
                    }, policy, fallback_text=shot.get("description", ""))]
                shot["dialogue"] = {
                    **dialogue,
                    "inner_voice": True,
                    "performed_by": source,
                    "host_character": host,
                    "host_mouth_closed": True,
                }
                shot["kind"] = "inner_monologue"
            if overlays:
                shot["inner_voice_host_mouth_closed"] = True
        elif timeline == "pre_transition":
            # The source is a real person before the transition, never a Q
            # overlay merely because its later identity is inner consciousness.
            overlays = []
            if (shot.get("kind") == "reaction"
                    and source in text
                    and characters
                    and source not in characters):
                # Legacy mixed-timeline scripts inserted historical listeners
                # after a modern monologue.  There is no valid physical
                # reaction subject in that timeline, so delete the bad beat.
                continue
        shot["characters"] = characters
        shot["narrative_overlays"] = overlays[:1]
        out.append(shot)
    return out, policy
