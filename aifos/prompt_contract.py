"""可执行的镜头提示词合同。

Seedance/图片模型更容易稳定执行“对象 → 场景 → 单一动作 → 摄影机 →
起止状态”这样的短结构。这个模块只做确定性的编译，不替模型补剧情，也不
把参考图的多个职责混在一条长提示词里。完整提示词仍由导演保存作审计，模型
请求优先使用这里编译出的短版。
"""

from __future__ import annotations

import re

from .camera_language import (
    camera_geometry_clause,
    enforce_composition_scale,
    enforce_position_capacity,
    enforce_scale_capacity,
    enforce_spatial_anchor_scale,
)
from .lighting_language import lighting_clause
from .spatial_language import spatial_lines
from .realism_language import realism_applicable


def _spatial_staging_block(shot, *, media="video"):
    """3D 空间调度 → {标签: 条款} 的可核验空间事实。"""
    shot = shot if isinstance(shot, dict) else {}
    block = shot.get("spatial_blocking")
    if not isinstance(block, dict) or not block.get("actors"):
        return {}
    camera = shot.get("camera")
    scale = ""
    if isinstance(camera, dict):
        scale = str(camera.get("景别") or "")
    else:
        for token in ("大特写", "特写", "中近景", "近景", "膝上景",
                      "七分身", "中景", "大远景", "全景", "远景"):
            if token in str(camera or ""):
                scale = token
                break
    phase = str(shot.get("frame_phase")
                or (shot.get("frame_target") or {}).get("phase")
                or "start").lower()
    phase = phase if phase in ("start", "end") else "start"
    lines = {
        label: text for label, text in spatial_lines(
            block, phase=phase, declared_scale=scale)}
    if str(media or "video").lower() != "video":
        # A still has no route.  Rendering a whole-take actor/camera path into
        # a frozen first/key/last-frame prompt makes the model average the
        # start and end states (an unconscious actor "walks closer", a person
        # who already left is kept in frame, or a hidden phone reappears).
        # Current position, occlusion and screen direction remain useful; all
        # motion-path clauses belong exclusively to the video timeline.
        lines = {
            label: text for label, text in lines.items()
            if not any(token in _text(label) for token in (
                "行动路线", "人物路线", "摄影机路线", "相机路线", "运动路线",
            ))
        }
    # A shot-local Codex repair can intentionally replace the earlier blocking
    # projection.  ``build_physical_contract`` already gives an explicit
    # ``某人屏幕左/右`` clause precedence, but the compact prompt also rendered
    # the untouched 3D ``空间站位/屏幕方向`` lines.  That reintroduced both
    # directions into the same provider request (shot 4: 沈左顾右 *and*
    # 顾左沈右), so every three-draw group was doomed before generation.
    #
    # Keep the useful camera route/distance lines.  Only replace the stale
    # screen projection when both final-frame sides are explicit and disagree
    # with the old projection.  The marker is also consumed by the reference
    # manifest so the top-down image cannot silently restore the superseded
    # labels.
    shot_contract = shot.get("shot_contract") or {}
    description = " ".join(_text(value) for value in (
        shot.get("description"), shot.get("action"), shot.get("camera"),
        shot_contract.get("画面内容描述")
        if isinstance(shot_contract, dict) else "",
        shot_contract.get("站位")
        if isinstance(shot_contract, dict) else "",
    ) if _text(value))
    explicit_left, explicit_right = _explicit_screen_side_names(
        shot, description)
    raw_direction = _text(lines.get("屏幕方向"))
    raw_matches = bool(
        explicit_left and explicit_right and raw_direction
        and f"{explicit_left}在画面左侧" in raw_direction
        and f"{explicit_right}在画面右侧" in raw_direction)
    if (explicit_left and explicit_right
            and explicit_left != explicit_right and not raw_matches):
        resolution = (
            f"最新镜头局部合同锁定：{explicit_left}固定成片屏幕左锚点，"
            f"{explicit_right}固定成片屏幕右锚点；本条替代旧3D投影、"
            "旧空间调度图或旧轴线文字中相反的左右/前后标签。空间图只继续"
            "提供人物对应、相对距离、摄影机路径和视锥，不得覆盖本裁决")
        lines = {
            "空间裁决": resolution,
            "空间站位": (
                f"{explicit_left}位于画面左侧，{explicit_right}位于画面右侧；"
                "具体前后层次、姿态、支撑和动作终态服从最新镜头描述"),
            **{
                label: text for label, text in lines.items()
                if label not in {"空间站位", "屏幕方向"}
            },
            "屏幕方向": (
                f"本镜最终屏幕左右关系：{explicit_left}在画面左侧、"
                f"{explicit_right}在画面右侧；禁止被旧调度标签反向覆盖"),
        }
    return lines


def lighting_lines_for_shot(shot, style, scene):
    """本镜【光影】条款:按剧本已有事实选型,非写实画风自动留空。"""
    effective_style = _style_for_scene(
        style or (shot or {}).get("style"), scene)
    if not realism_applicable(effective_style):
        return ""
    shot = shot if isinstance(shot, dict) else {}
    camera = shot.get("camera")
    camera_text = (
        "·".join(str(value) for value in camera.values() if value)
        if isinstance(camera, dict) else str(camera or ""))
    return lighting_clause(
        location=str(scene or shot.get("location") or ""),
        time_of_day=str(shot.get("time_of_day")
                        or shot.get("era_context") or ""),
        mood=str(shot.get("mood") or shot.get("emotion") or ""),
        camera=camera_text,
        scene_action=_strip_modern_ancient_exclusions(
            " ".join(str(value) for value in (
            shot.get("description"), shot.get("action"),
            shot.get("script_reference")) if value), scene),
        style_override=_filter_modern_incompatible_style(
            shot.get("lighting_style"), scene),
        include_genre_camera=False,
        # 题材决定视听基调:仙侠逆光体积光、悬疑低调硬光、甜宠柔光高调
        genre=_filter_modern_incompatible_style(
            " ".join(str(value) for value in (
            effective_style, scene, shot.get("genre"), shot.get("kind_label"),
            shot.get("project_kind"), shot.get("era_context"),
            shot.get("script_reference")) if value), scene))


PROMPT_CONTRACT_SCHEMA = "aifos.shot-prompt/v2.2"
PHYSICAL_CONTRACT_SCHEMA = "aifos.physical-space/v1"
NON_PICTURE_TEXT_CARRIERS = ("字幕", "对白字幕", "旁白字幕", "台词字幕")
FORBIDDEN_ON_SCREEN_METADATA = (
    "镜头合同", "主体", "场景", "起点", "终点", "单一主动作", "TASK",
    "WORLD / STYLE", "质检原因", "自动优化修订", "参考图职责", "硬约束",
)
SEMANTIC_WARDROBE_TOKENS = (
    "官袍", "直裰", "旅装", "布衣", "麻衣", "短褐", "中衣", "长衫",
    "长袍", "圆领袍", "交领袍", "盘领袍", "朝服", "常服", "公服",
    "制服", "工装", "西装", "衬衫", "外套", "夹克", "风衣", "大氅",
    "斗篷", "披风", "襦裙", "裙", "裤", "铠甲", "盔甲", "校服",
    "礼服", "睡衣", "道袍", "袈裟",
)
SEMANTIC_WARDROBE_CONFLICT_GROUPS = (
    {
        "官袍", "直裰", "旅装", "布衣", "麻衣", "短褐", "中衣",
        "长衫", "长袍", "圆领袍", "交领袍", "盘领袍", "朝服",
        "常服", "公服", "制服", "工装", "西装", "襦裙", "铠甲",
        "盔甲", "校服", "礼服", "睡衣", "道袍", "袈裟",
    },
    {"裙", "裤"},
    {"外套", "夹克", "风衣", "大氅", "斗篷", "披风"},
)
SEMANTIC_APPEARANCE_CHANGE_TOKENS = (
    "换装", "更衣", "换上", "换成", "改穿", "穿上", "套上", "披上",
    "披着", "已脱", "脱去", "脱下", "褪下", "摘下", "取下", "戴上",
    "系上", "换",
)
TERMINAL_DEATH_TOKENS = (
    "已咽气", "咽气", "断气", "尸身态", "死亡", "死去", "身亡",
    "被杀", "被刺死", "毙命", "中刀而亡", "当场死亡",
)
DEATH_TRANSITION_TOKENS = (
    *TERMINAL_DEATH_TOKENS, "杀死", "刺死", "击毙", "处死",
)
HEADWEAR_PRESENCE_VALUES = {"none", "worn", "unknown"}
HEADWEAR_KIND_VALUES = {
    "none", "hair_only", "soft_hat", "official_hat", "crown", "helmet",
    "veil", "hair_ornament", "other", "unknown",
}
HAIR_VISIBILITY_VALUES = {
    "fully_visible", "partially_visible", "covered", "unknown",
}
HEADWEAR_NONE_TOKENS = (
    "无头饰", "未戴头饰", "不戴头饰", "去除头饰", "摘下头饰",
    "无外戴帽冠", "无外戴", "无冠", "未戴冠", "不戴冠", "裸头", "光头",
)
HEADWEAR_KIND_TOKENS = (
    ("official_hat", ("乌纱帽", "乌纱", "官帽")),
    ("helmet", ("头盔", "盔", "兜鍪")),
    ("veil", ("面纱", "帷帽", "幂篱", "幕离")),
    ("soft_hat", ("网巾", "头巾", "软帽", "斗笠", "帽")),
    ("hair_ornament", ("簪", "钗", "步摇")),
    ("crown", ("凤冠", "发冠", "头冠", "戴冠", "冠")),
)
LIFE_STATE_VALUES = {"alive", "dead", "nonliving", "unknown"}
CONSCIOUSNESS_STATE_VALUES = {
    "awake", "asleep", "unconscious", "not_applicable", "unknown",
}
EMBODIMENT_VALUES = {
    "physical", "statue", "portrait", "imagined", "overlay", "unknown",
}
MOBILITY_VALUES = {
    "active", "limited", "immobile", "not_applicable", "unknown",
}
CONDITION_FIELD_ALIASES = {
    "life_state": ("life_state", "life", "vital_state", "生命状态"),
    "consciousness_state": (
        "consciousness_state", "consciousness", "awareness", "意识状态"),
    "embodiment": ("embodiment", "presence_type", "存在形态"),
    "mobility": ("mobility", "movement_capability", "行动能力"),
}
ACTIVE_GAZE_TOKENS = (
    "注视", "凝视", "看向", "望向", "眼神", "视线", "眨眼", "睁眼",
)
ACTIVE_SPEECH_TOKENS = (
    "开口", "说话", "说道", "说出", "回答", "呼喊", "低语", "呢喃", "对白",
)
ACTIVE_EXPRESSION_TOKENS = (
    "微表情", "微笑", "冷笑", "皱眉", "挑眉", "咬唇", "嘴角", "眼神变化",
)
ACTIVE_BREATH_TOKENS = ("呼吸", "喘息", "喘气", "吐息")
ACTIVE_MOTION_TOKENS = (
    "站起", "坐起", "转身", "抬手", "伸手", "走", "跑", "冲", "挣扎",
    "点头", "摇头", "身体前倾", "起身",
)
WAKE_TRANSITION_TOKENS = (
    "醒来", "惊醒", "苏醒", "转醒", "睁眼醒来", "恢复意识",
)
SLEEP_TRANSITION_TOKENS = (
    "睡着", "入睡", "沉睡", "熟睡", "睡眠", "闭眼睡去",
)
UNCONSCIOUS_TRANSITION_TOKENS = (
    "昏迷", "昏厥", "失去意识", "晕倒",
)
PROP_DISCLOSURE_TYPES = {
    "reflection", "mirror", "screen", "display", "painting", "picture",
    "inset", "overlay", "disclosure", "反射", "镜中", "屏幕", "画中画",
}
PREMODERN_CHINESE_ERA_TOKENS = (
    "明初", "明代", "大明", "洪武", "永乐", "古代", "县衙", "驿馆",
    "官舍", "公堂",
)
# 明确数字人数(严格共7人/共4名/3位…):存在即消除模糊词的歧义
_EXPLICIT_COUNT_RE = re.compile(
    r"(?:严格)?共?\s*\d+\s*[人名位](?!\d)")
VAGUE_POPULATION_TOKENS = (
    "几名", "数名", "多名", "若干名", "一群", "人群", "众人", "一众",
    "多人", "几人", "数人", "若干人", "成群", "大批人",
)
STATIC_PROCESS_PATTERNS = (
    r"从.{1,48}(?:到|至|变成|变为|转为)",
    r"由.{1,48}(?:到|至|变成|变为|转为)",
    r"先.{1,48}(?:再|随后|然后)",
    r"(?:开始|正在|逐渐|慢慢).{0,12}"
    r"(?:走|跑|起身|坐起|拿起|打开|转身|换装|摘下|戴上)",
    r"→",
)
_HISTORY_MARKER_RE = re.compile(r"已经|已从|先前|此前|早已|业已")
CORPSE_TOKENS = ("尸体", "尸身", "遗体", "尸骸", "死者", "尸首")
REFERENCE_ROLE_ALIASES = {
    "identity": "identity",
    "character_identity": "identity",
    "character_art": "identity",
    "character_candidate": "identity",
    "identity_detail": "identity",
    "character_sheet": "identity",
    "structure": "identity",
    "wardrobe": "wardrobe",
    "costume": "wardrobe",
    "costume_detail": "wardrobe",
    "prop": "prop",
    "prop_identity": "prop",
    "prop_candidate": "prop",
    "scene": "scene",
    "scene_art": "scene",
    "spatial": "spatial",
    "spatial_blocking": "spatial",
    "keyframe": "continuity",
    "image": "continuity",
    "first_frame": "continuity",
    "last_frame": "continuity",
    "continuity": "continuity",
    "revision_base": "continuity",
    "style": "style",
    "style_ref": "style",
    "composition": "composition",
    "inner_persona": "narrative_overlay",
    "narrative_overlay": "narrative_overlay",
    "reference": "reference",
    "manual": "reference",
}
REFERENCE_SCOPE_DEFAULTS = {
    # The compact ``identity`` scope is deliberately coarse for downstream
    # adapters. ``inherits`` below exposes the exact safe identity attributes.
    "identity": {
        "include": ["identity"],
        # 用户上传的人物参考图是身份最高标准：不仅锁脸与发型，也锁
        # 稳定妆造（眉眼、眼线、睫毛、唇妆体系）。服装仍由剧情另行
        # 决定；镜头若明确换妆，应通过当前 appearance state 覆盖，
        # 不能让服装/场景参考反向改掉人物身份妆造。
        "inherits": [
            "face", "hairstyle", "age", "gender", "makeup",
            "stable_makeup",
        ],
        "exclude": [
            "wardrobe", "pose", "composition", "background", "lighting",
            "props", "prop_position",
        ],
    },
    "scene": {
        "include": ["space", "materials", "key_light"],
        "inherits": ["space", "materials", "key_light"],
        "exclude": [
            "identity", "wardrobe", "pose", "action", "composition",
            "props", "prop_position", "text",
        ],
    },
    "wardrobe": {
        "include": ["wardrobe", "accessories"],
        "inherits": ["wardrobe", "accessories"],
        "exclude": [
            "identity", "pose", "composition", "background", "lighting",
            "prop_position",
        ],
    },
    "prop": {
        "include": ["props"],
        "inherits": ["shape", "structure", "materials", "craft"],
        # ``frame_share`` 是道具母图最容易外溢的一项:母图是占满画面的
        # 棚拍特写,不排除掉,模型会把「铃铛很大」当成道具事实继承。
        "exclude": [
            "identity", "wardrobe", "pose", "composition", "background",
            "lighting", "prop_position", "frame_share",
        ],
    },
    "spatial": {
        "include": ["spatial_blocking"],
        "inherits": [
            "figure_count", "positions", "occlusion", "camera_position",
        ],
        "exclude": [
            "identity", "wardrobe", "style", "text", "reference_annotations",
        ],
    },
    "continuity": {
        "include": ["continuity"],
        "inherits": [
            "composition", "state", "wardrobe", "props", "lighting",
        ],
        "exclude": ["identity_redesign", "background_replacement"],
    },
    "style": {
        "include": ["visual_medium", "materials", "palette", "lighting_style"],
        "inherits": [
            "visual_medium", "materials", "palette", "lighting_style",
        ],
        "exclude": [
            "identity", "wardrobe", "pose", "composition", "background",
            "prop_position",
        ],
    },
    "composition": {
        "include": ["camera_position", "composition", "action_path"],
        "inherits": ["camera_position", "composition", "action_path"],
        "exclude": ["identity", "wardrobe", "background", "lighting"],
    },
    "narrative_overlay": {
        "include": ["overlay_identity", "chibi_proportion"],
        "inherits": [
            "face", "hairstyle", "current_wardrobe", "chibi_proportion",
        ],
        "exclude": [
            "real_person_count", "spatial_blocking", "default_props",
        ],
    },
    "reference": {
        "include": [],
        "inherits": [],
        "exclude": [
            "identity", "wardrobe", "pose", "composition", "background",
            "lighting", "props", "prop_position",
        ],
    },
}
MEDIUM_2D_TOKENS = (
    "2d", "二维", "平面动画", "二维动画", "手绘动画", "赛璐璐",
)
MEDIUM_3D_TOKENS = (
    "3d", "三维", "三渲二", "cg三维", "半写实3d",
)
MEDIUM_LIVE_ACTION_TOKENS = (
    "真人摄影", "真人实拍", "实景拍摄", "live action",
    "live-action", "photographic live action",
)
MEDIUM_NON_LIVE_ACTION_TOKENS = (
    "非真人摄影", "不是真人摄影", "非真人实拍", "不是真人实拍",
)


def _text(value, fallback=""):
    value = "" if value is None else str(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or fallback


_STYLE_CAMERA_CLAUSE_TOKENS = (
    "焦段", "mm", "长焦", "广角", "鱼眼", "景别", "近景", "中景",
    "特写", "全景", "远景", "俯拍", "仰拍", "微俯", "跟拍", "推镜",
    "拉镜", "摇镜", "移镜", "环绕", "甩镜", "变焦", "手持",
    "构图采用", "镜头组合",
)


def _style_without_shot_camera_directives(value):
    """Keep visual aesthetics, but remove generic camera suggestions.

    A project style is shared by every shot.  Its palette, texture and light
    may be inherited; a sentence such as ``85mm close-up tracking shot`` may
    not override the repaired per-shot camera.  Keeping those suggestions in
    the rendered prompt made the image model and QC see two execution cameras.
    """
    kept = []
    for clause in re.split(r"[。；;\n]+", _text(value)):
        clause = clause.strip()
        if not clause:
            continue
        lowered = clause.lower()
        if any(token.lower() in lowered
               for token in _STYLE_CAMERA_CLAUSE_TOKENS):
            continue
        kept.append(clause)
    return "；".join(kept)


_MODERN_SCENE_TOKENS = (
    "现代", "当代", "酒店", "电梯", "办公室", "便利店", "高速公路",
    "都市", "轿车", "汽车", "驾驶座", "副驾驶", "别墅", "公寓",
)

_MODERN_INCOMPATIBLE_STYLE_TOKENS = (
    "明代宫殿", "明代烛台", "明代", "大明", "古代", "古风", "古装",
    "宫斗", "权谋", "朝堂", "后宫", "宅斗", "官场", "宫殿", "宫廷",
    "东宫", "寝殿", "紫禁城", "殿内", "殿堂", "宫灯", "烛火", "烛台",
    "书案", "香炉", "纱幕", "纱帐", "古室", "县衙", "驿馆", "官舍",
)

_MODERN_INCOMPATIBLE_SCENE_PROP_TOKENS = (
    "宫殿", "宫廷", "古室", "寝殿", "殿内", "殿堂", "书案", "香炉",
    "宫灯", "烛台", "纱幕", "纱帐", "卷册", "县衙", "驿馆", "官舍",
)


def _is_modern_scene(scene):
    text = _text(scene)
    return bool(
        re.search(r"(?:19|20|21)\d{2}年", text)
        or any(token in text for token in _MODERN_SCENE_TOKENS))


def _clean_style_fragment(fragment):
    cleaned = _text(fragment)
    for token in _MODERN_INCOMPATIBLE_STYLE_TOKENS:
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r"[、/]{2,}", "、", cleaned)
    cleaned = re.sub(r"^[的以与和及、/\s]+|[的与和及、/\s]+$", "", cleaned)
    if cleaned in {
            "", "为光源动机", "光源动机", "本剧题材视听基调",
            "题材", "风格"}:
        return ""
    return cleaned


def _filter_modern_incompatible_style(value, scene):
    """Keep aesthetics while removing an era/location design package.

    Project style is allowed to control palette, light, material, medium and
    cinematic finish.  It may not redesign an authoritative modern location
    as a palace.  Mixed clauses are salvaged (``鎏金柔雾写实古风`` becomes
    ``鎏金柔雾写实``); pure palace/furnishing clauses disappear.
    """
    text = _text(value)
    if not text or not _is_modern_scene(scene):
        return text
    clauses = []
    for clause in re.split(r"[。；;\n]+", text):
        fragments = []
        for fragment in re.split(r"[，,]+", clause):
            # A furnishing/location fragment cannot be made modern by merely
            # deleting nouns: "暖金古室中以书案、香炉..." used to become the
            # meaningless and still highly suggestive "暖金中以、...".  Drop
            # that complete fragment; palette/light/medium fragments survive.
            if any(token in fragment
                   for token in _MODERN_INCOMPATIBLE_SCENE_PROP_TOKENS):
                continue
            cleaned = _clean_style_fragment(fragment)
            if cleaned:
                fragments.append(cleaned)
        if fragments:
            clauses.append("，".join(fragments))
    return "；".join(clauses)


def _style_for_scene(value, scene):
    return _filter_modern_incompatible_style(
        _style_without_shot_camera_directives(value), scene)


def _strip_modern_ancient_exclusions(value, scene):
    """Drop only negated ancient exclusion lists from a modern shot state.

    They remain useful in an audit log, but naming every forbidden prop in the
    provider-facing target primes the generator with exactly those objects.
    Positive story facts (including a deliberate antique prop) are preserved.
    """
    text = _text(value)
    if not text or not _is_modern_scene(scene):
        return text
    kept_sentences = []
    for sentence in re.split(r"[。！？!?；;\n]+", text):
        parts = []
        for part in re.split(r"[，,]+", sentence):
            has_ancient = any(
                token in part for token in _MODERN_INCOMPATIBLE_STYLE_TOKENS)
            negated = any(
                marker in part for marker in _SCENE_NEGATION_MARKERS)
            bare_none = re.search(
                r"(?:^|[：:\s])(?:场景|画面)?无(?:任何|相关|一切|其他)?",
                part,
            )
            if has_ancient and (negated or bare_none):
                continue
            if _text(part):
                parts.append(_text(part))
        if parts:
            kept_sentences.append("，".join(parts))
    return "。".join(kept_sentences)


def _text_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    output = []
    for item in value:
        text = _text(item)
        if text and text not in output:
            output.append(text)
    return output


def _state_value(value):
    """Render a dedicated image/freeze state without assuming one shape."""
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return ""
    nested = value.get("state")
    if nested is not None and nested is not value:
        rendered = _state_value(nested)
        if rendered:
            return rendered
    for key in ("description", "freeze", "image", "value"):
        if _text(value.get(key)):
            return _text(value.get(key))
    return _state_line(value)


def _registered_state_value(shot, key):
    """Ignore stale named states for actors no longer visible in this shot."""
    value = shot.get(key)
    if not isinstance(value, dict):
        return _state_value(value)
    characters = {_text(name) for name in shot.get("characters") or []}
    state_fields = {
        "position", "pose", "direction", "wardrobe", "headwear",
        "hair_visibility", "hair_makeup", "prop", "injury", "emotion",
        "condition", "character_condition", "life_state",
        "consciousness_state", "embodiment", "mobility",
    }
    if state_fields & set(value):
        return _state_value(value)
    filtered = {
        name: state for name, state in value.items()
        if _text(name) in characters
    }
    return _state_value(filtered)


def _normalize_mode(mode):
    requested = _text(mode, "image").lower()
    if requested in {"video", "motion", "seedance"}:
        return "video", requested
    if requested in {"image", "keyframe", "first_frame", "last_frame"}:
        return "image", requested
    return "image", requested


def _static_frame_policy(shot):
    """Return the explicit policy controlling legacy static-frame derivation.

    v2.2 requires a producer-authored frame target. Existing saved storyboards
    may opt into their old start/end or description fallback, but the opt-in has
    to be carried by the shot rather than silently enabled at runtime.
    """
    raw = (
        shot.get("frame_target_policy")
        if "frame_target_policy" in shot
        else shot.get("static_frame_policy"))
    allow = False
    name = "strict_explicit"
    if isinstance(raw, dict):
        allow = bool(
            raw.get("allow_legacy_fallback")
            or raw.get("allow_start_end_derivation")
            or raw.get("legacy_compatibility"))
        name = _text(
            raw.get("name") or raw.get("policy"),
            "legacy_explicit" if allow else name)
    elif isinstance(raw, bool):
        allow = raw
        name = "legacy_explicit" if raw else name
    else:
        value = _text(raw).lower()
        allow = value in {
            "legacy", "legacy_explicit", "legacy_start_end",
            "allow_legacy_fallback", "allow_start_end_derivation",
        }
        if value:
            name = value
    return {
        "name": name,
        "allow_legacy_fallback": allow,
        "explicitly_declared": raw is not None,
    }


