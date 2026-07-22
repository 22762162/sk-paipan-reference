"""图片与 Seedance 的统一低/中/高质量策略。

质量不是单纯分辨率，而是按“出错后会污染多少下游镜头”分级。自动建议是
默认值；项目级和逐项人工覆盖始终优先，并把选择写入剧集文档供审计。
"""

from __future__ import annotations

import copy

from .errors import AifosError


QUALITY_LEVELS = ("low", "medium", "high")
QUALITY_LABELS = {"low": "低", "medium": "中", "high": "高"}
VIDEO_RESOLUTION_BY_QUALITY = {
    "low": "480p",
    "medium": "720p",
    "high": "1080p",
}
QUALITY_POLICY_SCHEMA = "aifos.quality-policy/v1"

_CLOSE_UP_WORDS = ("近景", "特写", "大特写", "面部", "脸部", "微距")
_EMOTION_WORDS = (
    "哭", "泪", "崩溃", "爆发", "暴怒", "愤怒", "绝望", "惊恐",
    "哽咽", "颤抖", "微表情", "眼眶", "嘴角抽搐", "下颌咬紧",
)


def normalize_quality(value, *, allow_auto=False, field="quality"):
    """把中英文/历史 lite 统一成 low|medium|high。"""
    aliases = {
        "低": "low", "low": "low", "lite": "low",
        "中": "medium", "medium": "medium", "standard": "medium",
        "高": "high", "high": "high",
        "自动": "auto", "auto": "auto", "": "auto",
    }
    quality = aliases.get(str(value or "").strip().lower())
    allowed = set(QUALITY_LEVELS) | ({"auto"} if allow_auto else set())
    if quality not in allowed:
        names = "auto|low|medium|high" if allow_auto else "low|medium|high"
        raise AifosError(f"{field} 只允许 {names}")
    return quality


def default_quality_policy():
    return {
        "schema": QUALITY_POLICY_SCHEMA,
        "image_default": "auto",
        # auto 的实际默认值仍是 Seedance Fast VIP / 720P（中档）。
        "video_default": "auto",
        "image_overrides": {},
        "video_overrides": {},
    }


def normalize_quality_policy(policy=None):
    result = default_quality_policy()
    if isinstance(policy, dict):
        result.update({key: copy.deepcopy(value) for key, value in policy.items()
                       if key in result})
    result["schema"] = QUALITY_POLICY_SCHEMA
    result["image_default"] = normalize_quality(
        result.get("image_default"), allow_auto=True,
        field="image_default")
    result["video_default"] = normalize_quality(
        result.get("video_default"), allow_auto=True,
        field="video_default")
    for field in ("image_overrides", "video_overrides"):
        raw = result.get(field) if isinstance(result.get(field), dict) else {}
        normalized = {}
        for key, value in raw.items():
            level = normalize_quality(value, allow_auto=True, field=field)
            if level != "auto":
                normalized[str(key)] = level
        result[field] = normalized
    return result


def recommend_shot_image_quality(shot, *, continuity_anchor=False):
    """正式关键帧默认中；五类下游污染风险自动升高。"""
    reasons = []
    readable = shot.get("readable_text") or {}
    if readable.get("required"):
        reasons.append("可读文字必须在静帧中锁定")
    try:
        people = int(shot.get("character_count", len(
            shot.get("characters") or [])) or 0)
    except (TypeError, ValueError):
        people = len(shot.get("characters") or [])
    if people >= 3:
        reasons.append(f"群像/准确人数（{people}人）")
    camera_design = ((shot.get("five_dimensions") or {}).get(
        "camera_design") or {})
    camera_text = " ".join(str(value or "") for value in (
        shot.get("camera"), camera_design.get("shot_scale"),
        camera_design.get("lens")))
    if any(word in camera_text for word in _CLOSE_UP_WORDS):
        reasons.append("人脸近景/细节会被视频放大")
    emotion_text = " ".join(str(value or "") for value in (
        shot.get("description"), shot.get("prompt"),
        (shot.get("performance") or {}).get("goal"),
        (shot.get("performance") or {}).get("micro_expression"),
        (shot.get("speech_timing") or {}).get("emotion")))
    if any(word in emotion_text for word in _EMOTION_WORDS):
        reasons.append("情绪爆发/哭戏/微表情")
    if continuity_anchor:
        reasons.append("前后视频段落连续性锚点")
    if reasons:
        return {"level": "high", "reasons": reasons,
                "rule": "critical_risk"}
    return {
        "level": "medium",
        "reasons": ["一次性普通正式镜头，默认生产档"],
        "rule": "formal_default",
    }


