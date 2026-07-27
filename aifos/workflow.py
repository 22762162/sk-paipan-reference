"""SK 漫剧工业流：连续性圣经、五维分镜、生产门禁与交付复核。

本模块只做确定性的结构化编排，不调用模型。Provider 负责创作原始剧本/分镜，
这里把结果收敛成可审、可机检、可交接给 Seedance 的生产合同。
"""

import copy
import html
import json
import re
import subprocess
import sys
from pathlib import Path

from .adapters.claude_script import (is_background_role,
                                     validate_script_bible)
from .quality_policy import default_quality_policy, resolve_video_quality
from .inner_persona import (
    apply_inner_persona_to_shots,
    normalize_inner_persona_policy,
    physical_scene_characters,
    shot_timeline_state,
)
from .prompt_contract import (
    build_physical_contract,
    compile_shot_prompt,
    readable_text_required,
    sanitize_text_whitelist,
)

from .spatial_blocking import (
    build_character_number_map,
    build_spatial_plan,
    mark_spatial_reference_requirements,
    requires_spatial_reference,
    validate_spatial_plan,
)


PIPELINE_VERSION = "sk-manju-v5"
TEXT_CARRIERS = (
    "弹幕", "聊天框", "直播屏", "手机屏", "电脑", "后台", "合同",
    "欠条", "门牌", "榜单", "公司标识", "大字", "屏幕", "字幕",
)
SHOT_FUNCTIONS = {
    "environment": "铺垫", "dialogue": "信息交代", "reaction": "反应",
    "beat": "留白", "physical": "蓄势", "inner_monologue": "内心戏",
}
APPEARANCE_STATE_FIELDS = (
    "wardrobe", "headwear", "hair_visibility", "hair_makeup")
APPEARANCE_STATE_VERSION = 2
APPEARANCE_STATE_ALIASES = {
    "wardrobe": ("wardrobe", "costume", "clothing", "服装", "衣着"),
    "headwear": ("headwear", "hat", "头饰", "帽冠", "冠帽"),
    "hair_visibility": (
        "hair_visibility", "hair_exposure", "头发可见度"),
    "hair_makeup": (
        "hair_makeup", "hair_and_makeup", "styling", "妆发", "发型妆容"),
}
WARDROBE_TOKENS = (
    "官袍", "直裰", "旅装", "布衣", "麻衣", "短褐", "长衫", "长袍",
    "圆领袍", "交领袍", "盘领袍",
    "朝服", "常服", "公服", "制服", "工装", "西装", "衬衫", "外套",
    "夹克", "风衣", "大氅", "斗篷", "披风", "裙", "裤", "铠甲",
    "盔甲", "校服", "礼服", "睡衣", "中衣", "襦裙", "道袍", "袈裟",
)
WARDROBE_CONFLICT_GROUPS = (
    {
        "官袍", "直裰", "旅装", "布衣", "麻衣", "短褐", "长衫",
        "长袍", "圆领袍", "交领袍", "盘领袍", "朝服", "常服",
        "公服", "制服", "工装", "西装", "校服", "礼服", "睡衣",
        "中衣", "襦裙", "道袍", "袈裟", "铠甲", "盔甲",
    },
    {"裙", "裤"},
    {"外套", "夹克", "风衣", "大氅", "斗篷", "披风"},
)
HEADWEAR_TOKENS = (
    "乌纱帽", "乌纱", "网巾", "发冠", "头冠", "冠", "帽", "头巾",
    "头盔", "斗笠", "簪", "钗",
)
HAIR_MAKEUP_TOKENS = (
    "束发", "散发", "披发", "发髻", "短发", "长发", "马尾", "妆容",
    "素颜", "眼妆", "唇妆", "底妆",
)
APPEARANCE_CHANGE_TOKENS = (
    "换装", "更衣", "换上", "换成", "改穿", "穿上", "套上", "披上",
    "披着", "已脱", "脱去", "脱下", "褪下", "摘下", "取下",
    "戴上", "系上", "换",
)
CONDITION_FIELDS = (
    "life_state", "consciousness_state", "embodiment", "mobility")
NORMAL_CONDITION = {
    "life_state": "alive",
    "consciousness_state": "awake",
    "embodiment": "physical",
    "mobility": "active",
}
TERMINAL_STATE_TOKENS = (
    "已咽气", "咽气", "断气", "死亡", "死去", "身亡", "尸身",
    "被杀", "杀死", "毙命", "击毙", "殒命", "丧命", "害死",
    "被刺死", "刺死", "被砍死", "砍死", "中刀而亡",
)
WAKE_STATE_TOKENS = (
    "醒来", "惊醒", "苏醒", "转醒", "恢复意识", "清醒过来",
)
SLEEP_STATE_TOKENS = (
    "睡着", "入睡", "沉睡", "闭眼睡去", "陷入睡眠",
)
UNCONSCIOUS_STATE_TOKENS = (
    "昏迷", "昏厥", "失去意识", "晕倒", "昏倒", "被打昏",
)


def _normalize_functional_figures(value):
    if isinstance(value, dict):
        value = [value]
    return [
        copy.deepcopy(item) for item in (value or [])
        if isinstance(item, dict)
        and (item.get("name") or item.get("label"))
        and type(item.get("count")) is int and item.get("count") > 0
    ]


def _functional_figure_count(value):
    return sum(
        item["count"] for item in _normalize_functional_figures(value))


def _functional_figure_line(value):
    figures = _normalize_functional_figures(value)
    return "、".join(
        f"{item.get('name') or item.get('label')}{item['count']}人"
        + (f"({item.get('state') or item.get('function')})"
           if item.get("state") or item.get("function") else "")
        for item in figures
    ) or "无"


def _rules_from_standard(standard):
    content = (standard or {}).get("content", standard or {})
    rules = content.get("rules", {}) if isinstance(content, dict) else {}
    return rules if isinstance(rules, dict) else {}


def content_key(standard, key, default=None):
    """兼容标准快照与裸内容的顶层字段读取。"""
    content = (standard or {}).get("content", standard or {})
    return content.get(key, default) if isinstance(content, dict) else default


def production_profile(config, standard=None):
    """返回每集随包归档的不可漂移生产配置与规则快照。"""
    configured = config.get("production", default={}) or {}
    jimeng = config.get("providers", "jimeng", default={}) or {}
    rules = _rules_from_standard(standard)
    production = rules.get("production", {})
    dialogue = rules.get("dialogue", {})
    performance = rules.get("performance", {})
    storyboard = rules.get("storyboard", {})
    delivery = rules.get("delivery", {})
    return {
        "pipeline_version": PIPELINE_VERSION,
        "standard_profile_key": (standard or {}).get(
            "profile_key", content_key(standard, "profile_key", PIPELINE_VERSION)),
        "standard_version": (standard or {}).get("version", 1),
        "standard_version_id": (standard or {}).get("version_id", 0),
        "standard_name": (standard or {}).get(
            "name", content_key(standard, "name", "SK 五维漫剧标准")),
        "standard_fingerprint": (standard or {}).get(
            "fingerprint", "legacy-config"),
        "video_model": production.get(
            "video_model", jimeng.get("model_version", "seedance2.0fast_vip")),
        "resolution": production.get(
            "resolution", jimeng.get("video_resolution", "720p")),
        "preferred_segment_seconds": production.get(
            "preferred_segment_seconds", configured.get(
                "preferred_segment_seconds", [5, 8])),
        "max_segment_seconds": production.get(
            "max_segment_seconds", configured.get("max_segment_seconds", 15)),
        "time_precision_seconds": production.get("time_precision_seconds", 0.5),
        "voice": production.get(
            "voice", configured.get("voice", "jimeng_builtin")),
        "lip_sync": bool(production.get(
            "lip_sync", configured.get("lip_sync", True))),
        "burn_subtitles": bool(production.get(
            "burn_subtitles", configured.get("burn_subtitles", False))),
        "text_lock_provider": production.get(
            "text_lock_provider", configured.get(
                "text_lock_provider", "ChatGPT关键帧")),
        "prompt_contract": copy.deepcopy(
            production.get("prompt_contract") or {
                "schema": "aifos.shot-prompt/v2.2",
                "compact_prompt_sent_to_model": True,
                "full_prompt_kept_for_audit": True,
            }),
        "max_dialogue_chars": dialogue.get("max_chars_per_shot", 25),
        "reaction_min_ratio": performance.get(
            "listener_duration_ratio", 2 / 3),
        "reaction_seconds": performance.get("reaction_seconds", [1.5, 3]),
        "beat_seconds": performance.get("beat_seconds", [2, 4]),
        "minimum_vertical_angles": storyboard.get(
            "minimum_vertical_angles_per_segment", 2),
        "shot_contract_columns": len(storyboard.get("required_columns", [])),
        "review_layers": delivery.get(
            "review_layers", ["自动文件检查", "抽帧检查板", "逐段内容复核"]),
        "rules": copy.deepcopy(rules),
    }


def build_continuity_bible(project, script, profile):
    """锁定项目级角色、场景、文字与生成配置。"""
    rules = profile.get("rules", {})
    continuity_rules = rules.get("continuity", {})
    character_asset_rules = rules.get("character_assets", {})
    text_rules = rules.get("text_assets", {})
    delivery_rules = rules.get("delivery", {})
    inner_policy = normalize_inner_persona_policy(
        script, rules.get("inner_persona"))
    characters = []
    for index, character in enumerate(script.get("characters", []), 1):
        name = character["name"]
        if is_background_role(character):
            # 逐镜身份合同要求每个登记出场人物都有明确的性别与年龄段，
            # 否则判死整集。背景路人剧本里通常不写这两项，但"由模型
            # 临场猜"正是合同要禁的事；这里由系统按名字确定性地定下
            # 一个具体值并落进连续性圣经，人工可在人物卡里直接改写。
            # 路人不建母资产、不跨镜比对，定成哪个值都不影响连续性。
            extra_gender = str(
                character.get("gender")
                or ("男" if sum(ord(ch) for ch in name) % 2 == 0 else "女"))
            extra_age = str(
                character.get("age_range") or "青年(20-35岁)")
            characters.append({
                "name": name,
                "role": character.get("role", "背景路人"),
                "background_role": True,
                "crowd_function": character.get("crowd_function", ""),
                "gender": extra_gender,
                "age_range": extra_age,
                "identity": str(
                    character.get("identity")
                    or character.get("crowd_function")
                    or "本场背景路人"),
                "identity_source": "system_default_background_extra",
                "identity_anchor": "仅锁定场次功能与人数；无独立人物设定或身份参考图",
                # 背景路人不建人物母资产，但仍必须有唯一的可见服装状态：
                # 缺这一条，逐镜生产合同会因为"缺少当前镜头唯一服装状态"
                # 直接判死，整集卡在关键帧。路人只锁时代/身份档次，不锁款式。
                "default_wardrobe": str(
                    character.get("costume_direction")
                    or "与本场时代、地域和身份相称的普通常服，"
                       "不得出现跨时代或现代服饰"),
                "costume_anchor": "只需符合本场时代与身份档次；不锁具体款式，"
                                  "不跨镜比对同一路人的衣服细节",
                "default_position": ["画面左1/3", "画面中", "画面右2/3"][
                    (index - 1) % 3],
            })
            continue
        characters.append({
            "name": name,
            "role": character.get("role", ""),
            "introduction": character.get("introduction", ""),
            "gender": character.get("gender", ""),
            "age_range": character.get("age_range", ""),
            "identity": character.get("identity", ""),
            "personality": character.get("personality", ""),
            "background_prompt": character.get("background_prompt", ""),
            "relationships": character.get("relationships", ""),
            "identity_anchor": f"{name}角色参考图 + 同名禁令",
            "face_hair_anchor": "继承项目角色参考，不改脸型、发型、发色与年龄感",
            "costume_anchor": "继承本场服装参考，不跨镜换装",
            "default_wardrobe": str(
                next((
                    item.get("costume") for item in (
                        character.get("visual_variants") or [])
                    if isinstance(item, dict) and item.get("costume")
                ), None)
                or character.get("costume_direction")
                or ""),
            "signature_prop": character.get("signature_props") or "无",
            "default_position": ["画面左1/3", "画面中", "画面右2/3"][
                (index - 1) % 3],
        })
    seen = set()
    scenes = []
    for scene in script.get("scenes", []):
        location = scene.get("location", "")
        if location in seen:
            continue
        seen.add(location)
        scenes.append({
            "name": location,
            "layout_anchor": f"{location}空间布局在本集内固定",
            "equipment_anchor": "设备现代且统一；旧空间也必须干净、可拍摄",
            "light_anchor": "主光方向与色温跨镜保持一致",
        })
    return {
        "pipeline_version": PIPELINE_VERSION,
        "standard_fingerprint": profile.get("standard_fingerprint", ""),
        "project": project.get("title", ""),
        "episode": script.get("episode_number", 0),
        "story_bible_version": script.get("story_bible_version", 1),
        "story_world": copy.deepcopy(script.get("story_world") or {}),
        "story_background": copy.deepcopy(
            script.get("story_background") or {}),
        "same_name_rule": ("同一实体全程使用完全相同的名字"
                           if continuity_rules.get(
                               "canonical_entity_names", True)
                           else "按项目自定义命名"),
        "state_fields": list(dict.fromkeys([
            *continuity_rules.get(
                "state_labels",
                ["姿态", "伤势", "持有道具", "情绪", "朝向关系"]),
            "服装", "头饰", "妆发",
        ])),
        "characters": characters,
        "scenes": scenes,
        "text_policy": {
            "whitelist": [],
            "readable_text_requires_keyframe": text_rules.get(
                "readable_text_requires_keyframe", True),
            "forbid_generated_gibberish": text_rules.get(
                "forbid_generated_gibberish", True),
        },
        "delivery_policy": {
            "forbid_dialogue_subtitles": not profile["burn_subtitles"],
            "forbid_unknown_people": continuity_rules.get(
                "on_stage_characters_only", True),
            "require_start_end_state": continuity_rules.get(
                "end_state_to_next_start", True),
            "require_contact_sheet": delivery_rules.get(
                "html_review_board_required", True),
            "require_content_review": delivery_rules.get(
                "content_review_required", True),
            "require_delivery_verifier": delivery_rules.get(
                "delivery_verifier_required", True),
        },
        "character_asset_policy": copy.deepcopy(character_asset_rules),
        "inner_persona_policy": copy.deepcopy(inner_policy),
        "production_profile": copy.deepcopy(profile),
    }