def _frame_target(shot, mode, requested_mode=""):
    """Select one static freeze state, or the three-part video timeline."""
    start = _registered_state_value(
        shot, "start_state") or "保持首帧状态"
    end = _registered_state_value(
        shot, "end_state") or "到达尾帧状态"
    if mode == "video":
        previous_contract = shot.get("prompt_contract")
        previous_action = (
            previous_contract.get("action")
            if isinstance(previous_contract, dict) else "")
        action_source = (
            shot.get("video_action") or shot.get("action")
            or previous_action or shot.get("description"))
    else:
        action_source = shot.get("description") or shot.get("action")
    action = _text(
        action_source, "环境保持稳定，只执行自然微动")
    if mode == "video":
        return {
            "phase": "timeline",
            "state": {"start": start, "action": action, "end": end},
            "source": "start_state/action/end_state",
            "fallback": False,
            "explicit": True,
            "compatibility_policy": "not_applicable",
            "legacy_compatibility": False,
        }

    policy = _static_frame_policy(shot)
    frame_kind = (
        _text(shot.get("frame_kind")).lower()
        or _text(requested_mode).lower())
    phase = "start" if frame_kind == "first_frame" else "end"
    target_key = (
        frame_kind
        if frame_kind in {"first_frame", "last_frame"}
        else "keyframe")
    targets = shot.get("frame_targets")
    declared = (
        targets.get(target_key)
        if isinstance(targets, dict) and target_key in targets
        else shot.get("frame_target"))
    declared_source = (
        f"frame_targets.{target_key}"
        if isinstance(targets, dict) and target_key in targets
        else "frame_target")
    if declared is not None:
        declared_phase = ""
        declared_state = ""
        declared_location = ""
        declared_fallback = False
        if isinstance(declared, dict):
            declared_phase = _text(
                declared.get("phase") or declared.get("frame_phase")).lower()
            declared_state = _state_value(
                declared.get("state")
                if "state" in declared else declared.get("frame_state"))
            declared_location = _text(
                declared.get("location") or declared.get("scene_location"))
            declared_fallback = bool(declared.get("fallback"))
        else:
            declared_state = _state_value(declared)
        if declared_state:
            target = {
                # Keep an invalid/missing phase invalid so validation can
                # BLOCK it; never wash malformed upstream state into start/end.
                "phase": declared_phase,
                "state": declared_state,
                "source": declared_source,
                "fallback": (
                    declared_fallback or not isinstance(declared, dict)),
                "explicit": isinstance(declared, dict),
                "fallback_declared": (
                    isinstance(declared, dict)
                    and "fallback" in declared),
                "compatibility_policy": policy["name"],
                "legacy_compatibility": policy["allow_legacy_fallback"],
            }
            if declared_location:
                # This is a provider-facing visible sub-location only.  The
                # Director keeps the shot's authoritative top-level location
                # for scene_model lookup and formal scene reference assets.
                target["location"] = declared_location
            return target
    for source in ("freeze_state", "image_state"):
        state = _state_value(shot.get(source))
        if state:
            return {
                "phase": "freeze",
                "state": state,
                "source": source,
                "fallback": True,
                "explicit": False,
                "compatibility_policy": policy["name"],
                "legacy_compatibility": policy["allow_legacy_fallback"],
            }
    # 存量镜头可能没有 frame_target，但已把唯一静态道具状态明确写成
    # phase=freeze。此时应把它理解为当前定格，而不能默认取 end 后又把
    # 画面中真实可见的道具全部过滤掉。多 phase 镜头仍必须依赖显式
    # frame_target，避免把 start 的亮屏手机带进 end 的隐藏终态。
    raw_frame_props = shot.get("frame_props") or []
    if isinstance(raw_frame_props, dict):
        raw_frame_props = list(raw_frame_props.values())
    prop_phases = {
        _normalize_prop_phase(item.get("phase"))
        for item in raw_frame_props
        if isinstance(item, dict) and _text(item.get("phase"))
    }
    if prop_phases == {"freeze"}:
        if _text(shot.get("description")):
            source, state = "description", _text(shot.get("description"))
        elif _text(shot.get("action")):
            source, state = "action", _text(shot.get("action"))
        else:
            source, state = "default", "环境保持稳定"
        return {
            "phase": "freeze",
            "state": state,
            "source": source,
            "fallback": True,
            "explicit": False,
            "compatibility_policy": policy["name"],
            "legacy_compatibility": policy["allow_legacy_fallback"],
        }
    state_source = "start_state" if phase == "start" else "end_state"
    state = _registered_state_value(shot, state_source)
    if state:
        return {
            "phase": phase,
            "state": state,
            "source": state_source,
            "fallback": True,
            "explicit": False,
            "compatibility_policy": policy["name"],
            "legacy_compatibility": policy["allow_legacy_fallback"],
        }
    if _text(shot.get("description")):
        source, state = "description", _text(shot.get("description"))
    elif _text(shot.get("action")):
        source, state = "action", _text(shot.get("action"))
    else:
        source, state = "default", "环境保持稳定"
    return {
        "phase": phase,
        "state": state,
        "source": source,
        "fallback": True,
        "explicit": False,
        "compatibility_policy": policy["name"],
        "legacy_compatibility": policy["allow_legacy_fallback"],
    }


def _declared_frame_target(shot, target):
    """Return the source object that produced a normalized frame target."""
    source = _text((target or {}).get("source"))
    if source.startswith("frame_targets."):
        key = source.split(".", 1)[1]
        targets = shot.get("frame_targets")
        value = targets.get(key) if isinstance(targets, dict) else None
        return value if isinstance(value, dict) else None
    if source == "frame_target":
        value = shot.get("frame_target")
        return value if isinstance(value, dict) else None
    return None


def _frame_character_scope(shot, target, output_media):
    """Resolve an optional static-frame subset of whole-take characters."""
    whole = []
    for value in shot.get("characters") or []:
        name = _text(value)
        if name and name not in whole:
            whole.append(name)
    if output_media == "video":
        return whole, [], False, "shot.characters"
    declared = _declared_frame_target(shot, target)
    if not isinstance(declared, dict) or "characters" not in declared:
        return whole, [], False, "shot.characters"
    raw = declared.get("characters")
    source = f"{_text(target.get('source'), 'frame_target')}.characters"
    if not isinstance(raw, (list, tuple)):
        return whole, [f"{source} 必须是人物姓名数组"], True, source
    selected = []
    issues = []
    for value in raw:
        name = _text(value)
        if not name:
            issues.append(f"{source} 不得包含空人物名")
            continue
        if name not in whole:
            issues.append(
                f"{source} 包含未登记人物「{name}」；"
                "只能从 shot.characters 选择")
            continue
        if name in selected:
            issues.append(f"{source} 重复声明人物「{name}」")
            continue
        selected.append(name)
    return selected, issues, True, source


def _frame_functional_scope(shot, target, output_media):
    """Resolve optional phase-specific non-identity people for a still."""
    whole = shot.get("functional_figures") or []
    if output_media == "video":
        return whole, [], False, "shot.functional_figures"
    declared = _declared_frame_target(shot, target)
    if not isinstance(declared, dict) or "functional_figures" not in declared:
        return whole, [], False, "shot.functional_figures"
    raw = declared.get("functional_figures")
    source = (
        f"{_text(target.get('source'), 'frame_target')}.functional_figures")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return whole, [f"{source} 必须是功能人物对象数组"], True, source
    selected = [dict(item) if isinstance(item, dict) else item for item in raw]
    return selected, [], True, source


def _shot_with_character_scope(
        shot, characters, functional_figures=None, target_state=""):
    """Make one shallow, static-only view without unrelated cast/staging."""
    scoped = dict(shot)
    allowed = set(characters)
    scoped["characters"] = list(characters)
    if functional_figures is not None:
        scoped["functional_figures"] = list(functional_figures)
        allowed.update(
            _text(item.get("name") or item.get("label"))
            for item in functional_figures if isinstance(item, dict))
        allowed.discard("")
    if _text(target_state):
        # Static compilation must not keep the whole-take action sentence once
        # the producer supplied a unique frozen state.  Apart from preventing
        # motion leakage, this removes not-yet-entered cast names from camera,
        # lighting and physical inference.
        scoped["description"] = _text(target_state)
        scoped["action"] = _text(target_state)
    for field in ("start_state", "end_state"):
        states = shot.get(field)
        if isinstance(states, dict):
            scoped[field] = {
                name: value for name, value in states.items()
                if name in allowed}
    # A saved whole-take composition/3D projection may contain a character who
    # has not entered yet. Rebuild composition from the scoped cast and keep
    # only the current actors in the spatial projection.
    scoped.pop("composition_contract", None)
    blocking = shot.get("spatial_blocking")
    if isinstance(blocking, dict):
        blocking = dict(blocking)
        actors = blocking.get("actors")
        if isinstance(actors, list):
            blocking["actors"] = [
                dict(item) for item in actors
                if isinstance(item, dict)
                and _text(item.get("name") or item.get("character")) in allowed
            ]
        dialogue = blocking.get("dialogue_continuity")
        if isinstance(dialogue, dict):
            names = {
                _text(dialogue.get("screen_left_name")),
                _text(dialogue.get("screen_right_name")),
            } - {""}
            if not names <= allowed or len(characters) < 2:
                blocking.pop("dialogue_continuity", None)
        scoped["spatial_blocking"] = blocking
    dialogue = shot.get("dialogue")
    if (isinstance(dialogue, dict)
            and _text(dialogue.get("character")) not in allowed):
        scoped["dialogue"] = {}
    overlays = shot.get("narrative_overlays")
    if isinstance(overlays, list):
        scoped["narrative_overlays"] = [
            dict(item) for item in overlays
            if isinstance(item, dict)
            and _text(item.get("host_character")) in allowed]

    excluded = {
        _text(value) for value in shot.get("characters") or []
        if _text(value) and _text(value) not in allowed}

    def keep_clause(value):
        return not any(name in _text(value) for name in excluded)

    for field in ("physical_contract", "physical_logic", "spatial_logic"):
        value = shot.get(field)
        if isinstance(value, dict):
            filtered = dict(value)
            for key in ("rules", "constraints", "objects", "object_relations"):
                rows = filtered.get(key)
                if isinstance(rows, list):
                    filtered[key] = [item for item in rows if keep_clause(item)]
                elif isinstance(rows, str):
                    filtered[key] = "；".join(
                        clause for clause in re.split(r"[。；;\n]+", rows)
                        if _text(clause) and keep_clause(clause))
            relations = filtered.get("spatial_relations")
            if isinstance(relations, list):
                filtered["spatial_relations"] = [
                    item for item in relations
                    if keep_clause(item)]
            scoped[field] = filtered
        elif isinstance(value, str):
            scoped[field] = "；".join(
                clause for clause in re.split(r"[。；;\n]+", value)
                if _text(clause) and keep_clause(clause))
    return scoped


def _normalize_functional_figures(shot):
    raw_items = shot.get("functional_figures") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    normalized = []
    issues = []
    if not isinstance(raw_items, (list, tuple)):
        raw_items = []
        issues.append("functional_figures 必须是对象列表")
    for position, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            issues.append(f"第{position}个功能人物不是对象")
            continue
        label = _text(item.get("name") or item.get("label"))
        raw_count = item.get("count")
        valid_count = (
            isinstance(raw_count, int)
            and not isinstance(raw_count, bool)
            and raw_count > 0
        )
        if not label:
            issues.append(f"第{position}个功能人物缺少 name/label")
        if not valid_count:
            issues.append(
                f"功能人物「{label or position}」的 count 必须是精确正整数")
        normalized.append({
            "name": _text(item.get("name")),
            "label": _text(item.get("label")) or label,
            "count": raw_count if valid_count else 0,
            "state": _text(item.get("state")),
            "function": _text(item.get("function")),
        })
    # A folded long take may carry one row per temporal beat for the same
    # functional person.  Equal names are sequential states of one continuous
    # body, not additional people.  Use the largest count declared by any beat
    # (the concurrent maximum) and retain the union of state/function wording.
    merged = {}
    order = []
    for position, item in enumerate(normalized):
        label = _text(item.get("name") or item.get("label"))
        if not label:
            # Preserve malformed rows so their validation issue is not hidden.
            malformed_key = ("__malformed__", position)
            order.append(malformed_key)
            merged[malformed_key] = item
            continue
        if label not in merged:
            merged[label] = dict(item)
            order.append(label)
            continue
        target = merged[label]
        target["count"] = max(
            int(target.get("count") or 0), int(item.get("count") or 0))
        for field in ("state", "function"):
            values = []
            for raw in (target.get(field), item.get(field)):
                for part in _text(raw).split("；"):
                    part = part.strip()
                    if part and part not in values:
                        values.append(part)
            target[field] = "；".join(values)
        if not target.get("name") and item.get("name"):
            target["name"] = item["name"]
        if not target.get("label") and item.get("label"):
            target["label"] = item["label"]
    return [merged[label] for label in order], issues


def _readable_carrier_visible_at_phase(
        readable, frame_props, target_phase, prop_registry):
    """Whether a static frame's text carrier is visible in its target phase.

    Text metadata is often authored for a whole long take (for example a phone
    showing 23:10 at ``start``).  A terminal keyframe whose same phone is hidden
    in a pocket must not inherit that earlier text requirement.
    """
    readable = readable if isinstance(readable, dict) else {}
    target_phase = _text(target_phase).lower()
    rows = [
        item for item in (frame_props or [])
        if isinstance(item, dict)
        and _text(item.get("phase")).lower() == target_phase
    ]
    if not rows:
        return True

    explicit_ids = {
        _text(readable.get(key))
        for key in ("prop_id", "carrier_prop_id", "carrier_id", "object_id")
        if _text(readable.get(key))
    }
    if explicit_ids:
        matching = [
            item for item in rows
            if _text(item.get("prop_id")) in explicit_ids]
    else:
        carrier = _text(readable.get("carrier")).lower()
        categories = []
        if any(token in carrier for token in ("手机", "锁屏", "平板", "tablet")):
            categories.append(("手机", "phone", "mobile", "平板", "tablet"))
        if any(token in carrier for token in ("电脑", "笔记本", "显示器", "显示屏")):
            categories.append(("电脑", "笔记本", "computer", "laptop", "显示器", "显示屏"))
        registry_names = {
            _text(item.get("prop_id")): _text(item.get("name")).lower()
            for item in (prop_registry or []) if isinstance(item, dict)
        }
        matching = []
        for item in rows:
            prop_id = _text(item.get("prop_id"))
            haystack = f"{prop_id} {registry_names.get(prop_id, '')}".lower()
            if any(any(token in haystack for token in category)
                   for category in categories):
                matching.append(item)
    if not matching:
        return True
    # Readable text and active device-use geometry require a directly visible
    # carrier.  ``occluded`` is still useful as a continuity fact, but its
    # screen cannot be read and must not create a hand/screen/gaze relation.
    return any(
        _text(item.get("visibility"), "visible").lower() == "visible"
        for item in matching)


def _functional_figure_line(item):
    label = _text(item.get("name") or item.get("label"), "功能人物")
    count = int(item.get("count") or 0)
    counter = "具" if any(token in f"{label}{item.get('state', '')}"
                          for token in CORPSE_TOKENS) else "名"
    details = "；".join(filter(None, (
        f"状态={_text(item.get('state'))}" if _text(item.get("state")) else "",
        f"功能={_text(item.get('function'))}"
        if _text(item.get("function")) else "",
    )))
    return f"{label}{count}{counter}" + (f"（{details}）" if details else "")


def _object_items(value, identity_key):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if not isinstance(value, dict):
        return value
    if identity_key in value or "name" in value or "label" in value:
        return [value]
    output = []
    for key, item in value.items():
        if isinstance(item, dict):
            normalized = dict(item)
            normalized.setdefault(identity_key, key)
        else:
            normalized = {identity_key: key, "position": item}
        output.append(normalized)
    return output


def _normalize_prop_phase(value, target_phase="end"):
    raw = _text(value).lower()
    aliases = {
        "起点": "start", "开始": "start", "start": "start",
        "终点": "end", "结束": "end", "end": "end",
        "freeze": "freeze", "current": "freeze", "frame": "freeze",
        "当前": "freeze", "定格": "freeze",
    }
    return aliases.get(raw, raw or target_phase)


def _is_prop_disclosure(value):
    occurrence = _text(value).lower()
    return bool(
        occurrence
        and any(token in occurrence for token in PROP_DISCLOSURE_TYPES))


def _prop_main_position(item):
    if not isinstance(item, dict):
        return ""
    holder = _text(item.get("holder"))
    location = _text(
        item.get("location") or item.get("primary_position")
        or item.get("position"))
    support = _text(item.get("support"))
    if holder:
        return f"holder:{holder}"
    if location:
        return f"location:{location}"
    if support:
        return f"support:{support}"
    return ""


def _normalize_frame_props(shot, target_phase="end"):
    raw_items = _object_items(shot.get("frame_props"), "prop_id")
    issues = []
    if not isinstance(raw_items, list):
        return [], ["frame_props 必须是对象列表或 prop_id 映射"]
    output = []
    for index, raw in enumerate(raw_items, 1):
        if not isinstance(raw, dict):
            issues.append(f"第{index}个 frame_prop 必须是对象")
            continue
        prop_id = _text(raw.get("prop_id") or raw.get("id"))
        representation = _text(
            raw.get("representation") or raw.get("occurrence_type")
            or raw.get("presence_type") or raw.get("channel"),
            "physical").lower()
        visibility = _text(raw.get("visibility"), "visible").lower()
        holder = _text(raw.get("holder"))
        location = _text(
            raw.get("location") or raw.get("primary_position")
            or raw.get("position"))
        support = _text(raw.get("support"))
        phase = _normalize_prop_phase(raw.get("phase"), target_phase)
        if not _text(raw.get("phase")):
            issues.append(
                f"frame_prop「{prop_id or index}」缺少显式 phase")
        if not _text(raw.get("visibility")):
            issues.append(
                f"frame_prop「{prop_id or index}」缺少显式 visibility")
        if not _text(
                raw.get("representation")
                or raw.get("occurrence_type")
                or raw.get("presence_type")
                or raw.get("channel")):
            issues.append(
                f"frame_prop「{prop_id or index}」缺少显式 representation")
        normalized = {
            "prop_id": prop_id,
            "phase": phase,
            "physical_state": _text(
                raw.get("physical_state") or raw.get("state")
                or raw.get("current_state")),
            "holder": holder,
            "location": location,
            "support": support,
            "visibility": visibility,
            "representation": representation,
        }
        physical = (
            visibility != "absent"
            and not _is_prop_disclosure(representation))
        if not prop_id:
            issues.append(f"第{index}个 frame_prop 缺少 prop_id")
        if physical and not _prop_main_position(normalized):
            issues.append(
                f"frame_prop「{prop_id or index}」是物理实例但缺少主位置")
        output.append(normalized)
    return output, issues


def _normalize_prop_transitions(shot):
    raw_items = _object_items(shot.get("prop_transitions"), "prop_id")
    issues = []
    if not isinstance(raw_items, list):
        return [], ["prop_transitions 必须是对象列表或 prop_id 映射"]
    output = []
    for index, raw in enumerate(raw_items, 1):
        if not isinstance(raw, dict):
            issues.append(f"第{index}个 prop_transition 必须是对象")
            continue
        prop_id = _text(raw.get("prop_id") or raw.get("id"))
        from_phase = _normalize_prop_phase(
            raw.get("from_phase") or raw.get("start_phase"), "start")
        to_phase = _normalize_prop_phase(
            raw.get("to_phase") or raw.get("end_phase"), "end")
        if not prop_id:
            issues.append(f"第{index}个 prop_transition 缺少 prop_id")
        output.append({
            "prop_id": prop_id,
            "from_phase": from_phase,
            "to_phase": to_phase,
            "action": _text(
                raw.get("action") or raw.get("transition")
                or raw.get("motion")),
        })
    return output, issues


def _render_frame_prop(item):
    representation = _text(item.get("representation"), "physical")
    visibility = _text(item.get("visibility"), "visible")
    disclosure = _is_prop_disclosure(representation)
    location = _prop_main_position(item) or "未声明位置"
    line = (
        f"{_text(item.get('prop_id'), '未登记道具')}="
        f"phase={_text(item.get('phase'))}；"
        f"physical_state={_text(item.get('physical_state'), '未声明')}；"
        f"holder={_text(item.get('holder'), '无')}；"
        f"location={_text(item.get('location'), '无')}；"
        f"support={_text(item.get('support'), '无')}；"
        f"visibility={visibility}；representation={representation}；"
        f"{'披露位置' if disclosure else '物理主位置'}={location}")
    if disclosure or visibility == "absent":
        line += (
            "；不计作第二个物理实例"
            if disclosure else "；本 phase 不计物理实例")
    return line


def _render_prop_transition(item, positions=None):
    positions = positions or {}
    prop_id = _text(item.get("prop_id"), "未登记道具")
    from_phase = _text(item.get("from_phase"), "start")
    to_phase = _text(item.get("to_phase"), "end")
    from_position = positions.get((prop_id, from_phase), "由 frame_props 锁定")
    to_position = positions.get((prop_id, to_phase), "由 frame_props 锁定")
    return (
        f"{prop_id}："
        f"{from_phase}@{from_position}"
        f"→{to_phase}@{to_position}"
        + (f"；动作={_text(item.get('action'))}"
           if _text(item.get("action")) else ""))


def _prop_transition_position(item):
    if not isinstance(item, dict):
        return ""
    visibility = _text(item.get("visibility"), "visible")
    representation = _text(item.get("representation"), "physical")
    if visibility == "absent":
        return "visibility=absent"
    if _is_prop_disclosure(representation):
        return f"representation={representation}"
    return _prop_main_position(item)


def _normalize_role_value(value):
    return REFERENCE_ROLE_ALIASES.get(_text(value).lower(), _text(value).lower())


def _split_role_values(value):
    if isinstance(value, (list, tuple, set)):
        return [_normalize_role_value(item) for item in value if _text(item)]
    text = _text(value)
    if not text:
        return []
    parts = [
        part for part in re.split(r"\s*(?:\+|,|，|、|\||/)\s*", text)
        if part
    ]
    return [_normalize_role_value(part) for part in parts]


def _normalize_reference(item):
    raw_role = item.get("role")
    raw_kind = item.get("kind")
    raw_roles = item.get("roles")
    role_values = _split_role_values(
        raw_roles if raw_roles is not None
        else raw_role if raw_role is not None else raw_kind)
    role_issues = []
    if len(set(role_values)) > 1:
        role_issues.append("参考图声明了多个 role；每张参考图只能承担单一职责")
    role = role_values[0] if role_values else "reference"
    # ``kind`` is often the storage/asset type (character_sheet, image, etc.)
    # while ``role`` is the one semantic responsibility for this request.
    # When role is explicit it is authoritative; comparing it with kind would
    # reject valid cases such as kind=image + role=revision_base.

    defaults = REFERENCE_SCOPE_DEFAULTS.get(
        role, REFERENCE_SCOPE_DEFAULTS["reference"])
    raw_scope = item.get("inherit_scope")
    raw_scope = raw_scope if isinstance(raw_scope, dict) else {}
    explicit_include = (
        raw_scope.get("include") if "include" in raw_scope
        else item.get("inherits"))
    explicit_exclude = (
        raw_scope.get("exclude") if "exclude" in raw_scope
        else item.get("excludes"))
    include = (
        _text_list(explicit_include)
        if explicit_include is not None
        else list(defaults["include"]))
    if explicit_include is not None and explicit_exclude is not None:
        # 装配方同时显式给出 include 与 exclude = 该图作用域已被人为
        # 裁决(如背面立绘要继承 wardrobe/prop_position、穿着类道具要
        # 继承 wardrobe)。此时排除域以显式声明为准,不再并入角色默认
        # ——否则显式覆盖永远无法移除默认排除项,include/exclude 必然
        # 交集,预检把全部此类镜头批量熔断(2026-07-28 镜头02/16实测)。
        # 显式声明内部自相矛盾仍会被下方交集检查拦截并可见。
        excludes = _text_list(explicit_exclude)
    else:
        # Safe role boundaries cannot be weakened by a lone explicit
        # include. Keeping an unsafe item in both lists makes the
        # conflict visible to preflight.
        excludes = list(defaults["exclude"])
        for value in _text_list(explicit_exclude):
            if value not in excludes:
                excludes.append(value)
    inherits = (
        _text_list(item.get("inherits"))
        if item.get("inherits") is not None
        else list(defaults["inherits"]))
    for value in include:
        if value not in inherits and role not in {"identity", "scene"}:
            inherits.append(value)

    raw_binding = item.get("binding")
    bindings = item.get("bindings")
    binding_values = _text_list(bindings)
    if raw_binding is not None and not binding_values:
        if isinstance(raw_binding, (list, tuple, set)):
            binding_values = _text_list(raw_binding)
    try:
        index = int(item.get("index"))
    except (TypeError, ValueError):
        index = item.get("index")
    return {
        "index": index,
        "label": _text(item.get("label") or item.get("name"), "参考图"),
        "role": role,
        "character": _text(item.get("character")),
        "binding": raw_binding if raw_binding is not None else "",
        "bindings": binding_values,
        "inherit_scope": {
            "include": include,
            "exclude": excludes,
        },
        "inherits": inherits,
        "excludes": excludes,
        "role_issues": role_issues,
    }


def _normalize_spatial_relations(shot, explicit=None):
    if isinstance(explicit, dict):
        relations = explicit.get("spatial_relations")
    else:
        relations = None
    if relations is None:
        relations = shot.get("spatial_relations")
    if isinstance(relations, dict):
        relations = [relations]
    if not isinstance(relations, (list, tuple)):
        return []
    return [
        dict(item) if isinstance(item, dict) else item
        for item in relations
    ]


def _normalize_visual_medium(shot, style=""):
    explicit_values = [
        _text(shot.get("visual_medium")),
        _text(shot.get("medium")),
        _text(style),
        _text(shot.get("style")),
    ]
    text = "；".join(value for value in explicit_values if value)
    lowered = text.lower()
    has_2d = any(token in lowered for token in MEDIUM_2D_TOKENS)
    has_3d = any(token in lowered for token in MEDIUM_3D_TOKENS)
    negative_live = any(token in lowered
                        for token in MEDIUM_NON_LIVE_ACTION_TOKENS)
    live_action = (
        any(token in lowered for token in MEDIUM_LIVE_ACTION_TOKENS)
        and not negative_live
    )
    semi_realistic_3d = (
        has_3d
        and any(token in lowered for token in (
            "半写实", "semi-realistic", "semirealistic"))
    )
    issues = []
    if has_2d and has_3d:
        issues.append("视觉媒介 2D 与 3D 声明冲突，无法执行")
    dimension = (
        "3D" if has_3d else "2D" if has_2d
        else "live_action" if live_action else "unspecified")
    return {
        "raw": text,
        "dimension": dimension,
        "semi_realistic_3d": semi_realistic_3d,
        "live_action_photography": live_action,
        "photography_excluded": bool(semi_realistic_3d or negative_live),
        "issues": issues,
    }


def sanitize_text_whitelist(values):
    """Keep only explicit user/story text, never prompt or QC metadata."""
    cleaned = []
    for value in values or []:
        text = _text(value)
        if not text or any(text == field or text.startswith(field)
                           for field in FORBIDDEN_ON_SCREEN_METADATA):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _semantic_wardrobe_signature(value):
    text = _text(value)
    return {
        token for token in SEMANTIC_WARDROBE_TOKENS
        if token in text
    }


