"""Deterministic screenplay logic and shootability checks.

The writing model is responsible for creative adaptation.  This module is the
fail-closed production gate that makes sure a generated or imported script has
enough concrete information for a director to stage it without inventing
physics, props, entrances, exits or character motivation downstream.
"""

from __future__ import annotations

import copy
import re

from .speaker_labels import is_non_person_label


SCRIPT_LOGIC_SCHEMA = "aifos.script-logic/v2"
ADAPTATION_REVIEW_FIELDS = (
    "source_to_screen_strategy",
    "source_material_policy",
    "causal_chain",
    "character_motivation",
    "information_continuity",
    "physical_reality",
    "spatial_continuity",
    "temporal_continuity",
    "prop_lifecycle",
    "missing_detail_completion",
    "story_density",
    "shootability",
    "local_rewrite_policy",
)
SCENE_LOGIC_FIELDS = (
    "dramatic_function",
    "entry_state",
    "information_state",
    "physical_actions",
    "prop_continuity",
    "spatial_logic",
    "time_continuity",
    "missing_details_completed",
    "exit_state",
    "director_intent",
)
CONTINUITY_CONTRACT_FIELDS = (
    "entry_boundary",
    "exit_boundary",
    "immutable_facts",
    "prop_ledger",
    "knowledge_state",
    "time_state",
    "local_rewrite_scope",
)
VAGUE_ACTIONS = {
    "", "推进剧情", "发生冲突", "展开故事", "人物互动", "继续对话",
    "按剧情发展", "营造氛围", "情绪变化", "自然表演",
}
PLACEHOLDER_CUES = (
    "待编剧", "按剧情决定", "按剧情发展", "自由发挥", "自行决定",
    "后续补充", "暂未明确",
)
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
        "information_state": (
            f"逐一明确{people or '本场重要人物'}在本场开始时已经知道、"
            "尚不知道和通过何种可见证据新获知的信息，禁止无来源知情"),
        "physical_actions": (
            action if action else
            "待编剧改写为能被摄影机直接拍到的单一动作链"),
        "prop_continuity": (
            "本场使用的关键道具必须说明来源、持有人、接触方式和离场去向；"
            "没有关键道具时明确为无"),
        "spatial_logic": (
            f"在{_text(scene.get('location')) or '已声明地点'}内明确人物入口、"
            "相对站位、视线对象、动作路径与出口，禁止瞬移或无支撑动作"),
        "time_continuity": (
            "明确本场与上一场的时间关系、动作耗时和必要过渡，"
            "禁止人物、道具或事件无时间成本地跳到结果"),
        "missing_details_completed": (
            "补齐原素材未写但实际拍摄必需的入口、支撑面、交接动作、"
            "环境反应和道具状态；不得新增改变主线的无依据事件"),
        "exit_state": (
            "明确本场结束时每名重要人物的位置、姿态、情绪、伤势和持有道具，"
            "供下一场继承"),
        "director_intent": (
            "把叙述性概括外化为观众可见的行动、反应或状态改变；"
            "不使用旁白替代核心事件"),
    }


