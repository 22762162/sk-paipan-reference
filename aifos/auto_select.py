"""候选自动定版合同:一组四张 → CODEX 主导评选 → 自动确认其中一张。

剧本第一次确认后立即开始生产图片资产。每一组画面(每个正式角色、每件核心
道具、每个场景)统一生成 4 张候选,再由 CODEX 视觉评选出最符合本剧本要求的
一张并自动确认;人工随时可以改选并重新确认(见 director.select_* 系列)。

本模块只做零依赖的合同与判定:
- 评选请求(给 Provider 的 payload)与要求清单如何组织;
- 评选应答(image_select)的结构校验与归一化;
- 自动确认与转人工的判定阈值。
真正的调用、锁定与下游失效由 AI 导演中心负责。
"""

SELECT_SCHEMA = "aifos.image-select/v1"

# 每一组画面统一 4 张候选:人物、核心道具、场景概念图共用同一组量。
CANDIDATE_GROUP_SIZE = 4

# 评选置信度低于该值一律不自动确认,退回人工四选一,避免"自动选错还锁死"。
DEFAULT_MIN_CONFIDENCE = 0.6

SUBJECT_CN = {
    "character": "人物立绘",
    "prop": "核心道具",
    "scene": "场景概念图",
}

# 每类资产的评选维度:顺序即权重顺序,评选必须逐项给出结论。
SUBJECT_CRITERIA = {
    "character": (
        "身份符合度:性别、年龄感、职业身份与人物关系必须与剧本人物表一致",
        "剧情造型:服装、发型、配饰与本剧时代/阶层/处境相符,不得穿越或错阶层",
        "视觉识别度:五官与轮廓辨识清楚,后续镜头可稳定复用为身份锚点",
        "画风统一:与本剧唯一画风一致,不得混入其他画风或写实/3D 味",
        "画面硬伤:肢体畸形、多手多指、穿模、脸部崩坏、文字乱码一律扣死",
    ),
    "prop": (
        "剧情功能:外形结构必须支撑剧本写明的功能与使用方式",
        "时代材质:材质、工艺、磨损与剧本时代和持有人身份相符",
        "识别度:远景/小尺寸下仍可一眼认出,不与其他道具混淆",
        "画风统一:与本剧唯一画风一致,背景干净、单件主体",
        "画面硬伤:结构不合理、比例失真、文字乱码一律扣死",
    ),
    "scene": (
        "空间可用:地形、通行路径、遮挡与远近层次清楚,能容纳本场调度",
        "剧情符合:时代、地域、天气、时间与剧本场次描述一致",
        "空镜纯净:不得出现任何人物、人形或一次性剧情痕迹",
        "画风统一:与本剧唯一画风一致,可作为后续镜头的场景基准",
        "画面硬伤:透视崩坏、建筑结构不成立、文字乱码一律扣死",
    ),
}


def _text(value, default=""):
    text = str(value or "").strip()
    return text or default


def subject_label(kind):
    return SUBJECT_CN.get(kind, "图片资产")


def build_requirements(kind, name, facts):
    """把剧本事实压成评选要求清单(每条一行,评选逐条核对)。"""
    lines = []
    for label, value in facts or ():
        text = _text(value)
        if text:
            lines.append(f"{label}:{text}")
    if not lines:
        lines.append(f"{name}的全部要求以正式剧本与本剧唯一画风为准")
    return lines


def build_selection_payload(kind, name, *, requirements, candidates,
                            style="", project_title="", episode_number=0,
                            premise=""):
    """构造 image_select 能力的请求 payload。"""
    items = []
    for candidate in candidates or []:
        index = int(candidate.get("index") or 0)
        uri = _text(candidate.get("uri"))
        if index < 1 or not uri:
            continue
        items.append({
            "index": index,
            "uri": uri,
            "variant_label": _text(candidate.get("variant_label"), "候选"),
            "variant_focus": _text(candidate.get("variant_focus")),
        })
    items.sort(key=lambda item: item["index"])
    return {
        "schema": SELECT_SCHEMA,
        "subject_kind": kind,
        "subject_label": subject_label(kind),
        "subject_name": name,
        "project_title": project_title,
        "episode_number": int(episode_number or 0),
        "premise": _text(premise),
        "style": _text(style),
        "requirements": list(requirements or []),
        "criteria": list(SUBJECT_CRITERIA.get(kind, ())),
        "candidates": items,
        "candidate_count": len(items),
    }