def _scene_map(script):
    return {s["scene_no"]: s for s in script.get("scenes", [])}


def _type_word(scene, shot):
    text = " ".join((scene.get("action", ""), shot.get("description", "")))
    if any(word in text for word in ("打", "追", "冲", "摔", "击", "逃")):
        return "动作打斗"
    if len(scene.get("characters", [])) >= 3:
        return "群戏调度"
    if shot.get("kind") == "dialogue" and len(scene.get("characters", [])) >= 2:
        return "对峙冲突"
    return "独白抒情" if shot.get("kind") in ("dialogue", "beat") else "大场面定场"


def _camera_plan(camera, kind, index, rules=None, prev_scale=None,
                 scene_start=False):
    library = (rules or {}).get("camera_library", {})
    storyboard_rules = (rules or {}).get("storyboard", {})
    scales = library.get(
        "shot_scales", ["远景", "全景", "中景", "近景", "特写", "大特写"])
    angles = library.get(
        "angles", ["平视", "俯拍", "仰拍", "高机位", "低机位"])
    positions = library.get(
        "positions", ["正面", "斜侧", "过肩", "侧面"])
    movements = library.get(
        "movements", ["固定", "推", "拉", "摇", "移", "跟", "环绕", "手持", "升降"])
    compositions = library.get(
        "compositions", ["黄金分割", "引导线", "中心构图", "对称构图"])
    camera = camera or ""
    explicit = next((mark for mark in ("大特写", "特写", "近景", "中景",
                                       "全景", "远景") if mark in camera),
                    None)
    if explicit:
        scale = explicit if explicit in scales else (
            "全景" if "全景" in scales else scales[0])
    elif scene_start:
        # 每场开场环境镜:远景/全景交替定场,避免整集都是近景
        wide = [s for s in ("远景", "全景") if s in scales]
        scale = wide[(index - 1) % len(wide)] if wide else scales[0]
    elif kind == "reaction":
        scale = "近景" if "近景" in scales else scales[0]
    elif kind == "beat":
        scale = "中景" if "中景" in scales else scales[0]
    else:
        # 对白/动作镜:中近全轮换,保证纵向景别有变化
        fallback_scales = [s for s in ("中景", "近景", "全景", "特写")
                           if s in scales]
        scale = (fallback_scales[(index - 1) % len(fallback_scales)]
                 if fallback_scales else scales[0])
    if (prev_scale and scale == prev_scale and not explicit
            and not scene_start):
        # 标准要求相邻景别变化:与上一镜相同时顺位换一档
        scale = next((s for s in ("中景", "全景", "近景", "特写")
                      if s in scales and s != prev_scale), scale)
    if any(token in camera for token in ("顶拍", "顶视", "鸟瞰")):
        angle = "顶拍"
    elif any(token in camera for token in ("俯拍", "高机位", "高角度")):
        angle = "俯拍"
    elif any(token in camera for token in ("仰拍", "低机位", "低角度")):
        angle = "仰拍"
    else:
        angle = angles[(index - 1) % len(angles)]
    angle = {
        "低机位": "仰拍",
        "高机位": "俯拍",
        "过肩": "平视",
    }.get(angle, angle)
    movement = "固定" if "固定" in movements else movements[0]
    movement_explicit = False
    for candidate in sorted(movements, key=len, reverse=True):
        if candidate in camera:
            movement = candidate
            movement_explicit = True
            break
    if kind in ("reaction", "beat") and not movement_explicit:
        movement = ("推" if kind == "reaction" and "推" in movements
                    else "固定" if "固定" in movements else movements[0])
    explicit_position = next((
        value for token, value in (
            ("过肩", "过肩"), ("背面", "背面"), ("背后", "背面"),
            ("侧面", "侧面"), ("侧脸", "侧面"), ("正面", "正面"),
        ) if token in camera), None)
    explicit_composition = next((
        value for value in compositions if value in camera), None)
    # 高/低机位属于垂直角度，不应再被当作正侧背方位独立轮换；
    # 否则会产生“俯拍 + 低机位”这种无法执行的双重合同。
    lateral_positions = [
        value for value in positions if value not in ("低机位", "高机位")]
    if not lateral_positions:
        lateral_positions = ["正面", "斜侧", "过肩", "侧面"]
    return {
        "shot_scale": scale,
        "angle": angle,
        "lens": "85mm" if scale in ("近景", "特写") else "35mm",
        "camera_position": (
            explicit_position
            or lateral_positions[(index - 1) % len(lateral_positions)]),
        "movement": movement,
        "composition": (
            explicit_composition
            or compositions[(index - 1) % len(compositions)]),
        "speed": "正常",
        "axis_offset_degrees": (
            ((index - 1) % 3 - 1) * float(storyboard_rules.get(
                "adjacent_camera_axis_change_degrees", 30))),
        "movement_motivation": "靠近角色情绪" if movement in ("推", "急推", "微推")
        else "建立清晰空间关系",
    }


def _state(name, continuity, emotion="专注", pose="站立，重心稳定"):
    anchor = next(
        (c for c in continuity.get("characters", []) if c["name"] == name), {})
    state = {
        "pose": pose,
        "injury": "无伤",
        "prop": anchor.get("signature_prop", "无"),
        "emotion": emotion,
        "direction": "面向本镜主体，视线不越轴",
        "position": anchor.get("default_position", "画面中"),
        "headwear": {
            "presence": "none",
            "kind": "none",
            "name": "none",
        },
        "hair_visibility": "fully_visible",
        "condition": copy.deepcopy(NORMAL_CONDITION),
    }
    wardrobe = str(anchor.get("default_wardrobe") or "").strip()
    if wardrobe:
        state["wardrobe"] = wardrobe
        headwear = [
            token for token in HEADWEAR_TOKENS if token in wardrobe
            and not any(token != other and token in other
                        for other in HEADWEAR_TOKENS if other in wardrobe)
        ]
        headwear = list(dict.fromkeys(headwear))
        if headwear:
            headwear_name = "、".join(headwear)
            if any(token in headwear_name for token in ("乌纱帽", "乌纱")):
                kind = "official_hat"
            elif "头盔" in headwear_name:
                kind = "helmet"
            elif any(
                    token in headwear_name
                    for token in ("发冠", "头冠", "冠")):
                kind = "crown"
            elif any(token in headwear_name for token in ("簪", "钗")):
                kind = "hair_ornament"
            else:
                kind = "soft_hat"
            state["headwear"] = {
                "presence": "worn",
                "kind": kind,
                "name": headwear_name,
            }
            state["hair_visibility"] = (
                "covered" if kind == "helmet"
                else "fully_visible" if kind == "hair_ornament"
                else "partially_visible")
    return state


def _normalized_explicit_state(value):
    """Normalize model/user state aliases without discarding unknown fields."""
    state = copy.deepcopy(value) if isinstance(value, dict) else {}
    for canonical, aliases in APPEARANCE_STATE_ALIASES.items():
        if state.get(canonical):
            continue
        for alias in aliases:
            if state.get(alias):
                state[canonical] = (
                    copy.deepcopy(state[alias])
                    if isinstance(state[alias], dict)
                    else str(state[alias]).strip())
                break
    return state


def _character_visual_clause(name, text):
    """Return the current-shot clause that visibly describes ``name``.

    This is a legacy fallback only. New storyboards carry structured
    wardrobe/headwear/hair_makeup fields. Keeping the fallback narrow lets old
    saved boards gain continuity without re-running the writer model.
    """
    source = str(text or "")
    if not source or not name:
        return ""
    matches = list(re.finditer(re.escape(str(name)), source))
    if not matches:
        return ""
    appearance_tokens = (
        *WARDROBE_TOKENS, *HEADWEAR_TOKENS, *HAIR_MAKEUP_TOKENS,
    )
    for match in matches:
        tail = source[match.start():match.start() + 96]
        # A comma normally starts another actor or a new visual fact. Keep the
        # first actor-local phrase only so another character's outfit can never
        # be recorded as this actor's wardrobe.
        clause = re.split(r"[，,。；\n]", tail, maxsplit=1)[0]
        separate_object = any(
            marker in clause for marker in (
                "身旁放", "旁边放", "边放", "挂着", "搭着", "摆着",
                "搁着", "散落", "置于", "放在", "搭在"))
        worn = any(
            marker in clause for marker in (
                "身穿", "穿着", "穿上", "换上", "换成", "改穿",
                "套上", "披上", "披着", "着一身"))
        if (any(token in clause for token in appearance_tokens)
                and (not separate_object or worn)):
            return clause.strip(" ，,；。")
    return ""


def _visible_appearance(name, text, explicit=None):
    explicit = _normalized_explicit_state(explicit)
    result = {
        field: copy.deepcopy(explicit.get(field))
        for field in APPEARANCE_STATE_FIELDS
        if str(explicit.get(field) or "").strip()
    }
    clause = _character_visual_clause(name, text)
    if not clause:
        return result
    if not result.get("wardrobe") and any(
            token in clause for token in WARDROBE_TOKENS):
        wardrobe = clause
        if wardrobe.startswith(str(name)):
            wardrobe = wardrobe[len(str(name)):]
        # For a legacy unstructured change such as
        # “已脱官袍披旧月白直裰”, retain the new visible look after the final
        # donning verb, not both the removed and newly worn outfits.
        markers = (
            "换上", "换成", "改穿", "穿上", "套上", "披上", "披着",
            "戴上", "换", "身穿", "穿", "披",
        )
        marker_hits = [
            (wardrobe.rfind(marker), marker)
            for marker in markers if marker in wardrobe
        ]
        if marker_hits:
            index, marker = max(marker_hits, key=lambda item: item[0])
            wardrobe = wardrobe[index + len(marker):]
        ends = [
            wardrobe.find(token) + len(token)
            for token in (*WARDROBE_TOKENS, *HEADWEAR_TOKENS)
            if token in wardrobe
        ]
        if ends:
            wardrobe = wardrobe[:max(ends)]
        result["wardrobe"] = wardrobe.strip(" ，,；。、")
    if not result.get("headwear"):
        headwear = list(dict.fromkeys(
            token for token in HEADWEAR_TOKENS if token in clause))
        if headwear:
            # Prefer precise tokens over their substrings (乌纱帽 > 乌纱 > 帽).
            headwear = [
                token for token in headwear
                if not any(token != other and token in other
                           for other in headwear)
            ]
            result["headwear"] = "、".join(headwear)
        elif (_appearance_transition(name, clause)
              and any(token in clause for token in HAIR_MAKEUP_TOKENS)):
            result["headwear"] = "无外戴帽冠（按本镜束发/妆发状态）"
    if not result.get("hair_makeup") and any(
            token in clause for token in HAIR_MAKEUP_TOKENS):
        result["hair_makeup"] = clause
    return result


def _appearance_transition(name, text):
    clause = _character_visual_clause(name, text)
    return bool(clause and any(
        token in clause for token in APPEARANCE_CHANGE_TOKENS))


def _appearance_signature(value):
    text = str(value or "")
    return {
        token for token in (*WARDROBE_TOKENS, *HEADWEAR_TOKENS)
        if token in text
    }


