"""Deterministic screenplay logic and shootability checks.

The writing model is responsible for creative adaptation.  This module is the
fail-closed production gate that makes sure a generated or imported script has
enough concrete information for a director to stage it without inventing
physics, props, entrances, exits or character motivation downstream.
"""

from __future__ import annotations

import copy
import re


SCRIPT_LOGIC_SCHEMA = "aifos.script-logic/v1"
SCENE_LOGIC_FIELDS = (
    "dramatic_function",
    "entry_state",
    "physical_actions",
    "prop_continuity",
    "spatial_logic",
    "exit_state",
    "director_intent",
)
VAGUE_ACTIONS = {
    "", "推进剧情", "发生冲突", "展开故事", "人物互动", "继续对话",
    "按剧情发展", "营造氛围", "情绪变化", "自然表演",
}
PHYSICAL_CUES = (
    "站", "坐", "跪", "躺", "卧", "走", "跑", "转身", "抬手", "伸手",
    "拿", "放", "递", "推", "拉", "扶", "趴", "起身", "进入", "离开",
    "看向", "低头", "抬头", "打开", "关闭", "触碰",
)


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _scene_action(scene) -> str:
    return _text(scene.get("action"))


def _default_scene_logic(scene: dict) -> dict:
    """Conservative legacy/import fallback without fabricating new plot."""
    action = _scene_action(scene)
    people = "、".join(
        str(name) for name in (scene.get("characters") or []) if name)
    return {
        "dramatic_function": (
            f"通过本场可见行动推进：{action}" if action
            else "待编剧明确本场可见事件与状态变化"),
        "entry_state": (
            f"{people or '场景'}进入本场时的位置、姿态、持有道具和情绪"
            "必须承接上一场或在本场开头明确建立"),
        "physical_actions": (
            action if action else
            "待编剧改写为能被摄影机直接拍到的单一动作链"),
        "prop_continuity": (
            "本场使用的关键道具必须说明来源、持有人、接触方式和离场去向；"
            "没有关键道具时明确为无"),
        "spatial_logic": (
            f"在{_text(scene.get('location')) or '已声明地点'}内明确人物入口、"
            "相对站位、视线对象、动作路径与出口，禁止瞬移或无支撑动作"),
        "exit_state": (
            "明确本场结束时每名重要人物的位置、姿态、情绪、伤势和持有道具，"
            "供下一场继承"),
        "director_intent": (
            "把叙述性概括外化为观众可见的行动、反应或状态改变；"
            "不使用旁白替代核心事件"),
    }


def normalize_script_logic(script: dict) -> dict:
    """Add an auditable director layer to new, imported and legacy scripts."""
    if not isinstance(script, dict):
        return script
    review = script.get("adaptation_review")
    if not isinstance(review, dict):
        review = {}
        script["adaptation_review"] = review
    review.setdefault(
        "source_to_screen_strategy",
        "保留原作因果与人物核心动机，把抽象叙述改写为可见行动、反应和状态变化")
    review.setdefault(
        "causal_chain",
        "逐场明确触发事件→人物选择→可见行动→直接结果→下一场钩子")
    review.setdefault(
        "character_motivation",
        "每个关键行动必须由人物目标、已知信息和当前风险驱动")
    review.setdefault(
        "physical_reality",
        "人物动作可达，道具来源与去向明确，重力、支撑、接触和设备使用方向成立")
    review.setdefault(
        "spatial_continuity",
        "人物入口、出口、相对站位、视线、运动路径与场景布局连续")
    review.setdefault(
        "shootability",
        "核心剧情必须能由镜头直接拍出，不依赖泛化旁白或不可见心理概括")
    review.setdefault("self_reviewed", False)

    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        defaults = _default_scene_logic(scene)
        logic = scene.get("director_logic")
        if not isinstance(logic, dict):
            logic = {}
            scene["director_logic"] = logic
        auto_filled = []
        for key, value in defaults.items():
            if not _text(logic.get(key)):
                logic[key] = value
                auto_filled.append(key)
        if auto_filled:
            logic["_auto_filled_fields"] = auto_filled
        else:
            logic.pop("_auto_filled_fields", None)
    script["script_logic_audit"] = audit_script_logic(script)
    return script