def validate_image_select(data):
    """校验评选应答结构;返回错误说明,空字符串表示通过。

    失败关闭:结构不合法一律不产生自动确认,交回人工四选一。
    """
    if not isinstance(data, dict):
        return "评选应答不是 JSON 对象"
    if "best_index" not in data:
        return "缺少 best_index 字段"
    try:
        best = int(data["best_index"])
    except (TypeError, ValueError):
        return "best_index 必须是整数"
    if isinstance(data["best_index"], bool):
        return "best_index 必须是整数"
    if best < 0:
        return "best_index 不能为负数"
    if "needs_human" in data and type(data["needs_human"]) is not bool:
        return "needs_human 必须是真正的 JSON 布尔值"
    if "confidence" in data:
        try:
            confidence = float(data["confidence"])
        except (TypeError, ValueError):
            return "confidence 必须是 0-1 之间的数值"
        if confidence < 0 or confidence > 1:
            return "confidence 必须落在 0-1 之间"
    scores = data.get("scores")
    if scores is not None and not isinstance(scores, list):
        return "scores 必须是数组"
    for item in scores or []:
        if not isinstance(item, dict):
            return "scores 每项必须是对象"
        if "index" not in item or "score" not in item:
            return "scores 每项必须含 index 与 score"
        try:
            int(item["index"])
            float(item["score"])
        except (TypeError, ValueError):
            return "scores 的 index/score 必须是数值"
    return ""


def _clean_list(value, limit=6):
    if isinstance(value, str):
        value = [value]
    return [_text(item) for item in (value or []) if _text(item)][:limit]


def normalize_selection(raw, candidate_count):
    """把 Provider 应答归一成导演中心可直接判定的评选结论。"""
    count = max(int(candidate_count or 0), 0)
    verdict = {
        "schema": SELECT_SCHEMA,
        "best_index": 0,
        "runner_up_index": 0,
        "confidence": 0.0,
        "needs_human": True,
        "reason": "",
        "scores": [],
    }
    if not isinstance(raw, dict):
        verdict["reason"] = "评选未返回可解析结论"
        return verdict
    scores = []
    for item in raw.get("scores") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item["index"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if index < 1 or (count and index > count):
            continue
        scores.append({
            "index": index,
            "score": max(0.0, min(100.0, round(score, 2))),
            "match": _clean_list(item.get("match") or item.get("reasons")),
            "violations": _clean_list(item.get("violations")),
        })
    scores.sort(key=lambda item: (-item["score"], item["index"]))
    verdict["scores"] = scores
    try:
        best = int(raw.get("best_index") or 0)
    except (TypeError, ValueError):
        best = 0
    if best < 1 or (count and best > count):
        best = scores[0]["index"] if scores else 0
    verdict["best_index"] = best
    runner_up = next(
        (item["index"] for item in scores if item["index"] != best), 0)
    verdict["runner_up_index"] = runner_up
    if "confidence" in raw:
        try:
            verdict["confidence"] = max(
                0.0, min(1.0, float(raw["confidence"])))
        except (TypeError, ValueError):
            verdict["confidence"] = 0.0
    elif scores:
        # 未给置信度时按第一名与第二名的分差推导:分差越大越可信。
        top = scores[0]["score"]
        second = next(
            (item["score"] for item in scores if item["index"] != best), 0.0)
        verdict["confidence"] = max(
            0.0, min(1.0, round((top - second) / 40.0 + 0.5, 3)))
    verdict["needs_human"] = bool(raw.get("needs_human"))
    verdict["reason"] = _text(
        raw.get("reason") or raw.get("summary"),
        "评选未给出理由")
    if best < 1:
        verdict["needs_human"] = True
        if not raw.get("reason"):
            verdict["reason"] = "评选未指出符合剧本要求的候选"
    return verdict


def selection_decision(verdict, candidate_count,
                       min_confidence=DEFAULT_MIN_CONFIDENCE):
    """自动确认还是转人工;附带可直接落库/展示的理由。"""
    count = max(int(candidate_count or 0), 0)
    index = int((verdict or {}).get("best_index") or 0)
    confidence = float((verdict or {}).get("confidence") or 0.0)
    reason = _text((verdict or {}).get("reason"))
    if index < 1 or (count and index > count):
        return {"auto_confirm": False, "index": 0, "confidence": confidence,
                "reason": reason or "评选未指出符合剧本要求的候选",
                "blocked_by": "no_choice"}
    if (verdict or {}).get("needs_human"):
        return {"auto_confirm": False, "index": index,
                "confidence": confidence,
                "reason": reason or "评选认为四张都不够好,需要人工判断",
                "blocked_by": "needs_human"}
    if confidence < float(min_confidence):
        return {"auto_confirm": False, "index": index,
                "confidence": confidence,
                "reason": (
                    f"评选置信度 {confidence:.2f} 低于自动确认阈值 "
                    f"{float(min_confidence):.2f}"
                    + (f";{reason}" if reason else "")),
                "blocked_by": "low_confidence"}
    return {"auto_confirm": True, "index": index, "confidence": confidence,
            "reason": reason or "评选判定该张最符合本剧本要求",
            "blocked_by": ""}
