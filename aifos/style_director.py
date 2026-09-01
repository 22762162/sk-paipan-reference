"""风格专属导演知识：镜头语法、视觉效果与逐镜选择规则。

FireFire 风格过去只有一段 ``compiled_style``，人物、材质、灯光、镜头和
特效混在一起。图片提示词尚可消费这种长文本，但分镜导演无法知道哪些镜头
是该风格的高频语法，也无法避免把另一种风格的运镜套进来。

本模块是无数据库依赖的叶子模块，负责三件事：

1. 把外部 JSON 规范为稳定的 ``firefire.director-style/v1``；
2. 将结构化知识临时嵌入送往制作圣经的风格文本，不污染
   ``projects.style``，人物资产不会误读运镜与转场；
3. 在制作圣经阶段重新拆出基础美术风格与导演知识，人物资产不会误吃运镜，
   分镜/Seedance 则能按镜头功能选择该风格自己的镜头与特效。
"""

import copy
import json
import re


DIRECTOR_STYLE_SCHEMA = "firefire.director-style/v1"
DIRECTOR_BLOCK_START = "<AIFOS_STYLE_DIRECTOR_KNOWLEDGE>"
DIRECTOR_BLOCK_END = "</AIFOS_STYLE_DIRECTOR_KNOWLEDGE>"

SHOT_FIELDS = (
    "shot_patterns",
    "shot_scales",
    "camera_angles",
    "camera_positions",
    "lenses",
    "camera_moves",
    "compositions",
    "transitions",
    "rhythm",
    "forbidden",
)
VISUAL_FIELDS = (
    "lighting",
    "atmosphere",
    "optical",
    "color_grade",
    "materials",
    "particles",
    "post_process",
    "forbidden",
)

SHOT_LABELS = {
    "shot_patterns": "高频镜头组合",
    "shot_scales": "景别",
    "camera_angles": "拍摄角度",
    "camera_positions": "机位",
    "lenses": "焦段",
    "camera_moves": "运镜",
    "compositions": "构图",
    "transitions": "转场",
    "rhythm": "镜头节奏",
    "forbidden": "镜头禁用项",
}
VISUAL_LABELS = {
    "lighting": "灯光",
    "atmosphere": "环境氛围",
    "optical": "光学效果",
    "color_grade": "调色",
    "materials": "材质响应",
    "particles": "粒子与能量",
    "post_process": "后期效果",
    "forbidden": "视觉禁用项",
}


def _items(value):
    if isinstance(value, (tuple, list)):
        source = value
    elif isinstance(value, str):
        source = re.split(r"[\n,，;；]+", value)
    else:
        source = []
    result = []
    for item in source:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _section(value, fields):
    value = value if isinstance(value, dict) else {}
    return {field: _items(value.get(field)) for field in fields}