def _appearance_conflicts(previous, current):
    """Only call two explicit looks different when their garment anchors clash."""
    previous_terms = _appearance_signature(previous)
    current_terms = _appearance_signature(current)
    return any(
        (previous_terms & group)
        and (current_terms & group)
        and (previous_terms & group).isdisjoint(current_terms & group)
        for group in WARDROBE_CONFLICT_GROUPS)


def _reconcile_explicit_appearance(name, text, explicit):
    """Let actor-local shot facts override a leaked global appearance.

    AI storyboard JSON can contain a structurally valid but story-invalid
    wardrobe copied from a character bible.  For example, the action says
    ``沈砚布旅装`` while ``start_state.沈砚.wardrobe`` contains his later
    official robe.  A direct actor-local clause in the current shot is the
    narrower source of truth and is safe to apply deterministically.
    """
    declared = _normalized_explicit_state(explicit)
    visible = _visible_appearance(name, text)
    corrections = []
    transition = _appearance_transition(name, text)
    for field in APPEARANCE_STATE_FIELDS:
        visible_raw = visible.get(field)
        declared_raw = declared.get(field)
        visible_value = str(visible_raw or "").strip()
        declared_value = str(declared_raw or "").strip()
        if not visible_value:
            continue
        # v2.2 structured headwear is authoritative. A narrow legacy phrase in
        # the action may be audited against it, but must never flatten the
        # object back into an untyped string.
        if field == "headwear" and isinstance(declared_raw, dict):
            continue
        conflicts = (
            _appearance_conflicts(declared_value, visible_value)
            if field == "wardrobe"
            else bool(declared_value and declared_value != visible_value
                      and declared_value not in visible_value
                      and visible_value not in declared_value))
        if conflicts:
            declared[field] = visible_value
            corrections.append({
                "character": str(name),
                "field": field,
                "from": declared_value,
                "to": visible_value,
                "reason": (
                    "当前镜头明确换装后的可见状态优先"
                    if transition else
                    "当前镜头动作中的人物局部事实优先于全局人物设定"),
            })
        elif not declared_value:
            declared[field] = visible_value
    return declared, corrections


def reconcile_shot_semantics(shot):
    """Repair one saved/current shot without changing its story action.

    The function is intentionally deterministic: it only reconciles explicit
    actor-local appearance evidence already present in the shot.  It never
    invents a new outfit, prop, action or plot beat.
    """
    repaired = copy.deepcopy(shot or {})
    action_text = "；".join(str(value or "") for value in (
        repaired.get("description"), repaired.get("action"),
        repaired.get("prompt")))
    start_states = (
        repaired.get("start_state")
        if isinstance(repaired.get("start_state"), dict) else {})
    end_states = (
        repaired.get("end_state")
        if isinstance(repaired.get("end_state"), dict) else {})
    corrections = list(repaired.get("semantic_corrections") or [])
    transition = any(
        token in action_text for token in APPEARANCE_CHANGE_TOKENS)
    for name in repaired.get("characters") or []:
        start = copy.deepcopy(start_states.get(name) or {})
        end = copy.deepcopy(end_states.get(name) or {})
        visible = _visible_appearance(name, action_text)
        visible_wardrobe = str(visible.get("wardrobe") or "").strip()
        if visible_wardrobe:
            for label, state in (("start_state", start), ("end_state", end)):
                current = str(state.get("wardrobe") or "").strip()
                # A visible change may legitimately start in the old look;
                # only its endpoint must match the new actor-local clause.
                if transition and label == "start_state":
                    continue
                if (_appearance_conflicts(current, visible_wardrobe)
                        or not current):
                    if current != visible_wardrobe:
                        corrections.append({
                            "character": str(name),
                            "field": f"{label}.wardrobe",
                            "from": current,
                            "to": visible_wardrobe,
                            "reason": (
                                "当前镜头动作中的人物局部服装事实优先于"
                                "人物全局造型或后续场景造型"),
                        })
                    state["wardrobe"] = visible_wardrobe
        start_states[name] = start
        end_states[name] = end
    repaired["start_state"] = start_states
    repaired["end_state"] = end_states
    repaired["semantic_corrections"] = list({
        json.dumps(item, ensure_ascii=False, sort_keys=True): item
        for item in corrections if isinstance(item, dict)
    }.values())
    return repaired


def _actor_semantic_windows(text, name, actors):
    """Actor-local clauses that stop before another named character."""
    source = str(text or "")
    actor = str(name or "")
    if not source or not actor:
        return []
    windows = []
    for match in re.finditer(re.escape(actor), source):
        window = source[match.start():match.start() + 120]
        cut_points = [
            index for index in (
                window.find("。"), window.find("；"), window.find("\n"))
            if index >= 0
        ]
        for other in actors or []:
            other = str(other or "")
            if not other or other == actor:
                continue
            index = window.find(other, len(actor))
            if index >= 0:
                cut_points.append(index)
        if cut_points:
            window = window[:min(cut_points)]
        windows.append(window)
    return windows


def _condition_from_state(state, *, default=None):
    """Return one complete v2.2 condition without mutating the source state."""
    state = state if isinstance(state, dict) else {}
    nested = state.get("condition")
    nested = nested if isinstance(nested, dict) else {}
    values = copy.deepcopy(default) if isinstance(default, dict) else {}
    for field in CONDITION_FIELDS:
        value = nested.get(field)
        if value is None:
            value = state.get(field)
        if value is not None and str(value).strip():
            values[field] = str(value).strip()
    return values


def _condition_signature(value):
    value = value if isinstance(value, dict) else {}
    return tuple(str(value.get(field) or "").strip() for field in CONDITION_FIELDS)


def _condition_transition_kind(name, text, actors=None):
    context = "；".join(_actor_semantic_windows(text, name, actors))
    actor = re.escape(str(name or ""))
    if not context or not actor:
        return ""
    passive_death_tokens = (
        "已咽气", "咽气", "断气", "死亡", "死去", "身亡", "尸身",
        "被杀", "毙命", "殒命", "丧命", "被刺死", "被砍死", "中刀而亡",
    )
    death_after_actor = any(
        re.search(
            rf"{actor}[^，,。；\n]{{0,36}}{re.escape(token)}",
            context)
        for token in passive_death_tokens)
    source = str(text or "")
    death_before_actor = any(
        re.search(
            rf"{re.escape(token)}[^，,。；\n]{{0,16}}{actor}",
            source)
        for token in (
            "杀死", "击毙", "害死", "刺死", "砍死"))
    if death_after_actor or death_before_actor:
        return "death"
    for kind, tokens in (
            ("wake", WAKE_STATE_TOKENS),
            ("sleep", SLEEP_STATE_TOKENS),
            ("unconscious", UNCONSCIOUS_STATE_TOKENS)):
        if any(
                re.search(
                    rf"{actor}[^，,。；\n]{{0,36}}{re.escape(token)}",
                    context)
                for token in tokens):
            return kind
    return ""


def _condition_change_allowed(
        name, text, previous, declared, actors=None):
    previous = previous if isinstance(previous, dict) else {}
    declared = declared if isinstance(declared, dict) else {}
    if _condition_signature(previous) == _condition_signature(declared):
        return True
    transition = _condition_transition_kind(name, text, actors)
    previous_life = str(previous.get("life_state") or "")
    declared_life = str(declared.get("life_state") or "")
    previous_consciousness = str(
        previous.get("consciousness_state") or "")
    declared_consciousness = str(
        declared.get("consciousness_state") or "")
    if transition == "death":
        return previous_life != "dead" and declared_life == "dead"
    if previous_life == "dead" or declared_life == "dead":
        return False
    if transition == "wake":
        return (
            previous_consciousness in {"asleep", "unconscious"}
            and declared_consciousness == "awake")
    if transition == "sleep":
        return (
            previous_consciousness == "awake"
            and declared_consciousness == "asleep")
    if transition == "unconscious":
        return (
            previous_consciousness == "awake"
            and declared_consciousness == "unconscious")
    return False


def _terminal_character_names(text, end_states, names):
    terminal = set()
    end_states = end_states if isinstance(end_states, dict) else {}
    for name in names or []:
        state = end_states.get(name) or {}
        state_text = json.dumps(state, ensure_ascii=False, default=str)
        condition = (
            state.get("condition")
            if isinstance(state, dict) else {})
        condition = condition if isinstance(condition, dict) else {}
        life_state = str(
            condition.get("life_state")
            or state.get("life_state") or "").strip().lower()
        if life_state:
            if life_state == "dead":
                terminal.add(name)
            continue
        windows = _actor_semantic_windows(text, name, names)
        if (_condition_transition_kind(name, text, names) == "death"
                or any(
                    any(token in window for token in TERMINAL_STATE_TOKENS)
                    for window in windows)):
            terminal.add(name)
        elif any(token in state_text for token in TERMINAL_STATE_TOKENS):
            terminal.add(name)
    return terminal


def _merge_shot_state(name, continuity, explicit, text, *, previous=None,
                      emotion="专注", ending=False, scene_changed=False):
    """Merge pose/prop with transition-aware appearance and condition state."""
    declared = _normalized_explicit_state(explicit)
    inherited = copy.deepcopy(previous or {})
    if declared:
        state = _shot_state(
            name, continuity, text, previous=inherited,
            emotion=emotion, ending=ending)
        state.update(declared)
    else:
        state = _shot_state(
            name, continuity, text, previous=inherited,
            emotion=emotion, ending=ending)

    inherited_condition = (
        _condition_from_state(inherited, default=NORMAL_CONDITION)
        if inherited else {})
    declared_condition = _condition_from_state(declared)
    state_condition = _condition_from_state(
        state, default=NORMAL_CONDITION)
    if inherited_condition:
        if not ending:
            # The first frame is the previous shot's terminal fact. The action
            # in this shot must never be pre-applied to its start state.
            state_condition = copy.deepcopy(inherited_condition)
        elif not declared_condition:
            state_condition = copy.deepcopy(inherited_condition)
        else:
            candidate = _condition_from_state(
                declared, default=inherited_condition)
            state_condition = (
                candidate
                if _condition_change_allowed(
                    name, text, inherited_condition, candidate,
                    actors=[
                        item.get("name") for item in (
                            continuity.get("characters") or [])
                        if isinstance(item, dict) and item.get("name")
                    ])
                else copy.deepcopy(inherited_condition))
    elif declared_condition:
        state_condition = _condition_from_state(
            declared, default=NORMAL_CONDITION)
    state["condition"] = state_condition

    current = _visible_appearance(name, text, declared)
    transition = _appearance_transition(name, text)
    for field in APPEARANCE_STATE_FIELDS:
        prior_raw = copy.deepcopy(inherited.get(field))
        current_raw = copy.deepcopy(current.get(field))
        declared_raw = copy.deepcopy(declared.get(field))
        prior_value = str(prior_raw or "").strip()
        current_value = str(current_raw or "").strip()
        declared_value = str(declared_raw or "").strip()
        if transition and not ending and prior_value and not scene_changed:
            # A visible change starts in the inherited look and finishes in the
            # declared/current look.
            state[field] = prior_raw
        elif declared_value:
            if prior_value and not scene_changed and not transition:
                # A compatible shorter phrase must not erase details already
                # locked by the previous frame (青官袍 must not drop 乌纱);
                # a conflicting phrase without a visible change is likewise
                # rejected and reported by the continuity audit.
                state[field] = prior_raw
            else:
                state[field] = declared_raw
        elif current_value and (
                not prior_value or scene_changed or transition):
            state[field] = current_raw
        elif prior_value:
            # Within one continuous scene, clothes may change only through a
            # declared visible transition. The inherited state is authoritative.
            state[field] = prior_raw
        elif current_value:
            state[field] = current_raw
    return state


def _visible_pose(text, fallback="保持当前可见姿态"):
    """Infer only a camera-visible pose; never invent story action."""
    text = str(text or "")
    rules = (
        (("仰卧", "卧榻", "卧床", "躺下", "躺在", "睡在"),
         "仰卧于明确承托面，身体由床榻/座具稳定支撑"),
        (("伏案", "趴向", "趴在"),
         "坐于桌前并俯身伏案，上身由座椅和桌面自然承托"),
        (("坐起", "坐在", "坐于", "落座"),
         "坐姿，臀部由座具或床榻稳定支撑"),
        (("跪下", "跪地", "跪在"),
         "跪姿，膝部与地面接触并保持重心稳定"),
        (("蹲下", "蹲在", "半蹲"),
         "蹲姿，双脚着地且重心位于支撑面内"),
        (("俯身", "弯腰", "躬身"),
         "俯身站姿，双脚着地并保持重心可达"),
        (("奔跑", "跑向", "冲向", "逃跑"),
         "跑动姿态，落地脚与运动方向一致"),
        (("走向", "走入", "走出", "行走", "迈步", "进入", "离开"),
         "行走姿态，落地脚支撑且朝向运动路径"),
        (("站起", "起身", "站立"),
         "站立，双脚着地且重心稳定"),
    )
    for tokens, pose in rules:
        if any(token in text for token in tokens):
            return pose
    return fallback