def audit_script_logic(script: dict) -> dict:
    """Check causal, physical, spatial and shootability facts before art."""
    issues: list[str] = []
    scene_reports: list[dict] = []
    declared = {
        str(item.get("name"))
        for item in (script.get("characters") or [])
        if isinstance(item, dict) and item.get("name")
    }
    review = script.get("adaptation_review")
    strict_director_review = bool(
        isinstance(review, dict) and review.get("self_reviewed") is True)
    previous_exit = ""
    seen_numbers = set()
    for position, scene in enumerate(script.get("scenes") or [], 1):
        if not isinstance(scene, dict):
            issues.append(f"第{position}场不是结构化场次")
            continue
        number = scene.get("scene_no", position)
        prefix = f"第{number}场"
        scene_issues: list[str] = []
        if number in seen_numbers:
            scene_issues.append("场次编号重复")
        seen_numbers.add(number)
        location = _text(scene.get("location"))
        action = _scene_action(scene)
        if not location:
            scene_issues.append("缺少可定位的场景地点")
        if strict_director_review and (
                action in VAGUE_ACTIONS or len(action) < 4):
            scene_issues.append("动作过于泛化，未写出可被镜头拍到的事件")
        lines = [
            line for line in (scene.get("lines") or [])
            if isinstance(line, dict)
        ]
        scene_people = {
            str(name) for name in (scene.get("characters") or []) if name
        }
        speakers = {
            str(line.get("character")) for line in lines
            if line.get("character")
        }
        unknown = sorted((scene_people | speakers) - declared)
        if unknown:
            scene_issues.append("出现未声明角色：" + "、".join(unknown))
        if not speakers <= scene_people:
            scene_issues.append("台词人物未列入本场人物名单")
        logic = scene.get("director_logic")
        if not isinstance(logic, dict):
            scene_issues.append("缺少导演级物理/空间改编")
            logic = {}
        for field in SCENE_LOGIC_FIELDS:
            if not _text(logic.get(field)):
                scene_issues.append(f"导演改编字段缺失：{field}")
        if strict_director_review and logic.get("_auto_filled_fields"):
            scene_issues.append(
                "AI 声明已完成导演自审，但导演改编字段仍由平台兜底："
                + "、".join(logic["_auto_filled_fields"]))
        physical_actions = _text(logic.get("physical_actions"))
        if physical_actions in VAGUE_ACTIONS:
            scene_issues.append("物理动作仍是概括，没有动作链")
        abstract_only = any(word in f"{action} {physical_actions}" for word in (
            "觉得", "认为", "意识到", "思考", "回忆", "陷入", "决定",
            "感到", "局势紧张", "发生冲突", "展开交锋"))
        if (strict_director_review and abstract_only
                and not any(cue in f"{action} {physical_actions}"
                            for cue in PHYSICAL_CUES)):
            scene_issues.append("抽象心理/局势未外化为人物或道具的可见动作")
        entry = _text(logic.get("entry_state"))
        exit_state = _text(logic.get("exit_state"))
        if position > 1 and not entry:
            scene_issues.append("未说明如何承接上一场结束状态")
        if previous_exit and entry and entry == previous_exit:
            # Exact equality is allowed but should still be explicit; no issue.
            pass
        previous_exit = exit_state
        scene_reports.append({
            "scene_no": number,
            "passed": not scene_issues,
            "issues": scene_issues,
            "location": location,
            "action": action,
        })
        issues.extend(f"{prefix}：{issue}" for issue in scene_issues)

    if not isinstance(review, dict):
        issues.append("缺少 adaptation_review 编剧/导演自审")
    else:
        for field in (
                "source_to_screen_strategy", "causal_chain",
                "character_motivation", "physical_reality",
                "spatial_continuity", "shootability"):
            if not _text(review.get(field)):
                issues.append(f"编剧/导演自审字段缺失：{field}")
    return {
        "schema": SCRIPT_LOGIC_SCHEMA,
        "passed": not issues,
        "issues": issues,
        "scene_reports": scene_reports,
        "strict_director_review": strict_director_review,
        "legacy_compatible": not strict_director_review,
        "summary": (
            f"{len(scene_reports)}场均具备因果、物理、空间和可拍摄性合同"
            if not issues else
            f"{len(scene_reports)}场中发现{len(issues)}项编剧/导演逻辑问题"),
    }


def script_logic_snapshot(script: dict) -> dict:
    """Return a detached report for UI/preflight storage."""
    normalized = copy.deepcopy(script or {})
    normalize_script_logic(normalized)
    return copy.deepcopy(normalized.get("script_logic_audit") or {})