def recommend_asset_quality(kind, *, reuse_count=0, core_prop=False):
    if kind in ("style_test", "prompt_ab", "shot_candidate"):
        return {"level": "low", "reasons": ["方向试错/候选，不可直接进视频"],
                "rule": "draft_only"}
    if kind in ("character_candidate", "character_identity",
                "character_sheet", "cover", "poster"):
        return {"level": "high", "reasons": ["人物母资产/独立展示资产"],
                "rule": "mother_asset"}
    if kind == "scene_art":
        if reuse_count >= 2 or core_prop:
            reason = (f"高频复用场景（服务{reuse_count}个镜头）"
                      if reuse_count >= 2 else "核心道具会跨镜复用")
            return {"level": "high", "reasons": [reason],
                    "rule": "reused_asset"}
        return {"level": "low", "reasons": ["场景/道具方向试错"],
                "rule": "draft_only"}
    return {"level": "medium", "reasons": ["普通正式生产图"],
            "rule": "formal_default"}


def resolve_image_quality(recommendation, policy, item_id,
                          explicit_override=None):
    policy = normalize_quality_policy(policy)
    explicit = (normalize_quality(explicit_override, allow_auto=True,
                                  field="image_quality")
                if explicit_override is not None else "auto")
    override = explicit
    if override == "auto":
        override = policy["image_overrides"].get(str(item_id), "auto")
    if override == "auto":
        override = policy["image_default"]
    recommended = normalize_quality(recommendation.get("level"),
                                    field="recommended_quality")
    level = recommended if override == "auto" else override
    return {
        "level": level,
        "recommended": recommended,
        "source": "auto" if override == "auto" else "manual",
        "reasons": list(recommendation.get("reasons") or []),
        "rule": recommendation.get("rule", ""),
    }


def image_task_class_for(level, *, readable_text=False, final=False):
    level = normalize_quality(level, field="image_quality")
    if readable_text and level == "high":
        return "complex_text"
    if final:
        return "final"
    if level == "high":
        return "important"
    return "batch"


def resolve_video_quality(policy, shot_no=None, explicit_override=None):
    policy = normalize_quality_policy(policy)
    override = (normalize_quality(explicit_override, allow_auto=True,
                                  field="video_quality")
                if explicit_override is not None else "auto")
    if override == "auto" and shot_no is not None:
        override = policy["video_overrides"].get(f"shot:{int(shot_no)}", "auto")
    if override == "auto":
        override = policy["video_default"]
    # Seedance 自动档遵守项目默认：Fast VIP / 720P。
    level = "medium" if override == "auto" else override
    return {"level": level,
            "resolution": VIDEO_RESOLUTION_BY_QUALITY[level],
            "source": "auto" if override == "auto" else "manual"}


def set_policy_choices(policy, *, image_default=None, video_default=None,
                       image_overrides=None, video_overrides=None):
    """合并用户选择；传 auto 会移除逐项覆盖并恢复自动判级。"""
    result = normalize_quality_policy(policy)
    if image_default is not None:
        result["image_default"] = normalize_quality(
            image_default, allow_auto=True, field="image_default")
    if video_default is not None:
        result["video_default"] = normalize_quality(
            video_default, allow_auto=True, field="video_default")
    for field, updates in (("image_overrides", image_overrides),
                           ("video_overrides", video_overrides)):
        if not isinstance(updates, dict):
            continue
        for key, value in updates.items():
            level = normalize_quality(value, allow_auto=True, field=field)
            if level == "auto":
                result[field].pop(str(key), None)
            else:
                result[field][str(key)] = level
    return normalize_quality_policy(result)


def formal_reference_allowed(level):
    return normalize_quality(level, field="reference_quality") in (
        "medium", "high")