def _shot_state(name, continuity, text, *, previous=None,
                emotion="专注", ending=False):
    """Build a concrete state from the current visible action.

    Previous state is inherited only when it already describes a concrete
    pose. Generic legacy placeholders must never override a visible sit/lie/
    kneel action in the current shot.
    """
    visible_pose = _visible_pose(text)
    previous = copy.deepcopy(previous or {})
    generic = {
        "", "站立，重心稳定", "完成本镜主要动作，重心可供下一镜继承",
        "保持当前可见姿态",
    }
    if previous and not ending:
        # A shot starts from the exact prior visible state.  The current
        # shot's action changes only the end state; changing emotion/pose at
        # frame zero would create a discontinuity before the action begins.
        return previous
    if previous and str(previous.get("pose") or "") not in generic \
            and visible_pose == "保持当前可见姿态":
        state = previous
        if ending:
            state["emotion"] = emotion or state.get("emotion")
        return state
    pose = visible_pose
    if ending and pose == "保持当前可见姿态":
        pose = "保持本镜最终可见姿态，重心与接触点可供下一镜继承"
    return _state(name, continuity, emotion=emotion, pose=pose)


def _text_asset(shot, rules=None):
    # 新版分镜可直接提供结构化文字资产卡；它是唯一的可读文字来源，
    # 不再从整段系统提示词或 QC 字段猜测屏幕内容。
    declared = shot.get("readable_text")
    if isinstance(declared, dict) and (
            declared.get("carrier") or declared.get("whitelist")):
        whitelist = sanitize_text_whitelist(declared.get("whitelist") or [])
        carrier = str(declared.get("carrier") or "").strip()
        return {
            "required": bool(carrier and whitelist),
            "carrier": carrier,
            "whitelist": whitelist,
            "layout": str(declared.get("layout") or "").strip(),
            "style": str(declared.get("style") or "").strip(),
            "perspective": str(declared.get("perspective") or "").strip(),
            "priority": str(declared.get("priority") or "must_read").strip(),
            "source": str(declared.get("source") or "storyboard").strip(),
            "locked_by": declared.get("locked_by", ""),
            "keyframe_uri": declared.get("keyframe_uri", ""),
            "rule": ("文字必须由ChatGPT关键帧锁定，Seedance只保持原字"
                     if carrier and whitelist else "无显式文字白名单"),
        }
    blob = " ".join((shot.get("description", ""), shot.get("prompt", "")))
    # “无字幕/禁止文字”等负向约束不代表画面里真的有文字载体。
    # 旧逻辑只要看到“字幕”二字就把它标成 required，随后既拿不到白名单，
    # 又会诱导图片模型凭空生成文字。
    searchable = re.sub(
        r"(?:无|没有|不含|不要|禁止|不得|避免)(?:任何)?"
        r"(?:画面)?(?:字幕|对白字幕|旁白字幕|台词字幕|文字|Logo|水印)",
        "",
        blob,
        flags=re.IGNORECASE,
    )
    carriers = (rules or {}).get("text_assets", {}).get(
        "carriers", list(TEXT_CARRIERS))
    carrier = next((word for word in carriers if word in searchable), "")
    # 仅在存在文字载体时提取书名号/引号内容，避免把口头台词当画面文字。
    texts = re.findall(
        r"[《「『【]([^》」』】]{1,40})[》」』】]",
        searchable,
    ) if carrier else []
    # 同一条要求通常同时出现在 description 和 prompt；白名单必须只来自
    # 当前镜头明确声明的原文，不能从某个历史项目推断并永久追加专有词。
    texts = sanitize_text_whitelist(texts)
    required = bool(carrier)
    return {
        "required": required,
        "carrier": carrier,
        "whitelist": texts,
        "layout": "",
        "style": "",
        "perspective": "",
        "priority": "must_read" if required else "none",
        "source": "legacy_inferred" if required else "none",
        "locked_by": "",
        "keyframe_uri": "",
        "rule": "文字必须由ChatGPT关键帧锁定，Seedance只保持原字" if required else "无可读文字",
    }


def _emotion_band(raw, dialogue_rules):
    hint = " ".join((str(raw.get("emotion", "")),
                     str((raw.get("dialogue") or {}).get("dialogue", "")),
                     str(raw.get("description", ""))))
    if any(word in hint for word in ("颤", "哽咽", "发抖", "恐惧")):
        return "trembling"
    if any(word in hint for word in ("悲", "哭", "温柔", "低声", "伤心")):
        return "sad_gentle"
    if any(word in hint for word in ("怒", "吼", "紧张", "质问", "威胁", "!", "！")):
        return "tense_angry"
    return "daily"


def _round_duration(value, precision):
    precision = float(precision or 0.5)
    units = int(value / precision)
    if units * precision < value - 1e-9:
        units += 1
    return round(max(precision, units * precision), 3)


def _format_timecode(seconds):
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:04.1f}"


def _dialogue_duration(dialogue, rules=None, emotion="daily"):
    if not dialogue:
        preferred = (rules or {}).get("production", {}).get(
            "preferred_segment_seconds", [5, 8])
        return float(preferred[0])
    text = dialogue.get("dialogue", "")
    dialogue_rules = (rules or {}).get("dialogue", {})
    profiles = (dialogue_rules.get("speech_profiles")
                or dialogue_rules.get("speech_rates") or {})
    band = (profiles.get(emotion) or profiles.get("daily")
            or {"chars_per_second": [4, 5], "buffer_seconds": [0.5, 0.8]})
    rate_range = band.get(
        "chars_per_second", [band.get("min", 4), band.get("max", 5)])
    rate = sum(float(x) for x in rate_range) / len(rate_range)
    buffer_range = band.get("buffer_seconds", band.get("buffer", [0.5, 0.8]))
    buffer_seconds = sum(float(x) for x in buffer_range) / len(buffer_range)
    precision = (rules or {}).get("production", {}).get(
        "time_precision_seconds", 0.5)
    raw_duration = max(float(precision), len(text) / rate + buffer_seconds)
    return _round_duration(raw_duration, precision)


def _split_dialogue_text(text, max_chars):
    """优先按自然停顿拆台词；仅在极端长词组时才做硬切。"""
    if len(text) <= max_chars:
        return [text]
    tokens = re.findall(r"[^，。！？!?；;、,]+[，。！？!?；;、,~～]?", text)
    pieces, current = [], ""
    for token in tokens or [text]:
        if current and len(current) + len(token) > max_chars:
            pieces.append(current)
            current = ""
        while len(token) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(token[:max_chars])
            token = token[max_chars:]
        current += token
    if current:
        pieces.append(current)
    return [piece for piece in pieces if piece]


def _split_dialogue_shots(raw_shots, rules):
    dialogue_rules = rules.get("dialogue", {})
    performance_rules = rules.get("performance", {})
    if not dialogue_rules.get("split_at_natural_pause", True):
        return [copy.deepcopy(raw) for raw in raw_shots]
    max_chars = int(dialogue_rules.get("max_chars_per_shot", 25))
    out = []
    for raw in raw_shots:
        dialogue = raw.get("dialogue")
        if not dialogue:
            out.append(copy.deepcopy(raw))
            continue
        working = copy.deepcopy(raw)
        description = str(raw.get("description", ""))
        physical_cues = (
            "转身", "起身", "坐下", "跪", "握拳", "抬手", "扇", "推开",
            "抓住", "摔", "冲向", "拔剑", "挥刀", "拥抱", "后退",
        )
        if (performance_rules.get("physical_action_separate_shot", True)
                and any(cue in description for cue in physical_cues)):
            physical = copy.deepcopy(raw)
            physical["kind"] = "physical"
            physical["dialogue"] = None
            physical["duration"] = max(
                2.0, min(4.0, float(raw.get("duration") or 3.0) / 2))
            physical["prompt"] = f"独立肢体动作镜：{description}"
            out.append(physical)
            speaker = dialogue.get("character", "角色")
            working["description"] = f"{speaker}完成动作后稳定状态，说出台词"
            working["prompt"] = f"{speaker}说话，动作已在上一镜完成"
        source = dialogue.get("dialogue", "")
        parts = _split_dialogue_text(source, max_chars)
        emotion = _emotion_band(working, dialogue_rules)
        source_duration = sum(
            _dialogue_duration({**dialogue, "dialogue": part}, rules, emotion)
            for part in parts)
        for part_index, part in enumerate(parts, 1):
            split = copy.deepcopy(working)
            split["dialogue"]["dialogue"] = part
            split["dialogue_source"] = source
            split["dialogue_part"] = {
                "index": part_index, "total": len(parts),
                "source_duration": source_duration,
            }
            split["duration"] = _dialogue_duration(
                split["dialogue"], rules, emotion)
            split["speech_emotion"] = emotion
            out.append(split)
    return out


def _append_performance_beats(raw_shots, script, rules=None):
    """关键台词后补听者反应镜，每场结尾补有内容的留白镜。"""
    rules = rules or {}
    performance_rules = rules.get("performance", {})
    raw_shots = _split_dialogue_shots(raw_shots, rules)
    add_reaction = performance_rules.get("reaction_after_key_dialogue", True)
    reaction_ratio = float(performance_rules.get(
        "listener_duration_ratio", performance_rules.get(
            "reaction_min_ratio", 2 / 3)))
    reaction_range = performance_rules.get(
        "reaction_seconds", performance_rules.get(
            "reaction_duration_seconds", [2, 4]))
    beat_range = performance_rules.get(
        "beat_seconds", performance_rules.get("beat_duration_seconds", [2, 4]))
    add_beat = performance_rules.get("beat_at_emotional_peak", True)
    scenes = _scene_map(script)
    inner_policy = normalize_inner_persona_policy(
        script, rules.get("inner_persona"))
    out = []
    grouped = {}
    for raw in raw_shots:
        grouped.setdefault(raw.get("scene_no"), []).append(copy.deepcopy(raw))
    for scene_no in sorted(grouped):
        scene = scenes.get(scene_no, {})
        scene_people = list(scene.get("characters", []))
        timeline = "unknown"
        dead_names = set()
        for raw in grouped[scene_no]:
            timeline = shot_timeline_state(raw, timeline)
            physical_people = physical_scene_characters(
                scene_people, timeline, inner_policy)
            out.append(raw)
            terminal_text = " ".join(str(value or "") for value in (
                raw.get("description"), raw.get("action"), raw.get("prompt")))
            terminal_states = (
                raw.get("end_state")
                if isinstance(raw.get("end_state"), dict) else {})
            dead_names.update(_terminal_character_names(
                terminal_text, terminal_states, scene_people))
            dialogue = raw.get("dialogue")
            part = raw.get("dialogue_part") or {"index": 1, "total": 1}
            if (not dialogue or dialogue.get("inner_voice")
                    or not add_reaction
                    or part.get("index") != part.get("total")):
                continue
            speaker = dialogue.get("character")
            listeners = [
                name for name in physical_people
                if name != speaker and name not in dead_names]
            if listeners:
                listener = listeners[0]
                reaction_end = (
                    f"{listener}仍在原站位，视线停在{speaker}，"
                    "眉眼与下颌已形成明确反应，身体由原支撑面稳定承托")
                out.append({
                    "scene_no": scene_no,
                    "kind": "reaction",
                    "description": f"{listeners[0]}消化{speaker}刚说的话，眼神发生变化",
                    "camera": "近景微推",
                    # 听者镜头 ≥ 当前说话镜的 2/3 是硬规则；reaction_seconds
                    # 仅作为常规建议区间，不能反过来截短有效反应。
                    "duration": max(
                        float(reaction_range[0]),
                        float(raw.get("duration", 3)) * reaction_ratio),
                    "characters": [listener],
                    "dialogue": None,
                    "prompt": f"{listeners[0]}听完{speaker}的话后的近景反应",
                    "frame_targets": {
                        "keyframe": {
                            "phase": "end", "state": reaction_end,
                            "fallback": False},
                        "first_frame": {
                            "phase": "start",
                            "state": (
                                f"{listener}保持上一镜结束站位与支撑，"
                                f"刚听完{speaker}的话，表情尚未发生本镜反应"),
                            "fallback": False},
                        "last_frame": {
                            "phase": "end", "state": reaction_end,
                            "fallback": False},
                    },
                    "source_dialogue": dialogue.get("dialogue", ""),
                })
        lead = [
            name for name in physical_scene_characters(
                scene_people, timeline, inner_policy)
            if name not in dead_names
        ][:1]
        if lead and add_beat:
            beat_end = (
                f"{lead[0]}保持本场结束站位与支撑，视线稳定，"
                "情绪停在本场余波最清晰的单一表情")
            out.append({
                "scene_no": scene_no,
                "kind": "beat",
                "description": f"{lead[0]}用呼吸、眼神和细微肢体完成本场情绪余波",
                "camera": "特写定镜",
                "duration": sum(float(x) for x in beat_range) / len(beat_range),
                "characters": lead,
                "dialogue": None,
                "prompt": f"{lead[0]}无台词留白表演，具体微表情与呼吸变化",
                "frame_targets": {
                    "keyframe": {
                        "phase": "end", "state": beat_end,
                        "fallback": False},
                    "first_frame": {
                        "phase": "start",
                        "state": (
                            f"{lead[0]}保持本场最后一个动作完成时的站位、"
                            "支撑和朝向，情绪尚未收束"),
                        "fallback": False},
                    "last_frame": {
                        "phase": "end", "state": beat_end,
                        "fallback": False},
                },
            })
    return out