def _selection_rules(value):
    result = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append({
                    "when": "按剧情功能判断",
                    "shots": [],
                    "effects": [],
                    "purpose": text,
                })
            continue
        if not isinstance(item, dict):
            continue
        when = str(item.get("when") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        shots = _items(item.get("shots") or item.get("shot_patterns"))
        effects = _items(item.get("effects") or item.get("visual_effects"))
        if any((when, purpose, shots, effects)):
            result.append({
                "when": when or "按剧情功能判断",
                "shots": shots,
                "effects": effects,
                "purpose": purpose or "让镜头与视觉效果服务本镜叙事功能",
            })
    return result


def normalize_director_knowledge(value):
    """返回稳定、可 JSON 序列化的风格导演知识。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    value = value if isinstance(value, dict) else {}
    shot = value.get("shot_language")
    if not isinstance(shot, dict):
        shot = value.get("camera") if isinstance(value.get("camera"), dict) else {}
    visual = value.get("visual_effects")
    if not isinstance(visual, dict):
        visual = value.get("effects") if isinstance(value.get("effects"), dict) else {}
    return {
        "schema": DIRECTOR_STYLE_SCHEMA,
        "shot_language": _section(shot, SHOT_FIELDS),
        "visual_effects": _section(visual, VISUAL_FIELDS),
        "selection_rules": _selection_rules(value.get("selection_rules")),
    }


def director_counts(value):
    knowledge = normalize_director_knowledge(value)
    shot = knowledge["shot_language"]
    visual = knowledge["visual_effects"]
    return {
        "shot_language": len(set(
            item for field in SHOT_FIELDS
            for item in shot.get(field, [])
            if field != "forbidden")),
        "visual_effects": len(set(
            item for field in VISUAL_FIELDS
            for item in visual.get(field, [])
            if field != "forbidden")),
        "selection_rules": len(knowledge["selection_rules"]),
    }


def director_ready(value):
    counts = director_counts(value)
    return bool(
        counts["shot_language"]
        and counts["visual_effects"]
        and counts["selection_rules"])


def strip_director_knowledge(style):
    """从项目风格文本移除嵌入块，只留下基础美术/媒介风格。"""
    text = str(style or "")
    pattern = (
        re.escape(DIRECTOR_BLOCK_START)
        + r".*?"
        + re.escape(DIRECTOR_BLOCK_END))
    return re.sub(pattern, "", text, flags=re.DOTALL).strip()


def extract_director_knowledge(style):
    """读取 ``compile_director_style`` 嵌入的机器可读导演知识。"""
    text = str(style or "")
    pattern = (
        re.escape(DIRECTOR_BLOCK_START)
        + r"(.*?)"
        + re.escape(DIRECTOR_BLOCK_END))
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        return normalize_director_knowledge({})
    try:
        payload = json.loads(match.group(1).strip())
    except (TypeError, ValueError):
        payload = {}
    return normalize_director_knowledge(payload)


def compile_director_style(compiled_style, director_knowledge):
    """把风格基础提示词与导演知识封装进兼容旧字段的项目风格文本。"""
    base = strip_director_knowledge(compiled_style)
    knowledge = normalize_director_knowledge(director_knowledge)
    if not director_ready(knowledge):
        return base
    payload = json.dumps(
        knowledge, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (
        f"{base}\n\n{DIRECTOR_BLOCK_START}\n"
        f"{payload}\n{DIRECTOR_BLOCK_END}")


def _summary(section, fields, labels):
    parts = []
    for field in fields:
        values = section.get(field) or []
        if values:
            parts.append(f"{labels[field]}={'、'.join(values)}")
    return "；".join(parts)


def camera_language_summary(value):
    knowledge = normalize_director_knowledge(value)
    return _summary(
        knowledge["shot_language"], SHOT_FIELDS, SHOT_LABELS)


def visual_effects_summary(value):
    knowledge = normalize_director_knowledge(value)
    return _summary(
        knowledge["visual_effects"], VISUAL_FIELDS, VISUAL_LABELS)


def _rule_terms(value):
    text = str(value or "")
    terms = [
        item.strip()
        for item in re.split(r"[、，,；;或和与及/]+", text)
        if len(item.strip()) >= 2
    ]
    functional = (
        "对白", "台词", "试探", "关系", "升温", "触碰", "饰件", "身份",
        "反应", "迟疑", "停顿", "离场", "中止", "收束", "登场", "境界",
        "升级", "进阶", "神装", "蓄势", "攻击", "爆发", "战斗", "能力",
        "神性", "群像", "季终",
    )
    terms.extend(item for item in functional if item in text)
    return list(dict.fromkeys(terms))


def _matched_rule(knowledge, context):
    context_text = json.dumps(
        context if isinstance(context, dict) else {},
        ensure_ascii=False, default=str)
    kind = str((context or {}).get("kind") or "")
    aliases = {
        "dialogue": "对白 台词 试探 关系 升温",
        "beat": "反应 停顿 迟疑 情绪 神性",
        "physical": "动作 战斗 攻击 触碰 爆发",
        "environment": "登场 场景 远景 空间 收束",
        "transition": "离场 转场 形态 进阶 升级",
    }
    haystack = f"{context_text} {aliases.get(kind, '')}"
    best = None
    best_score = 0
    for rule in knowledge["selection_rules"]:
        score = sum(
            max(1, len(term) // 2)
            for term in _rule_terms(rule.get("when"))
            if term in haystack)
        if score > best_score:
            best, best_score = rule, score
    return best


def _canonical_pattern(candidate, patterns):
    candidate = str(candidate or "").strip()
    if not candidate:
        return ""
    if candidate in patterns:
        return candidate
    return next(
        (pattern for pattern in patterns
         if candidate in pattern or pattern in candidate),
        candidate)


def select_shot_direction(value, index, *, raw=None, camera=None, kind=""):
    """为一镜收敛出一个镜头组合与少量视觉效果，避免把整库堆进单镜。"""
    knowledge = normalize_director_knowledge(value)
    context = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    raw_direction = context.get("style_direction")
    if not isinstance(raw_direction, dict):
        raw_direction = context
    camera = camera if isinstance(camera, dict) else {}
    patterns = knowledge["shot_language"]["shot_patterns"]
    rule = _matched_rule(
        knowledge, {**context, "kind": kind or context.get("kind", "")})
    pattern = _canonical_pattern(
        raw_direction.get("shot_pattern"), patterns)
    if not pattern and rule and rule["shots"]:
        pattern = _canonical_pattern(
            rule["shots"][
                (max(1, int(index)) - 1) % len(rule["shots"])],
            patterns)
    if not pattern and patterns:
        pattern = patterns[(max(1, int(index)) - 1) % len(patterns)]
    chosen = _items(raw_direction.get("visual_effects"))
    if not chosen and rule:
        chosen = _items(rule.get("effects"))
    if not chosen:
        visual = knowledge["visual_effects"]
        stable = []
        for field in ("lighting", "optical", "color_grade"):
            if visual[field]:
                stable.append(visual[field][0])
        rotating = [
            item for field in (
                "atmosphere", "materials", "particles", "post_process")
            for item in visual[field]]
        if rotating:
            stable.append(rotating[(max(1, int(index)) - 1) % len(rotating)])
        chosen = list(dict.fromkeys(stable))[:5]
    reason = str(raw_direction.get("selection_reason") or "").strip()
    if not reason and rule:
        reason = str(rule.get("purpose") or "").strip()
    if not reason:
        reason = (
            f"服务{kind or '本镜'}的叙事功能；每镜只执行一个主要运镜，"
            "特效不遮挡人物身份与关键动作")
    return {
        "schema": DIRECTOR_STYLE_SCHEMA,
        "shot_pattern": pattern,
        "visual_effects": chosen,
        "selection_reason": reason,
        "camera_contract": {
            key: camera.get(key, "")
            for key in (
                "shot_scale", "angle", "lens", "camera_position",
                "movement", "composition")
        },
    }