def _semantic_wardrobes_conflict(first, second):
    left = _semantic_wardrobe_signature(first)
    right = _semantic_wardrobe_signature(second)
    return any(
        (left & group)
        and (right & group)
        and (left & group).isdisjoint(right & group)
        for group in SEMANTIC_WARDROBE_CONFLICT_GROUPS)


def _actor_local_clause(text, actor):
    """Return one actor-local visible clause from the current shot action."""
    source = str(text or "")
    if not source or not actor:
        return ""
    for match in re.finditer(re.escape(str(actor)), source):
        clause = source[match.start():match.start() + 120]
        clause = re.split(r"[，,。；\n]", clause, maxsplit=1)[0]
        signature = _semantic_wardrobe_signature(clause)
        separate_object = any(
            marker in clause for marker in (
                "身旁放", "旁边放", "边放", "挂着", "搭着", "摆着",
                "搁着", "散落", "置于", "放在", "搭在"))
        worn = any(
            marker in clause for marker in (
                "身穿", "穿着", "穿上", "换上", "换成", "改穿",
                "套上", "披上", "披着", "着一身"))
        if signature and (not separate_object or worn):
            return clause.strip(" ，,；。")
    return ""


def _actor_semantic_clause(text, actor, actors=None):
    """Return an actor-local semantic window without swallowing another actor."""
    source = str(text or "")
    name = str(actor or "")
    if not source or not name:
        return ""
    match = re.search(re.escape(name), source)
    if not match:
        return ""
    prefix = source[max(0, match.start() - 32):match.start()]
    # Only transitive kill verbs may legally bind from before the victim's
    # name ("刺客杀死书童").  Intransitive terminal words such as "咽气"
    # belong to the preceding actor; carrying "陈允咽气，" into 沈砚's
    # window falsely marks the surviving reactor as dead.
    actor_targeting_death_tokens = (
        "杀死", "刺死", "击毙", "处死", "害死")
    transition_hits = [
        prefix.rfind(token)
        for token in actor_targeting_death_tokens
        if token in prefix
    ]
    # “刺客杀死书童”把死亡动词写在受害者姓名之前。只把最靠近姓名
    # 的死亡谓词带入受害者窗口，不吞入施害者此前的冲刺等主动动作。
    prefix = (
        prefix[max(transition_hits):]
        if transition_hits else "")
    tail = prefix + source[match.start():match.start() + 180]
    cut_points = [
        index for index in (
            tail.find("。"), tail.find("；"), tail.find("\n"))
        if index >= 0
    ]
    for other in actors or []:
        other = str(other or "")
        if not other or other == name:
            continue
        index = tail.find(other, len(name))
        if index >= 0:
            cut_points.append(index)
    if cut_points:
        tail = tail[:min(cut_points)]
    return tail.strip(" ，,；。")


def _actor_death_transition(text, actor, actors=None):
    """Recognize that this actor dies without assigning the killer's verb."""
    local = _actor_semantic_clause(text, actor, actors)
    if any(token in local for token in TERMINAL_DEATH_TOKENS):
        return True
    source = str(text or "")
    name = re.escape(str(actor or ""))
    if not source or not name:
        return False
    return any(
        re.search(
            rf"{re.escape(token)}[^，,。；\n]{{0,16}}{name}",
            source)
        for token in ("杀死", "刺死", "击毙", "处死", "害死"))


def _has_placed_object_mention(text, token):
    """Detect an outfit/prop described as a separate placed scene object."""
    source = str(text or "")
    object_name = re.escape(str(token or ""))
    if not source or not object_name:
        return False
    location = (
        r"(?:榻边|床边|旁边|身旁|桌上|案上|地上|架上|椅上|"
        r"墙上|一旁)")
    placement = (
        r"(?:挂着|搭着|搭在|放着|放在|摆着|摆在|搁着|搁在|"
        r"散落|另放|平放|叠放)")
    # Require an actual placement verb near this exact object.  A mere nearby
    # location word (e.g. “沈砚布旅装跪坐榻边”) is not evidence that the
    # garment itself exists in a second location.
    patterns = (
        rf"{location}[^，,。；]{{0,8}}{placement}"
        rf"[^，,。；]{{0,8}}{object_name}",
        rf"{placement}[^，,。；]{{0,8}}{object_name}",
        rf"{object_name}[^，,。；]{{0,8}}"
        rf"(?:挂在|搭在|放在|摆在|搁在){location}",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def build_era_object_constraints(shot):
    """Compile high-risk historical props into visible morphology rules.

    Generic nouns such as ``油灯`` are visually under-specified: image models
    often turn them into a nineteenth-century glass-chimney kerosene lamp.
    The rule is deterministic and shot-local, so the next prompt states what
    the object is *and* which common anachronistic form is forbidden.
    """
    shot = shot if isinstance(shot, dict) else {}
    authoritative_scene = _text(
        shot.get("location") or shot.get("scene_location")
        or shot.get("scene_context") or shot.get("world_state"))
    if _is_modern_scene(authoritative_scene):
        return []
    context = " ".join(_text(value) for value in (
        shot.get("era_context"), shot.get("era"), shot.get("world_state"),
        shot.get("location"), shot.get("scene_context"), shot.get("style"),
        shot.get("description"), shot.get("action"), shot.get("prompt"),
    ) if _text(value))
    visible = " ".join(_text(value) for value in (
        shot.get("description"), shot.get("action"), shot.get("prompt"),
        (shot.get("shot_contract") or {}).get("画面内容描述")
        if isinstance(shot.get("shot_contract"), dict) else "",
    ) if _text(value))
    sanctioned = " ".join(
        _text(value) for value in (
            shot.get("sanctioned_anachronisms") or [])
        if _text(value))
    historical = _contains_affirmed_scene_hint(
        context, PREMODERN_CHINESE_ERA_TOKENS)
    if not historical:
        return []
    rules = []
    if _mentions_present_object(visible, ("油灯",)) and not any(
            token in sanctioned for token in (
                "煤油灯", "玻璃灯罩", "玻璃罩灯")):
        rules.append(
            "时代物件锁定—油灯：只画明代可成立的陶制或青铜开放式浅盏"
            "油灯，灯油与棉芯可见；绝不画玻璃灯罩、煤油灯筒、现代调节"
            "旋钮、电灯泡或工业金属灯座")
    if _mentions_present_object(visible, ("提灯", "灯笼")) and not any(
            token in sanctioned for token in (
                "玻璃提灯", "煤油提灯")):
        rules.append(
            "时代物件锁定—提灯：只画明代竹木骨架配纸或薄绢灯罩的笼灯，"
            "内部烛火受罩保护；绝不画玻璃煤油提灯、现代金属提手灯或电灯")
    if _mentions_present_object(visible, ("烛台", "烛火", "孤烛")):
        rules.append(
            "时代物件锁定—烛台：使用裸露蜡烛与明代可成立的铜、铁或陶"
            "烛台；禁止套用玻璃灯罩、煤油灯芯机构或电灯结构")
    return list(dict.fromkeys(rules))


def _semantic_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(
            part for part in (
                _semantic_text(key) + " " + _semantic_text(item)
                for key, item in value.items())
            if part.strip())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_semantic_text(item) for item in value)
    return _text(value)


def _canonical_headwear_kind(value):
    raw = _text(value).lower()
    aliases = {
        "无": "none", "none": "none", "hair_only": "hair_only",
        "仅头发": "hair_only", "soft_hat": "soft_hat", "软帽": "soft_hat",
        "official_hat": "official_hat", "官帽": "official_hat",
        "crown": "crown", "冠": "crown", "helmet": "helmet",
        "头盔": "helmet", "veil": "veil", "面纱": "veil",
        "hair_ornament": "hair_ornament", "发饰": "hair_ornament",
        "other": "other", "其他": "other", "unknown": "unknown",
        "未知": "unknown",
    }
    if raw in aliases:
        return aliases[raw]
    for kind, tokens in HEADWEAR_KIND_TOKENS:
        if any(token.lower() in raw for token in tokens):
            return kind
    return "other" if raw else "unknown"


def _canonical_headwear_presence(value):
    raw = _text(value).lower()
    if raw in {"none", "无", "未佩戴", "不佩戴", "absent"}:
        return "none"
    if raw in {"worn", "佩戴", "戴着", "present", "有"}:
        return "worn"
    return "unknown"


def _canonical_hair_visibility(value):
    raw = _text(value).lower()
    aliases = {
        "fully_visible": "fully_visible", "full": "fully_visible",
        "完整可见": "fully_visible", "完全可见": "fully_visible",
        "全露": "fully_visible", "partially_visible": "partially_visible",
        "partial": "partially_visible", "部分可见": "partially_visible",
        "半遮": "partially_visible", "covered": "covered",
        "遮盖": "covered", "完全遮盖": "covered", "不可见": "covered",
        "unknown": "unknown", "未知": "unknown",
    }
    return aliases.get(raw, "unknown")


def _normalize_headwear(state):
    """Normalize structured and legacy headwear without dropping source text."""
    state = state if isinstance(state, dict) else {}
    raw_value = (
        state.get("headwear")
        if "headwear" in state else state.get("headwear_state"))
    raw = raw_value if isinstance(raw_value, dict) else {}
    legacy = "" if isinstance(raw_value, dict) else _text(raw_value)
    presence = _canonical_headwear_presence(
        raw.get("presence") or state.get("headwear_presence"))
    kind = _canonical_headwear_kind(
        raw.get("kind") or raw.get("category")
        or state.get("headwear_kind"))
    name = _text(
        raw.get("name") or raw.get("label")
        or state.get("headwear_name") or legacy)
    hair_makeup = _text(state.get("hair_makeup"))
    semantic = " ".join(filter(None, (legacy, name, hair_makeup)))
    headwear_declares_none = (
        legacy.strip() in {"无", "none"}
        or any(token in f"{legacy} {name}" for token in HEADWEAR_NONE_TOKENS))
    hair_declares_none = any(
        token in hair_makeup for token in HEADWEAR_NONE_TOKENS)
    visible_headwear_text = f"{legacy} {name}"
    if legacy.strip() in {"无", "none"}:
        visible_headwear_text = ""
    for token in HEADWEAR_NONE_TOKENS:
        visible_headwear_text = visible_headwear_text.replace(token, "")
    detected_kind = _canonical_headwear_kind(
        visible_headwear_text) if visible_headwear_text.strip() else "unknown"
    if kind == "unknown" and detected_kind != "unknown":
        kind = detected_kind
    if presence == "unknown":
        if (headwear_declares_none and kind == "unknown") or kind in {
                "none", "hair_only"}:
            presence = "none"
        elif kind not in {"unknown", "none"} or name:
            presence = "worn"
        elif hair_declares_none:
            presence = "none"
    if kind == "unknown" and presence == "none":
        kind = "none"

    visibility_value = (
        state.get("hair_visibility")
        if "hair_visibility" in state
        else raw.get("hair_visibility"))
    hair_visibility = _canonical_hair_visibility(visibility_value)
    if hair_visibility == "unknown":
        if presence == "none" or kind in {"hair_only", "hair_ornament"}:
            hair_visibility = "fully_visible"
        elif kind == "helmet":
            hair_visibility = "covered"
        elif presence == "worn":
            hair_visibility = "partially_visible"

    # A name such as ``official_hat`` or ``old silver hairpin`` is not a
    # sufficiently precise visual contract.  Preserve optional morphology
    # fields supplied by continuity/storyboard data so generation, reference
    # binding and QC can agree on the same visible object.  The aliases keep
    # older project data readable while the canonical keys make new contracts
    # deterministic.
    visual_fields = {}
    visual_aliases = {
        "shape": ("shape", "silhouette", "main_shape"),
        "material": ("material", "main_material"),
        "color": ("color", "main_color"),
        "placement": ("placement", "wearing_position", "position_on_head"),
        "signature_details": (
            "signature_details", "ornament_details", "terminal_details"),
        "forbidden_variants": (
            "forbidden_variants", "negative_variants", "must_not_be"),
    }
    for canonical, aliases in visual_aliases.items():
        value = next(
            (raw.get(alias) for alias in aliases
             if raw.get(alias) not in (None, "", [])),
            None)
        if value is not None:
            visual_fields[canonical] = (
                list(value) if isinstance(value, (list, tuple)) else value)

    issues = []
    visually_worn_kind = kind not in {
        "none", "hair_only", "unknown", "hair_ornament",
    }
    if presence == "none" and visually_worn_kind:
        issues.append("headwear.presence=none 但 kind/name 声明了在戴头饰")
    if (presence == "none"
            and detected_kind not in {"unknown", "none", "hair_only"}):
        issues.append(
            "headwear.presence=none 但 name/legacy_text 含可见头饰")
    if presence == "worn" and kind in {"none", "hair_only"}:
        issues.append("headwear.presence=worn 与 kind=none/hair_only 冲突")
    if ((hair_declares_none or headwear_declares_none)
            and presence == "worn" and visually_worn_kind):
        issues.append("无冠/无头饰声明与正在佩戴的 headwear 冲突")
    if (hair_visibility == "fully_visible"
            and kind in {
                "soft_hat", "official_hat", "crown", "helmet", "veil"}):
        issues.append(
            f"hair_visibility=fully_visible 与 headwear.kind={kind} 冲突")
    if (hair_visibility == "covered"
            and presence == "none"
            and kind in {"none", "hair_only", "unknown"}):
        issues.append("hair_visibility=covered 但未声明任何遮盖头部的头饰")
    if presence not in HEADWEAR_PRESENCE_VALUES:
        issues.append(f"未知 headwear.presence:{presence}")
    if kind not in HEADWEAR_KIND_VALUES:
        issues.append(f"未知 headwear.kind:{kind}")
    if hair_visibility not in HAIR_VISIBILITY_VALUES:
        issues.append(f"未知 hair_visibility:{hair_visibility}")
    return {
        "presence": presence,
        "kind": kind,
        "name": name,
        "hair_visibility": hair_visibility,
        "legacy_text": legacy,
        "issues": list(dict.fromkeys(issues)),
        **visual_fields,
    }


def _render_headwear(value):
    if not isinstance(value, dict):
        return _text(value)
    presence = _text(value.get("presence"), "unknown")
    kind = _text(value.get("kind"), "unknown")
    name = _text(value.get("name"))
    visibility = _text(value.get("hair_visibility"), "unknown")
    rendered = (
        f"presence={presence},kind={kind}"
        + (f",name={name}" if name else "")
        + f",hair_visibility={visibility}")
    field_labels = (
        ("shape", "shape"),
        ("material", "material"),
        ("color", "color"),
        ("placement", "placement"),
        ("signature_details", "signature_details"),
        ("forbidden_variants", "forbidden_variants"),
    )
    for key, label in field_labels:
        raw = value.get(key)
        if raw in (None, "", []):
            continue
        detail = "、".join(str(item) for item in raw) \
            if isinstance(raw, (list, tuple)) else _text(raw)
        rendered += f",{label}={detail}"
    return rendered


def _condition_value(source, field):
    source = source if isinstance(source, dict) else {}
    for alias in CONDITION_FIELD_ALIASES[field]:
        if source.get(alias) is not None:
            return source.get(alias)
    return None


def _canonical_condition_value(field, value):
    raw = _text(value).lower()
    aliases = {
        "life_state": {
            "alive": "alive", "生存": "alive", "活着": "alive",
            "dead": "dead", "死亡": "dead", "已死亡": "dead",
            "nonliving": "nonliving", "非生命": "nonliving",
            "not_applicable": "nonliving", "unknown": "unknown", "未知": "unknown",
        },
        "consciousness_state": {
            "awake": "awake", "清醒": "awake", "asleep": "asleep",
            "睡眠": "asleep", "熟睡": "asleep", "unconscious": "unconscious",
            "昏迷": "unconscious", "昏厥": "unconscious",
            "not_applicable": "not_applicable", "不适用": "not_applicable",
            "unknown": "unknown", "未知": "unknown",
        },
        "embodiment": {
            "physical": "physical", "实体": "physical", "真人实体": "physical",
            "statue": "statue", "雕像": "statue", "portrait": "portrait",
            "画像": "portrait", "imagined": "imagined", "想象": "imagined",
            "overlay": "overlay", "叠层": "overlay", "unknown": "unknown",
            "未知": "unknown",
        },
        "mobility": {
            "active": "active", "可主动行动": "active", "limited": "limited",
            "受限": "limited", "immobile": "immobile", "不可主动行动": "immobile",
            "not_applicable": "not_applicable", "不适用": "not_applicable",
            "unknown": "unknown", "未知": "unknown",
        },
    }
    return aliases[field].get(raw, raw or "unknown")


def _normalize_character_condition(state, extra_text=""):
    state = state if isinstance(state, dict) else {}
    raw_condition = (
        state.get("condition")
        if state.get("condition") is not None
        else state.get("character_condition"))
    legacy_condition = _text(
        raw_condition).lower() if not isinstance(raw_condition, dict) else ""
    legacy_defaults = {
        "normal": {
            "life_state": "alive", "consciousness_state": "awake",
            "embodiment": "physical", "mobility": "active",
        },
        "正常": {
            "life_state": "alive", "consciousness_state": "awake",
            "embodiment": "physical", "mobility": "active",
        },
        "asleep": {
            "life_state": "alive", "consciousness_state": "asleep",
            "embodiment": "physical", "mobility": "limited",
        },
        "熟睡": {
            "life_state": "alive", "consciousness_state": "asleep",
            "embodiment": "physical", "mobility": "limited",
        },
        "unconscious": {
            "life_state": "alive", "consciousness_state": "unconscious",
            "embodiment": "physical", "mobility": "immobile",
        },
        "昏迷": {
            "life_state": "alive", "consciousness_state": "unconscious",
            "embodiment": "physical", "mobility": "immobile",
        },
        "dead": {
            "life_state": "dead", "consciousness_state": "not_applicable",
            "embodiment": "physical", "mobility": "immobile",
        },
        "死亡": {
            "life_state": "dead", "consciousness_state": "not_applicable",
            "embodiment": "physical", "mobility": "immobile",
        },
        "statue": {
            "life_state": "nonliving",
            "consciousness_state": "not_applicable",
            "embodiment": "statue", "mobility": "immobile",
        },
        "portrait": {
            "life_state": "nonliving",
            "consciousness_state": "not_applicable",
            "embodiment": "portrait", "mobility": "not_applicable",
        },
        "imagined": {
            "life_state": "nonliving",
            "consciousness_state": "not_applicable",
            "embodiment": "imagined", "mobility": "not_applicable",
        },
    }
    nested = raw_condition or {}
    nested = nested if isinstance(nested, dict) else {}
    if legacy_condition in legacy_defaults:
        nested = legacy_defaults[legacy_condition]
    values = {
        field: _canonical_condition_value(
            field,
            _condition_value(nested, field)
            if _condition_value(nested, field) is not None
            else _condition_value(state, field))
        for field in CONDITION_FIELD_ALIASES
    }
    text = " ".join(filter(None, (
        _semantic_text(state), _text(extra_text))))
    if values["embodiment"] == "unknown":
        if any(token in text for token in ("雕像", "石像", "塑像")):
            values["embodiment"] = "statue"
        elif any(token in text for token in ("画像", "肖像", "画中人物")):
            values["embodiment"] = "portrait"
        elif any(token in text for token in ("想象", "幻象", "脑海中")):
            values["embodiment"] = "imagined"
        elif any(token in text for token in ("叠层", "Q版", "内心人格")):
            values["embodiment"] = "overlay"
    if values["life_state"] == "unknown":
        if any(token in text for token in TERMINAL_DEATH_TOKENS):
            values["life_state"] = "dead"
        elif values["embodiment"] in {
                "statue", "portrait", "imagined", "overlay"}:
            values["life_state"] = "nonliving"
        elif any(token in text for token in (
                *WAKE_TRANSITION_TOKENS, *SLEEP_TRANSITION_TOKENS,
                *UNCONSCIOUS_TRANSITION_TOKENS, "清醒", "正常")):
            values["life_state"] = "alive"
    if values["consciousness_state"] == "unknown":
        if values["life_state"] in {"dead", "nonliving"}:
            values["consciousness_state"] = "not_applicable"
        elif any(token in text for token in WAKE_TRANSITION_TOKENS):
            values["consciousness_state"] = "awake"
        elif any(token in text for token in UNCONSCIOUS_TRANSITION_TOKENS):
            values["consciousness_state"] = "unconscious"
        elif any(token in text for token in SLEEP_TRANSITION_TOKENS):
            values["consciousness_state"] = "asleep"
        elif "清醒" in text:
            values["consciousness_state"] = "awake"
    if values["embodiment"] == "unknown" and values["life_state"] in {
            "alive", "dead"}:
        values["embodiment"] = "physical"
    if values["mobility"] == "unknown":
        if (values["life_state"] in {"dead", "nonliving"}
                or values["consciousness_state"] == "unconscious"):
            values["mobility"] = "immobile"
        elif values["consciousness_state"] == "asleep":
            values["mobility"] = "limited"
        elif (values["life_state"] == "alive"
              and values["consciousness_state"] == "awake"):
            values["mobility"] = "active"

    issues = []
    allowed = {
        "life_state": LIFE_STATE_VALUES,
        "consciousness_state": CONSCIOUSNESS_STATE_VALUES,
        "embodiment": EMBODIMENT_VALUES,
        "mobility": MOBILITY_VALUES,
    }
    for field, valid in allowed.items():
        if values[field] not in valid:
            issues.append(f"未知 {field}:{values[field]}")
    if (values["life_state"] in {"dead", "nonliving"}
            and values["consciousness_state"] not in {
                "not_applicable", "unknown"}):
        issues.append(
            f"life_state={values['life_state']} 与 "
            f"consciousness_state={values['consciousness_state']} 冲突")
    if (values["life_state"] in {"dead", "nonliving"}
            and values["mobility"] in {"active", "limited"}):
        issues.append(
            f"life_state={values['life_state']} 与 mobility="
            f"{values['mobility']} 冲突")
    nonphysical = values["embodiment"] in {
        "statue", "portrait", "imagined", "overlay",
    }
    if nonphysical and values["life_state"] not in {
            "nonliving", "unknown"}:
        issues.append(
            f"embodiment={values['embodiment']} 必须使用 "
            "life_state=nonliving")
    if nonphysical and values["consciousness_state"] not in {
            "not_applicable", "unknown"}:
        issues.append(
            f"embodiment={values['embodiment']} 不得声明清醒、睡眠或昏迷")
    allowed_nonphysical_mobility = (
        {"immobile", "not_applicable", "unknown"}
        if values["embodiment"] in {"statue", "portrait"}
        else {"not_applicable", "unknown"})
    if nonphysical and values["mobility"] not in allowed_nonphysical_mobility:
        issues.append(
            f"embodiment={values['embodiment']} 与 "
            f"mobility={values['mobility']} 冲突")
    values["issues"] = list(dict.fromkeys(issues))
    return values


def _character_condition_map(
        shot, characters, action, target_phase="end"):
    start_states = (
        shot.get("start_state")
        if isinstance(shot.get("start_state"), dict) else {})
    end_states = (
        shot.get("end_state")
        if isinstance(shot.get("end_state"), dict) else {})
    declared = (
        shot.get("character_conditions")
        if isinstance(shot.get("character_conditions"), dict) else {})
    output = {}
    for name in characters:
        local = _actor_semantic_clause(action, name, characters)
        if len(characters) == 1 and not local:
            local = _text(action)
        per_actor = declared.get(name) or {}
        per_actor = per_actor if isinstance(per_actor, dict) else {}
        start_state = dict(start_states.get(name) or {})
        end_state = dict(end_states.get(name) or {})
        explicit_start = (
            per_actor.get("start")
            if "start" in per_actor else per_actor.get("start_condition"))
        explicit_end = (
            per_actor.get("end")
            if "end" in per_actor else per_actor.get("end_condition"))
        if (explicit_start is None and explicit_end is None
                and any(
                    _condition_value(per_actor, field) is not None
                    for field in CONDITION_FIELD_ALIASES)):
            explicit_start = per_actor
            explicit_end = per_actor
        if isinstance(explicit_start, dict):
            start_state["condition"] = explicit_start
        if isinstance(explicit_end, dict):
            end_state["condition"] = explicit_end
        start_condition = _normalize_character_condition(start_state)
        end_condition = _normalize_character_condition(end_state, local)
        actor_conditions = {
            "start": start_condition,
            "end": end_condition,
        }
        if target_phase == "freeze":
            explicit_freeze = (
                per_actor.get("freeze")
                if "freeze" in per_actor
                else per_actor.get("freeze_condition"))
            if isinstance(explicit_freeze, dict):
                actor_conditions["freeze"] = (
                    _normalize_character_condition(
                        {"condition": explicit_freeze}))
            else:
                condition_fields = tuple(CONDITION_FIELD_ALIASES)
                start_signature = tuple(
                    start_condition.get(field) for field in condition_fields)
                end_signature = tuple(
                    end_condition.get(field) for field in condition_fields)
                if start_signature == end_signature:
                    actor_conditions["freeze"] = dict(end_condition)
                else:
                    # 编剧未显式声明定格状态时,按静帧的默认电影语义
                    # 承接"动作完成态"(end);带 derived 溯源标记供审计,
                    # 不再作为阻断性缺陷——漏写的机械默认可本地补齐,
                    # 显式声明(上方分支)永远优先。
                    actor_conditions["freeze"] = {
                        **dict(end_condition),
                        "derived_from": "end_condition",
                    }
        output[_text(name)] = actor_conditions
    return output


def _behavior_hits(text):
    """Return positive visible behaviours, ignoring explicit prohibitions."""
    groups = {
        "呼吸": ACTIVE_BREATH_TOKENS,
        "注视/眨眼": ACTIVE_GAZE_TOKENS,
        "说话": ACTIVE_SPEECH_TOKENS,
        "微表情": ACTIVE_EXPRESSION_TOKENS,
        "主动动作": ACTIVE_MOTION_TOKENS,
    }
    hits = set()
    for major_clause in re.split(r"[。；\n]", _text(text)):
        if not major_clause:
            continue
        first_behavior = min(
            (major_clause.find(token) for tokens in groups.values()
             for token in tokens if token in major_clause),
            default=-1)
        prefix = (
            major_clause[:first_behavior]
            if first_behavior >= 0 else major_clause)
        if any(marker in prefix for marker in (
                "禁止", "不得", "不能", "不允许", "没有", "无")):
            continue
        for clause in re.split(r"[，,]", major_clause):
            if not clause:
                continue
            first_clause_behavior = min(
                (clause.find(token) for tokens in groups.values()
                 for token in tokens if token in clause),
                default=-1)
            clause_prefix = (
                clause[:first_clause_behavior]
                if first_clause_behavior >= 0 else clause)
            if any(marker in clause_prefix for marker in (
                    "禁止", "不得", "不能", "不允许", "没有", "无")):
                continue
            for label, tokens in groups.items():
                if any(token in clause for token in tokens):
                    hits.add(label)
    return hits