def _normalize_ai_shot(raw):
    """AI 分镜的宽松产出 → 统一结构。

    真实编剧模型偶尔会把 dialogue 写成字符串、characters 写成单个名字、
    camera 写成对象、duration 写成带单位字符串;逐项纠正,
    避免 'str' object has no attribute 'get' 之类的崩溃。"""
    if isinstance(raw, str):
        raw = {"description": raw}
    elif not isinstance(raw, dict):
        return None
    shot = dict(raw)
    dialogue = shot.get("dialogue")
    if isinstance(dialogue, str):
        text = dialogue.strip()
        shot["dialogue"] = ({"character": "", "dialogue": text}
                            if text else None)
    elif isinstance(dialogue, dict):
        text = str(dialogue.get("dialogue") or "").strip()
        shot["dialogue"] = ({"character": str(dialogue.get("character")
                                              or ""),
                             "dialogue": text} if text else None)
    else:
        shot["dialogue"] = None
    characters = shot.get("characters")
    if isinstance(characters, str):
        characters = [characters]
    shot["characters"] = [str(c) for c in (characters or []) if c]
    shot["functional_figures"] = _normalize_functional_figures(
        shot.get("functional_figures"))
    overlays = shot.get("narrative_overlays")
    if isinstance(overlays, dict):
        overlays = [overlays]
    shot["narrative_overlays"] = [
        dict(item) for item in (overlays or []) if isinstance(item, dict)]
    if shot["dialogue"] and not shot["dialogue"]["character"]:
        shot["dialogue"]["character"] = (shot["characters"][0]
                                         if shot["characters"] else "")
    if not isinstance(shot.get("camera"), str):
        shot["camera"] = str(shot.get("camera") or "")
    if not isinstance(shot.get("description"), str):
        shot["description"] = str(shot.get("description") or "")
    if not isinstance(shot.get("prompt"), str):
        shot["prompt"] = str(shot.get("prompt") or "")
    try:
        shot["scene_no"] = int(shot.get("scene_no"))
    except (TypeError, ValueError):
        shot["scene_no"] = None
    if shot.get("duration") is not None:
        try:
            shot["duration"] = float(shot["duration"])
        except (TypeError, ValueError):
            shot["duration"] = None
    return shot


def enrich_storyboard(script, storyboard, continuity, profile, style=""):
    """把 Provider 的轻量分镜升级为五维生产分镜。"""
    rules = profile.get("rules", {})
    production_analysis = (
        script.get("production_analysis")
        if isinstance(script.get("production_analysis"), dict) else {})
    prompt_bible = (
        production_analysis.get("prompt_bible")
        if isinstance(production_analysis.get("prompt_bible"), dict) else {})
    visual_bible = (
        production_analysis.get("visual")
        if isinstance(production_analysis.get("visual"), dict) else {})
    seedance_master = str(
        prompt_bible.get("seedance_prefix") or "").strip()
    scenes = _scene_map(script)
    normalized = []
    fallback_scene = next(
        (s.get("scene_no") for s in script.get("scenes", [])), 1)
    last_scene = None
    for raw in (storyboard or {}).get("shots", []):
        shot = _normalize_ai_shot(raw)
        if shot is None:
            continue
        if shot.get("scene_no") is None:
            # AI 忘写场次 → 继承上一镜(开头则归入第一场)
            shot["scene_no"] = (last_scene if last_scene is not None
                                else fallback_scene)
        last_scene = shot["scene_no"]
        normalized.append(shot)
    normalized, inner_policy = apply_inner_persona_to_shots(
        script, normalized, rules.get("inner_persona"))
    raw_shots = _append_performance_beats(normalized, script, rules)
    raw_shots, inner_policy = apply_inner_persona_to_shots(
        script, raw_shots, rules.get("inner_persona"))
    character_number_map = build_character_number_map(
        continuity, {"shots": raw_shots})
    character_by_name = {
        character["name"]: character
        for character in character_number_map.values()
    }
    previous = {}
    shots = []
    elapsed = 0.0
    prev_camera_scale = None
    prev_scene_no = None
    for index, raw in enumerate(raw_shots, 1):
        scene = scenes.get(raw.get("scene_no"), {})
        scene_changed = (
            prev_scene_no is None or raw.get("scene_no") != prev_scene_no)
        previous_before_shot = copy.deepcopy(previous)
        kind = raw.get("kind") or ("dialogue" if raw.get("dialogue") else "environment")
        characters = list(dict.fromkeys(raw.get("characters", [])))
        functional_figures = _normalize_functional_figures(
            raw.get("functional_figures"))
        functional_count = _functional_figure_count(functional_figures)
        narrative_overlays = [
            copy.deepcopy(item)
            for item in (raw.get("narrative_overlays") or [])
            if isinstance(item, dict)
        ][:1]
        if not characters:
            # 环境/空镜必须允许 0 人。旧逻辑会从场次人物表里擅自塞进第一名
            # 角色，导致提示词、人数质检和最终画面一起多出人。只有对白或
            # 明确的人物表演镜缺名单时才做确定性补全。
            speaker = str((raw.get("dialogue") or {}).get(
                "character") or "").strip()
            if speaker:
                characters = [speaker]
            elif kind in ("reaction", "beat", "dialogue", "physical"):
                characters = physical_scene_characters(
                    scene.get("characters", []),
                    raw.get("timeline_state", "unknown"),
                    inner_policy)[:1]
        visible_figure_count = len(characters) + functional_count
        action_text = "；".join(str(value or "") for value in (
            raw.get("description"), raw.get("physical_logic"),
            raw.get("prompt")))
        declared_start = raw.get("start_state")
        declared_start = (
            declared_start if isinstance(declared_start, dict) else {})
        semantic_corrections = []
        start_state = {}
        for name in characters:
            explicit_state, corrections = _reconcile_explicit_appearance(
                name, action_text, declared_start.get(name))
            semantic_corrections.extend(corrections)
            start_state[name] = _merge_shot_state(
                name, continuity, explicit_state, action_text,
                previous=previous.get(name), emotion="专注",
                scene_changed=scene_changed)
        emotion = "消化信息" if kind == "reaction" else (
            "情绪余波" if kind == "beat" else "推进事件")
        declared_end = raw.get("end_state")
        declared_end = (
            declared_end if isinstance(declared_end, dict) else {})
        end_state = {}
        for name in characters:
            explicit_state, corrections = _reconcile_explicit_appearance(
                name, action_text, declared_end.get(name))
            semantic_corrections.extend(corrections)
            if not explicit_state and kind in ("reaction", "beat"):
                explicit_state = _state(
                    name, continuity, emotion=emotion,
                    pose="保持原位，完成眼神与呼吸变化")
            end_state[name] = _merge_shot_state(
                name, continuity, explicit_state, action_text,
                previous=start_state.get(name), emotion=emotion,
                ending=True, scene_changed=scene_changed)
        appearance_continuity_issues = []
        if not scene_changed:
            for name in characters:
                inherited = previous_before_shot.get(name) or {}
                current = _visible_appearance(
                    name, action_text,
                    (declared_start.get(name)
                     if isinstance(declared_start.get(name), dict) else None))
                if (_appearance_conflicts(
                        inherited.get("wardrobe"),
                        current.get("wardrobe"))
                        and not _appearance_transition(name, action_text)):
                    appearance_continuity_issues.append(
                        f"{name}本镜声明服装与上一镜不一致，且没有换装动作；"
                        "已继续继承上一镜服装")
        previous.update(copy.deepcopy(end_state))
        camera = _camera_plan(
            raw.get("camera", ""), kind, index, rules,
            prev_scale=prev_camera_scale,
            scene_start=(raw.get("scene_no") != prev_scene_no
                         and kind == "environment"))
        prev_camera_scale = camera["shot_scale"]
        prev_scene_no = raw.get("scene_no")
        text_asset = _text_asset(raw, rules)
        dialogue = raw.get("dialogue")
        speech_emotion = raw.get("speech_emotion") or _emotion_band(
            raw, rules.get("dialogue", {}))
        duration = float(raw.get("duration") or _dialogue_duration(
            dialogue, rules, speech_emotion))
        if kind == "dialogue":
            duration = max(duration, _dialogue_duration(
                dialogue, rules, speech_emotion))
        precision = float(profile.get("time_precision_seconds", 0.5))
        duration = min(
            profile["max_segment_seconds"], _round_duration(duration, precision))
        start_time = elapsed
        elapsed = round(elapsed + duration, 3)
        timecode = f"{_format_timecode(start_time)}-{_format_timecode(elapsed)}"
        script_reference = (raw.get("dialogue_source")
                            or (dialogue or {}).get("dialogue")
                            or raw.get("description")
                            or scene.get("action", ""))
        people = "、".join(characters) or "无人"
        shot_character_map = {
            character_by_name[name]["actor_id"]: copy.deepcopy(
                character_by_name[name])
            for name in characters if name in character_by_name
        }
        numbered_people = "、".join(
            f"{item['actor_id']}（{item['role']}·{item['name']}）"
            for item in shot_character_map.values()) or "无人"
        end_summary = "；".join(
            f"{name}{state['pose']}" for name, state in end_state.items())
        text_rule = ("保持首帧中文字完全一致，不新增文字" if text_asset["required"]
                     else "不生成字幕条或任何画面文字")
        station = "；".join(
            f"{name}{state['position']}" for name, state in start_state.items())
        gaze_parts = []
        micro_expression_parts = []
        for actor_name in characters:
            actor_state_text = json.dumps(
                {
                    "start": start_state.get(actor_name) or {},
                    "end": end_state.get(actor_name) or {},
                },
                ensure_ascii=False, default=str)
            if any(token in actor_state_text for token in (
                    '"life_state": "dead"', "已咽气", "断气",
                    "死亡", "尸身态")):
                actor_gaze = "视线固定，不发生追视"
                actor_micro = "身体与面部肌肉保持完全静止"
            elif any(token in actor_state_text for token in (
                    '"consciousness_state": "asleep"',
                    '"consciousness_state": "unconscious"',
                    "熟睡", "昏迷", "昏厥")):
                actor_gaze = "双眼保持闭合，不发生追视"
                actor_micro = "身体保持被动稳定"
            else:
                actor_gaze = "凝视/瞥向对手或核心物件"
                actor_micro = "眉眼变化·下颌张力·呼吸节奏"
            gaze_parts.append(f"{actor_name}:{actor_gaze}")
            micro_expression_parts.append(
                f"{actor_name}:{actor_micro}")
        gaze = "；".join(gaze_parts) or "无人，不发生人物视线行为"
        micro_expression = (
            "；".join(micro_expression_parts)
            or "无人，不发生人物表演")
        if narrative_overlays:
            micro_expression += (
                "；内心Q版以夸张眉眼、嘴形、手势和身体弹性强化吐槽/冲突，"
                "真人宿主保持闭口，不替内心声音做口型")
        performance_goal = raw.get("description") or "完成本镜叙事任务"
        seedance_prompt = (
            (f"【制作圣经】{seedance_master}。" if seedance_master else "") +
            "【输入】首帧是唯一动作起点，尾帧是唯一动作终点。"
            "【人物编号映射（仅用于提示词引用，不生成画面文字）】"
            f"{numbered_people}。"
            f"【真人总量】登记角色{len(characters)}人（{people}）；"
            f"功能人物{functional_count}人"
            f"（{_functional_figure_line(functional_figures)}）；"
            f"成片实际可见真人严格共{visible_figure_count}人，不得新增、"
            "漏画、复制、合并或换人；功能人物没有独立身份资产，只按"
            "本镜精确数量、状态和剧情功能呈现。"
            f"【起点】{station or '无人空镜，保持场景初始状态'}。"
            f"【单一主动作】{raw.get('description', '') or '环境保持自然变化'}。"
            f"【表演】{gaze}；{micro_expression}，动作连贯自然。"
            f"【运镜】只执行一次{camera['movement']}，"
            f"{camera['shot_scale']}·{camera['angle']}，"
            f"动机是{camera['movement_motivation']}。"
            f"【终点】{end_summary or '保持空镜构图稳定'}。"
            f"【文字】{text_rule}。"
            "【禁止】最终画面不得出现P01等人物编号、姓名标签、坐标、箭头、"
            "空间调度图符号、字幕、Logo或水印。"
        )
        if narrative_overlays:
            overlay = narrative_overlays[0]
            seedance_prompt += (
                "【非现实内心Q版叠层】"
                f"{overlay.get('name')}是{overlay.get('host_character')}的"
                "内心人格，不是真实人物，不计入上述真实人数，不参与物理站位、"
                "遮挡或空间调度；只有宿主内心感知，其他人物不得看见、回应、"
                "触碰或与其对视。继承锁定的当前服装，不继承任何默认道具；"
                "比例严格保持大头小身：约1.8头身，头占总高约58%，"
                "身体、肩宽、躯干、四肢、手脚都明显小于头部；"
                f"以夸张Q版表情和动作表现：{overlay.get('expression')}；"
                f"{overlay.get('action')}。内心声音出现时宿主闭口，禁止旁白字幕。"
            )
        environment_sound = (
            f"{scene.get('location', '场景')}环境底噪·空间空气声·动作触发声")
        visual_hook = "主体视线或动作方向承接下一镜"
        sound_design = {
            "environment": environment_sound,
            "effects": "动作发生时同步真实拟音",
            "music": "无BGM" if rules.get("delivery", {}).get(
                "no_bgm", True) else "按项目音乐策略",
        }
        shot_contract = {
            "时间码": timecode,
            "景别": camera["shot_scale"],
            "角度": camera["angle"],
            "焦段": camera["lens"],
            "机位": camera["camera_position"],
            "运镜": camera["movement"],
            "构图": camera["composition"],
            "拍摄速度": camera["speed"],
            "站位": station or "画面中",
            "视线": gaze,
            "画面内容描述": raw.get("description", ""),
            "台词": (dialogue or {}).get("dialogue", ""),
            "表演重点": performance_goal,
            "微表情": micro_expression,
            "音效": environment_sound,
            "视觉钩子": visual_hook,
            "镜头功能": SHOT_FUNCTIONS.get(kind, "信息交代"),
        }
        physical_contract = build_physical_contract({
            **raw,
            "description": raw.get("description", ""),
            "action": raw.get("description", ""),
            "shot_contract": shot_contract,
            "readable_text": text_asset,
        })
        frame_targets = copy.deepcopy(raw.get("frame_targets") or {})
        keyframe_target = copy.deepcopy(
            frame_targets.get("keyframe") or {})
        shot = {
            **raw,
            "shot_no": index,
            "unit_id": f"U{index:02d}",
            "pipeline_version": PIPELINE_VERSION,
            "prop_registry": copy.deepcopy(
                script.get("prop_registry")
                or script.get("core_props") or []),
            "kind": kind,
            "duration": duration,
            "timecode": timecode,
            "characters": characters,
            "character_count": len(characters),
            "functional_figures": functional_figures,
            "narrative_overlays": narrative_overlays,
            "inner_persona_policy": copy.deepcopy(inner_policy),
            "visible_figure_count": visible_figure_count,
            "character_number_map": shot_character_map,
            "character_number_ids": list(shot_character_map),
            "type_word": _type_word(scene, raw),
            "shot_function": SHOT_FUNCTIONS.get(kind, "信息交代"),
            "start_state": start_state,
            "end_state": end_state,
            # v2.2 只传递编剧/分镜上游已经登记的唯一静态目标；这里不从
            # start/end 或动作文本临时推导，缺失时由合同硬门禁阻断。
            "frame_targets": frame_targets,
            "frame_target": keyframe_target,
            "appearance_state_required": True,
            "appearance_continuity_issues": appearance_continuity_issues,
            "semantic_corrections": semantic_corrections,
            "era_context": "；".join(str(value or "").strip() for value in (
                (script.get("story_world") or {}).get("name"),
                (script.get("story_world") or {}).get("era_and_location"),
                (script.get("story_world") or {}).get("hard_rules"),
            ) if str(value or "").strip()),
            "sanctioned_anachronisms": list(
                (script.get("story_world") or {}).get(
                    "sanctioned_anachronisms") or []),
            "script_reference": script_reference,
            "readable_text": text_asset,
            "visual_hook": visual_hook,
            "performance": {
                "goal": performance_goal,
                "gaze": gaze,
                "micro_expression": micro_expression,
                "beat": kind if kind in ("reaction", "beat") else "acting",
            },
            "speech_timing": ({
                "emotion": speech_emotion,
                "source_dialogue": raw.get("dialogue_source")
                    or (dialogue or {}).get("dialogue", ""),
                "part": copy.deepcopy(raw.get("dialogue_part", {})),
            } if dialogue else None),
            "sound_design": sound_design,
            "shot_contract": shot_contract,
            "physical_contract": physical_contract,
            "five_dimensions": {
                "subject_motion": raw.get("description") or "主体保持明确动势",
                "environment_light": f"{scene.get('location', '')}参与叙事，主光方向稳定",
                "camera_design": camera,
                "time_state": {
                    "start": "继承上一镜结尾状态",
                    "evolution": f"{raw.get('description', '')} → {emotion}",
                    "end": end_summary,
                },
                "aesthetics": {
                    "style": (visual_bible.get("user_style_constraint")
                              or style or "项目既定美术风格"),
                    "render": (visual_bible.get("texture_and_render")
                               or "高反差、暗部保留层次、轻微胶片颗粒"),
                    "palette": visual_bible.get("palette") or [],
                    "lighting": visual_bible.get("lighting") or "",
                    "purpose": "服务事件可读性与角色情绪",
                },
            },
            "seedance_prompt": seedance_prompt,
            "transition": "硬切",
        }
        contract, compact_prompt = compile_shot_prompt(
            shot, location=scene.get("location", ""),
            style=(visual_bible.get("user_style_constraint")
                   or style or "项目既定美术风格"), mode="video")
        shot["prompt_contract"] = contract
        shot["seedance_prompt_compact"] = compact_prompt
        shots.append(shot)
    return {
        "episode_title": storyboard.get("episode_title", script.get("episode_title", "")),
        "pipeline_version": PIPELINE_VERSION,
        "prop_contract_schema": "aifos.prop-contract/v2.2",
        "prop_registry": copy.deepcopy(
            script.get("prop_registry")
            or script.get("core_props") or []),
        "appearance_state_version": APPEARANCE_STATE_VERSION,
        "profile": copy.deepcopy(profile),
        "standard_fingerprint": profile.get("standard_fingerprint", ""),
        "total_duration": elapsed,
        "character_number_map": character_number_map,
        "character_ids_by_name": {
            character["name"]: actor_id
            for actor_id, character in character_number_map.items()
        },
        "inner_persona_policy": copy.deepcopy(inner_policy),
        "shots": shots,
    }


