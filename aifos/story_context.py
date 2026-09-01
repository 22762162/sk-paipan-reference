"""Deterministic per-shot story/era context for human production review.

The storyboard already carries a concise shot description and its source
script line.  This module adds the missing review axis: which story beat is
being filmed, and whether it belongs to the modern or historical timeline.
It deliberately does not rewrite prompts or mutate the stored script.
"""

from __future__ import annotations

import re
from typing import Any


_MODERN_MARKERS = (
    "现代书房", "现代场景", "电脑", "笔记本", "台灯", "键盘",
    "查史料", "伏案", "书桌", "屏幕", "青年",
)
_HISTORICAL_MARKERS = (
    "明代", "明末", "大明", "东宫", "太子", "寝殿", "宫", "太监",
    "崇祯", "乾清宫", "紫禁城", "明式", "铜炉", "帐幔", "锦缎",
    "毛笔", "奏折", "更漏", "李继周",
)
_MODERN_EXPLICIT = ("现代书房", "现代场景", "现代时空", "穿越前")
_HISTORICAL_EXPLICIT = (
    "画面切换至明", "明末", "明代", "穿越后", "东宫寝殿", "太子寝殿",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return "；".join(_text(item) for item in value.values() if item)
    if isinstance(value, (list, tuple)):
        return "；".join(_text(item) for item in value if item)
    return str(value).strip()


def _shorten(value: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _scene_map(script: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for scene in (script or {}).get("scenes", []) or []:
        try:
            result[int(scene.get("scene_no"))] = scene
        except (TypeError, ValueError):
            continue
    return result


def _scene_design(scene: dict[str, Any]) -> dict[str, Any]:
    design = scene.get("production_design")
    return design if isinstance(design, dict) else {}


def _default_era(scene: dict[str, Any]) -> str:
    design = _scene_design(scene)
    design_text = _text({
        "location": scene.get("location"),
        "environment": design.get("environment"),
        "time": design.get("time_weather"),
    })
    # 场景制作设计比叙事动作中的“青年/电脑”等词更稳定；先尊重其
    # 世界观基准，再由镜头描述中的明确“现代/明末”标记覆盖。
    if any(marker in design_text for marker in _HISTORICAL_MARKERS):
        return "明代"
    if any(marker in design_text for marker in _MODERN_MARKERS):
        return "现代"
    action_text = _text(scene.get("action"))
    if any(marker in action_text for marker in _HISTORICAL_MARKERS):
        return "明代"
    if any(marker in action_text for marker in _MODERN_MARKERS):
        return "现代"
    return "未明确"


def _location_for(era: str, text: str, scene: dict[str, Any]) -> str:
    design = _scene_design(scene)
    environment = _text(design.get("environment"))
    scene_location = _text(scene.get("location"))
    if era == "现代":
        if any(marker in text for marker in ("书房", "电脑", "台灯", "书桌", "伏案")):
            return "现代书房"
        return "现代场景"
    if era == "明代":
        if any(marker in text + environment for marker in ("东宫", "寝殿", "太子")):
            return "明末东宫太子寝殿"
        if "乾清宫" in text:
            return "明代乾清宫"
        if scene_location and scene_location != "主场景":
            return f"明代·{scene_location}"
        return "明代宫廷场景"
    return scene_location or "剧本未明确"


def build_shot_story_contexts(
    script: dict[str, Any] | None,
    storyboard: dict[str, Any] | None,
) -> dict[int, dict[str, Any]]:
    """Return a compact, review-safe context for every storyboard shot.

    Era is explicit when the shot says so; otherwise it inherits the previous
    shot.  This handles reaction shots such as shot 4/6/8, whose descriptions
    intentionally omit the location while remaining in the same timeline.
    """

    scenes = _scene_map(script)
    contexts: dict[int, dict[str, Any]] = {}
    current_era: dict[int, str] = {}
    current_location: dict[int, str] = {}
    for raw in (storyboard or {}).get("shots", []) or []:
        try:
            shot_no = int(raw.get("shot_no"))
        except (TypeError, ValueError):
            continue
        try:
            scene_no = int(raw.get("scene_no"))
        except (TypeError, ValueError):
            scene_no = None
        scene = scenes.get(scene_no, {})
        dialogue = raw.get("dialogue") or {}
        source = _text(raw.get("script_reference") or raw.get("dialogue_source"))
        description = _text(raw.get("description"))
        dialogue_text = _text(dialogue.get("dialogue") if isinstance(dialogue, dict) else dialogue)
        shot_text = "；".join(part for part in (description, source, dialogue_text) if part)
        explicit_modern = any(marker in shot_text for marker in _MODERN_EXPLICIT)
        explicit_historical = any(marker in shot_text for marker in _HISTORICAL_EXPLICIT)
        if explicit_modern and not explicit_historical:
            era = "现代"
        elif explicit_historical and not explicit_modern:
            era = "明代"
        elif scene_no in current_era:
            era = current_era[scene_no]
        else:
            era = _default_era(scene)
        if era == "未明确" and scene_no in current_era:
            era = current_era[scene_no]
        if era != "未明确":
            current_era[scene_no or 0] = era

        design = _scene_design(scene)
        time_weather = _text(design.get("time_weather"))
        if era == "现代":
            time_label = "现代·深夜（穿越前）"
        elif era == "明代":
            time_label = time_weather or "明代·剧本时间待确认"
        else:
            time_label = time_weather or "剧本未明确"
        location = _location_for(era, shot_text, scene)
        if (era != "未明确" and scene_no in current_era
                and current_era[scene_no] == era
                and location in ("现代场景", "明代宫廷场景")):
            location = current_location.get(scene_no, location)
        if era != "未明确":
            current_location[scene_no or 0] = location
        context = {
            "era": era,
            "era_label": f"{era}·{'穿越前' if era == '现代' else '穿越后' if era == '明代' else '待确认'}",
            "location": location,
            "time": time_label,
            "story": _shorten(description or source or dialogue_text),
            "script_excerpt": _shorten(source or dialogue_text),
            "dialogue": dialogue_text,
            "scene_no": scene_no,
            "scene_story": _shorten(_text(design.get("story_function")) or _text(scene.get("action")), 180),
            "basis": "镜头描述 + 剧本对应原句",
        }
        contexts[shot_no] = context
    return contexts


def attach_shot_story_context(
    script: dict[str, Any] | None,
    storyboard: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[int, dict[str, Any]]]:
    """Copy storyboard and attach contexts without mutating persisted data."""

    contexts = build_shot_story_contexts(script, storyboard)
    if storyboard is None:
        return None, contexts
    copied = dict(storyboard)
    copied["shots"] = []
    for shot in storyboard.get("shots", []) or []:
        enriched = dict(shot)
        try:
            shot_no = int(shot.get("shot_no"))
        except (TypeError, ValueError):
            shot_no = None
        if shot_no in contexts:
            enriched["story_context"] = contexts[shot_no]
        copied["shots"].append(enriched)
    return copied, contexts