def _state_line(states):
    values = []
    if not isinstance(states, dict):
        return ""
    for name, state in (states or {}).items():
        state = state if isinstance(state, dict) else {}
        details = [
            _text(state.get("position")) or "原位",
            _text(state.get("pose")) or "自然状态",
            f"朝向{_text(state.get('direction')) or '按画面'}",
        ]
        if state.get("headwear") is not None or any(
                state.get(key) is not None for key in (
                    "headwear_state", "headwear_presence", "headwear_kind",
                    "headwear_name", "hair_visibility")):
            details.append(f"头饰{_render_headwear(_normalize_headwear(state))}")
        for key, label in (
                ("wardrobe", "服装"), ("hair_makeup", "妆发"),
                ("prop", "道具"), ("injury", "伤势"), ("emotion", "情绪")):
            if _text(state.get(key)):
                details.append(f"{label}{_text(state[key])}")
        if any(
                state.get(key) is not None for key in (
                    "condition", "character_condition", "life_state",
                    "consciousness_state", "embodiment", "mobility")):
            condition = _normalize_character_condition(state)
            details.append(
                "状态"
                f"life_state={condition['life_state']},"
                f"consciousness_state={condition['consciousness_state']},"
                f"embodiment={condition['embodiment']},"
                f"mobility={condition['mobility']}")
        values.append(f"{_text(name)}:" + ",".join(details))
    return "；".join(values)


def _appearance_map(states):
    if not isinstance(states, dict):
        return {}
    output = {}
    for name, state in (states or {}).items():
        if not isinstance(state, dict):
            continue
        look = {}
        if _text(state.get("wardrobe")):
            look["wardrobe"] = _text(state.get("wardrobe"))
        if _text(state.get("hair_makeup")):
            look["hair_makeup"] = _text(state.get("hair_makeup"))
        if state.get("headwear") is not None or any(
                state.get(key) is not None for key in (
                    "headwear_state", "headwear_presence", "headwear_kind",
                    "headwear_name", "hair_visibility")):
            headwear = _normalize_headwear(state)
            look["headwear"] = headwear
            look["hair_visibility"] = headwear["hair_visibility"]
        output[_text(name)] = look
    return output


_READABLE_TEXT_PHASES = ("start", "freeze", "end")
_READABLE_TEXT_PHASE_LABELS = {
    "start": "起点",
    "freeze": "中间定格",
    "end": "终点",
}


def _readable_text_for_phase(value, phase):
    """Return only the readable-text facts belonging to ``phase``.

    ``readable_text`` used to describe an entire take.  New contracts may use
    ``readable_text.phases`` so a start clock, middle game card and unreadable
    end screen do not get composited into every generated frame.  Once phase
    buckets exist, a missing bucket means "no phase-specific readable text";
    global legacy fields are not inherited because doing so would recreate the
    cross-phase leak this schema is designed to prevent.
    """
    value = value if isinstance(value, dict) else {}
    phases = value.get("phases")
    if not isinstance(phases, dict):
        return dict(value)
    selected = phases.get(_text(phase).lower())
    if not isinstance(selected, dict):
        return {"required": False}
    return dict(selected)


def _readable_text_has_phases(value):
    return isinstance(value, dict) and isinstance(value.get("phases"), dict)


def readable_text_required(value):
    """Only treat an explicit on-screen whitelist as an image text asset.

    Dialogue/subtitle metadata occasionally arrives as ``required=true`` with an
    empty whitelist. Sending that to an image model as "字幕 / 白名单为空"
    invites invented text even though the production profile forbids subtitles.
    """
    value = value if isinstance(value, dict) else {}
    phases = value.get("phases")
    if isinstance(phases, dict):
        return any(
            readable_text_required(phases.get(phase))
            for phase in _READABLE_TEXT_PHASES)
    if not value.get("required"):
        return False
    carrier = _text(value.get("carrier"))
    if any(label in carrier for label in NON_PICTURE_TEXT_CARRIERS):
        return False
    return bool(sanitize_text_whitelist(value.get("whitelist") or []))


def _readable_text_rule(value):
    """Render one phase/legacy readable-text card without device confusion."""
    value = value if isinstance(value, dict) else {}
    required = readable_text_required(value)
    carrier = _text(value.get("carrier"), "指定载体")
    if not required:
        if _text(value.get("carrier")):
            return (
                f"{carrier}在本阶段不要求可读，禁止生成可识别文字；"
                "不新增任何其他画面文字、字幕、Logo或水印")
        return "无画面文字、无字幕、无Logo、无水印"

    whitelist = "、".join(sanitize_text_whitelist(
        value.get("whitelist") or [])) or "白名单"
    layout = _text(value.get("layout"))
    text_style = _text(value.get("style"))
    perspective = _text(value.get("perspective"))
    presentation = "；".join(filter(None, (
        f"版式/位置:{layout}" if layout else "",
        f"字体/颜色/层级:{text_style}" if text_style else "",
        f"透视/反光:{perspective}" if perspective else "",
    )))
    carrier_lower = carrier.lower()
    # A phone/tablet is a hand-held display, not a laptop.  Check the specific
    # device first; the old generic ``屏幕`` branch mislabeled ``手机屏幕`` as a
    # computer and then introduced keyboard/desk geometry into phone shots.
    handheld = any(token in carrier_lower for token in (
        "手机", "锁屏", "平板", "tablet", "mobile", "phone"))
    computer = (not handheld and any(
        token in carrier_lower for token in (
            "电脑", "笔记本", "显示器", "显示屏", "屏幕", "laptop",
            "computer")))
    if computer:
        return (
            f"电脑屏幕必须打开并清晰显示白名单原文:{whitelist}；"
            + (presentation + "；" if presentation else "")
            + "屏幕不是冷白光效/空白占位面，禁止随机乱码、模糊色块和黑白占位；"
            + "屏幕外无字幕、Logo、水印和无关文字")
    return (
        f"{carrier}内文字只保持原样:{whitelist}；"
        + (presentation + "；" if presentation else "")
        + "禁止新增文字")


def _readable_text_timeline_rule(
        value, frame_props, prop_registry):
    """Render independent start/freeze/end text beats for a video."""
    value = value if isinstance(value, dict) else {}
    phases = value.get("phases")
    if not isinstance(phases, dict):
        return _readable_text_rule(value)
    beats = []
    for phase in _READABLE_TEXT_PHASES:
        if phase not in phases:
            continue
        current = _readable_text_for_phase(value, phase)
        label = _READABLE_TEXT_PHASE_LABELS[phase]
        visible = _readable_carrier_visible_at_phase(
            current, frame_props, phase, prop_registry)
        if readable_text_required(current) and not visible:
            carrier = _text(current.get("carrier"), "文字载体")
            rule = f"{carrier}隐藏/遮挡/不在画面，不生成该阶段文字"
        else:
            rule = _readable_text_rule(current)
        beats.append(f"{label}:{rule}")
    if not beats:
        return "无画面文字、无字幕、无Logo、无水印"
    return (
        "文字时间线（各阶段按时间先后独立执行）:"
        + "；".join(beats)
        + "；禁止把不同阶段的文字、屏幕状态或版式同时塞进同一帧")


def _spatial_anchor_count(shot):
    """本镜必须同框呈现的空间锚点数:人物 + 不在任何人手上的可见道具。

    「沈眉站在书案右侧、银铃静止在书案上」这类空间指向,取景要同时
    装下人和那件离身道具;特写只框得住人物本身,合同必然执行不了。
    只数「可见且无人持有」的道具,握在手里的道具随人入画不额外占位。
    """
    shot = shot or {}
    characters = [
        name for name in (shot.get("characters") or []) if _text(name)]
    anchors = 1 if characters else 0
    seen = set()
    for row in (shot.get("frame_props") or []):
        if not isinstance(row, dict):
            continue
        if _text(row.get("visibility")).lower() not in ("visible", "occluded"):
            continue
        holder = _text(row.get("holder")).lower()
        if holder and holder not in ("none", "无", "无人", "无人持有", "-"):
            continue  # 握在手上,随人入画
        prop_id = _text(row.get("prop_id"))
        if prop_id and prop_id not in seen:
            seen.add(prop_id)
    return anchors + len(seen)


def _camera(shot, visible_count=None):
    dimensions = shot.get("five_dimensions") or {}
    design = dimensions.get("camera_design") or {}
    contract = shot.get("shot_contract") or {}
    raw = _text(shot.get("camera"))

    def explicit(tokens):
        """Return the earliest explicit camera token in the camera sentence.

        Repaired camera prose often starts with the executable scale and then
        describes depth staging (for example ``28mm全景，沈砚舟在近景南侧``).
        The previous priority lookup saw the later ``近景`` first and silently
        rewrote the shot back to a medium/close view.  Source order is the only
        safe precedence here; longer compound tokens still win at the same
        position.
        """
        matches = []
        for token, value in tokens:
            for match in re.finditer(re.escape(token), raw):
                matches.append((match.start(), -len(token), value))
        return min(matches)[2] if matches else ""

    def explicit_movement():
        """Parse positive camera motion without reviving negated clauses.

        ``固定，不环绕`` used to become ``环绕`` because the generic token
        search did not distinguish a prohibition from an instruction.  Also,
        actor locks such as ``顾明昭固定为屏幕右锚点`` are staging, not camera
        motion, so a bare ``固定`` is accepted only in camera/movement syntax.
        """
        if re.search(r"(?:无|不)运镜|机位(?:全程)?锁定|锁定机位", raw):
            return "固定"
        candidates = []
        tokens = (
            ("急推", "急推"), ("缓推", "缓推"), ("推近", "推"),
            ("上摇", "上摇"), ("下摇", "下摇"), ("环绕", "环绕"),
            ("跟拍", "跟拍"), ("拉远", "拉"), ("横移", "移"),
            ("固定", "固定"),
        )
        for token, value in tokens:
            for match in re.finditer(re.escape(token), raw):
                start, end = match.span()
                prefix = raw[max(0, start - 10):start]
                suffix = raw[end:end + 6]
                if re.search(
                        r"(?:不|无|未|禁止|不得|严禁|避免|没有|不再)\s*$",
                        prefix):
                    continue
                if token == "固定":
                    camera_context = (
                        suffix.startswith("机位")
                        or bool(re.search(
                            r"(?:摄影机|镜头|机位)[^，。；]{0,12}$", prefix))
                        or bool(re.search(
                            r"(?:^|[，。；、])\s*固定(?:$|[，。；、])",
                            raw)))
                    actor_lock = suffix.startswith(
                        ("为", "在", "成片", "画面", "屏幕", "位置"))
                    if actor_lock or not camera_context:
                        continue
                candidates.append((start, -len(token), value))
        return min(candidates)[2] if candidates else ""

    # The author/director's explicit current-shot camera text is authoritative.
    # Five-dimension defaults may fill omissions, but must never contradict it.
    raw_scale = explicit((
        ("大特写", "大特写"), ("特写", "特写"),
        # Compound scale must be matched before its ``近景`` suffix.
        ("中近景", "中近景"), ("近景", "近景"),
        ("中景", "中景"), ("全景", "全景"), ("远景", "远景"),
    ))
    raw_angle = explicit((
        ("顶拍", "顶拍"), ("顶视", "顶拍"), ("鸟瞰", "顶拍"),
        ("微俯", "俯拍"),
        ("俯拍", "俯拍"), ("高机位", "俯拍"), ("高角度", "俯拍"),
        ("仰拍", "仰拍"), ("低机位", "仰拍"), ("低角度", "仰拍"),
        ("平视", "平视"),
    ))
    raw_position = explicit((
        ("过肩", "过肩"), ("背面", "背面"), ("背后", "背面"),
        ("斜侧", "侧面"),
        ("侧面", "侧面"), ("侧脸", "侧面"), ("正面", "正面"),
    ))
    raw_movement = explicit_movement()
    raw_lens_match = re.search(
        r"(?<!\d)(\d{2,3})\s*mm(?=$|[^A-Za-z])", raw, re.I)
    raw_lens = (
        f"{raw_lens_match.group(1)}mm" if raw_lens_match else "")
    raw_composition = explicit((
        ("中心对称", "中心对称"), ("框中框", "框中框"),
        ("前景遮挡", "前景遮挡"), ("水平分割", "水平分割"),
        ("对角线", "对角线"), ("三分", "三分法"),
        ("引导线", "引导线"), ("留白", "留白"),
    ))
    def _strip_aspect(value):
        # 画幅比例(16:9/2.35:1…)不属于镜头语言,唯一画幅执行值是
        # 项目 aspect 字段;残留在镜头字段里会与之同级互斥并熔断。
        cleaned = re.sub(
            r"\d+(?:\.\d+)?\s*[:：]\s*\d+(?:\.\d+)?", "", str(value or ""))
        return re.sub(r"[、，;；]{2,}", "，", cleaned).strip(" ，、;；·")

    resolved_scale = _strip_aspect(_text(
        raw_scale or contract.get("景别") or design.get("shot_scale"),
        "按分镜"))
    # 可行性门禁(编译侧兜底):已保存的旧分镜可能带着「特写×N人全见」
    # 这类同级互斥合同;裁决体系(条款(c))对同级互斥只能熔断,唯一
    # 可行方向是编译时把景别升到装得下人数合同的档位。
    executed_scale, capacity_note = enforce_scale_capacity(
        resolved_scale, visible_count)
    # 空间锚点:本镜要同框呈现「人物 + 不在其手上的道具」时,紧景别
    # 框不住两者的位置关系,模型只能拉宽再被质检判景别不符。
    # A repaired tabletop/detail composition may deliberately place two
    # cropped faces/hands and one small loose prop in the same local close-up.
    # The generic anchor rule assumes full bodies plus a separate scene prop
    # and would silently turn ``135mm局部近景`` back into ``35mm中景``. Honour
    # this explicit, already-Codex-repaired framing while retaining the safety
    # upgrade for ordinary close-ups.
    repaired_local_closeup = bool(
        shot.get("prompt_block_repair")
        and executed_scale in {"大特写", "特写", "近景", "中近景"}
        and any(token in raw for token in (
            "局部近景", "局部特写", "桌面特写", "手部特写")))
    if repaired_local_closeup:
        anchor_note = ""
    else:
        executed_scale, anchor_note = enforce_spatial_anchor_scale(
            executed_scale, _spatial_anchor_count(shot))
    notes = [note for note in (capacity_note, anchor_note) if note]
    lens = _strip_aspect(_text(
        raw_lens or design.get("lens") or contract.get("焦段")))
    if notes:
        # 长焦(≥85mm)绑定近景与特写;景别升档后仍写长焦会再造一对
        # 矛盾(合同要中景、焦段却宣告特写观感),模型两头不讨好。
        focal = re.match(r"\s*(\d+)", lens)
        if focal and int(focal.group(1)) >= 85:
            lens = "35mm"
    composition = _strip_aspect(_text(
        raw_composition or contract.get("构图")
        or design.get("composition"), "主体清楚"))
    composition, composition_note = enforce_composition_scale(
        executed_scale, composition)
    if composition_note:
        notes.append(composition_note)
    position = _strip_aspect(_text(
        raw_position or contract.get("机位") or design.get("camera_position")))
    single_subject_over_shoulder = (
        position == "过肩"
        and visible_count == 1
        and any(token in raw + " " + _text(shot.get("description"))
                for token in ("背对镜头", "肩后", "后脑", "背影")))
    if single_subject_over_shoulder:
        position_note = ""
    else:
        position, position_note = enforce_position_capacity(
            position, visible_count)
    if position_note:
        notes.append(position_note)
    result = {
        "景别": executed_scale,
        "角度": _strip_aspect(_text(
            raw_angle or contract.get("角度") or design.get("angle"),
            "保持轴线")),
        "焦段": lens,
        "机位": position,
        "运镜": _strip_aspect(_text(
            raw_movement or contract.get("运镜") or design.get("movement"),
            "固定")),
        "动机": _text(design.get("movement_motivation"), "服务主体动作"),
        "构图": composition,
    }
    if notes:
        # 渲染按键取值,本键只进合同 JSON 留审计,不进提示词正文。
        result["容量修正"] = "；".join(notes)
    return result


def shot_local_scene(shot, fallback=""):
    """Resolve only the current shot's visible place/era.

    Structured scene data is authoritative: a shot-local location wins first,
    followed by the caller's script ``scene_no`` lookup.  Keyword inference is
    only a compatibility path for old storyboards that have neither.  That
    compatibility path must ignore negative clauses: ``不得出现明代宫殿`` is
    not evidence that the visible scene is a Ming palace.
    """
    shot = shot or {}
    explicit = _text(
        shot.get("location") or shot.get("scene_location")
        or shot.get("scene_context") or shot.get("world_state"))
    if explicit:
        return explicit
    authoritative = _text(fallback)
    if authoritative:
        return authoritative
    text = " ".join(_text(value) for value in (
        shot.get("description"), shot.get("action"), shot.get("prompt"),
    ) if _text(value))
    hints = (
        ("现代酒店", ("现代高档酒店", "现代酒店", "酒店走廊", "酒店房间")),
        ("现代书房", ("现代书房", "现代书桌")),
        ("现代办公室", ("现代办公室", "现代办公")),
        ("现代都市", ("现代都市", "都市街道", "现代街道")),
        ("明代东宫寝殿", ("东宫", "寝殿", "太子殿")),
        ("明代宫殿内景", ("明代宫殿", "宫殿", "紫禁城")),
    )
    for label, tokens in hints:
        if _contains_affirmed_scene_hint(text, tokens):
            return label + ("（闪回）" if "闪回" in text and "现代" in label
                            else "")
    return "按场景基准图"


_SCENE_NEGATION_MARKERS = (
    "不得出现", "禁止出现", "严禁出现", "不要出现", "不可出现",
    "不得", "禁止", "严禁", "不要", "不可", "不出现", "不包含",
    "不表示", "不存在", "排除", "删除", "移除", "避免", "没有",
)

_SCENE_AFFIRMATION_RESETS = (
    "改为", "应为", "实际为", "场景为", "画面是", "而是", "位于",
    "切到", "进入", "来到", "回到",
)


def _scene_hint_is_negated(clause, index):
    """Check negation scoped to the comma-delimited phrase before a hint.

    Chinese exclusion lists commonly use ``、`` (``禁止宫殿、宫灯``), so it
    intentionally does not end the scope.  A comma does: this keeps an earlier
    delivery rule such as ``无字幕、水印，画面是现代酒店`` from suppressing
    the later, affirmative location.
    """
    prefix = clause[:index]
    local_prefix = re.split(r"[，,]", prefix)[-1]
    last_negative = max(
        (local_prefix.rfind(marker) for marker in _SCENE_NEGATION_MARKERS),
        default=-1,
    )
    bare_none = re.search(
        r"(?:^|[：:\s])(?:场景|画面)?无(?:任何|相关|一切|其他)?"
        r"[^，,、]{0,10}$",
        local_prefix,
    )
    if bare_none:
        last_negative = max(last_negative, bare_none.start())
    last_reset = max(
        (local_prefix.rfind(marker) for marker in _SCENE_AFFIRMATION_RESETS),
        default=-1,
    )
    return last_negative >= 0 and last_negative > last_reset


def _contains_affirmed_scene_hint(text, tokens):
    """Return true only for a positive, visible-scene keyword mention."""
    for clause in re.split(r"[。！？!?；;\n]+", _text(text)):
        for token in tokens:
            start = 0
            while True:
                index = clause.find(token, start)
                if index < 0:
                    break
                if not _scene_hint_is_negated(clause, index):
                    return True
                start = index + len(token)
    return False


_OBJECT_NEGATION_MARKERS = (
    "不得出现", "禁止出现", "严禁出现", "不要出现", "不可出现",
    "不得", "禁止", "严禁", "不要", "不可", "不出现", "不包含",
    "不表示", "不存在", "排除", "删除", "移除", "避免", "没有", "无",
)


def _mentions_present_object(text, tokens):
    """Return whether a shot clause asserts that an object is present.

    Repair prompts often say things such as ``不得出现笔记本电脑、屏幕``.
    Treating that negative list as evidence that the shot contains a laptop
    re-injects the complete laptop-use contract on every repair round and makes
    the input self-contradictory.  Work clause by clause and ignore an object
    mention when a nearby prefix negates its presence.
    """
    for clause in re.split(r"[。！？!?；;\n]+", _text(text)):
        clause = clause.strip()
        if not clause:
            continue
        for token in tokens:
            start = 0
            while True:
                index = clause.find(token, start)
                if index < 0:
                    break
                # Delivery-view wording is not an in-scene telephone.  In
                # particular, historical-shot repairs use ``手机端可读性`` to
                # explain a restrained prop-size tolerance; that must not
                # inject a hand-held screen relationship into the scene.
                tail = clause[index:index + 16]
                if token == "手机" and tail.startswith((
                        "手机端", "手机竖屏", "手机观看", "手机播放")):
                    start = index + len(token)
                    continue
                prefix = clause[max(0, index - 24):index]
                if not any(marker in prefix
                           for marker in _OBJECT_NEGATION_MARKERS):
                    return True
                start = index + len(token)
    return False


def _dialogue_gaze_clause(description):
    """Keep the 180-degree lock without overriding an explicit gaze target."""
    text = _text(description)
    eye_contact = re.search(
        r"对视|看向对方|注视对方|凝视对方|视线.{0,8}(对方|双眼)", text)
    gaze_target = re.search(
        r"视线|注视|凝视|目光|看向|盯住|盯着|低头看|抬眼看|"
        r"共同看|共同望|只看", text)
    if gaze_target and not eye_contact:
        return (
            "双方身体朝向服从本镜动作，视线严格落在本镜明确的动作目标上，"
            "不得强制改成互看双眼，且画内方向互补；")
    return "双方身体朝向彼此，视线精确落在对方双眼附近且画内方向互补；"


def _explicit_screen_side_names(shot, description):
    """Resolve explicit final-frame left/right before stale blocking labels.

    A repaired shot can intentionally change composition while its top-down
    blocking document still carries the earlier screen-side names.  The latest
    shot text is the executable contract, so phrases such as ``沈砚舟屏幕左前``
    must override those historical labels.
    """
    names = [
        _text(value) for value in (shot or {}).get("characters", [])
        if _text(value)
    ]
    text = _text(description)
    resolved = {"left": "", "right": ""}
    for side, token in (("left", "左"), ("right", "右")):
        for name in names:
            escaped = re.escape(name)
            if (re.search(
                    rf"{escaped}[^，。；！？,;!?]{{0,20}}屏幕{token}", text)
                    or re.search(
                        rf"屏幕{token}(?:侧|边缘|前景|后景|前|后)?"
                        rf"(?:为|是|：|:)?{escaped}", text)):
                resolved[side] = name
                break
    return resolved["left"], resolved["right"]


def _static_terminal_physical_rules(rules):
    """Remove timeline/camera-motion clauses from a still-image contract."""
    stable = []
    camera_terms = ("摄影机", "镜头", "机位", "camera")
    camera_motion = (
        "跟", "推", "拉", "摇", "移", "升降", "环绕", "变焦", "运动",
    )
    subject_motion = (
        "后退", "前进", "走向", "走到", "靠近", "离开", "起身", "坐下",
        "站起", "转身", "伸手", "收手", "抬手", "放下", "拿起", "掀开",
        "打开", "关闭", "逐渐", "随后", "然后", "过程中",
    )
    for rule in rules:
        # A single stored rule can mix actor state and camera movement. Split
        # it so stable terminal relationships survive the still conversion.
        segments = [
            value.strip() for value in re.split(r"[。；;，,\n]+", _text(rule))
            if value.strip()
        ]
        kept = []
        for segment in segments:
            lowered = segment.lower()
            if (any(token in lowered for token in camera_terms)
                    and any(token in segment for token in camera_motion)):
                continue
            # Explicitly completed states remain useful. Bare movement prose
            # belongs to the video timeline; frame_target carries the still's
            # authoritative end state.
            if (any(token in segment for token in subject_motion)
                    and not any(token in segment for token in (
                        "已", "站稳", "停住", "保持", "不动", "静止"))):
                continue
            kept.append(segment)
        if kept:
            stable.extend(kept)
    return stable


def _static_observable_state_rules(value):
    """Extract only support/pose facts from a frozen-state sentence.

    The core frame target may legitimately say how the character arrived
    (``已后退半步站稳``).  That provenance is useful in the audit/core line,
    but the physical section of a still must not repeat motion verbs.
    """
    motion_tokens = (
        "后退", "前进", "走向", "走到", "走近", "靠近", "离开", "进入",
        "起身", "坐下", "站起", "转身", "伸手", "收手", "抬手", "放下",
        "拿起", "掀开", "打开", "关闭", "移动", "移向", "递出", "接过",
    )
    return [
        segment.strip() for segment in re.split(r"[。；;\n]+", _text(value))
        if segment.strip()
        and not any(token in segment for token in motion_tokens)
    ]


def _static_rule_supported_by_target(rule, target_state):
    """Conservatively retain a stable stored rule only when target echoes it."""
    def bigrams(value):
        compact = "".join(re.findall(r"[\u4e00-\u9fff]+", _text(value)))
        return {compact[index:index + 2]
                for index in range(max(0, len(compact) - 1))}

    rule_grams = bigrams(rule)
    target_grams = bigrams(target_state)
    return bool(
        rule_grams and target_grams
        and len(rule_grams & target_grams) / len(rule_grams) >= 0.45)