def repair_storyboard_appearance_continuity(storyboard, continuity,
                                             script=None):
    """Upgrade a saved storyboard without re-running or expanding its shots.

    Structured appearance state and derived prompts are reconciled.  A legacy
    auto-added reaction/beat that asks a dead actor to breathe is retargeted to
    a living scene partner (or to environment-only motion when none exists).
    Existing shot numbers, dialogue, timing, camera and assets remain
    untouched, so a reused episode can be repaired without regenerating art.
    """
    repaired = copy.deepcopy(storyboard or {})
    if repaired.get("appearance_state_version") == APPEARANCE_STATE_VERSION:
        return repaired
    continuity = copy.deepcopy(continuity or {})
    script_characters = {
        item.get("name"): item
        for item in (script or {}).get("characters", [])
        if isinstance(item, dict) and item.get("name")
    }
    for character in continuity.get("characters", []):
        if character.get("default_wardrobe"):
            continue
        source = script_characters.get(character.get("name")) or {}
        character["default_wardrobe"] = str(
            next((
                item.get("costume") for item in (
                    source.get("visual_variants") or [])
                if isinstance(item, dict) and item.get("costume")
            ), None)
            or source.get("costume_direction")
            or "")
    previous = {}
    previous_scene = None
    scene_people = {
        scene.get("scene_no"): list(scene.get("characters") or [])
        for scene in (script or {}).get("scenes", [])
        if isinstance(scene, dict)
    }
    dead_by_scene = {}
    character_catalog = {
        str(item.get("name")): copy.deepcopy(item)
        for item in repaired.get("character_number_map", {}).values()
        if isinstance(item, dict) and item.get("name")
    }
    for shot_index, original_shot in enumerate(
            repaired.get("shots", [])):
        shot = reconcile_shot_semantics(original_shot)
        repaired["shots"][shot_index] = shot
        characters = list(dict.fromkeys(shot.get("characters") or []))
        scene_no = shot.get("scene_no")
        dead_names = dead_by_scene.setdefault(scene_no, set())
        living_performance = any(
            token in " ".join(str(value or "") for value in (
                shot.get("description"), shot.get("prompt"),
                (shot.get("performance") or {}).get("micro_expression")
                if isinstance(shot.get("performance"), dict) else ""))
            for token in (
                "呼吸", "喘息", "眼神", "视线", "微表情", "眨眼"))
        retargeted_legacy_beat = False
        if (shot.get("kind") in ("reaction", "beat")
                and living_performance and characters
                and all(name in dead_names for name in characters)):
            replacement = next((
                name for name in scene_people.get(scene_no, [])
                if name not in dead_names), "")
            old_description = str(shot.get("description") or "")
            if replacement:
                old_names = list(characters)
                characters = [replacement]
                shot["characters"] = characters
                shot["character_count"] = 1
                shot["visible_figure_count"] = (
                    1 + _functional_figure_count(
                        shot.get("functional_figures")))
                actor = character_catalog.get(replacement)
                shot["character_number_map"] = ({
                    actor["actor_id"]: copy.deepcopy(actor)
                } if actor and actor.get("actor_id") else {})
                shot["character_number_ids"] = list(
                    shot["character_number_map"])
                label = (
                    "听者反应" if shot.get("kind") == "reaction"
                    else "本场情绪余波")
                shot["description"] = (
                    f"{replacement}承接{label}，保持原位，以呼吸停顿、"
                    "眼神和细微肢体表现冲击")
                shot["prompt"] = (
                    f"{replacement}无台词近景，承接上一镜情绪，"
                    "只表现一次呼吸停顿与眼神变化")
                performance = dict(shot.get("performance") or {})
                performance.update({
                    "goal": shot["description"],
                    "gaze": f"{replacement}视线承接上一镜事件",
                    "micro_expression": (
                        f"{replacement}呼吸停顿、眼神凝住和极小幅度重心变化"),
                })
                shot["performance"] = performance
                shot.setdefault("semantic_corrections", []).append({
                    "field": "characters/description",
                    "from": f"{'、'.join(old_names)}：{old_description}",
                    "to": f"{replacement}：{shot['description']}",
                    "reason": (
                        "自动补出的反应/留白镜不得让已死亡人物继续呼吸"
                        "或表演微表情，改由仍在场的存活人物承接"),
                })
            else:
                names = "、".join(characters)
                shot["description"] = (
                    f"{names}尸身保持绝对静止；只由灯焰、帷帐或环境"
                    "阴影完成本场余波")
                shot["prompt"] = (
                    f"{names}尸身绝对静止，无呼吸、无眨眼、无微表情；"
                    "只允许环境光影微动")
                performance = dict(shot.get("performance") or {})
                performance.update({
                    "goal": shot["description"],
                    "gaze": "尸身无视线反应",
                    "micro_expression": (
                        "尸身绝对静止；禁止呼吸、眨眼、眼神或微表情变化"),
                })
                shot["performance"] = performance
                shot.setdefault("semantic_corrections", []).append({
                    "field": "description/performance",
                    "from": old_description,
                    "to": shot["description"],
                    "reason": "场内无存活人物，改用环境变化承接情绪余波",
                })
            shot_contract = dict(shot.get("shot_contract") or {})
            shot_contract.update({
                "画面内容描述": shot["description"],
                "表演重点": shot["description"],
                "微表情": shot["performance"]["micro_expression"],
            })
            shot["shot_contract"] = shot_contract
            retargeted_legacy_beat = True
        scene_changed = previous_scene is None or scene_no != previous_scene
        before = copy.deepcopy(previous)
        action_text = "；".join(str(value or "") for value in (
            shot.get("description"), shot.get("action"), shot.get("prompt")))
        declared_start = (
            shot.get("start_state")
            if isinstance(shot.get("start_state"), dict) else {})
        declared_end = (
            shot.get("end_state")
            if isinstance(shot.get("end_state"), dict) else {})
        start_state, end_state = {}, {}
        semantic_corrections = list(
            shot.get("semantic_corrections") or [])
        for name in characters:
            start_explicit, start_corrections = \
                _reconcile_explicit_appearance(
                    name, action_text, declared_start.get(name))
            end_explicit, end_corrections = \
                _reconcile_explicit_appearance(
                    name, action_text, declared_end.get(name))
            semantic_corrections.extend(start_corrections)
            semantic_corrections.extend(end_corrections)
            start_state[name] = _merge_shot_state(
                name, continuity, start_explicit, action_text,
                previous=before.get(name), emotion="专注",
                scene_changed=scene_changed)
            end_state[name] = _merge_shot_state(
                name, continuity, end_explicit, action_text,
                previous=start_state[name], emotion=(
                    (declared_end.get(name) or {}).get("emotion")
                    if isinstance(declared_end.get(name), dict)
                    else "推进事件"),
                ending=True, scene_changed=scene_changed)
        issues = []
        if not scene_changed:
            for name in characters:
                current = _visible_appearance(
                    name, action_text, declared_start.get(name))
                action_current = _visible_appearance(name, action_text)
                if (_appearance_conflicts(
                        (before.get(name) or {}).get("wardrobe"),
                        current.get("wardrobe"))
                        and not _appearance_transition(name, action_text)):
                    # A direct actor-local wardrobe in the shot action is a
                    # genuine unresolved story conflict.  A conflicting value
                    # found only in legacy structured state is a known global
                    # design leak; inherit and audit the correction without
                    # blocking production.
                    if action_current.get("wardrobe"):
                        issues.append(
                            f"{name}本镜声明服装与上一镜不一致，且没有换装动作；"
                            "已继续继承上一镜服装")
                    semantic_corrections.append({
                        "character": str(name),
                        "field": "start_state/end_state.wardrobe",
                        "from": str(current.get("wardrobe") or ""),
                        "to": str(
                            (before.get(name) or {}).get("wardrobe") or ""),
                        "reason": "同场连续镜头没有换装动作，继承上一镜服装",
                    })
        shot["start_state"] = start_state
        shot["end_state"] = end_state
        shot["appearance_state_required"] = True
        shot["appearance_continuity_issues"] = issues
        shot["semantic_corrections"] = list({
            json.dumps(item, ensure_ascii=False, sort_keys=True): item
            for item in semantic_corrections if isinstance(item, dict)
        }.values())
        previous.update(copy.deepcopy(end_state))
        dead_names.update(_terminal_character_names(
            action_text, end_state,
            scene_people.get(scene_no, characters)))
        previous_scene = scene_no
        style = str(
            ((shot.get("prompt_contract") or {}).get("style")
             if isinstance(shot.get("prompt_contract"), dict) else "")
            or "")
        contract, compact = compile_shot_prompt(
            shot, location=str(
                shot.get("location")
                or ((shot.get("prompt_contract") or {}).get("scene")
                    if isinstance(shot.get("prompt_contract"), dict) else "")
                or ""),
            style=style, mode="video")
        shot["prompt_contract"] = contract
        shot["seedance_prompt_compact"] = compact
        if retargeted_legacy_beat:
            shot["seedance_prompt"] = compact
    repaired["appearance_state_version"] = APPEARANCE_STATE_VERSION
    return repaired