def _default_continuity_contract(logic: dict) -> dict:
    return {
        "entry_boundary": _text(logic.get("entry_state")),
        "exit_boundary": _text(logic.get("exit_state")),
        "immutable_facts": (
            "人物身份、世界规则、已经发生的事件、已建立的伏笔和后续必达结果"
            "不可在局部返编中静默改变"),
        "prop_ledger": _text(logic.get("prop_continuity")),
        "knowledge_state": _text(logic.get("information_state")),
        "time_state": _text(logic.get("time_continuity")),
        "local_rewrite_scope": (
            "默认只修改本场；若改变前一场出口、后一场入口、伏笔、后续结果或"
            "已生产资产，必须先列出影响范围并等待确认"),
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
        "source_material_policy",
        "小说、梗概和导入文本是可改编素材，不是已锁定正式剧本；"
        "正式剧本经第一道门禁锁定后才保护台词和场次合同")
    review.setdefault(
        "causal_chain",
        "逐场明确触发事件→人物选择→可见行动→直接结果→下一场钩子")
    review.setdefault(
        "character_motivation",
        "每个关键行动必须由人物目标、已知信息和当前风险驱动")
    review.setdefault(
        "information_continuity",
        "逐场核对人物知道什么、从哪里得知、何时得知，禁止无来源知情或遗忘")
    review.setdefault(
        "physical_reality",
        "人物动作可达，道具来源与去向明确，重力、支撑、接触和设备使用方向成立")
    review.setdefault(
        "spatial_continuity",
        "人物入口、出口、相对站位、视线、运动路径与场景布局连续")
    review.setdefault(
        "temporal_continuity",
        "逐场明确时间顺序、动作耗时和场间过渡，禁止时间跳跃造成状态断裂")
    review.setdefault(
        "prop_lifecycle",
        "逐件关键道具明确出现来源、初始状态、持有人、使用与交接、状态变化和去向")
    review.setdefault(
        "missing_detail_completion",
        "主动补齐原素材省略的服化道、入口出口、交接动作、环境反应、"
        "支撑接触和可见结果，但不得凭空改变核心剧情")
    review.setdefault(
        "story_density",
        "每场必须产生新信息、选择、冲突、反应或结果；不以空镜和泛化旁白凑时长")
    review.setdefault(
        "shootability",
        "核心剧情必须能由镜头直接拍出，不依赖泛化旁白或不可见心理概括")
    review.setdefault(
        "local_rewrite_policy",
        "后续发现剧本根因时只返编问题场，锁定前场出口和后场入口；"
        "先做影响分析，只使受影响分镜与资产失效")
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
        contract = logic.get("continuity_contract")
        if not isinstance(contract, dict):
            contract = {}
            logic["continuity_contract"] = contract
        for key, value in _default_continuity_contract(logic).items():
            if not _text(contract.get(key)):
                contract[key] = value
                auto_filled.append(f"continuity_contract.{key}")
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
        # 旁白/音效/字幕是声音来源,不是人物实体:不参与“角色必须已声明”
        # 与“台词人物须在本场名单内”的核对。否则模型把旁白正确写成声音
        # 来源反而校验失败,下一轮就会把旁白补进人物表来迎合校验。
        speakers = {
            str(line.get("character")) for line in lines
            if line.get("character")
            and not line.get("non_person_voice")
            and not is_non_person_label(line.get("character"))
        }
        unknown = sorted(
            name for name in ((scene_people | speakers) - declared)
            if not is_non_person_label(name))
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
        contract = logic.get("continuity_contract")
        if not isinstance(contract, dict):
            scene_issues.append("缺少局部返编连续性边界合同")
            contract = {}
        for field in CONTINUITY_CONTRACT_FIELDS:
            if not _text(contract.get(field)):
                scene_issues.append(f"连续性边界字段缺失：{field}")
        if strict_director_review and logic.get("_auto_filled_fields"):
            scene_issues.append(
                "AI 声明已完成导演自审，但导演改编字段仍由平台兜底："
                + "、".join(logic["_auto_filled_fields"]))
        if strict_director_review:
            placeholder_fields = [
                field for field in SCENE_LOGIC_FIELDS
                if any(cue in _text(logic.get(field))
                       for cue in PLACEHOLDER_CUES)
            ]
            if placeholder_fields:
                scene_issues.append(
                    "编剧仍留下未完成占位内容："
                    + "、".join(placeholder_fields))
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
        for field in ADAPTATION_REVIEW_FIELDS:
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
            f"{len(scene_reports)}场均具备因果、人物信息、物理、时间、空间、"
            "道具生命周期和局部返编边界合同"
            if not issues else
            f"{len(scene_reports)}场中发现{len(issues)}项编剧/导演逻辑问题"),
    }


def script_logic_snapshot(script: dict) -> dict:
    """Return a detached report for UI/preflight storage."""
    normalized = copy.deepcopy(script or {})
    normalize_script_logic(normalized)
    return copy.deepcopy(normalized.get("script_logic_audit") or {})