def build_physical_contract(shot, *, media="video", target_phase=""):
    """Build a short, shot-local physical/spatial contract.

    The image model must receive object-user-camera relationships explicitly;
    a generic "natural proportions" negative prompt is not enough to catch
    impossible setups such as a laptop facing the camera while its user sits
    behind the display.  Only inspect current-shot fields here, never the full
    episode/story bible.
    """
    shot = shot or {}
    explicit = (shot.get("physical_contract")
                or shot.get("physical_logic")
                or shot.get("spatial_logic"))
    spatial_relations = _normalize_spatial_relations(
        shot, explicit if isinstance(explicit, dict) else None)
    if isinstance(explicit, dict):
        raw_rules = explicit.get("rules") or explicit.get("constraints") or []
        if isinstance(raw_rules, str):
            raw_rules = [raw_rules]
        rules = [_text(value) for value in raw_rules if _text(value)]
        objects = explicit.get("objects") or explicit.get("object_relations") or []
        if isinstance(objects, str):
            objects = [objects]
    else:
        rules = [_text(explicit)] if explicit else []
        objects = []
    static_rule_candidates = []
    if str(media or "video").lower() != "video":
        # Stored physical_contract rules describe the whole take and commonly
        # mix phases even after sentence splitting ("phone hidden" beside
        # "holds phone", "driver outside" beside "driver at the wheel").  A
        # still must be rebuilt only from its frame_target, visible frame_props
        # and scoped blocking below.  Keep the source contract for audit, but
        # never send its timeline prose to an image provider.
        static_rule_candidates = _static_terminal_physical_rules(rules)
        rules = []
        objects = []
    shot_contract = shot.get("shot_contract")
    shot_contract = shot_contract if isinstance(shot_contract, dict) else {}
    media_name = str(media or "video").lower()
    frame_target = shot.get("frame_target")
    declared_phase = _text(target_phase).lower()
    if not declared_phase and isinstance(frame_target, dict):
        declared_phase = _text(frame_target.get("phase")).lower()
    derived_target = None
    if media_name != "video" and declared_phase not in _READABLE_TEXT_PHASES:
        derived_target = _frame_target(
            shot, "image", _text(shot.get("frame_kind")))
        declared_phase = _text(derived_target.get("phase")).lower()
    phase_explicit = declared_phase in _READABLE_TEXT_PHASES
    readable_source = shot.get("readable_text") or {}
    raw_frame_props = _object_items(shot.get("frame_props"), "prop_id")
    raw_frame_props = (
        raw_frame_props if isinstance(raw_frame_props, (list, tuple)) else [])
    carrier_visibility_blocked = False
    phase_phone_visibility_blocked = False
    phase_laptop_visibility_blocked = False
    static_target_state = ""

    if media_name != "video" and phase_explicit:
        # A still is one frozen fact.  Do not mine the whole-take description
        # for a phone, driving action or bed pose that belonged to another
        # phase; use only its target state and currently visible props.
        readable = _readable_text_for_phase(readable_source, declared_phase)
        carrier_visible = _readable_carrier_visible_at_phase(
            readable, raw_frame_props, declared_phase,
            shot.get("prop_registry") or [])
        carrier_visibility_blocked = bool(
            _text(readable.get("carrier")) and not carrier_visible)
        carrier = (
            _text(readable.get("carrier")) if carrier_visible else "")
        target_state = _state_value(
            frame_target.get("state") if isinstance(frame_target, dict)
            else (derived_target or {}).get("state")
            or shot.get("frame_target_state"))
        static_target_state = target_state
        visible_prop_facts = []
        registry_names = {
            _text(item.get("prop_id")): _text(item.get("name"))
            for item in (shot.get("prop_registry") or [])
            if isinstance(item, dict)}
        phase_phone_rows = []
        phase_laptop_rows = []
        for item in raw_frame_props:
            if not isinstance(item, dict):
                continue
            if _normalize_prop_phase(item.get("phase")) != declared_phase:
                continue
            prop_id = _text(item.get("prop_id"))
            prop_label = " ".join(filter(None, (
                prop_id, _text(item.get("name")),
                registry_names.get(prop_id, "")))).lower()
            if any(token in prop_label for token in (
                    "手机", "phone", "mobile", "平板", "tablet")):
                phase_phone_rows.append(item)
            if any(token in prop_label for token in (
                    "电脑", "笔记本", "computer", "laptop", "显示器")):
                phase_laptop_rows.append(item)
            if _text(item.get("visibility"), "visible").lower() != "visible":
                continue
            visible_prop_facts.append(" ".join(filter(None, (
                _text(item.get("name")) or registry_names.get(prop_id, ""),
                _text(item.get("physical_state")),
                _text(item.get("holder")),
                _text(item.get("location") or item.get("position")),
            ))))
        phase_phone_visibility_blocked = bool(
            phase_phone_rows and not any(
                _text(item.get("visibility"), "visible").lower() == "visible"
                for item in phase_phone_rows))
        phase_laptop_visibility_blocked = bool(
            phase_laptop_rows and not any(
                _text(item.get("visibility"), "visible").lower() == "visible"
                for item in phase_laptop_rows))
        description = " ".join(filter(None, (
            target_state,
            " ".join(visible_prop_facts),
            _text(shot_contract.get("构图")),
        )))
    else:
        readable = readable_source
        if media_name == "video" and _readable_text_has_phases(readable_source):
            visible_carriers = []
            for phase in _READABLE_TEXT_PHASES:
                current = _readable_text_for_phase(readable_source, phase)
                if (phase in (readable_source.get("phases") or {})
                        and _readable_carrier_visible_at_phase(
                            current, raw_frame_props, phase,
                            shot.get("prop_registry") or [])):
                    value = _text(current.get("carrier"))
                    if value and value not in visible_carriers:
                        visible_carriers.append(value)
            carrier = " ".join(visible_carriers)
        else:
            carrier = _text(readable.get("carrier"))
        description = " ".join(_text(value) for value in (
            shot.get("description"), shot.get("action"),
            shot_contract.get("画面内容描述"),
            shot_contract.get("构图"),
        ) if _text(value))
    scene_text = _text(
        shot.get("location") or shot.get("scene_location")
        or shot.get("scene_context"))
    object_text = f"{description} {carrier} {scene_text}".lower()
    generic = (
        "人物、镜头与道具的前后左右关系必须真实成立；道具服从重力并与桌面/地面/手部"
        "保持自然接触；人物朝向、视线和手部动作必须指向实际使用对象；禁止漂浮、穿模、"
        "镜像反向、无支撑或无法完成动作的姿势。"
    )
    if not any(_text(rule).rstrip("。") == generic.rstrip("。")
               for rule in rules):
        rules.insert(0, generic)
    if media_name != "video" and static_target_state:
        # Re-state exactly one observable freeze fact after discarding the
        # stored whole-take physical timeline.  This preserves useful stable
        # support/pose facts without resurrecting another phase's location,
        # prop or gaze instructions.
        rules.extend(
            f"当前静态物理状态：{state_rule}"
            for state_rule in _static_observable_state_rules(
                static_target_state))
        rules.extend(
            rule for rule in static_rule_candidates
            if _static_rule_supported_by_target(rule, static_target_state))
    if str(media or "video").lower() == "video":
        motion_rule = (
            "人和物品道具的运动轨迹必须符合真实物理世界的运动轨迹和逻辑；"
            "人物动作服从重力、惯性、关节活动范围、重心与支撑关系；物品位移、"
            "旋转、碰撞、接触和交接连续且有明确受力来源，前后状态一致")
        if not any("运动轨迹必须符合真实物理世界" in _text(rule)
                   for rule in rules):
            rules.insert(1, motion_rule)
    vehicle_interior = any(token in object_text for token in (
        "车内", "轿车内", "汽车内", "驾驶座", "驾驶位", "副驾驶",
        "方向盘", "中控台", "车载中控",
    ))
    if vehicle_interior:
        rules.append(
            "现代乘用车车内结构完整：驾驶座与副驾驶座成对存在且方向一致，"
            "两座均有座垫、靠背和头枕；方向盘位于驾驶座前方，中控台位于"
            "两座前方中央，车门、仪表台、挡风玻璃和座椅不得缺失、互换、"
            "重叠或悬空。镜头只可裁出画面，不能把未入镜结构解释成车辆不存在。")
        objects.append(
            "现代车内：驾驶座/方向盘↔中控台↔副驾驶座，座椅头枕结构完整")
    seatbelt_worn = bool(re.search(
        r"(?:系|系着|系好|系上|系住|佩戴|扣好|扣上|斜跨)[^，,。；]{0,6}安全带|"
        r"安全带[^，,。；]{0,6}(?:系着|系好|已系|扣好|斜跨)",
        object_text,
    ))
    if seatbelt_worn:
        rules.append(
            "三点式安全带路径：肩带从乘员座椅外侧上方固定点出发，斜跨胸口"
            "到身体内侧锁扣；腰带从外侧下锚点横跨左右髋部接入同一锁扣。"
            "织带贴合衣物表面但不勒入、不穿过身体、手臂或座椅，锁扣必须"
            "落在座椅内侧且受力路径连续。")
        objects.append("三点式安全带：外侧上锚点↔胸口↔内侧锁扣+髋部腰带")
    # ``屏幕左/右`` is the established staging vocabulary for image-plane
    # position, not a display device.  A bare ``屏幕`` therefore cannot prove
    # that a laptop exists; require an actual device noun or display-specific
    # phrase.  ``readable_text.carrier=屏幕`` remains an explicit presence
    # declaration and is handled separately below.
    laptop_tokens = (
        "笔记本", "电脑", " laptop", "显示器", "显示屏",
        "屏幕内容", "屏幕页面", "屏幕正面", "屏幕背面", "屏幕上的",
    )
    laptop_carrier_tokens = (*laptop_tokens, "屏幕")
    phone_tokens = ("手机", "平板", "tablet")
    handheld_present = (
        any(token in carrier.lower() for token in phone_tokens)
        or _mentions_present_object(object_text, phone_tokens))
    handheld_use = (
        handheld_present
        and not carrier_visibility_blocked
        and not phase_phone_visibility_blocked
        and (
        media_name == "video"
        or bool(carrier)
        or bool(re.search(
            r"(?:手持|拿着|握着|举着|持有|查看|低头看|看向|展示|递出|"
            r"接过)[^，,。；]{0,10}(?:手机|平板)|"
            r"(?:手机|平板)[^，,。；]{0,10}(?:拿|握|举|看|展示|递|接)",
            object_text))))
    laptop_present = (
        not phase_laptop_visibility_blocked
        and (any(token in carrier.lower() for token in laptop_carrier_tokens)
             or _mentions_present_object(object_text, laptop_tokens)))
    if media_name != "video" and phase_explicit:
        # A persisted video physical contract may already contain relations for
        # other beats.  Strip those derived device-use rows before rebuilding
        # this still's one-phase contract.
        if not handheld_use:
            rules = [
                rule for rule in rules
                if "手持屏幕关系" not in _text(rule)]
            objects = [
                value for value in objects
                if "手持屏幕" not in _text(value)]
        if not laptop_present:
            rules = [
                rule for rule in rules
                if "电脑使用关系" not in _text(rule)]
            objects = [
                value for value in objects
                if "笔记本电脑" not in _text(value)]
    if handheld_use:
        rules.append(
            "手持屏幕关系：屏幕正面必须朝向实际使用者或被展示的观看者；"
            "若人物正在查看，屏幕只可向摄影机小角度倾斜，由使用者同侧斜角"
            "或越肩机位保证可读，禁止为了文字清晰把屏幕完全翻向镜头、背对"
            "使用者或造成手腕反折。若当前定格还要求人物注视床上、门口或"
            "其他明确对象，视线只落在该对象，手机保持合理手持方向；不得让"
            "双眼同时注视两个目标。手指与机身接触自然，手腕、手臂和持握"
            "方向连续。"
        )
        objects.append("手持屏幕：使用者/观看者↔屏幕正面")
    elif laptop_present:
        rules.append(
            "电脑使用关系：屏幕正面、键盘和使用者必须位于同一使用侧；键盘朝向使用者，"
            "屏幕与底座由铰链连接并由桌面支撑；人物视线落在屏幕可见区域。若需要看清屏幕文字，"
            "镜头必须采用使用者同侧的越肩或侧面机位，禁止人物坐在屏幕背面却看到屏幕正面。"
        )
        objects.append("笔记本电脑：使用者↔键盘/屏幕正面↔桌面支撑")
    # “马车”不是一个可独立运动的箱体。历史失败里模型只画车厢、漏掉
    # 马匹与挽具，画面虽像马车却没有动力来源。只在本镜明确表现移动/
    # 驾驶时强制完整动力链；停放、纯车厢内景、马已死亡或逃离等剧情
    # 状态不擅自补马，交给剧本当前事实决定。
    carriage_present = _mentions_present_object(object_text, ("马车",))
    carriage_motion = bool(re.search(
        r"马车.{0,8}(?:疾驰|奔驰|飞驰|驶入|驶出|行进|前行|移动|冲来|冲出|赶来)",
        object_text,
    )) or any(token in object_text for token in (
        "马车疾驰", "马车奔驰", "马车飞驰", "马车驶", "马车冲",
        "马车赶", "马车前行", "马车行进", "马车移动", "车轮滚动",
        "赶着马车", "驾驶马车", "驾着马车", "赶车", "策马驾车",
    ))
    horse_explicitly_absent = bool(re.search(
        r"(?:马|马匹).{0,4}(?:已经|已|都)?(?:死|逃|跑)", object_text,
    )) or any(token in object_text for token in (
        "无马", "没有马", "马已死", "马死了", "死马", "马跑了",
        "马匹逃走", "马已逃", "解下马匹", "卸下马匹",
    ))
    if carriage_present and carriage_motion:
        if horse_explicitly_absent:
            rules.append(
                "马车动力合同冲突：本镜同时要求马车移动且明确没有可用马匹；"
                "生成前必须由剧本明确其他真实动力来源，禁止让无动力车厢自行滑行。")
            objects.append("马车：移动要求与无马状态冲突，需先修剧情动力来源")
        else:
            rules.append(
                "移动马车完整动力链：画面必须存在与车体正确连接的马匹、"
                "辕杆/车衡和受力合理的挽具；车夫持缰控制，马匹朝行进方向，"
                "蹄步、车轮转动与车身位移方向一致。禁止只画车厢不画马、"
                "挽具断开、马匹朝反向或车体无动力自行移动。")
            objects.append("移动马车：马匹↔挽具/辕杆↔车体↔车夫缰绳")
    era_object_constraints = build_era_object_constraints(shot)
    rules.extend(era_object_constraints)
    for rule in era_object_constraints:
        object_name = (
            "油灯" if "油灯" in rule
            else "提灯" if "提灯" in rule
            else "烛台" if "烛台" in rule
            else "时代物件")
        objects.append(f"{object_name}：结构与材质服从当前时代物件锁定")
    blocking = shot.get("spatial_blocking") or {}
    if isinstance(blocking, dict):
        camera = blocking.get("camera") or {}
        if isinstance(camera, dict):
            camera_position = _text(camera.get("position")
                                     or camera.get("camera_position"))
            if camera_position:
                rules.append(f"空间调度机位：{camera_position}；保持与人物和道具关系一致。")
        actors = blocking.get("actors") or []
        if actors:
            positions = []
            for actor in actors:
                if not isinstance(actor, dict):
                    continue
                name = _text(actor.get("name") or actor.get("character"))
                raw_pos = actor.get("start") or actor.get("position")
                # Top-down/pixel/world coordinate dictionaries are useful to
                # the 3D validator but are not screen coordinates.  Rendering
                # ``{'x': 300, 'y': 330}`` into the image prompt made models and
                # QC treat them as a second, contradictory left/right contract.
                pos = "" if isinstance(raw_pos, dict) else _text(raw_pos)
                direction = _text(
                    actor.get("direction") or actor.get("facing"))
                if name and (pos or direction):
                    positions.append(f"{name}:{pos or '原位'}{('，朝向' + direction) if direction else ''}")
            if positions:
                rules.append("人物站位与朝向：" + "；".join(positions) + "。")
        dialogue = blocking.get("dialogue_continuity") or {}
        if isinstance(dialogue, dict) and dialogue.get("axis_id"):
            explicit_left, explicit_right = _explicit_screen_side_names(
                shot, description)
            left = explicit_left or _text(dialogue.get("screen_left_name"))
            right = explicit_right or _text(dialogue.get("screen_right_name"))
            side = _text(dialogue.get("camera_side"))
            coverage = _text(dialogue.get("coverage"))
            gaze_clause = _dialogue_gaze_clause(description)
            rules.append(
                "双人对话180°轴线合同："
                f"axis_id={dialogue.get('axis_id')}；"
                f"{left or '左侧角色'}固定成片屏幕左锚点，"
                f"{right or '右侧角色'}固定成片屏幕右锚点；"
                f"摄影机起点和终点都在演员连线的{side or '指定'}半平面；"
                f"本镜制式={coverage or '同侧对话镜头'}；"
                f"{gaze_clause}"
                "禁止交换左右、并排同向、看空气、随机第三人过肩、"
                "镜像翻转或无可见重建的越轴。"
            )
    rules = list(dict.fromkeys(rule for rule in rules if rule))
    objects = list(dict.fromkeys(_text(value) for value in objects if _text(value)))
    return {
        "schema": PHYSICAL_CONTRACT_SCHEMA,
        "required": True,
        "rules": rules,
        "objects": objects,
        "spatial_relations": spatial_relations,
    }


def _character_lines(shot):
    characters = list(shot.get("characters") or [])
    number_map = shot.get("character_number_map") or {}
    visuals = shot.get("character_visuals") or {}
    actor_by_name = {
        item.get("name"): actor_id
        for actor_id, item in number_map.items()
        if isinstance(item, dict) and item.get("name")
    }
    return [
        (
            f"{actor_by_name.get(name) or f'P{index:02d}'}={name}"
            + (f"（{_text(visuals.get(name))}）"
               if _text(visuals.get(name)) else "")
        )
        for index, name in enumerate(characters, 1)
    ]


def _character_identity_facts(shot):
    """Return structured identity facts for every registered actor.

    The rendered actor line remains optimized for image/video models, while
    these fields give the validator an exact source for identity completeness.
    Natural-language placeholders such as “以参考图为准” must not count as a
    known gender or age range.
    """
    characters = list(shot.get("characters") or [])
    raw = (
        shot.get("character_facts")
        if isinstance(shot.get("character_facts"), dict)
        else shot.get("character_background"))
    raw = raw if isinstance(raw, dict) else {}
    facts = []
    for name in characters:
        value = raw.get(name)
        value = value if isinstance(value, dict) else {}
        facts.append({
            "name": _text(name),
            "species": _text(value.get("species"), "人类"),
            "gender": _text(value.get("gender") or value.get("sex")),
            "age_range": _text(value.get("age_range")),
            "identity": _text(
                value.get("identity") or value.get("occupation")),
        })
    return facts


def build_composition_contract(shot):
    """Derive per-actor visibility duties from the current shot only.

    An over-shoulder dialogue has one primary face and one registered foreground
    counterpart. The foreground shoulder/back remains one expected character;
    it is not a duplicate or an extra body.
    """
    shot = shot or {}
    characters = list(shot.get("characters") or [])
    functional_figures, _ = _normalize_functional_figures(shot)
    functional_count = sum(
        int(item.get("count") or 0) for item in functional_figures)
    visible_count = len(characters) + functional_count
    dialogue = shot.get("dialogue") or {}
    contract = shot.get("shot_contract") or {}
    dimensions = shot.get("five_dimensions") or {}
    camera_design = dimensions.get("camera_design") or {}
    camera_text = " ".join(_text(value) for value in (
        shot.get("camera"), contract.get("角度"), contract.get("机位"),
        contract.get("构图"), camera_design.get("angle"),
        camera_design.get("camera_position"),
        camera_design.get("composition"),
    ) if _text(value))
    framing_text = " ".join(
        value for value in (camera_text, _text(shot.get("description")))
        if value)

    view_cues = {
        "back": ("背面", "背影", "背对", "肩后", "过肩前景", "后脑"),
        "front": ("正面", "正脸", "面向镜头", "面对镜头", "三分之四正面"),
        "profile": ("侧面", "侧脸", "严格侧身", "profile"),
    }
    actor_views = {}
    for name in characters:
        name_positions = [
            match.start() for match in re.finditer(
                re.escape(str(name)), framing_text, flags=re.IGNORECASE)]
        nearest = None
        for view, cues in view_cues.items():
            for cue in cues:
                for match in re.finditer(
                        re.escape(cue), framing_text, flags=re.IGNORECASE):
                    for position in name_positions:
                        distance = abs(match.start() - position)
                        candidate = (distance, view)
                        if distance <= 18 and (
                                nearest is None or candidate < nearest):
                            nearest = candidate
        if nearest:
            actor_views[name] = nearest[1]
    back_names = {
        name for name, view in actor_views.items() if view == "back"}
    front_names = {
        name for name, view in actor_views.items() if view == "front"}
    profile_names = {
        name for name, view in actor_views.items() if view == "profile"}
    single_over_shoulder = (
        "过肩" in framing_text
        and len(characters) == 1
        and visible_count == 1)
    over_shoulder = ("过肩" in framing_text and len(characters) >= 2) or (
        ("背面" in framing_text or "背影" in framing_text)
        and len(characters) >= 2
        and bool(dialogue.get("dialogue")))
    profile = any(
        word in camera_text for word in ("侧面", "侧脸", "profile"))
    back = any(
        word in camera_text for word in ("背面", "背对", "back view"))
    speaker = _text(dialogue.get("character"))
    primary = next(
        (name for name in characters if name in front_names),
        next(
            (name for name in characters if name not in back_names),
            speaker if speaker in characters else (
                characters[0] if characters else "")))
    actors = []
    for name in characters:
        if single_over_shoulder:
            expected_view = (
                "profile" if any(
                    cue in framing_text for cue in ("侧面", "侧脸"))
                else "back_or_over_shoulder")
            role = "single_subject"
            basis = (
                "profile_silhouette" if expected_view == "profile"
                else "back_silhouette")
            coverage = "partial"
        elif over_shoulder:
            is_primary = name == primary and name not in back_names
            expected_view = (
                "front_or_three_quarter" if is_primary
                else "back_or_over_shoulder")
            role = "primary_subject" if is_primary else "foreground_counterpart"
            basis = "face" if is_primary else "back_silhouette"
            coverage = "face_visible" if is_primary else "partial"
        elif name in back_names or (back and not front_names):
            expected_view, role = "back", "subject"
            basis, coverage = "back_silhouette", "body_visible"
        elif name in profile_names or (profile and not front_names):
            expected_view, role = "profile", "subject"
            basis, coverage = "profile_silhouette", "profile_visible"
        else:
            expected_view, role = "front_or_three_quarter", "subject"
            basis, coverage = "face", "face_visible"
        actors.append({
            "character": name,
            "role": role,
            "expected_view": expected_view,
            "coverage": coverage,
            "identity_basis": basis,
        })
    return {
        "composition_type": (
            "over_shoulder_dialogue" if over_shoulder
            else "single_subject_over_shoulder" if single_over_shoulder
            else "back_view" if back
            else "profile_view" if profile
            else "standard"),
        "expected_primary_count": (
            1 if over_shoulder and characters else len(characters)),
        "expected_visible_figure_count": visible_count,
        "actors": actors,
        "count_rule": (
            "前景半身背影/肩膀是已登记的对话者本人，只计作该角色1人，"
            "不得另算成新增人物或人物复制"
            if over_shoulder else
            "近机位肩背、头部和可见侧脸必须属于同一具连续身体，"
            "同一人物只出现一次，不得生成第二具身体"
            if single_over_shoulder else "每个可见人物只计一次"),
    }