def lock_text_assets(storyboard, image_uris, provider_name):
    """关键帧落盘后锁定所有可读文字资产。"""
    locked = copy.deepcopy(storyboard)
    manifest = []
    for shot in locked.get("shots", []):
        asset = shot.get("readable_text") or {}
        if not asset.get("required"):
            continue
        uri = image_uris.get(shot["shot_no"], "")
        if uri:
            asset["locked_by"] = provider_name
            asset["keyframe_uri"] = uri
        manifest.append({
            "unit_id": shot.get("unit_id"),
            "shot_no": shot["shot_no"],
            "carrier": asset.get("carrier", ""),
            "whitelist": asset.get("whitelist", []),
            "layout": asset.get("layout", ""),
            "style": asset.get("style", ""),
            "perspective": asset.get("perspective", ""),
            "priority": asset.get("priority", "must_read"),
            "locked_by": asset.get("locked_by", ""),
            "keyframe_uri": asset.get("keyframe_uri", ""),
        })
    return locked, {
        "pipeline_version": PIPELINE_VERSION,
        "assets": manifest,
        "passed": all(item["locked_by"] and item["keyframe_uri"] for item in manifest),
        "note": "无文字镜头自动通过" if not manifest else "逐字白名单已绑定关键帧",
    }


def _gate(gate_id, label, passed, detail):
    return {"id": gate_id, "label": label, "passed": bool(passed), "detail": detail}


def build_preflight(script, storyboard, continuity, text_manifest, frames,
                    profile, blocking=None, quality_policy=None,
                    character_assets=None):
    shots = storyboard.get("shots", [])
    rules = profile.get("rules", {})
    production_rules = rules.get("production", {})
    dialogue_rules = rules.get("dialogue", {})
    performance_rules = rules.get("performance", {})
    storyboard_rules = rules.get("storyboard", {})
    gate_config = {
        item.get("id"): item for item in rules.get("quality_gates", [])
        if isinstance(item, dict) and item.get("id")
    }
    frame_map = {f["shot_no"]: f for f in frames}
    formal_frame_quality_ok = all(
        str(frame.get("image_quality", "medium")).lower()
        in ("medium", "high") for frame in frames)
    frame_visual_qc_ok = all(
        frame.get("qc_passed") is True for frame in frames)
    required = (
        "unit_id", "character_count", "start_state", "end_state",
        "shot_function", "script_reference", "readable_text", "visual_hook",
        "performance", "five_dimensions", "seedance_prompt", "shot_contract",
        "sound_design", "timecode",
    )
    precision = float(profile.get("time_precision_seconds", 0.5))
    video_quality = resolve_video_quality(
        quality_policy or default_quality_policy())
    config_ok = (
        profile["video_model"] == "seedance2.0fast_vip"
        and profile["resolution"].lower() == "720p"
        and profile["voice"] == "jimeng_builtin"
        and profile["lip_sync"]
        and not profile["burn_subtitles"]
    )
    script_dialogue = [
        (scene.get("scene_no"), line.get("character"), line.get("dialogue", ""))
        for scene in script.get("scenes", []) for line in scene.get("lines", [])
    ]
    storyboard_dialogue = []
    dialogue_lengths_ok = True
    max_chars = int(dialogue_rules.get("max_chars_per_shot", 25))
    for shot in shots:
        dialogue = shot.get("dialogue")
        if not dialogue:
            continue
        part = shot.get("dialogue_part") or {"index": 1, "total": 1}
        if part.get("index", 1) == 1:
            storyboard_dialogue.append((
                shot.get("scene_no"), dialogue.get("character"),
                shot.get("dialogue_source") or dialogue.get("dialogue", "")))
        dialogue_lengths_ok = dialogue_lengths_ok and (
            len(dialogue.get("dialogue", "")) <= max_chars)

    reaction_ok = True
    inner_policy = normalize_inner_persona_policy(
        script, rules.get("inner_persona"))
    reaction_ratio = float(performance_rules.get(
        "listener_duration_ratio", performance_rules.get(
            "reaction_min_ratio", 2 / 3)))
    if performance_rules.get("reaction_after_key_dialogue", True):
        scene_people = {
            scene.get("scene_no"): list(scene.get("characters", []))
            for scene in script.get("scenes", [])
        }
        for index, shot in enumerate(shots):
            dialogue = shot.get("dialogue")
            part = shot.get("dialogue_part") or {"index": 1, "total": 1}
            physical_people = physical_scene_characters(
                scene_people.get(shot.get("scene_no"), []),
                shot.get("timeline_state", "unknown"), inner_policy)
            if (not dialogue or dialogue.get("inner_voice")
                    or part.get("index") != part.get("total")
                    or len(physical_people) < 2):
                continue
            following = shots[index + 1] if index + 1 < len(shots) else {}
            reaction_ok = reaction_ok and (
                following.get("kind") == "reaction"
                and float(following.get("duration", 0)) + 1e-9
                >= float(shot.get("duration", 0)) * reaction_ratio)

    beat_ok = True
    if performance_rules.get("beat_at_emotional_peak", True):
        beat_range = performance_rules.get(
            "beat_seconds", performance_rules.get(
                "beat_duration_seconds", [2, 4]))
        for scene in script.get("scenes", []):
            beats = [shot for shot in shots
                     if shot.get("scene_no") == scene.get("scene_no")
                     and shot.get("kind") == "beat"]
            beat_ok = beat_ok and bool(beats) and all(
                float(beat_range[0]) <= float(shot.get("duration", 0))
                <= float(beat_range[1]) for shot in beats)

    required_columns = storyboard_rules.get("required_columns", [
        "时间码", "景别", "角度", "焦段", "机位", "运镜", "构图", "拍摄速度",
        "站位", "视线", "画面内容描述", "台词", "表演重点", "微表情", "音效",
        "视觉钩子", "镜头功能",
    ])
    contract_ok = bool(shots) and all(
        all(column in (shot.get("shot_contract") or {})
            for column in required_columns) for shot in shots)
    sound_required = (storyboard_rules.get("environment_sound_required", True)
                      or rules.get("delivery", {}).get(
                          "environment_sound_required", True))
    sound_ok = bool(shots) and (not sound_required or all(
        bool((shot.get("sound_design") or {}).get("environment"))
        for shot in shots))

    min_angles = int(storyboard_rules.get(
        "minimum_vertical_angles_per_segment", storyboard_rules.get(
            "min_vertical_angles_per_segment", 2)))
    camera_ok = True
    scale_library = rules.get("camera_library", {}).get("shot_scales", [])
    jump_levels = int(storyboard_rules.get(
        "adjacent_shot_scale_jump_levels", 2))
    axis_change = float(storyboard_rules.get(
        "adjacent_camera_axis_change_degrees", 30))
    for scene in script.get("scenes", []):
        scene_shots = [shot for shot in shots
                       if shot.get("scene_no") == scene.get("scene_no")]
        angles = {
            ((shot.get("five_dimensions") or {}).get("camera_design") or {}).get(
                "angle") for shot in scene_shots
        }
        angles.discard(None)
        camera_ok = camera_ok and len(angles) >= min(
            min_angles, len(scene_shots))
        for previous_shot, current_shot in zip(scene_shots, scene_shots[1:]):
            previous_camera = ((previous_shot.get("five_dimensions") or {}).get(
                "camera_design") or {})
            current_camera = ((current_shot.get("five_dimensions") or {}).get(
                "camera_design") or {})
            try:
                scale_jump = abs(
                    scale_library.index(previous_camera.get("shot_scale"))
                    - scale_library.index(current_camera.get("shot_scale")))
            except ValueError:
                scale_jump = 0
            offset_jump = abs(
                float(previous_camera.get("axis_offset_degrees", 0))
                - float(current_camera.get("axis_offset_degrees", 0)))
            if storyboard_rules.get("forbid_repeated_scale_and_angle", True):
                camera_ok = camera_ok and (
                    scale_jump >= jump_levels or offset_jump >= axis_change)

    cast = {character.get("name") for character in continuity.get("characters", [])}
    people_ok = all(
        shot.get("character_count") == len(shot.get("characters", []))
        and set(shot.get("characters", [])) <= cast
        and all(
            overlay.get("physical_presence") is False
            and overlay.get("counts_as_real_character") is False
            and overlay.get("included_in_spatial_blocking") is False
            and overlay.get("visible_to") == "host_only"
            and overlay.get("historical_characters_may_react") is False
            and overlay.get("host_character") in shot.get("characters", [])
            for overlay in (shot.get("narrative_overlays") or []))
        for shot in shots)
    threshold = storyboard_rules.get(
        "spatial_blocking_required_for_group", 3)
    expected_blocking = build_spatial_plan(
        script, storyboard, continuity, group_threshold=threshold)
    if blocking is None:
        blocking = expected_blocking
    mark_spatial_reference_requirements(blocking)
    spatial_validation = validate_spatial_plan(blocking, storyboard)
    spatial_ok = (spatial_validation["passed"]
                  and (blocking.get("source_fingerprint")
                       == expected_blocking.get("source_fingerprint")))
    spatial_required = [
        block for block in (blocking.get("shot_index") or {}).values()
        if requires_spatial_reference(block)]
    spatial_references_ok = all(
        bool(block.get("spatial_reference_uri"))
        and (str(block["spatial_reference_uri"]).startswith(
                 ("http://", "https://"))
             or Path(block["spatial_reference_uri"]).exists())
        for block in spatial_required)
    script_gate_error = validate_script_bible(script)
    script_logic = script.get("script_logic_audit") or {}
    character_assets = (
        character_assets if isinstance(character_assets, dict) else {})
    asset_policy = character_assets.get("policy") or {}
    cast_selection = character_assets.get("selection") or {}
    identities_ready = bool(cast_selection.get("passed"))
    extended_required = bool(asset_policy.get("generate_sheets"))
    extended_ready = bool(cast_selection.get("canonical_assets_ready"))
    character_assets_ok = (
        identities_ready and (not extended_required or extended_ready))
    character_assets_detail = (
        "所有正式角色和核心道具已人工锁定；本集采用简化人物资产模式，"
        "不要求四视图/细节图"
        if identities_ready and not extended_required
        else "所有正式角色最终立绘、核心道具及完整四视图/细节母资产已锁定"
        if character_assets_ok
        else "人物最终立绘、核心道具或当前人物资产模式要求的母资产尚未齐全")
    appearance_issues = [
        str(issue)
        for shot in shots
        for issue in (shot.get("appearance_continuity_issues") or [])
        if str(issue).strip()
    ]
    appearance_complete = all(
        all(
            str(((shot.get(state_key) or {}).get(name) or {}).get(
                "wardrobe") or "").strip()
            for state_key in ("start_state", "end_state"))
        for shot in shots
        for name in (shot.get("characters") or []))
    appearance_ok = appearance_complete and not appearance_issues
    available_gates = [
        _gate("script_bible", "剧本第一道总闸门与制作圣经",
              script_gate_error is None,
              (script_gate_error
               or (script_logic.get("summary")
                   or "世界观、人物、因果、信息、物理、时间、空间、"
                      "道具生命周期、可拍摄性与局部返编边界已锁定"))),
        _gate(
            "character_assets", "人物与核心道具母资产",
            character_assets_ok, character_assets_detail),
        _gate("continuity", "连续性圣经", bool(continuity.get("characters"))
              and bool(continuity.get("scenes"))
              and appearance_ok
              and continuity.get("standard_fingerprint") == profile.get(
                  "standard_fingerprint"),
              ("角色、场景、文字、服装、头饰与妆发连续性已锁定"
               if appearance_ok else
               "存在无过渡换装或镜头缺少服装状态:"
               + ("；".join(appearance_issues[:3])
                  if appearance_issues else "请补齐逐角色首尾服装状态"))),
        _gate("spatial", "空间调度图", spatial_ok,
              f"{blocking.get('summary', {}).get('scenes', 0)} 场 / "
              f"{blocking.get('summary', {}).get('shots', 0)} 镜已锁定人物走位、"
              "机位、视锥与屏幕轴线"),
        _gate(
            "spatial_seedance", "Seedance 空间参考图",
            spatial_references_ok,
            f"{sum(1 for block in spatial_required if block.get('spatial_reference_uri'))}"
            f"/{len(spatial_required)} 个多人或变机位镜头已生成并绑定必传空间 PNG"),
        _gate("five_dimensions", "五维分镜字段", bool(shots) and all(
            all(key in shot for key in required) for shot in shots)
            and contract_ok,
            f"{len(shots)} 个单元均含五维参数与 {len(required_columns)} 列镜头合同"),
        _gate("duration", "时长与时间粒度", bool(shots) and all(
            0 < float(shot.get("duration", 0)) <= profile["max_segment_seconds"]
            and abs(float(shot["duration"]) / precision
                    - round(float(shot["duration"]) / precision)) < 1e-7
            for shot in shots),
            f"时间码精确到{precision:g}秒，单元不超过{profile['max_segment_seconds']:g}秒"),
        _gate("dialogue", "台词保真与语速", script_dialogue == storyboard_dialogue
              and dialogue_lengths_ok,
              f"原台词逐字覆盖，单镜台词不超过 {max_chars} 字并按情绪计算语速"),
        _gate("performance", "反应镜与表演节拍", reaction_ok and beat_ok
              and (not performance_rules.get("performance_goal_required", True)
                   or all(bool((shot.get("performance") or {}).get("goal"))
                          for shot in shots)),
              "关键台词后保留听者反应，高潮含2–4秒留白，逐镜有表演目标"),
        _gate("camera", "镜头语言与防重复", camera_ok,
              f"每段至少 {min_angles} 种纵向角度；相邻景别跳 {jump_levels} 级"
              f"或机位偏转 {axis_change:g}°"),
        _gate("people", "人物数量", people_ok,
              "人物名单与人数逐单元一致，禁止新增路人"),
        _gate("text", "文字关键帧", bool(text_manifest.get("passed", False)),
              text_manifest.get("note", "")),
        _gate("frames", "首尾帧与段间状态", len(frame_map) == len(shots)
              and all(f.get("first") and f.get("last") for f in frames)
              and formal_frame_quality_ok and frame_visual_qc_ok,
              f"{len(frame_map)}/{len(shots)} 个单元首尾帧就绪；"
              + ("均为中/高质量且已通过视觉质检"
                 if formal_frame_quality_ok and frame_visual_qc_ok
                 else "含低质量试错帧或缺少视觉质检")),
        _gate("audio", "环境声与声音策略", sound_ok,
              "每镜均含环境声设计，声音与空间共同参与叙事"),
        _gate("profile", "即梦生产配置", config_ok,
              "Seedance 2.0 Fast VIP / "
              f"{video_quality['level']}档 {video_quality['resolution']} / "
              "随视频配音与口型 / 无字幕母版"),
    ]
    gates = []
    for gate in available_gates:
        setting = gate_config.get(gate["id"], {})
        mandatory = bool(setting.get("mandatory"))
        if setting.get("enabled", True) is False and not mandatory:
            continue
        if setting.get("label"):
            gate["label"] = setting["label"]
        gate["severity"] = (
            "block" if mandatory
            else setting.get("severity", "block"))
        gate["mandatory"] = mandatory
        gate["owner"] = setting.get("owner", "")
        gates.append(gate)
    return {
        "pipeline_version": PIPELINE_VERSION,
        "standard_fingerprint": profile.get("standard_fingerprint", ""),
        "standard_version": profile.get("standard_version"),
        "passed": all(gate["passed"] for gate in gates
                      if gate.get("severity") != "warning"),
        "gates": gates,
        "profile": copy.deepcopy(profile),
        "quality_policy": copy.deepcopy(
            quality_policy or default_quality_policy()),
        "selected_video_quality": video_quality,
        "script_lines": sum(len(s.get("lines", [])) for s in script.get("scenes", [])),
        "units": len(shots),
    }


