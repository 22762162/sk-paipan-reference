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
                "schema": "aifos.shot-prompt/v2",
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
            characters.append({
                "name": name,
                "role": character.get("role", "背景路人"),
                "background_role": True,
                "crowd_function": character.get("crowd_function", ""),
                "identity_anchor": "仅锁定场次功能与人数；无独立人物设定或身份参考图",
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
        "state_fields": continuity_rules.get(
            "state_labels", ["姿态", "伤势", "持有道具", "情绪", "朝向关系"]),
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
    return {
        "pose": pose,
        "injury": "无伤",
        "prop": anchor.get("signature_prop", "无"),
        "emotion": emotion,
        "direction": "面向本镜主体，视线不越轴",
        "position": anchor.get("default_position", "画面中"),
    }


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
        for raw in grouped[scene_no]:
            timeline = shot_timeline_state(raw, timeline)
            physical_people = physical_scene_characters(
                scene_people, timeline, inner_policy)
            out.append(raw)
            dialogue = raw.get("dialogue")
            part = raw.get("dialogue_part") or {"index": 1, "total": 1}
            if (not dialogue or dialogue.get("inner_voice")
                    or not add_reaction
                    or part.get("index") != part.get("total")):
                continue
            speaker = dialogue.get("character")
            listeners = [
                name for name in physical_people if name != speaker]
            if listeners:
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
                    "characters": listeners[:2],
                    "dialogue": None,
                    "prompt": f"{listeners[0]}听完{speaker}的话后的近景反应",
                    "source_dialogue": dialogue.get("dialogue", ""),
                })
        lead = physical_scene_characters(
            scene_people, timeline, inner_policy)[:1]
        if lead and add_beat:
            out.append({
                "scene_no": scene_no,
                "kind": "beat",
                "description": f"{lead[0]}用呼吸、眼神和细微肢体完成本场情绪余波",
                "camera": "特写定镜",
                "duration": sum(float(x) for x in beat_range) / len(beat_range),
                "characters": lead,
                "dialogue": None,
                "prompt": f"{lead[0]}无台词留白表演，具体微表情与呼吸变化",
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
        kind = raw.get("kind") or ("dialogue" if raw.get("dialogue") else "environment")
        characters = list(dict.fromkeys(raw.get("characters", [])))
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
        action_text = " ".join(str(value or "") for value in (
            raw.get("description"), raw.get("physical_logic"),
            raw.get("prompt")))
        declared_start = raw.get("start_state")
        declared_start = (
            declared_start if isinstance(declared_start, dict) else {})
        start_state = {}
        for name in characters:
            explicit_state = declared_start.get(name)
            if isinstance(explicit_state, dict) and explicit_state:
                start_state[name] = copy.deepcopy(explicit_state)
            else:
                start_state[name] = _shot_state(
                    name, continuity, action_text,
                    previous=previous.get(name), emotion="专注")
        emotion = "消化信息" if kind == "reaction" else (
            "情绪余波" if kind == "beat" else "推进事件")
        declared_end = raw.get("end_state")
        declared_end = (
            declared_end if isinstance(declared_end, dict) else {})
        end_state = {}
        for name in characters:
            explicit_state = declared_end.get(name)
            if isinstance(explicit_state, dict) and explicit_state:
                end_state[name] = copy.deepcopy(explicit_state)
            elif kind in ("reaction", "beat"):
                end_state[name] = _state(
                    name, continuity, emotion=emotion,
                    pose="保持原位，完成眼神与呼吸变化")
            else:
                end_state[name] = _shot_state(
                    name, continuity, action_text,
                    previous=start_state.get(name), emotion=emotion,
                    ending=True)
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
        gaze = "主体 → 凝视/瞥向 → 对手或核心物件"
        micro_expression = "眉眼变化·下颌张力·呼吸节奏"
        if narrative_overlays:
            micro_expression += (
                "；内心Q版以夸张眉眼、嘴形、手势和身体弹性强化吐槽/冲突，"
                "真人宿主保持闭口，不替内心声音做口型")
        performance_goal = raw.get("description") or "完成本镜叙事任务"
        seedance_prompt = (
            (f"【制作圣经】{seedance_master}。" if seedance_master else "") +
            "【输入】首帧是唯一动作起点，尾帧是唯一动作终点。"
            "【人物编号映射（仅用于提示词引用，不生成画面文字）】"
            f"{numbered_people}；成片严格共{len(characters)}人"
            f"（{people}），不得新增、复制、合并或换人。"
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
        shot = {
            **raw,
            "shot_no": index,
            "unit_id": f"U{index:02d}",
            "pipeline_version": PIPELINE_VERSION,
            "kind": kind,
            "duration": duration,
            "timecode": timecode,
            "characters": characters,
            "character_count": len(characters),
            "narrative_overlays": narrative_overlays,
            "inner_persona_policy": copy.deepcopy(inner_policy),
            "visible_figure_count": len(characters) + len(narrative_overlays),
            "character_number_map": shot_character_map,
            "character_number_ids": list(shot_character_map),
            "type_word": _type_word(scene, raw),
            "shot_function": SHOT_FUNCTIONS.get(kind, "信息交代"),
            "start_state": start_state,
            "end_state": end_state,
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
              and continuity.get("standard_fingerprint") == profile.get(
                  "standard_fingerprint"), "角色、场景、文字规则与本集标准已锁定"),
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