def build_shot_prompt_contract(
        shot, *, location="", style="", references=None, mode="image"):
    """从已通过五维分镜的镜头构造可审计的结构化合同。

    不读取故事背景长文；只有当镜头实际需要时才保留场景、动作和状态，避免
    全局风格/角色经历与参考图抢控制权。
    """
    shot = shot if isinstance(shot, dict) else {}
    source_shot = shot
    authoritative_scene = shot_local_scene(shot, location)
    output_media, requested_mode = _normalize_mode(mode)
    target = _frame_target(shot, output_media, requested_mode)
    target = dict(target)
    joint_frames = bool(
        requested_mode == "frames"
        or _text(shot.get("frame_kind")).lower() == "frames")
    target_location = _text(target.get("location"))
    scene = (
        target_location
        if output_media == "image" and not joint_frames and target_location
        else authoritative_scene)
    scene_style = _style_for_scene(style or shot.get("style"), scene)
    characters, character_scope_issues, character_scope_declared, character_source = (
        _frame_character_scope(source_shot, target, output_media))
    functional_source, functional_scope_issues, functional_scope_declared, functional_source_name = (
        _frame_functional_scope(source_shot, target, output_media))
    phase_population_declared = (
        character_scope_declared or functional_scope_declared)
    if phase_population_declared:
        shot = _shot_with_character_scope(
            source_shot, characters, functional_source,
            target_state=target.get("state"))
    else:
        characters = list(shot.get("characters") or [])
    target["characters"] = list(characters)
    if functional_scope_declared:
        target["functional_figures"] = [
            dict(item) if isinstance(item, dict) else item
            for item in functional_source]
    target["state"] = _strip_modern_ancient_exclusions(
        target.get("state"), scene)
    # Never fall back to the raw storyboard prompt here. It may contain the
    # whole episode bible and unrelated scenes, which makes the provider blend
    # facts from other shots into this image.
    if output_media == "video":
        previous_contract = shot.get("prompt_contract")
        previous_action = (
            previous_contract.get("action")
            if isinstance(previous_contract, dict) else "")
        action_source = (
            shot.get("video_action") or shot.get("action")
            or previous_action or shot.get("description"))
    else:
        action_source = shot.get("description") or shot.get("action")
    action = _text(
        _strip_modern_ancient_exclusions(action_source, scene),
        "环境保持稳定，只执行自然微动")
    target_phase = (
        target.get("phase")
        if target.get("phase") in {"start", "end", "freeze"}
        else "end")
    frame_props, frame_prop_issues = _normalize_frame_props(
        shot, target_phase)
    all_prop_transitions, prop_transition_issues = (
        _normalize_prop_transitions(shot))
    timeline_contract = bool(output_media == "video" or joint_frames)
    # A transition is a relationship between phases, not an observable fact
    # inside one frozen image.  Keep it only for motion / paired-frame timeline
    # contracts; otherwise it injects future actors and actions into the still.
    prop_transitions = (
        all_prop_transitions if timeline_contract else [])
    character_conditions = _character_condition_map(
        shot, characters, action, target_phase)
    functional_figures, population_issues = _normalize_functional_figures(
        shot)
    population_issues.extend(character_scope_issues)
    population_issues.extend(functional_scope_issues)
    registered_count = len(characters)
    functional_count = sum(
        int(item.get("count") or 0) for item in functional_figures)
    visible_count = registered_count + functional_count
    raw_functional_items = shot.get("functional_figures") or []
    if isinstance(raw_functional_items, dict):
        raw_functional_items = [raw_functional_items]
    raw_functional_count = sum(
        item.get("count")
        for item in raw_functional_items
        if isinstance(item, dict)
        and isinstance(item.get("count"), int)
        and not isinstance(item.get("count"), bool)
        and item.get("count") > 0
    ) if isinstance(raw_functional_items, (list, tuple)) else 0
    dialogue = shot.get("dialogue") or {}
    readable_source = shot.get("readable_text") or {}
    readable = (
        readable_source if output_media == "video"
        else _readable_text_for_phase(readable_source, target_phase))
    readable_required = readable_text_required(readable)
    readable_carrier_visible = (
        output_media == "video"
        or _readable_carrier_visible_at_phase(
            readable, frame_props, target_phase,
            shot.get("prop_registry") or []))
    if output_media == "video" and _readable_text_has_phases(readable_source):
        text_rule = _readable_text_timeline_rule(
            readable_source, frame_props, shot.get("prop_registry") or [])
    elif readable_required and readable_carrier_visible:
        text_rule = _readable_text_rule(readable)
    elif readable_required:
        text_rule = (
            "当前静态目标phase中文字载体已隐藏、遮挡或不在画面；"
            "不生成该载体文字，也不新增任何其他画面文字、字幕、Logo或水印")
    else:
        text_rule = "无画面文字、无字幕、无Logo、无水印"
    refs = []
    for item in references or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_reference(item)
        if output_media == "image" and not joint_frames:
            raw_role = _text(item.get("role")).lower()
            raw_kind = _text(item.get("kind")).lower()
            person_bound_role = bool(
                normalized.get("role") in {
                    "identity", "wardrobe", "costume", "headwear"}
                or raw_role in {
                    "identity", "character_identity", "identity_detail",
                    "character_sheet", "wardrobe", "costume", "headwear"}
                or raw_kind in {
                    "identity", "character_identity", "identity_detail",
                    "character_sheet", "wardrobe", "costume", "headwear"})
            raw_name = _text(
                item.get("character") or item.get("attach_to")
                or item.get("name"))
            bound_character = raw_name.split(":", 1)[0].strip()
            if (person_bound_role and bound_character
                    and bound_character not in characters):
                continue
        refs.append(normalized)
    physical_shot = {
        **shot, "location": scene, "style": scene_style,
    }
    if target.get("explicit"):
        # For frame_targets.first/key/last, force the exact selected boundary
        # into physical compilation instead of leaving the shot-level keyframe
        # (usually the end/freeze target) in place.  Legacy fallback shots keep
        # their historical description/readable-text inference.
        physical_shot["frame_target"] = dict(target)
    physical = build_physical_contract(
        physical_shot, media=output_media, target_phase=target_phase)
    physical["frame_props"] = list(frame_props)
    physical["prop_transitions"] = list(prop_transitions)
    overlays = []
    for item in shot.get("narrative_overlays") or []:
        if not isinstance(item, dict):
            continue
        overlays.append({
            "kind": _text(item.get("kind"), "inner_persona_chibi"),
            "name": _text(item.get("name"), "内心Q版"),
            "host_character": _text(item.get("host_character"), "宿主"),
            "function": _text(item.get("function"), "inner_commentary"),
            "expression": _text(
                item.get("expression"),
                "夸张但清晰可读的Q版眉眼、嘴形、手势和身体弹性"),
            "proportion_style": "oversized_head_tiny_body",
            "total_height_in_heads": 1.8,
            "head_height_ratio": 0.58,
            "body_smaller_than_head": True,
            "action": _text(
                item.get("action"), "在宿主肩旁完成内心反应"),
            "dialogue": _text(item.get("dialogue")),
            "physical_presence": False,
            "counts_as_real_character": False,
            "included_in_spatial_blocking": False,
            "visible_to": "host_only",
            "inherit_signature_props": False,
            "host_mouth_closed": True,
        })
    overlays = overlays[:1]
    visible_entity_count = visible_count + len(overlays)
    declared_target = _declared_frame_target(source_shot, target)
    declared_scene_layout = (
        _text(declared_target.get("scene_layout"))
        if isinstance(declared_target, dict) else "")
    whole_scene_layout = _strip_modern_ancient_exclusions(
        shot.get("scene_layout"), scene)
    static_cross_location = bool(
        output_media == "image" and not joint_frames and target_location
        and _text(target_location) != _text(authoritative_scene))
    scene_layout = (
        declared_scene_layout
        if static_cross_location
        else declared_scene_layout or whole_scene_layout)
    declared_visible = (
        declared_target.get("visible_figure_count")
        if (phase_population_declared
            and isinstance(declared_target, dict)
            and "visible_figure_count" in declared_target)
        else None if phase_population_declared
        else shot.get("visible_figure_count"))
    legacy_declared_repaired = bool(
        isinstance(declared_visible, int)
        and not isinstance(declared_visible, bool)
        and raw_functional_count > functional_count
        and declared_visible == registered_count + raw_functional_count)
    if declared_visible is not None:
        if (not isinstance(declared_visible, int)
                or isinstance(declared_visible, bool)):
            population_issues.append("visible_figure_count 必须是整数")
        elif declared_visible != visible_count and not legacy_declared_repaired:
            population_issues.append(
                "人数声明冲突："
                f"visible_figure_count={declared_visible}，"
                f"登记角色={registered_count}，功能人物={functional_count}，"
                f"求和={visible_count}")
    population_text = " ".join(_text(value) for value in (
        shot.get("description"), shot.get("action"), shot.get("prompt"),
        shot.get("camera"),
        (shot.get("shot_contract") or {}).get("画面内容描述")
        if isinstance(shot.get("shot_contract"), dict) else "",
        (shot.get("shot_contract") or {}).get("构图")
        if isinstance(shot.get("shot_contract"), dict) else "",
    ) if _text(value))
    # 上诉庭固化(12次误杀里9次是本条):文本里已有明确数字人数
    # (「严格共7人」「4名弓兵」)时,模糊词只是修辞,歧义已被消除;
    # 只有全镜找不到任何明确数量时才判败。
    if (functional_count == 0
            and declared_visible is None
            and not _EXPLICIT_COUNT_RE.search(population_text)
            and any(token in population_text
                    for token in VAGUE_POPULATION_TOKENS)):
        population_issues.append(
            "镜头使用了几名/数名/多名/一群等模糊人数，但未声明"
            " functional_figures 的明确 count")
    composition = (
        dict(shot.get("composition_contract"))
        if isinstance(shot.get("composition_contract"), dict)
        else build_composition_contract({
            **shot, "functional_figures": functional_figures,
        }))
    composition["expected_visible_figure_count"] = visible_count
    medium = _normalize_visual_medium(shot, style)
    identity_facts_required = bool(
        shot.get("identity_facts_required")
        or isinstance(shot.get("character_facts"), dict)
        or isinstance(shot.get("character_background"), dict))
    contract = {
        "schema": PROMPT_CONTRACT_SCHEMA,
        # ``mode=shot`` is a legacy discriminator. Media/output semantics live
        # in the new structured ``output`` field.
        "mode": "shot",
        "output": {
            "media": output_media,
            "frame_phase": target["phase"],
            "temporal_policy": (
                "timeline" if output_media == "video"
                else "terminal_only"),
        },
        "frame_target": dict(target),
        "frame_target_state": target["state"],
        "frame_target_source": target["source"],
        "frame_target_fallback": bool(target["fallback"]),
        "frame_target_explicit": bool(target.get("explicit")),
        "frame_target_policy": {
            "name": _text(
                target.get("compatibility_policy"), "strict_explicit"),
            "allow_legacy_fallback": bool(
                target.get("legacy_compatibility")),
        },
        "frame_kind": _text(shot.get("frame_kind")),
        "subject": {
            # v1/v2 compatibility: count remains the number of identity-locked
            # named characters, not every visible human body.
            "count": registered_count,
            "registered_count": registered_count,
            "functional_count": functional_count,
            "visible_count": visible_count,
            "actors": _character_lines(shot),
            "identity_facts": _character_identity_facts(shot),
            "identity_facts_required": identity_facts_required,
            "functional_figures": functional_figures,
            "frame_scope": {
                "declared": bool(phase_population_declared),
                "character_source": character_source,
                "functional_figure_source": functional_source_name,
            },
        },
        "population": {
            "counts": {
                "named_characters": registered_count,
                "functional_people": functional_count,
                "real_people_total": visible_count,
                "non_real_overlays": len(overlays),
                "visible_entity_instances_total": visible_entity_count,
            },
            "functional_figures": functional_figures,
            "declared_visible_figure_count": (
                visible_count if legacy_declared_repaired
                else declared_visible),
            "source_declared_visible_figure_count": (
                declared_visible if legacy_declared_repaired else None),
            "temporal_duplicate_count_repaired": legacy_declared_repaired,
            "issues": population_issues,
        },
        "composition": composition,
        "scene": scene,
        # 场景陈设的固定坐标条款(由 blocking 层按三维场景+本镜机位算好)。
        # 没有它,【场景】只有一句地名,模型每张图重新想象家具在哪——
        # 「说在纱帐后面却画在镜前」「后面纱帐经常变」就是这么来的。
        "scene_layout": scene_layout,
        "script_reference": _text(shot.get("script_reference")),
        "era_context": _text(shot.get("era_context")),
        "era_object_constraints": build_era_object_constraints({
            **shot, "location": scene, "style": scene_style,
        }),
        "style": scene_style,
        "style_direction": (
            dict(shot.get("style_direction"))
            if isinstance(shot.get("style_direction"), dict) else {}),
        "visual_medium": medium["dimension"],
        "medium": medium,
        "start": _registered_state_value(
            shot, "start_state") or "保持首帧状态",
        "start_appearance": _appearance_map(shot.get("start_state")),
        "character_conditions": character_conditions,
        # subject.actors 已被渲染成「P01=林川（外观）」,取不到裸名;
        # 逐镜负向清单要按名字点出该静止的角色,所以另存一份原始名单。
        "actor_names": list(characters),
        "action": action,
        "performance": _text(
            (shot.get("performance") or {}).get("micro_expression"),
            "表演严格服从逐角色 condition，不自行增加任何行为",
        ),
        "camera": _camera(shot, visible_count=visible_count),
        # 镜位显式裁决条款:_camera 已按「分镜原文 > 镜头合同 > 五维
        # 默认」融合出唯一执行值;审核上下文里若还残留其他来源的机位
        # /构图描述,以融合值为准,不构成需要裁决的同级冲突。
        "aspect_precedence": (
            "画幅以 aspect 字段为唯一执行值(来自项目/本集制作标准);"
            "镜头或其他描述中出现的画幅比例字样已在编译时剥离,"
            "如仍残留仅为杂讯,不构成画幅冲突,也不需要裁决"),
        # 光影执行条款:按场景时间/地点/情绪与本镜景别自动选型,
        # 非写实画风自动为空(不给二次元塞摄影术语)。
        "lighting": lighting_lines_for_shot(shot, scene_style, scene),
        # 3D 空间调度 → 可核验文字:屏幕定位、遮挡序、朝向视线、
        # 行动路线、屏幕方向轴线。此前这些数字只画进示意图,模型得
        # "看懂图";现在图与文字各司其职,质检也按同一判据核验。
        "spatial_staging": _spatial_staging_block(
            shot, media=output_media),
        "camera_precedence": (
            "本合同 camera 字段是唯一执行镜位,已按「分镜原文 > 镜头"
            "合同 > 五维默认」融合完毕;上下文中任何其他来源的机位、"
            "视角或构图描述与之并列时,直接以 camera 字段为准,仅作"
            "溯源参考,不构成需要裁决的冲突,也不需要猜测优先级。"
            "景别取景边界同为执行值:被边界裁出画的身体部位、伤情、"
            "服装细节或道具,其『必须可见』要求在本镜自动不适用"
            "(由包含该部位的其他镜头承担核验),不构成可见性冲突。"
            "机位同样决定人物可见面与朝向语义:被机位背向的面部表情、"
            "口型、眼神及胸前/正面细节,其『必须可见/清晰画出』要求"
            "本镜自动不适用,由包含相应朝向的其他镜头承担;人物朝向、"
            "表演与正面细节描述和机位并列冲突时一律以机位为准"
            "(如背面机位下的『相向』指两人身体相对、以背面呈现),"
            "不构成需要裁决的冲突。"
            "景别同时决定细节可辨尺度:低于本景别物理可辨尺度的微细节"
            "(刃口缺口、指腹墨渍、织物纹理、饰品细纹、印文等),其"
            "『必须可见/清晰』要求在远景、全景、中景自动不适用,由近景/"
            "特写镜头承担核验;道具的在场性、大形态、颜色系与持有人"
            "在任何景别都必须正确,不在免验之列"),
        "physical": physical,
        "spatial_relations": list(physical.get("spatial_relations") or []),
        "prop_registry": [
            dict(value) for value in (shot.get("prop_registry") or [])
            if isinstance(value, dict)
        ],
        "frame_props": frame_props,
        "prop_transitions": prop_transitions,
        # Non-rendering audit trail for the upstream whole-take plan.  Static
        # validation/provider prompts use ``prop_transitions`` above (empty),
        # so this cannot re-enter image physical rules or compact instructions.
        "prop_transitions_audit": (
            [] if timeline_contract else all_prop_transitions),
        "prop_issues": [
            *frame_prop_issues,
            *(prop_transition_issues if timeline_contract else []),
        ],
        "end": _registered_state_value(
            shot, "end_state") or "到达尾帧状态",
        "end_appearance": _appearance_map(shot.get("end_state")),
        "appearance_state_required": bool(
            shot.get("appearance_state_required")),
        "appearance_issues": [
            _text(value) for value in (
                shot.get("appearance_continuity_issues") or [])
            if _text(value)
        ],
        "semantic_corrections": [
            dict(value) for value in (
                shot.get("semantic_corrections") or [])
            if isinstance(value, dict)
        ],
        "dialogue": (
            "" if dialogue.get("inner_voice")
            else _text(dialogue.get("dialogue"))),
        "speaker": (
            "" if dialogue.get("inner_voice")
            else _text(dialogue.get("character"))),
        "inner_voice": bool(dialogue.get("inner_voice")),
        "readable_text": (
            {"phases": {
                phase: dict(value)
                for phase, value in (readable_source.get("phases") or {}).items()
                if phase in _READABLE_TEXT_PHASES and isinstance(value, dict)
            }}
            if _readable_text_has_phases(readable_source)
            else dict(readable_source)
        ),
        "readable_text_current": dict(readable),
        "text": text_rule,
        "narrative_overlays": overlays,
        "references": refs,
        "output_issues": (
            [f"不支持的提示词输出 mode: {requested_mode}"]
            if requested_mode not in {
                "image", "keyframe", "first_frame", "last_frame",
                "video", "motion", "seedance",
            } else []),
        "hard": (
            "视频只执行一个主动作和一个运镜；静态图只定格 frame_target；"
            "人物身份、服装、场景、构图分别服从"
            "对应参考图；不得重新设计人物，不得新增/复制真实人物或把参考图内容"
            "贴进成片（已声明的功能人物除外）；服装、头饰、妆发必须逐人服从"
            "本镜起止状态，未写换装/"
            "摘戴/改妆动作时不得自行改变；非现实Q版叠层不得转化成真人、"
            "实体角色或空间站位；逐角色生命、意识、存在形态和行动能力必须"
            "服从 condition；同一 physical prop_id 在同一 phase 只能有一个"
            "物理主位置，reflection/screen/painting/overlay 只作披露，"
            "visibility=absent 不计物理实例"
        ),
    }
    return contract


def _reference_role(item):
    role = _normalize_role_value(item.get("role") or item.get("kind"))
    if role in {"identity", "character_identity", "character_art", "character_candidate"}:
        return "身份：只锁脸、发型、年龄、性别"
    if role in {"identity_detail", "character_sheet", "structure"}:
        return "人物细节：只补充结构/妆发"
    if role in {"wardrobe", "costume", "costume_detail"}:
        return "服装：只锁服装、配饰、道具结构"
    if role in {"prop", "prop_identity", "prop_candidate"}:
        return "核心道具：只锁轮廓、结构、材质、工艺与识别细节"
    if role in {"scene", "scene_art"}:
        return "场景：只锁空间、陈设、主光方向"
    if role in {"spatial", "spatial_blocking"}:
        return "调度：只锁人数、站位、遮挡、机位"
    if role in {"keyframe", "image", "first_frame", "last_frame", "continuity"}:
        return "连续性：只承接构图、状态、服装、道具、光线"
    if role in {"style", "style_ref"}:
        return "画风：只锁媒介、材质、色彩、光影"
    if role == "composition":
        return "构图：只锁机位、构图、动作路径"
    if role in {"inner_persona", "narrative_overlay"}:
        return (
            "内心Q版：只锁Q版脸、发型、当前衣着和Q版比例；"
            "保持约1.8头身，头占总高约58%，身体明显小于头；"
            "不增加真实人物、站位或默认道具")
    return "弱参考：不得覆盖已锁定身份和场景"


def _render_spatial_relation(item):
    if not isinstance(item, dict):
        return _text(item)
    subject = _text(item.get("subject"))
    relation = _text(item.get("relation") or item.get("predicate"))
    object_ = _text(item.get("object"))
    if not (subject and relation and object_):
        return ""
    return f"{subject}→{relation}→{object_}"


def _render_reference(item):
    base = f"图{item['index']}={item['label']}({_reference_role(item)})"
    scope = item.get("inherit_scope") or {}
    include = "、".join(_text_list(scope.get("include")))
    exclude = "、".join(_text_list(scope.get("exclude")))
    details = []
    binding = item.get("binding")
    bindings = _text_list(item.get("bindings"))
    if binding not in (None, ""):
        details.append(f"binding={_text(binding)}")
    elif bindings:
        details.append(f"binding={'、'.join(bindings)}")
    if include:
        details.append(f"include={include}")
    if exclude:
        details.append(f"exclude={exclude}")
    return base + (f"[{'；'.join(details)}]" if details else "")


def _medium_prompt_line(medium):
    medium = medium if isinstance(medium, dict) else {}
    if medium.get("semi_realistic_3d"):
        return "半写实3D视觉媒介；明确非真人摄影、非真人实拍"
    if medium.get("dimension") == "3D":
        return "3D视觉媒介"
    if medium.get("dimension") == "2D":
        return "2D视觉媒介"
    if medium.get("live_action_photography"):
        return "真人摄影/真人实拍视觉媒介"
    return ""


_CONDITION_FIELDS = (
    "life_state", "consciousness_state", "embodiment", "mobility")


def _condition_has_signal(phases, media, target_phase):
    """该角色的 condition 是否携带任何可执行信息。

    只要任一 phase 的任一字段有非空、非 unknown 的值就算有信号;全空则这一
    行只会输出一串 unknown,应整条跳过。
    """
    if media == "video":
        buckets = (phases.get("start") or {}, phases.get("end") or {})
    else:
        buckets = (phases.get(target_phase) or {},)
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for field in _CONDITION_FIELDS:
            value = _text(bucket.get(field))
            if value and value.lower() != "unknown":
                return True
    return False


_PLACEHOLDER_CAMERA_VALUES = (
    "按分镜", "按合同", "未指定", "未知", "不详", "待定", "待确认",
    "待补充", "以参考图为准", "以剧本为准", "自行判断", "自由发挥",
)


def _is_placeholder(value):
    """未裁决出具体值时不要生成锁定句。

    `_camera` 在分镜没写景别时回落到占位符「按分镜」。把它照抄进负向清单会
    产出"景别锁定为按分镜"这种无可执行标准的指令——正是原文批评的"自然过渡"
    式空话,反而占掉开头权重。宁可少一条也不给模型假约束。
    """
    text = _text(value)
    if not text:
        return True
    return any(token in text for token in _PLACEHOLDER_CAMERA_VALUES)


def build_model_constraints(contract, *, media="video"):
    """把本镜已知事实反写成"模型不许做的事",逐镜生成。

    2026-07-30 A/B 实测(即梦 Seedance 2.0 Fast VIP / 720P / 5s,同一首帧、
    同一参数,只换提示词写法):只给正向描述时,模型把"人物慢慢向树后缩"
    执行成了镜头追着脸推近——2 秒后中景 3 名杀手与倒地书童被挤出画面,
    可见人形 5→1,景别中景→大特写,机位从侧面偷窥转成正面,一次违反 6 项
    合同硬约束;把同一批事实补成带具体数字与角色名的负向清单后,9 项全过。

    已有的【硬约束】是 shot 无关的常量,说不出"严格 N 人"、"只执行这一种
    运镜"、"景别锁死为中景",所以另起本清单按镜生成。只反写合同里已经存在
    的事实,不引入新判断。
    """
    contract = contract if isinstance(contract, dict) else {}
    subject = contract.get("subject") or {}
    camera = contract.get("camera") or {}
    clauses = []

    # 人数:常量硬约束只说"不得新增/复制",说不出上限是几。实测里丢人先于
    # 加人,但同一句把上限写成具体数字,两个方向一起封。
    visible_count = int(subject.get(
        "visible_count", subject.get("count", 0)) or 0)
    if visible_count > 0:
        clauses.append(
            f"总可见人形严格为 {visible_count} 人,"
            f"禁止出现第 {visible_count + 1} 人,"
            "禁止复制、倒影或以海报/画中人形式增加人形")

    # 景别:A 组从中景一路推成大特写,是本次最直接的违约项。
    scale = camera.get("景别")
    if not _is_placeholder(scale):
        clauses.append(
            f"景别锁定为{_text(scale)},镜内不得改变景别、不得推成更紧景别")

    if media == "video":
        # 运镜:合同已裁决出唯一执行值,这里显式否掉其余全部运动。
        # A 组崩溃的机制就是模型自行叠加了一次推近。占位符按"固定"处理:
        # 分镜没写运镜时,模型自由运镜的代价远高于一个不动的机位。
        move = camera.get("运镜")
        if not _is_placeholder(move) and _text(move) not in {
                "固定", "静止", "锁定"}:
            clauses.append(
                f"本镜只执行「{_text(move)}」一种镜头运动,"
                "不得叠加推、拉、摇、移、升降、环绕或变焦")
        else:
            clauses.append(
                "机位固定,不推、不拉、不摇、不移、不升降、不环绕、不变焦")

        # 机位/角度:越轴会让反打关系与视线方向失效。
        held = "、".join(
            _text(value) for value in (camera.get("机位"), camera.get("角度"))
            if not _is_placeholder(value))
        if held:
            clauses.append(f"机位保持{held},不得越轴到对侧")

        # 未参与主动作的角色必须静止。实测 B 组里"杀手不得走动、不得挥刀;
        # 书童不得起身"是守住中景的关键句;名字不出现在主动作里的角色,
        # 就是本镜该按住的角色。
        action_text = _text(contract.get("action"))
        idle = [
            name for name in (contract.get("actor_names") or [])
            if name and name not in action_text
        ]
        if idle:
            clauses.append(
                "、".join(idle)
                + "保持起点状态,不执行本合同未写的动作、不移动站位")
    else:
        clauses.append("只定格当前状态,不表现任何动作过程或运动拖影")

    return clauses