def build_content_review(script, storyboard, continuity):
    cast = {c["name"] for c in continuity.get("characters", [])}
    units = []
    for shot in storyboard.get("shots", []):
        characters_ok = set(shot.get("characters", [])) <= cast
        text_asset = shot.get("readable_text") or {}
        text_required = readable_text_required(text_asset)
        text_ok = not text_required or bool(text_asset.get("locked_by"))
        event_ok = bool(shot.get("script_reference"))
        passed = characters_ok and text_ok and event_ok
        units.append({
            "unit_id": shot.get("unit_id"),
            "script_reference": shot.get("script_reference", ""),
            "event_visible": event_ok,
            "character_consistency": characters_ok,
            "costume_consistency": bool(continuity.get("characters")),
            "prop_scene_consistency": bool(continuity.get("scenes")),
            "text_accuracy": text_ok if text_required else None,
            "drift_issue": "" if passed else "结构化映射或文字锁定缺失",
            "verdict": "PASS" if passed else "FAIL",
        })
    return {
        "pipeline_version": PIPELINE_VERSION,
        "standard_fingerprint": storyboard.get("standard_fingerprint", ""),
        "passed": bool(units) and all(u["verdict"] == "PASS" for u in units),
        "review_basis": "剧本映射 + 连续性圣经 + 关键帧/首尾帧检查板",
        "units": units,
    }


def write_review_board(ctx, content_review):
    """生成可在浏览器打开的逐段图文检查板。"""
    out_root = Path(ctx["out_root"])
    path = out_root / "review_board.html"
    images = {i["shot_no"]: i.get("uri", "") for i in ctx.get("images", [])}
    frames = {f["shot_no"]: f for f in ctx.get("frames", [])}
    rows = []
    review_map = {u["unit_id"]: u for u in content_review.get("units", [])}
    profile = (ctx.get("storyboard") or {}).get("profile", {})
    for shot in ctx["storyboard"].get("shots", []):
        frame = frames.get(shot["shot_no"], {})
        review = review_map.get(shot.get("unit_id"), {})
        thumbs = []
        for label, uri in (("关键帧", images.get(shot["shot_no"])),
                           ("首帧", frame.get("first")),
                           ("尾帧", frame.get("last"))):
            if uri and not uri.startswith(("http://", "https://")):
                uri = Path(uri).resolve().as_uri()
            if uri:
                thumbs.append(
                    f'<figure><img src="{html.escape(uri)}"><figcaption>{label}</figcaption></figure>')
        rows.append(
            f'<section><header><b>{shot.get("unit_id")}</b> '
            f'<span>{html.escape(shot.get("shot_function", ""))} · '
            f'{shot.get("duration")}s · {review.get("verdict", "PENDING")}</span></header>'
            f'<p>剧本对应：{html.escape(shot.get("script_reference", ""))}</p>'
            f'<div class="thumbs">{"".join(thumbs)}</div></section>')
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AIFOS 图文检查板</title>
<style>body{font:14px system-ui;background:#0d0d0d;color:#fff;margin:24px}.meta{background:#12243a;border:1px solid #31567e;border-radius:12px;padding:14px}.meta small{display:block;color:#9fb4cb;margin-top:4px}section{background:#1a1a19;border:1px solid #333;border-radius:12px;padding:14px;margin:12px 0}header{display:flex;justify-content:space-between}p{color:#bbb}.thumbs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}figure{margin:0}img{width:100%;max-height:360px;object-fit:contain;background:#111}figcaption{color:#999;text-align:center}</style>"""
    standard_meta = (
        f'<div class="meta"><b>{html.escape(profile.get("standard_name", "SK 五维漫剧标准"))} '
        f'v{profile.get("standard_version", 1)}</b><small>制作标准指纹：'
        f'{html.escape(profile.get("standard_fingerprint", ""))}</small></div>')
    path.write_text(
        document + standard_meta + "".join(rows) + "</html>",
        encoding="utf-8")
    return str(path)


def write_delivery_verifier(ctx, review_board, content_review):
    """落盘并实际运行每集交付复核脚本。"""
    out_root = Path(ctx["out_root"])
    script_path = out_root / "check-delivery.py"
    expected = {
        "files": [
            *(i.get("uri", "") for i in ctx.get("images", [])),
            *(f.get(key, "") for f in ctx.get("frames", []) for key in ("first", "last")),
            *(v.get("uri", "") for v in ctx.get("videos", [])),
            ctx.get("final_uri", ""), review_board,
        ],
        "unit_count": len(ctx["storyboard"].get("shots", [])),
        "image_count": len(ctx.get("images", [])),
        "frame_count": len(ctx.get("frames", [])),
        "video_count": len(ctx.get("videos", [])),
        "final_uri": ctx.get("final_uri", ""),
        "width": ctx.get("dims", {}).get("width"),
        "height": ctx.get("dims", {}).get("height"),
        "duration": sum(float(s.get("duration", 0))
                        for s in ctx["storyboard"].get("shots", [])),
        "content_review_passed": bool(content_review.get("passed")),
        "standard_fingerprint": ctx.get("production_profile", {}).get(
            "standard_fingerprint", ""),
    }
    body = """import json
from pathlib import Path

EXPECTED = __EXPECTED__
missing = [p for p in EXPECTED["files"] if p and not p.startswith(("http://", "https://")) and not Path(p).exists()]
counts_ok = (EXPECTED["unit_count"] == EXPECTED["image_count"] == EXPECTED["frame_count"] == EXPECTED["video_count"])
final_ok = bool(EXPECTED["final_uri"])
standard_ok = bool(EXPECTED["standard_fingerprint"])
if final_ok and EXPECTED["final_uri"].endswith(".json") and Path(EXPECTED["final_uri"]).exists():
    data = json.loads(Path(EXPECTED["final_uri"]).read_text(encoding="utf-8"))
    final_ok = data.get("shot_count") == EXPECTED["unit_count"] and data.get("total_duration", 0) > 0
result = {
    "passed": not missing and counts_ok and final_ok and standard_ok and EXPECTED["content_review_passed"],
    "missing": missing, "counts_ok": counts_ok, "final_ok": final_ok,
    "content_review_passed": EXPECTED["content_review_passed"],
    "resolution": [EXPECTED["width"], EXPECTED["height"]],
    "duration": EXPECTED["duration"], "unit_count": EXPECTED["unit_count"],
    "standard_fingerprint": EXPECTED["standard_fingerprint"],
    "standard_ok": standard_ok,
}
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["passed"] else 1)
""".replace("__EXPECTED__", repr(expected))
    script_path.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script_path)], capture_output=True, text=True,
        timeout=30)
    try:
        result = json.loads((proc.stdout or "{}").splitlines()[-1])
    except (ValueError, IndexError):
        result = {"passed": False, "error": proc.stderr.strip() or "复核脚本无有效输出"}
    result["script"] = str(script_path)
    result["executed"] = True
    result_path = out_root / "delivery_check.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