def render_shot_prompt(contract, *, mode=None):
    """Render a still freeze or a video timeline, never a hybrid of both."""
    contract = contract if isinstance(contract, dict) else {}
    subject_contract = contract.get("subject") or {}
    subject = "、".join(subject_contract.get("actors") or []) or "无人"
    count = int(subject_contract.get("count") or 0)
    visible_count = int(
        subject_contract.get("visible_count", count) or 0)
    functional_figures = subject_contract.get("functional_figures") or []
    output = contract.get("output") or {}
    if mode is None:
        media = _text(output.get("media"), "image").lower()
    else:
        media, _ = _normalize_mode(mode)
    camera = contract.get("camera") or {}
    camera_values = [
        camera.get("景别"), camera.get("角度"), camera.get("焦段"),
        camera.get("机位"),
    ]
    if media == "video":
        camera_values.append(
            f"{camera.get('运镜')}({camera.get('动机')})")
    else:
        camera_values.append("静态关键帧只定格当前可见机位与构图")
    camera_values.append(f"构图{camera.get('构图')}")
    camera_line = "；".join(value for value in camera_values if value)
    # 抽象镜头术语(俯拍/背面/过肩…)对图像模型约束力弱,是历史视角类
    # 质检失败的主因;翻译成可见几何特征,生成与质检按同一标准执行。
    camera_geometry = camera_geometry_clause(camera)
    if camera_geometry:
        camera_line = f"{camera_line}；{camera_geometry}"
    # 光影是第四维:只说机位不说布光,模型必然给平光,成片没有氛围
    # (2026-07-28 盘点:镜头语言14条全在景别/角度/机位,光影为0)。
    lighting_line = contract.get("lighting") or ""
    # 空间调度文字合同:屏幕定位/遮挡序/朝向视线/行动路线/轴线方向
    staging_lines = [
        f"【{label}】{text}。"
        for label, text in (contract.get("spatial_staging") or {}).items()
        if text]
    if contract.get("camera_precedence"):
        camera_line = (
            f"{camera_line}；本行为唯一执行镜位(camera_precedence):"
            "与其他机位/构图描述并列冲突时直接以本行为准")
    lines = [
        "【镜头合同v2.2】只执行下列事实，不自行补剧情。",
    ]
    # 首句权重:图像模型对提示词开头的服从度最高。台账TOP1缺陷
    # (核心道具执行缺失13次)与状态执行不到位,都源于核心事实被埋在
    # 合同中部。把"这张图必须画出什么"压缩成一句放在最前。
    if media != "video":
        target_ref = contract.get("frame_target")
        core_state = _text(
            target_ref.get("state") if isinstance(target_ref, dict)
            else contract.get("frame_target_state"))
        core_props = []
        unscaled_props = []
        # 内部 prop_id 不入提示词(会被判提示词泄漏);经注册表解析中文名
        registry_names = {
            _text(entry.get("prop_id")): _text(entry.get("name"))
            for entry in (contract.get("prop_registry") or [])
            if isinstance(entry, dict)}
        # 道具尺度必须写成「画面内参照物」。缺这一条时,binding 里的
        # 「尺寸服从当前镜头合同」就指向一份对尺寸只字未提的合同,模型
        # 只能退回去继承参考图——而道具母图是占满画面的棚拍特写,于是
        # 一枚两指可捏的小铃铛被画成 3-4 倍大(2026-07-28 EP1 实测)。
        # 实测「1.5厘米」这类绝对尺寸无效,只有参照物描述能纠正。
        registry_scales = {
            _text(entry.get("prop_id")): _text(entry.get("scale_reference"))
            for entry in (contract.get("prop_registry") or [])
            if isinstance(entry, dict)}
        for item in (contract.get("frame_props") or []):
            if not isinstance(item, dict):
                continue
            if _text(item.get("phase")).lower() != _text(
                    output.get("frame_phase")).lower():
                continue
            if str(item.get("visibility") or "") != "visible":
                continue
            prop_name = _text(item.get("name")) or registry_names.get(
                _text(item.get("prop_id")), "")
            if not prop_name or any(
                    prop_name in existing for existing in core_props):
                continue
            holder = _text(item.get("holder"))
            state = _text(item.get("physical_state"))
            scale = _text(item.get("scale_reference")) or registry_scales.get(
                _text(item.get("prop_id")), "")
            detail = "、".join(filter(None, (
                f"由{holder}持有" if holder else "", state,
                f"画面内尺度:{scale}" if scale else "")))
            core_props.append(
                f"{prop_name}({detail})" if detail else prop_name)
            if not scale:
                unscaled_props.append(prop_name)
            if len(core_props) >= 2:
                break
        if core_state or core_props:
            core_line = core_state or "按定格状态执行"
            if core_props:
                core_line += "；必须清晰画出:" + "、".join(core_props)
            lines.append(
                f"【核心画面】{core_line}(以上均以本镜机位的可见面"
                "为准,被机位背向或裁出画的细节免验)。")
        if unscaled_props:
            # 存量剧本没有 scale_reference。没有这句兜底,「尺寸服从合同」
            # 就指向一份对尺寸只字未提的合同,模型只能退回去继承道具母图
            # ——而母图是占满画面的棚拍特写,小物件因此被画成数倍大。
            lines.append(
                "【道具尺度】" + "、".join(dict.fromkeys(unscaled_props))
                + "未声明画面内尺度：严禁参照其母资产图在画面中的占比"
                "(母图是把道具放大数十倍的棚拍特写)，必须按本镜的持有"
                "方式、与人手/身体/家具的真实比例关系推断它应有的大小，"
                "宁小勿大。")
    if media == "video":
        lines.append(
            "【输入】图1是唯一动作起点，图2是唯一动作终点；"
            "只让已锁定画面动起来。")
    lines.append(
        f"【主体】严格共{count}人：{subject}（登记角色，均为真实人物）；"
        f"画面可见真人严格共{visible_count}人。")
    if functional_figures:
        lines.append(
            "【功能人物】"
            + "；".join(_functional_figure_line(item)
                       for item in functional_figures)
            + "；功能人物不锁身份，但每具真人身体都计入总可见真人。")
    condition_lines = []
    target_phase = _text(output.get("frame_phase"), "end")
    for name, phases in (
            contract.get("character_conditions") or {}).items():
        if not isinstance(phases, dict):
            continue
        # 四个字段全缺时这一行会渲染成 life=unknown,consciousness=unknown,
        # embodiment=unknown,mobility=unknown ——零信息却照吃提示词权重。
        # 2026-07-30 消融实测:5 人镜里本段占整条提示词 36.5%,是最大的一块,
        # 而真正的镜头内容(起止+主动作+表演)只占 5.5%;把提示词压到 40%
        # 后九项硬约束仍全过,证明这部分冗余可安全削掉。
        # 下方 hard_state_lines 才是死亡/昏迷/静止的真正执行条款(本文件已
        # 注明英文键值对"约束力近零"),所以跳过全 unknown 不损失约束力。
        if not _condition_has_signal(phases, media, target_phase):
            continue
        if media == "video":
            start_condition = phases.get("start") or {}
            end_condition = phases.get("end") or {}
            condition_lines.append(
                f"{name}:start("
                f"life={start_condition.get('life_state', 'unknown')},"
                f"consciousness={start_condition.get('consciousness_state', 'unknown')},"
                f"embodiment={start_condition.get('embodiment', 'unknown')},"
                f"mobility={start_condition.get('mobility', 'unknown')})→end("
                f"life={end_condition.get('life_state', 'unknown')},"
                f"consciousness={end_condition.get('consciousness_state', 'unknown')},"
                f"embodiment={end_condition.get('embodiment', 'unknown')},"
                f"mobility={end_condition.get('mobility', 'unknown')})")
        else:
            condition = phases.get(target_phase) or {}
            condition_lines.append(
                f"{name}:life={condition.get('life_state', 'unknown')},"
                f"consciousness={condition.get('consciousness_state', 'unknown')},"
                f"embodiment={condition.get('embodiment', 'unknown')},"
                f"mobility={condition.get('mobility', 'unknown')}")
    if condition_lines:
        lines.append("【人物状态合同】" + "；".join(condition_lines) + "。")
    # 死亡/昏迷/静止是顶着模型"活人先验"的反常态内容,英文键值对
    # (life=dead)约束力近零——实测阿砚"已死亡"被画成睁眼注视的活人。
    # 翻译成强制性视觉判据,与质检同一口径。
    hard_state_lines = []
    for name, phases in (
            contract.get("character_conditions") or {}).items():
        if not isinstance(phases, dict):
            continue
        condition = phases.get(
            "end" if media == "video" else target_phase) or {}
        life = str(condition.get("life_state") or "")
        consciousness = str(condition.get("consciousness_state") or "")
        mobility = str(condition.get("mobility") or "")
        if life in ("dead", "nonliving"):
            hard_state_lines.append(
                f"{name}已死亡:双眼完全闭合,无任何眼神、注视方向或"
                "表情张力,面部肌肉彻底松弛静止;身体无自主支撑,姿态"
                "完全由重力与接触面决定;绝不允许睁眼、聚焦、惊恐、"
                "咬牙或任何『活人感』")
        elif consciousness in ("unconscious", "none"):
            hard_state_lines.append(
                f"{name}昏迷/无意识:双眼闭合,面部松弛无表情张力,"
                "身体无自主支撑,不得出现注视、皱眉或主动姿态")
        elif mobility in ("immobile", "none"):
            hard_state_lines.append(
                f"{name}身体完全静止:无动作趋势、无重心移动,"
                "四肢位置由当前支撑决定")
    if hard_state_lines:
        lines.append(
            "【硬状态·强制执行】" + "；".join(hard_state_lines)
            + "。本条为最高优先视觉事实,任何表情/动作描述与之冲突时"
            "以本条为准。")
    overlays = contract.get("narrative_overlays") or []
    if overlays:
        overlay = overlays[0]
        inner_dialogue = (
            f"；内心说「{overlay['dialogue']}」"
            if overlay.get("dialogue") else "")
        lines.append(
            "【非现实内心Q版叠层】"
            f"{overlay['name']}是{overlay['host_character']}的内心人格，"
            f"用途={overlay['function']}；{overlay['expression']}；"
            f"{overlay['action']}{inner_dialogue}。它不是真实人物，不计入"
            f"上述{visible_count}名真实主体，不参与物理站位、遮挡、空间调度或真实"
            "连续性；只有宿主内心感知，其他人物不得看见、回应、触碰、对视；"
            "严格保持大头小身：约1.8头身，头占总高约58%，身体、肩宽、"
            "躯干、四肢、手脚都明显小于头部；"
            "继承锁定的当前衣着，不继承默认道具；内心发声时宿主闭口，"
            "不画旁白/吐槽字幕。")
    lines.append(f"【场景】{contract.get('scene', '按场景基准图')}。")
    scene_layout = _text(contract.get("scene_layout"))
    if scene_layout:
        lines.append(scene_layout)
    composition = contract.get("composition") or {}
    if composition.get("composition_type") == "over_shoulder_dialogue":
        duties = "；".join(
            f"{item.get('character')}={item.get('role')}/"
            f"{item.get('expected_view')}"
            for item in composition.get("actors") or [])
        lines.append(
            "【过肩构图】"
            f"主体{composition.get('expected_primary_count', 1)}人，"
            f"实际可见人形{composition.get('expected_visible_figure_count', visible_count)}人；"
            f"{duties}；{composition.get('count_rule', '')}。")
    elif composition.get(
            "composition_type") == "single_subject_over_shoulder":
        duties = "；".join(
            f"{item.get('character')}={item.get('role')}/"
            f"{item.get('expected_view')}"
            for item in composition.get("actors") or [])
        lines.append(
            "【单人过肩构图】"
            "严格只有1名人物、1具连续身体；"
            f"实际可见人形{composition.get('expected_visible_figure_count', visible_count)}人；"
            f"{duties}；{composition.get('count_rule', '')}。")
    if media == "video":
        lines.extend([
            f"【起点】{contract.get('start', '保持首帧状态')}。",
            f"【单一主动作】{contract.get('action', '环境保持稳定')}。",
            f"【表演】{contract.get('performance', '自然微表情')}。",
            f"【镜头】{camera_line}。",
            *([f"【光影】{lighting_line}。"] if lighting_line else []),
            *staging_lines,
            f"【终点】{contract.get('end', '到达尾帧状态')}。",
        ])
    else:
        target = contract.get("frame_target") or {}
        target_state = (
            target.get("state") if isinstance(target, dict)
            else contract.get("frame_target_state"))
        target_state = _text(
            target_state or contract.get("frame_target_state"),
            "环境保持稳定")
        fallback = bool(
            target.get("fallback")
            if isinstance(target, dict)
            else contract.get("frame_target_fallback"))
        source = _text(
            target.get("source") if isinstance(target, dict)
            else contract.get("frame_target_source"))
        fallback_note = (
            f"（fallback=true；来源={source or 'description/action'}；"
            "默认生产阻断，只有显式 legacy policy 可兼容）"
            if fallback else "")
        lines.extend([
            f"【定格状态】{target_state}{fallback_note}。",
            f"【镜头】{camera_line}。",
            *([f"【光影】{lighting_line}。"] if lighting_line else []),
            *staging_lines,
        ])
    frame_props = contract.get("frame_props") or []
    visible_props = (
        frame_props if media == "video"
        else [
            item for item in frame_props
            if _text(item.get("phase")) == target_phase
        ])
    if visible_props:
        lines.append(
            "【道具定格】"
            + "；".join(_render_frame_prop(item) for item in visible_props)
            + "。")
    prop_transitions = contract.get("prop_transitions") or []
    if prop_transitions:
        prop_positions = {
            (_text(item.get("prop_id")), _text(item.get("phase"))):
            _prop_transition_position(item)
            for item in frame_props
            if isinstance(item, dict)
            and _prop_transition_position(item)
        }
        if media == "video":
            lines.append(
                "【道具状态变化】"
                + "；".join(
                    _render_prop_transition(item, prop_positions)
                    for item in prop_transitions)
                + "。")
        else:
            lines.append(
                "【道具变化审计】"
                + "；".join(
                    _render_prop_transition(item, prop_positions)
                    for item in prop_transitions)
                + "；静态帧禁止表现变化过程，只以【道具定格】的当前 phase"
                " 主位置为准。")
    physical = contract.get("physical") or {}
    physical_rules = "；".join(physical.get("rules") or [])
    if physical_rules:
        physical_line = (
            f"【物理/空间逻辑】{physical_rules}"
            + (f"；对象关系：{'；'.join(physical.get('objects') or [])}。"
               if physical.get("objects") else "。"))
        relation_lines = [
            _render_spatial_relation(item)
            for item in physical.get("spatial_relations") or []
        ]
        relation_lines = [value for value in relation_lines if value]
        if relation_lines:
            physical_line += "【空间关系】" + "；".join(relation_lines) + "。"
        lines.append(physical_line)
    medium_line = _medium_prompt_line(contract.get("medium"))
    if medium_line:
        lines.append(f"【视觉媒介】{medium_line}。")
    if contract.get("style"):
        lines.append(f"【画风】{contract['style']}（只沿用项目基准，不改媒介）。")
    direction = contract.get("style_direction") or {}
    if isinstance(direction, dict) and (
            direction.get("shot_pattern")
            or direction.get("visual_effects")):
        effects = "、".join(direction.get("visual_effects") or [])
        lines.append(
            "【风格导演执行】"
            + (f"镜头组合={direction.get('shot_pattern')}；"
               if direction.get("shot_pattern") else "")
            + (f"视觉效果={effects}；" if effects else "")
            + f"动机={direction.get('selection_reason') or '服务本镜叙事功能'}。")
    if media == "video" and contract.get("dialogue"):
        speaker = contract.get("speaker") or "说话人"
        lines.append(f"【对白】{speaker}说出「{contract['dialogue']}」，自然口型；不画字幕。")
    lines.append(f"【文字】{contract['text']}。")
    if contract.get("references"):
        refs = "；".join(
            _render_reference(item)
            for item in contract["references"]
        )
        lines.append(f"【参考图职责】{refs}。")
    # 逐镜负向清单排在 shot 无关的【硬约束】之前:实测中模型对带具体数字与
    # 角色名的否定句服从度明显高于通用禁令,让它先被读到。
    model_constraints = build_model_constraints(contract, media=media)
    if model_constraints:
        lines.append("【模型约束】" + ";".join(model_constraints) + "。")
    lines.append(f"【硬约束】{contract['hard']}。")
    return "\n".join(lines)


def compile_shot_prompt(shot, *, location="", style="", references=None, mode="image"):
    contract = build_shot_prompt_contract(
        shot, location=location, style=style, references=references,
        mode=mode)
    return contract, render_shot_prompt(contract)


def _without_obsolete_camera_rules(value):
    """Keep actor/prop physics while removing an old camera execution clause."""
    if isinstance(value, dict):
        value = value.get("rules") or value.get("constraints") or []
    if isinstance(value, (list, tuple, set)):
        value = "；".join(_text(item) for item in value if _text(item))
    camera_terms = ("摄影机", "镜头", "机位", "camera")
    movement_terms = (
        "固定", "跟", "推", "拉", "摇", "移", "升降", "环绕",
        "变焦", "运动",
    )
    bare_camera_motion = re.compile(
        r"^(?:不|仅|只)?(?:跟拍?|推近?|拉远?|摇|移|升降|环绕|变焦|运动)$")
    kept = []
    for sentence in re.split(r"[。；;\n]+", _text(value)):
        parts = [part.strip() for part in re.split(r"[，,、]+", sentence)
                 if part.strip()]
        stable = [
            part for part in parts
            if not (
                any(token in part.lower() for token in camera_terms)
                and any(token in part for token in movement_terms)
            )
            and not bare_camera_motion.fullmatch(part)
        ]
        if stable:
            kept.append("，".join(stable))
    return kept


def synchronize_shot_execution_contract(
        shot, *, location="", style="", references=None):
    """Persist one repaired camera decision into every executable shot field.

    Codex repair writes the newest camera sentence first.  Older storyboards
    also duplicate that decision in ``shot_contract``, five-dimensional
    design, physical rules and Seedance prompts.  Leaving any copy stale makes
    the next stage revive the rejected camera.  This function intentionally
    updates those derived copies together and recompiles the video contract;
    versioned documents retain the pre-repair source for audit.
    """
    if not isinstance(shot, dict):
        return shot
    previous_contract = shot.get("prompt_contract")
    previous_action = (
        previous_contract.get("action")
        if isinstance(previous_contract, dict) else "")
    shot["video_action"] = _text(
        shot.get("video_action") or shot.get("action")
        or previous_action or shot.get("description"),
        "环境保持稳定，只执行自然微动")
    characters = list(shot.get("characters") or [])
    visible_count = shot.get("visible_figure_count")
    if not isinstance(visible_count, int) or isinstance(visible_count, bool):
        visible_count = len(characters)
    camera = _camera(shot, visible_count=visible_count)

    shot_contract = shot.get("shot_contract")
    shot_contract = (dict(shot_contract)
                     if isinstance(shot_contract, dict) else {})
    camera_keys = ("景别", "角度", "焦段", "机位", "运镜", "构图")
    for key in camera_keys:
        shot_contract[key] = camera.get(key, "")

    dimensions = shot.get("five_dimensions")
    dimensions = dict(dimensions) if isinstance(dimensions, dict) else {}
    design = dimensions.get("camera_design")
    design = dict(design) if isinstance(design, dict) else {}
    design.update({
        "shot_scale": camera.get("景别", ""),
        "angle": camera.get("角度", ""),
        "lens": camera.get("焦段", ""),
        "camera_position": camera.get("机位", ""),
        "movement": camera.get("运镜", ""),
        "composition": camera.get("构图", ""),
        "movement_motivation": camera.get("动机", ""),
    })
    dimensions["camera_design"] = design

    movement = _text(camera.get("运镜"), "固定")
    if "固定" in movement:
        movement_rule = (
            "摄影机全程固定在当前执行机位，不跟拍、不推、不拉、不摇、"
            "不移、不升降、不环绕、不变焦。")
        movement_label = "固定机位"
    else:
        movement_rule = (
            f"摄影机全程只执行一次{movement}，除该单一运镜外不叠加推、"
            "拉、摇、移、升降、环绕或变焦。")
        movement_label = movement
    pattern = "".join(filter(None, (
        _text(camera.get("焦段")), _text(camera.get("角度")),
        _text(camera.get("机位")), _text(camera.get("景别")),
    )))
    pattern = "，".join(filter(None, (
        pattern, movement_label, _text(camera.get("构图")),
    )))
    shot_contract["风格镜头组合"] = pattern
    shot["shot_contract"] = shot_contract

    direction = shot.get("style_direction")
    direction = dict(direction) if isinstance(direction, dict) else {}
    direction["shot_pattern"] = pattern
    direction_camera = direction.get("camera_contract")
    direction_camera = (
        dict(direction_camera)
        if isinstance(direction_camera, dict) else {})
    direction_camera.update({
        "shot_scale": camera.get("景别", ""),
        "angle": camera.get("角度", ""),
        "lens": camera.get("焦段", ""),
        "camera_position": camera.get("机位", ""),
        "movement": camera.get("运镜", ""),
        "composition": camera.get("构图", ""),
    })
    direction["camera_contract"] = direction_camera
    shot["style_direction"] = direction
    aesthetics = dimensions.get("aesthetics")
    aesthetics = dict(aesthetics) if isinstance(aesthetics, dict) else {}
    aesthetics["shot_pattern"] = pattern
    dimensions["aesthetics"] = aesthetics
    shot["five_dimensions"] = dimensions

    physical_source = shot.get("physical_logic")
    if not physical_source:
        physical_source = shot.get("physical_contract")
    physical_rules = _without_obsolete_camera_rules(physical_source)
    physical_rules.append(movement_rule.rstrip("。"))
    shot["physical_logic"] = "；".join(dict.fromkeys(physical_rules)) + "。"
    physical_input = dict(shot)
    physical_input.pop("physical_contract", None)
    shot["physical_contract"] = build_physical_contract(
        physical_input, media="video")

    scene = shot_local_scene(shot, location)
    action = _text(
        shot.get("description") or shot.get("action"), "环境保持稳定")
    prompt_parts = [
        f"场景：{scene}" if scene else "",
        f"镜头：{pattern}",
        f"动作：{action}",
        f"空间与机位：{shot['physical_logic']}",
        "无字幕、无Logo、无水印",
    ]
    shot["prompt"] = "。".join(part for part in prompt_parts if part)
    contract, compact = compile_shot_prompt(
        shot, location=scene, style=style, references=references,
        mode="video")
    shot["prompt_contract"] = contract
    shot["seedance_prompt_compact"] = compact
    shot["seedance_prompt"] = compact
    return shot


def _binding_categories(reference):
    values = []
    raw = reference.get("binding")
    if isinstance(raw, dict):
        values.extend(_text(value) for value in raw.values() if _text(value))
    elif isinstance(raw, (list, tuple, set)):
        values.extend(_text_list(raw))
    elif _text(raw):
        # ``binding`` is a legacy human-readable instruction (for example
        # “锁脸、发型；当前镜头服装服从状态表”).  It may mention several
        # domains while explicitly saying that some of them are excluded, so
        # keyword-mining the prose creates false multi-role conflicts.  Only a
        # canonical single role is safe to interpret structurally; new callers
        # must use ``bindings`` for an explicit multi-role declaration.
        canonical = _normalize_role_value(raw)
        if canonical in REFERENCE_SCOPE_DEFAULTS:
            values.append(canonical)
    values.extend(_text_list(reference.get("bindings")))
    text = " ".join(values).lower()
    categories = set()
    token_map = {
        "identity": (
            "identity", "character_identity", "身份", "人物身份"),
        "wardrobe": ("wardrobe", "costume", "服装", "造型"),
        "scene": ("scene", "场景", "空间基准"),
        "spatial": ("spatial", "blocking", "调度", "站位"),
        "prop": ("prop", "道具"),
        "style": ("style", "画风", "媒介"),
        "composition": ("composition", "构图", "机位"),
        "continuity": (
            "continuity", "keyframe", "first_frame", "last_frame",
            "连续性", "首帧", "尾帧"),
    }
    for category, tokens in token_map.items():
        if any(token in text for token in tokens):
            categories.add(category)
    return categories


def validate_shot_prompt_contract(contract):
    """Fail before generation when a shot contract cannot be executed."""
    contract = contract if isinstance(contract, dict) else {}
    issues = []
    if _text(contract.get("schema")) != PROMPT_CONTRACT_SCHEMA:
        issues.append(
            f"镜头合同 schema 必须是 {PROMPT_CONTRACT_SCHEMA}")
    subject = contract.get("subject") or {}
    try:
        count = int(subject.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
        issues.append("subject.count 必须是整数")
    registered_count = subject.get("registered_count", count)
    functional_figures = subject.get("functional_figures") or []
    normalized_functional_count = 0
    for position, item in enumerate(functional_figures, 1):
        if not isinstance(item, dict):
            issues.append(f"第{position}个功能人物不是对象")
            continue
        value = item.get("count")
        if (not isinstance(value, int) or isinstance(value, bool)
                or value <= 0):
            issues.append(
                "功能人物"
                f"「{_text(item.get('name') or item.get('label'), position)}」"
                "的 count 必须是精确正整数")
            continue
        if not _text(item.get("name") or item.get("label")):
            issues.append(f"第{position}个功能人物缺少 name/label")
        normalized_functional_count += value
    functional_count = subject.get(
        "functional_count", normalized_functional_count)
    visible_count = subject.get(
        "visible_count", count + normalized_functional_count)
    for label, value in (
            ("subject.registered_count", registered_count),
            ("subject.functional_count", functional_count),
            ("subject.visible_count", visible_count)):
        if not isinstance(value, int) or isinstance(value, bool):
            issues.append(f"{label} 必须是整数")
    if registered_count != count:
        issues.append("subject.count 与 registered_count 不一致")
    if functional_count != normalized_functional_count:
        issues.append("subject.functional_count 与 functional_figures 求和不一致")
    if visible_count != count + normalized_functional_count:
        issues.append("subject.visible_count 与登记角色数和功能人物数之和不一致")
    population = contract.get("population") or {}
    population_counts = population.get("counts") or {}
    expected_population_counts = {
        "named_characters": count,
        "functional_people": normalized_functional_count,
        "real_people_total": count + normalized_functional_count,
        "non_real_overlays": len(contract.get("narrative_overlays") or []),
        "visible_entity_instances_total": (
            count + normalized_functional_count
            + len(contract.get("narrative_overlays") or [])),
    }
    for key, expected in expected_population_counts.items():
        if key in population_counts and population_counts.get(key) != expected:
            issues.append(f"population.counts.{key} 与镜头实体求和不一致")
    issues.extend(
        _text(value) for value in population.get("issues") or []
        if _text(value))
    declared_visible = population.get("declared_visible_figure_count")
    if (declared_visible is not None
            and declared_visible != count + normalized_functional_count):
        issues.append(
            "显式 visible_figure_count 与登记角色数和功能人物数之和不一致")
    composition = contract.get("composition") or {}
    if composition.get(
            "expected_visible_figure_count",
            count + normalized_functional_count,
    ) != count + normalized_functional_count:
        issues.append("构图可见人数与登记角色数和功能人物数之和不一致")
    if (composition.get("composition_type") == "over_shoulder_dialogue"
            and count < 2):
        issues.append("单人镜头不得使用双人过肩对话合同")
    if (composition.get("composition_type")
            == "single_subject_over_shoulder"
            and (count != 1 or normalized_functional_count != 0)):
        issues.append(
            "单人过肩合同必须严格为1个登记角色、0个功能人物、1具连续身体")

    identity_facts_required = bool(
        subject.get("identity_facts_required"))
    identity_facts = subject.get("identity_facts")
    if not isinstance(identity_facts, list):
        if identity_facts_required:
            issues.append("subject.identity_facts 必须是按登记角色列出的列表")
        identity_facts = []
    identity_by_name = {
        _text(item.get("name")): item
        for item in identity_facts
        if isinstance(item, dict) and _text(item.get("name"))
    }
    ambiguous_identity_tokens = (
        "未指定", "未知", "不详", "待定", "待确认", "待补充",
        "以参考图为准", "以剧本为准", "按参考图", "按剧本",
        "自行判断", "自行推断", "模型判断", "自由发挥",
    )

    def explicit_identity_value(value):
        value = _text(value)
        return bool(value) and not any(
            token in value for token in ambiguous_identity_tokens)

    registered_names = []
    for actor in subject.get("actors") or []:
        value = _text(actor)
        if "=" in value:
            value = value.split("=", 1)[1].split("（", 1)[0].strip()
        if value:
            registered_names.append(value)
    if identity_facts_required and len(identity_facts) != count:
        issues.append(
            "subject.identity_facts 数量必须与登记角色数完全一致")
    for name in registered_names if identity_facts_required else []:
        facts = identity_by_name.get(name)
        if facts is None:
            issues.append(f"{name}缺少结构化人物身份事实")
            continue
        if not explicit_identity_value(facts.get("gender")):
            issues.append(f"{name}性别未明确，禁止交给图像/视频模型猜测")
        if not explicit_identity_value(facts.get("age_range")):
            issues.append(f"{name}年龄段未明确，禁止交给图像/视频模型猜测")

    refs = contract.get("references") or []
    indexes = [item.get("index") for item in refs if isinstance(item, dict)]
    if indexes and indexes != list(range(1, len(indexes) + 1)):
        issues.append("参考图编号不是与提交顺序一致的连续编号")
    for position, item in enumerate(refs, 1):
        if not isinstance(item, dict):
            issues.append(f"第{position}张参考图合同不是对象")
            continue
        issues.extend(
            f"图{item.get('index') or position}：{_text(value)}"
            for value in item.get("role_issues") or []
            if _text(value))
        role = _normalize_role_value(item.get("role"))
        if not role:
            issues.append(f"图{item.get('index') or position}缺少单一 role")
        if len(set(_split_role_values(item.get("role")))) > 1:
            issues.append(
                f"图{item.get('index') or position}声明了多个 role")
        scope = item.get("inherit_scope") or {}
        include = set(_text_list(scope.get("include")))
        exclude = set(_text_list(scope.get("exclude")))
        overlap = sorted(include & exclude)
        if overlap:
            issues.append(
                f"图{item.get('index') or position}的 include/exclude 交集:"
                + "、".join(overlap))
        binding_categories = _binding_categories(item)
        if len(binding_categories) > 1:
            issues.append(
                f"图{item.get('index') or position}的 binding 声明多个参考职责:"
                + "、".join(sorted(binding_categories)))
        elif (binding_categories and role in REFERENCE_SCOPE_DEFAULTS
              and role != "reference" and role not in binding_categories):
            issues.append(
                f"图{item.get('index') or position}的 binding 与"
                f" {role} inherit_scope 明显冲突")

    action = _text(contract.get("action"))
    start_appearance = contract.get("start_appearance") or {}
    end_appearance = contract.get("end_appearance") or {}
    actor_names = registered_names
    if contract.get("appearance_state_required"):
        for name in actor_names:
            start_look = start_appearance.get(name) or {}
            end_look = end_appearance.get(name) or {}
            if not _text(
                    start_look.get("wardrobe") or end_look.get("wardrobe")):
                issues.append(f"{name}缺少当前镜头唯一服装状态")
    headwear_change_tokens = (
        "戴上", "戴好", "摘下", "摘去", "取下", "脱下", "去掉",
        "除去", "换帽", "换冠", "换头饰", "重新戴",
    )
    for name in actor_names:
        phase_headwear = {}
        for phase, appearances in (
                ("start", start_appearance), ("end", end_appearance)):
            look = appearances.get(name) or {}
            raw_headwear = look.get("headwear")
            if raw_headwear is None and look.get("hair_visibility") is None:
                continue
            headwear = _normalize_headwear({
                "headwear": raw_headwear,
                "hair_visibility": (
                    look.get("hair_visibility")
                    or (raw_headwear.get("hair_visibility")
                        if isinstance(raw_headwear, dict) else None)),
                "hair_makeup": look.get("hair_makeup"),
            })
            phase_headwear[phase] = headwear
            issues.extend(
                f"{name}{phase}头部状态冲突：{_text(value)}"
                for value in headwear.get("issues") or []
                if _text(value))
        start_headwear = phase_headwear.get("start")
        end_headwear = phase_headwear.get("end")
        if start_headwear and end_headwear:
            start_signature = (
                start_headwear.get("presence"),
                start_headwear.get("kind"),
                _text(start_headwear.get("name")),
                start_headwear.get("hair_visibility"),
            )
            end_signature = (
                end_headwear.get("presence"),
                end_headwear.get("kind"),
                _text(end_headwear.get("name")),
                end_headwear.get("hair_visibility"),
            )
            local_action = _actor_semantic_clause(
                action, name, actor_names)
            if len(actor_names) == 1 and not local_action:
                local_action = action
            if (start_signature != end_signature
                    and not any(
                        token in local_action
                        for token in headwear_change_tokens)):
                issues.append(
                    f"{name}起点与终点 headwear/hair_visibility 不同，"
                    "但本镜没有摘戴或更换头饰动作")
    issues.extend(
        _text(value) for value in contract.get("appearance_issues") or []
        if _text(value))
    start = _text(contract.get("start"))
    end = _text(contract.get("end"))
    state_text = f"{start} {end}"
    lie = any(token in action for token in (
        "仰卧", "卧榻", "卧床", "躺下", "躺在", "睡在"))
    sit = any(token in action for token in (
        "坐在", "坐于", "伏案", "趴向", "趴在"))
    if "站立" in state_text and lie:
        issues.append("人物状态要求站立，但当前动作要求仰卧")
    if "站立" in state_text and sit:
        issues.append("人物状态要求站立，但当前动作要求坐姿/伏案")

    # The current-shot action is more local than a character's global design.
    # Reject contracts such as “沈砚布旅装” + “沈砚穿青官袍” before any
    # billable generation call.
    for name in actor_names:
        local_clause = _actor_local_clause(action, name)
        appearance_change = any(
            token in local_clause
            for token in SEMANTIC_APPEARANCE_CHANGE_TOKENS)
        local_signature = _semantic_wardrobe_signature(local_clause)
        start_wardrobe = _text(
            (start_appearance.get(name) or {}).get("wardrobe"))
        end_wardrobe = _text(
            (end_appearance.get(name) or {}).get("wardrobe"))
        start_signature = _semantic_wardrobe_signature(start_wardrobe)
        end_signature = _semantic_wardrobe_signature(end_wardrobe)
        if (local_signature and start_signature
                and _semantic_wardrobes_conflict(
                    local_clause, start_wardrobe)
                and not appearance_change):
            issues.append(
                f"{name}当前动作服装「{local_clause}」与起点服装"
                f"「{start_wardrobe}」冲突")
        if (local_signature and end_signature
                and _semantic_wardrobes_conflict(
                    local_clause, end_wardrobe)):
            issues.append(
                f"{name}当前动作服装「{local_clause}」与终点服装"
                f"「{end_wardrobe}」冲突")
        if (start_signature and end_signature
                and _semantic_wardrobes_conflict(
                    start_wardrobe, end_wardrobe)
                and not appearance_change):
            issues.append(
                f"{name}起点与终点服装不同，但本镜没有换装动作")
        placed_signature = {
            token for token in start_signature | end_signature
            if _has_placed_object_mention(action, token)
        }
        duplicate_is_explicit = any(
            marker in action for marker in (
                "另一件", "另有一件", "第二件", "两件", "备用"))
        if (placed_signature and not duplicate_is_explicit
                and not local_signature.isdisjoint(placed_signature)):
            # The actor-local clause explicitly wears the same garment that is
            # also staged elsewhere.  Without a second-item fact, this is the
            # exact “one robe worn and beside the bed” contradiction.
            garment = "、".join(sorted(
                local_signature & placed_signature))
            issues.append(
                f"{name}身上的「{garment}」又被当作独立物件放在场景中；"
                "若确有第二件必须写明数量，否则只保留一个位置")

    # Condition checks are actor-local. A surviving actor may react while
    # another dies, and an awakening actor may act only after the declared
    # consciousness transition.
    conditions = contract.get("character_conditions") or {}
    if not isinstance(conditions, dict):
        issues.append("character_conditions 必须是按角色名索引的对象")
        conditions = {}
    performance = _text(contract.get("performance"))
    speaker = _text(contract.get("speaker"))
    dialogue = _text(contract.get("dialogue"))
    for name in actor_names:
        local_action = _actor_semantic_clause(action, name, actor_names)
        if len(actor_names) == 1 and not local_action:
            local_action = action
        local_performance = _actor_semantic_clause(
            performance, name, actor_names)
        if len(actor_names) == 1 and not local_performance:
            local_performance = performance
        state_clauses = {}
        for phase, value in (("start", start), ("end", end)):
            match = re.search(
                rf"(?:^|；){re.escape(name)}:([^；]*)", value)
            if match:
                state_clauses[phase] = match.group(1)
        phases = conditions.get(name) or {}
        if not isinstance(phases, dict):
            issues.append(f"{name}的 character_conditions 必须是对象")
            phases = {}
        raw_start_condition = phases.get("start")
        raw_end_condition = phases.get("end")
        start_condition = _normalize_character_condition(
            {"condition": raw_start_condition}
            if isinstance(raw_start_condition, dict)
            else {"pose": state_clauses.get("start", "")})
        end_condition = _normalize_character_condition(
            {"condition": raw_end_condition}
            if isinstance(raw_end_condition, dict)
            else {"pose": state_clauses.get("end", "")},
            local_action)
        phase_conditions = [
            ("start", start_condition), ("end", end_condition)]
        condition_output = contract.get("output") or {}
        if (isinstance(condition_output, dict)
                and condition_output.get("media") == "image"
                and condition_output.get("frame_phase") == "freeze"):
            raw_freeze_condition = phases.get("freeze")
            if not isinstance(raw_freeze_condition, dict):
                issues.append(
                    f"{name}的 phase=freeze 缺少 "
                    "character_conditions.freeze")
            else:
                issues.extend(
                    f"{name}freeze condition 冲突：{_text(value)}"
                    for value in raw_freeze_condition.get("issues") or []
                    if _text(value))
                phase_conditions.append((
                    "freeze",
                    _normalize_character_condition({
                        "condition": raw_freeze_condition})))
        for phase, condition in phase_conditions:
            issues.extend(
                f"{name}{phase} condition 冲突：{_text(value)}"
                for value in condition.get("issues") or []
                if _text(value))

        action_hits = _behavior_hits(local_action)
        performance_hits = _behavior_hits(local_performance)
        state_hits = _behavior_hits(
            " ".join(state_clauses.values()))
        dialogue_hits = {"说话"} if speaker == name and dialogue else set()
        all_hits = (
            action_hits | performance_hits | state_hits | dialogue_hits)
        start_life = start_condition.get("life_state")
        end_life = end_condition.get("life_state")
        death_transition = _actor_death_transition(
            action, name, actor_names)
        if death_transition and end_life != "dead":
            issues.append(
                f"{name}发生明确死亡过程，但 end.condition.life_state"
                " 未登记为 dead")
        forbidden = set()
        if start_life == "dead":
            forbidden |= all_hits
        elif end_life == "dead":
            if not death_transition:
                forbidden |= all_hits
            else:
                # Actions before a declared death may be alive actions. Anything
                # assigned to performance/end state, or written after the death
                # token, is post-terminal and therefore impossible.
                forbidden |= performance_hits
                terminal_indexes = [
                    local_action.find(token)
                    for token in TERMINAL_DEATH_TOKENS
                    if token in local_action]
                if terminal_indexes:
                    forbidden |= _behavior_hits(
                        local_action[min(terminal_indexes):])
        if start_life == "nonliving" or end_life == "nonliving":
            forbidden |= all_hits
        if forbidden:
            if "dead" in {start_life, end_life}:
                issues.append(
                    f"{name}已死亡/呈尸身态，却仍被要求"
                    + "、".join(sorted(forbidden)))
            else:
                issues.append(
                    f"{name}的 life_state={end_life or start_life}，"
                    "却仍被要求" + "、".join(sorted(forbidden)))

        start_consciousness = start_condition.get("consciousness_state")
        end_consciousness = end_condition.get("consciousness_state")
        restrictive_states = {"asleep", "unconscious"}
        wake_transition = any(
            token in local_action for token in WAKE_TRANSITION_TOKENS)
        sleep_transition = any(
            token in local_action for token in SLEEP_TRANSITION_TOKENS)
        loses_consciousness = any(
            token in local_action for token in UNCONSCIOUS_TRANSITION_TOKENS)
        active_mind_hits = all_hits & {
            "注视/眨眼", "说话", "微表情", "主动动作",
        }
        consciousness_forbidden = set()
        if (start_consciousness in restrictive_states
                and end_consciousness in restrictive_states):
            consciousness_forbidden |= active_mind_hits
        elif (start_consciousness in restrictive_states
              and end_consciousness == "awake"):
            if not wake_transition:
                consciousness_forbidden |= active_mind_hits
        elif end_consciousness in restrictive_states:
            expected_transition = (
                sleep_transition
                if end_consciousness == "asleep"
                else loses_consciousness)
            if not expected_transition:
                consciousness_forbidden |= active_mind_hits
            else:
                # End-state performance belongs after the transition.
                consciousness_forbidden |= performance_hits & {
                    "注视/眨眼", "说话", "微表情", "主动动作",
                }
        if consciousness_forbidden:
            issues.append(
                f"{name}的 consciousness_state="
                f"{end_consciousness or start_consciousness}，"
                "却仍被要求"
                + "、".join(sorted(consciousness_forbidden)))

        mobility_values = {
            start_condition.get("mobility"),
            end_condition.get("mobility"),
        }
        if ("immobile" in mobility_values
                and "主动动作" in all_hits
                and not (wake_transition or sleep_transition
                         or loses_consciousness or death_transition)):
            issues.append(
                f"{name}的 mobility=immobile，却仍被要求主动动作")

    era_constraints = [
        _text(value) for value in (
            contract.get("era_object_constraints") or [])
        if _text(value)
    ]
    if ("油灯" in action
            and any(token in f"{contract.get('scene', '')} "
                    f"{contract.get('era_context', '')} "
                    f"{contract.get('style', '')}"
                    for token in PREMODERN_CHINESE_ERA_TOKENS)
            and not any("时代物件锁定—油灯" in value
                        for value in era_constraints)):
        issues.append("历史场景中的油灯缺少时代结构锁定，容易误生成玻璃煤油灯")

    camera = contract.get("camera") or {}
    angle = _text(camera.get("角度"))
    physical_contract = contract.get("physical") or {}
    if not isinstance(physical_contract, dict):
        physical_contract = {}
        issues.append("physical 必须是对象")
    physical = " ".join(physical_contract.get("rules") or [])
    if angle in {"顶拍", "俯拍"} and any(
            token in physical for token in ("低机位", "仰拍")):
        issues.append("镜头要求俯拍/顶拍，但空间合同要求低机位/仰拍")
    if angle == "仰拍" and any(
            token in physical for token in ("顶拍", "摄影机在人物上方")):
        issues.append("镜头要求仰拍，但空间合同要求人物上方俯拍")

    issues.extend(
        _text(value) for value in contract.get("prop_issues") or []
        if _text(value))
    frame_props = contract.get("frame_props")
    if frame_props is None:
        frame_props = physical_contract.get("frame_props") or []
    if not isinstance(frame_props, list):
        issues.append("frame_props 必须是列表")
        frame_props = []
    prop_registry = contract.get("prop_registry")
    if not isinstance(prop_registry, list):
        issues.append("prop_registry 必须是列表")
        prop_registry = []
    registered_prop_ids = set()
    for position, item in enumerate(prop_registry, 1):
        if not isinstance(item, dict):
            issues.append(f"prop_registry[{position}] 必须是对象")
            continue
        prop_id = _text(item.get("prop_id"))
        if not prop_id:
            issues.append(f"prop_registry[{position}] 缺少 prop_id")
        elif prop_id in registered_prop_ids:
            issues.append(f"prop_registry 中 prop_id 重复：{prop_id}")
        else:
            registered_prop_ids.add(prop_id)
    physical_positions = {}
    physical_prop_phases = set()
    declared_prop_phases = set()
    for position, item in enumerate(frame_props, 1):
        if not isinstance(item, dict):
            issues.append(f"第{position}个 frame_prop 必须是对象")
            continue
        prop_id = _text(item.get("prop_id"))
        phase = _text(item.get("phase"))
        location = _prop_main_position(item)
        representation = _text(
            item.get("representation") or item.get("occurrence_type")
            or item.get("presence_type") or item.get("channel"),
            "physical").lower()
        visibility = _text(item.get("visibility"), "visible").lower()
        disclosure = _is_prop_disclosure(representation)
        physical_instance = visibility != "absent" and not disclosure
        if not prop_id:
            issues.append(f"第{position}个 frame_prop 缺少 prop_id")
            continue
        if prop_id not in registered_prop_ids:
            issues.append(f"frame_prop「{prop_id}」未登记到 prop_registry")
        if visibility not in {"visible", "occluded", "hidden", "absent"}:
            issues.append(
                f"frame_prop「{prop_id}」的 visibility 非法：{visibility}")
        if representation not in {
                "physical", "reflection", "screen", "painting", "overlay"}:
            issues.append(
                f"frame_prop「{prop_id}」的 representation 非法："
                f"{representation}")
        for field in ("physical_state", "holder", "location", "support"):
            if not _text(item.get(field)):
                issues.append(
                    f"frame_prop「{prop_id}」的 {field} 必须显式填写，"
                    "无则写 none")
        declared_prop_phases.add((prop_id, phase))
        if phase not in {"start", "end", "freeze"}:
            issues.append(
                f"frame_prop「{prop_id}」的 phase 必须是 start/end/freeze")
        if physical_instance and not location:
            issues.append(
                f"frame_prop「{prop_id}」是物理实例但缺少主位置")
        if not physical_instance:
            # Mirror/screen/painting appearances disclose the same object but
            # never create another physical location.
            continue
        key = (prop_id, phase)
        physical_prop_phases.add(key)
        previous_location = physical_positions.get(key)
        if previous_location:
            if previous_location != location:
                issues.append(
                    f"同一 physical prop_id「{prop_id}」在 phase={phase}"
                    f"同时位于「{previous_location}」和「{location}」；"
                    "同一 phase 只能有一个物理主位置")
            else:
                issues.append(
                    f"同一 physical prop_id「{prop_id}」在 phase={phase}"
                    "被重复登记；一个 prop_id 只能代表一个物理实例")
        elif location:
            physical_positions[key] = location

    prop_transitions = contract.get("prop_transitions")
    if prop_transitions is None:
        prop_transitions = physical_contract.get("prop_transitions") or []
    if not isinstance(prop_transitions, list):
        issues.append("prop_transitions 必须是列表")
        prop_transitions = []
    for position, item in enumerate(prop_transitions, 1):
        if not isinstance(item, dict):
            issues.append(f"第{position}个 prop_transition 必须是对象")
            continue
        prop_id = _text(item.get("prop_id"))
        from_phase = _text(item.get("from_phase"))
        to_phase = _text(item.get("to_phase"))
        if not prop_id:
            issues.append(f"第{position}个 prop_transition 缺少 prop_id")
        if from_phase != "start" or to_phase != "end":
            issues.append(
                f"prop_transition「{prop_id or position}」必须从 start 到 end")
        if prop_id and (prop_id, from_phase) not in declared_prop_phases:
            issues.append(
                f"prop_transition「{prop_id}」的 {from_phase} 状态"
                "必须由对应 frame_props 声明")
        if prop_id and (prop_id, to_phase) not in declared_prop_phases:
            issues.append(
                f"prop_transition「{prop_id}」的 {to_phase} 状态"
                "必须由对应 frame_props 声明")

    output_for_props = contract.get("output") or {}
    if isinstance(output_for_props, dict):
        prop_media = _text(output_for_props.get("media"))
        target_phase = _text(output_for_props.get("frame_phase"))
        if prop_media == "video":
            timeline_prop_ids = {
                prop_id for prop_id, phase in declared_prop_phases
                if phase in {"start", "end"}}
            for prop_id in timeline_prop_ids:
                for required_phase in ("start", "end"):
                    if (prop_id, required_phase) not in declared_prop_phases:
                        issues.append(
                            f"视频中的 prop_id「{prop_id}」缺少 "
                            f"phase={required_phase} 的 frame_props；"
                            "未出现时也必须显式写 visibility=absent")
        if prop_media == "image" and prop_transitions:
            for item in prop_transitions:
                if not isinstance(item, dict):
                    continue
                prop_id = _text(item.get("prop_id"))
                if (prop_id
                        and (prop_id, "freeze") not in declared_prop_phases
                        and (prop_id, target_phase)
                        not in declared_prop_phases):
                    issues.append(
                        f"静态图中的 prop_id「{prop_id}」只有 transition，"
                        "缺少 phase=freeze（或当前 start/end）的 frame_props"
                        " 定格记录；"
                        "transition 不能代替静态定格状态")

    relations = (
        contract.get("spatial_relations")
        if "spatial_relations" in contract
        else physical_contract.get("spatial_relations"))
    if relations is None:
        relations = []
    if not isinstance(relations, list):
        issues.append("spatial_relations 必须是列表")
    else:
        for position, item in enumerate(relations, 1):
            if not isinstance(item, dict):
                issues.append(f"第{position}条空间关系必须是对象")
                continue
            if not _text(item.get("subject")):
                issues.append(f"第{position}条空间关系缺少 subject")
            if not _text(item.get("relation") or item.get("predicate")):
                issues.append(
                    f"第{position}条空间关系缺少 relation/predicate")
            if not _text(item.get("object")):
                issues.append(f"第{position}条空间关系缺少 object")

    medium = contract.get("medium") or {}
    if not isinstance(medium, dict):
        issues.append("medium 必须是对象")
        medium = {}
    issues.extend(
        _text(value) for value in medium.get("issues") or []
        if _text(value))
    if (medium.get("dimension") == "2D"
            and medium.get("semi_realistic_3d")):
        issues.append("视觉媒介 2D 与 3D 声明冲突，无法执行")
    output_present = "output" in contract
    output = contract.get("output") or {}
    if output_present and not isinstance(output, dict):
        issues.append("output 必须是对象")
    elif output_present:
        media = _text(output.get("media"))
        phase = _text(output.get("frame_phase"))
        policy = _text(output.get("temporal_policy"))
        if media not in {"image", "video"}:
            issues.append("output.media 必须是 image 或 video")
        if media == "image" and (
                phase not in {"start", "end", "freeze"}
                or policy != "terminal_only"):
            issues.append(
                "静态图 output 必须指定 start/end/freeze 且只使用 terminal_only")
        if media == "image":
            frame_target = contract.get("frame_target")
            target_policy = contract.get("frame_target_policy") or {}
            if not isinstance(target_policy, dict):
                target_policy = {}
            allow_legacy = bool(
                target_policy.get("allow_legacy_fallback"))
            if not isinstance(frame_target, dict):
                issues.append("静态图缺少显式 frame_target 对象")
                frame_target = {}
            target_state = _state_value(
                frame_target.get("state")
                if "state" in frame_target
                else contract.get("frame_target_state"))
            fallback = bool(
                frame_target.get(
                    "fallback", contract.get("frame_target_fallback")))
            explicit = bool(
                frame_target.get(
                    "explicit", contract.get("frame_target_explicit")))
            if not target_state:
                issues.append("静态图 frame_target 缺少唯一可见定格状态")
            elif any(
                    re.search(pattern, target_state)
                    for pattern in STATIC_PROCESS_PATTERNS) and not (
                    _HISTORY_MARKER_RE.search(target_state)
                    and not re.search(r"→", target_state)):
                # 上诉庭固化:「已经从昏迷中醒来」是交代历史、终态唯一,
                # 不是要求同帧画出两个阶段;带已/已经/先前/此前标记且
                # 无显式箭头轨迹的,放行。
                issues.append(
                    "静态图 frame_target 同时描述多个时间状态或动作过程；"
                    "必须改写为单一可见定格结果")
            if (fallback or not explicit) and not allow_legacy:
                issues.append(
                    "静态图必须由人工/上游显式声明 frame_target；"
                    "运行时从 start/end/description/action 回退默认阻断")
            if (explicit and not frame_target.get("fallback_declared")):
                issues.append(
                    "静态图 frame_target 必须显式声明 fallback=false")
            if allow_legacy and not _text(target_policy.get("name")):
                issues.append(
                    "静态图 legacy 兼容必须显式命名 frame_target_policy")
        if media == "video" and (
                phase != "timeline" or policy != "timeline"):
            issues.append("视频 output 必须指定 timeline")
    issues.extend(
        _text(value) for value in contract.get("output_issues") or []
        if _text(value))

    issues = list(dict.fromkeys(issues))
    warnings = []
    target_policy = contract.get("frame_target_policy") or {}
    frame_target = contract.get("frame_target") or {}
    if (isinstance(target_policy, dict)
            and target_policy.get("allow_legacy_fallback")
            and isinstance(frame_target, dict)
            and (frame_target.get("fallback")
                 or not frame_target.get("explicit"))):
        warnings.append(
            "当前静态图使用显式 legacy frame_target 兼容策略；"
            "应尽快迁移为上游直接登记的唯一静态定格")
    semantic_corrections = [
        dict(value) for value in (
            contract.get("semantic_corrections") or [])
        if isinstance(value, dict)
    ]
    status = "BLOCK" if issues else ("WARN" if warnings else "PASS")
    return {
        "passed": not issues,
        "status": status,
        "severity": status,
        "issues": issues,
        "warnings": warnings,
        "semantic_corrections": semantic_corrections,
    }


_FRAME_SEGMENT_SPLIT = re.compile(r"(?=【)")
_FRAME_SEGMENT_HEAD = re.compile(r"^【([^】]{1,24})】")


def _frame_segments(text):
    """把渲染好的静态合同按【标题】切段，保序。"""
    return [seg.rstrip()
            for seg in _FRAME_SEGMENT_SPLIT.split(str(text or ""))
            if seg.strip()]


def _frame_segment_head(segment):
    match = _FRAME_SEGMENT_HEAD.match(str(segment or ""))
    return match.group(1) if match else ""


def merge_frame_compacts(first_compact, last_compact):
    """首尾帧两份静态合同合并成「两帧共用 + 首帧独有 + 尾帧独有」。

    旧写法是把两份**完整**合同前后拼接。实测 EP1 一条 frames 提示词
    9153 字、35 段，两半各 17 段结构完全相同，其中 13 段逐字节重复
    (主体 435、参考图职责 1468、画风 464、道具定格 363…)，合计 4348 字
    = 全文 47.5% 是纯复制。真正区分首尾帧的只有【核心画面】和
    【定格状态】两段、加起来不到 270 字。

    模型要在两份几乎一样的合同里找出那几处差别，差别反而被淹没——
    这正是「提示词越长越不准」的具体机制。合并后共用段只说一次，
    差异段单独列出并显式标注归属。
    """
    first_segs = _frame_segments(first_compact)
    last_segs = _frame_segments(last_compact)
    if not first_segs or not last_segs:
        # 任一侧解析不出段落就退回原样拼接，宁可冗余也不丢事实。
        return None
    last_pool = {}
    for seg in last_segs:
        last_pool.setdefault(_frame_segment_head(seg), []).append(seg)
    shared, first_only, matched = [], [], set()
    for seg in first_segs:
        head = _frame_segment_head(seg)
        pool = last_pool.get(head) or []
        if seg in pool:
            shared.append(seg)
            pool.remove(seg)
            matched.add(id(seg))
        else:
            first_only.append(seg)
    remaining = [seg for segs in last_pool.values() for seg in segs]
    last_only = [seg for seg in last_segs if seg in remaining]
    if not first_only and not last_only:
        # 两份完全一致说明相位没生效，这本身是缺陷；照原样返回让上游可见。
        return None
    blocks = ["【两帧共用】以下事实首帧与尾帧完全相同，只声明一次：",
              "\n".join(shared)]
    if first_only:
        blocks += ["【仅首帧】以下是首帧独有的状态，尾帧不得沿用：",
                   "\n".join(first_only)]
    if last_only:
        blocks += ["【仅尾帧】以下是尾帧独有的状态，首帧不得提前出现：",
                   "\n".join(last_only)]
    blocks.append(
        "【联合生成约束】分别执行两份静态合同；共用段两帧照搬，"
        "差异段各归各帧；首帧不得混入尾帧状态，"
        "尾帧不得保留已完成动作的起点状态。")
    return "\n".join(blocks)
