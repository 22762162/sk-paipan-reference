"""可执行的镜头提示词合同。

Seedance/图片模型更容易稳定执行“对象 → 场景 → 单一动作 → 摄影机 →
起止状态”这样的短结构。这个模块只做确定性的编译，不替模型补剧情，也不
把参考图的多个职责混在一条长提示词里。完整提示词仍由导演保存作审计，模型
请求优先使用这里编译出的短版。
"""

from __future__ import annotations

import re


PROMPT_CONTRACT_SCHEMA = "aifos.shot-prompt/v2.1"
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
)
LIVING_PERFORMANCE_TOKENS = (
    "呼吸", "喘息", "眼神变化", "视线变化", "微表情", "眨眼", "开口",
)
PREMODERN_CHINESE_ERA_TOKENS = (
    "明初", "明代", "大明", "洪武", "永乐", "古代", "县衙", "驿馆",
    "官舍", "公堂",
)
VAGUE_POPULATION_TOKENS = (
    "几名", "数名", "多名", "若干名", "一群", "人群", "众人", "一众",
    "多人", "几人", "数人", "若干人", "成群", "大批人",
)
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
        "inherits": ["face", "hairstyle", "age", "gender"],
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
        "exclude": [
            "identity", "wardrobe", "pose", "composition", "background",
            "lighting", "prop_position",
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
        "hair_makeup", "prop", "injury", "emotion",
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


def _frame_target(shot, mode, requested_mode=""):
    """Select one static freeze state, or the three-part video timeline."""
    start = _registered_state_value(
        shot, "start_state") or "保持首帧状态"
    end = _registered_state_value(
        shot, "end_state") or "到达尾帧状态"
    action = _text(
        shot.get("description") or shot.get("action"),
        "环境保持稳定，只执行自然微动",
    )
    if mode == "video":
        return {
            "phase": "timeline",
            "state": {"start": start, "action": action, "end": end},
            "source": "start_state/action/end_state",
            "fallback": False,
        }

    frame_kind = (
        _text(shot.get("frame_kind")).lower()
        or _text(requested_mode).lower())
    phase = "start" if frame_kind == "first_frame" else "end"
    for source in ("freeze_state", "image_state"):
        state = _state_value(shot.get(source))
        if state:
            return {
                "phase": phase,
                "state": state,
                "source": source,
                "fallback": False,
            }
    state_source = "start_state" if phase == "start" else "end_state"
    state = _registered_state_value(shot, state_source)
    if state:
        return {
            "phase": phase,
            "state": state,
            "source": state_source,
            "fallback": False,
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
    }


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
    return normalized, issues


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
    excludes = list(defaults["exclude"])
    for value in _text_list(explicit_exclude):
        if value not in excludes:
            excludes.append(value)
    # Safe role boundaries cannot be weakened by an explicit include. Keeping
    # an unsafe item in both lists makes the conflict visible to preflight.
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
    tail = source[match.start():match.start() + 180]
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
    historical = any(token in context for token in
                     PREMODERN_CHINESE_ERA_TOKENS)
    if not historical:
        return []
    rules = []
    if "油灯" in visible and not any(
            token in sanctioned for token in (
                "煤油灯", "玻璃灯罩", "玻璃罩灯")):
        rules.append(
            "时代物件锁定—油灯：只画明代可成立的陶制或青铜开放式浅盏"
            "油灯，灯油与棉芯可见；绝不画玻璃灯罩、煤油灯筒、现代调节"
            "旋钮、电灯泡或工业金属灯座")
    if any(token in visible for token in ("提灯", "灯笼")) and not any(
            token in sanctioned for token in (
                "玻璃提灯", "煤油提灯")):
        rules.append(
            "时代物件锁定—提灯：只画明代竹木骨架配纸或薄绢灯罩的笼灯，"
            "内部烛火受罩保护；绝不画玻璃煤油提灯、现代金属提手灯或电灯")
    if any(token in visible for token in ("烛台", "烛火", "孤烛")):
        rules.append(
            "时代物件锁定—烛台：使用裸露蜡烛与明代可成立的铜、铁或陶"
            "烛台；禁止套用玻璃灯罩、煤油灯芯机构或电灯结构")
    return list(dict.fromkeys(rules))


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
        for key, label in (
                ("wardrobe", "服装"), ("headwear", "头饰"),
                ("hair_makeup", "妆发"), ("prop", "道具"),
                ("injury", "伤势"), ("emotion", "情绪")):
            if _text(state.get(key)):
                details.append(f"{label}{_text(state[key])}")
        values.append(f"{_text(name)}:" + ",".join(details))
    return "；".join(values)


def _appearance_map(states):
    if not isinstance(states, dict):
        return {}
    return {
        _text(name): {
            key: _text(state.get(key))
            for key in ("wardrobe", "headwear", "hair_makeup")
            if _text(state.get(key))
        }
        for name, state in (states or {}).items()
        if isinstance(state, dict)
    }


def readable_text_required(value):
    """Only treat an explicit on-screen whitelist as an image text asset.

    Dialogue/subtitle metadata occasionally arrives as ``required=true`` with an
    empty whitelist. Sending that to an image model as "字幕 / 白名单为空"
    invites invented text even though the production profile forbids subtitles.
    """
    value = value if isinstance(value, dict) else {}
    if not value.get("required"):
        return False
    carrier = _text(value.get("carrier"))
    if any(label in carrier for label in NON_PICTURE_TEXT_CARRIERS):
        return False
    return bool(sanitize_text_whitelist(value.get("whitelist") or []))


def _camera(shot):
    dimensions = shot.get("five_dimensions") or {}
    design = dimensions.get("camera_design") or {}
    contract = shot.get("shot_contract") or {}
    raw = _text(shot.get("camera"))

    def explicit(tokens):
        return next((value for token, value in tokens if token in raw), "")

    # The author/director's explicit current-shot camera text is authoritative.
    # Five-dimension defaults may fill omissions, but must never contradict it.
    raw_scale = explicit((
        ("大特写", "大特写"), ("特写", "特写"), ("近景", "近景"),
        ("中景", "中景"), ("全景", "全景"), ("远景", "远景"),
    ))
    raw_angle = explicit((
        ("顶拍", "顶拍"), ("顶视", "顶拍"), ("鸟瞰", "顶拍"),
        ("俯拍", "俯拍"), ("高机位", "俯拍"), ("高角度", "俯拍"),
        ("仰拍", "仰拍"), ("低机位", "仰拍"), ("低角度", "仰拍"),
        ("平视", "平视"),
    ))
    raw_position = explicit((
        ("过肩", "过肩"), ("背面", "背面"), ("背后", "背面"),
        ("侧面", "侧面"), ("侧脸", "侧面"), ("正面", "正面"),
    ))
    raw_movement = explicit((
        ("急推", "急推"), ("缓推", "缓推"), ("推近", "推"),
        ("上摇", "上摇"), ("下摇", "下摇"), ("环绕", "环绕"),
        ("跟拍", "跟拍"), ("拉远", "拉"), ("横移", "移"),
        ("固定", "固定"),
    ))
    return {
        "景别": _text(
            raw_scale or contract.get("景别") or design.get("shot_scale"),
            "按分镜"),
        "角度": _text(
            raw_angle or contract.get("角度") or design.get("angle"),
            "保持轴线"),
        "焦段": _text(design.get("lens") or contract.get("焦段")),
        "机位": _text(
            raw_position or contract.get("机位")
            or design.get("camera_position")),
        "运镜": _text(
            raw_movement or contract.get("运镜") or design.get("movement"),
            "固定"),
        "动机": _text(design.get("movement_motivation"), "服务主体动作"),
        "构图": _text(
            contract.get("构图") or design.get("composition"), "主体清楚"),
    }


def shot_local_scene(shot, fallback=""):
    """Resolve only the current shot's visible place/era.

    Legacy storyboards often omitted a per-shot location and left only a raw
    prompt such as “现代书房闪回”. Do not silently use the episode's later
    historical scene in that case; use an explicit shot value or a narrow
    keyword hint, then fall back to the scene baseline.
    """
    shot = shot or {}
    explicit = _text(
        shot.get("location") or shot.get("scene_location")
        or shot.get("scene_context") or shot.get("world_state"))
    if explicit:
        return explicit
    text = " ".join(_text(value) for value in (
        shot.get("description"), shot.get("action"), shot.get("prompt"),
    ) if _text(value))
    hints = (
        ("现代书房", ("现代书房", "现代书桌")),
        ("现代办公室", ("现代办公室", "现代办公")),
        ("现代都市", ("现代都市", "都市街道", "现代街道")),
        ("明代东宫寝殿", ("东宫", "寝殿", "太子殿")),
        ("明代宫殿内景", ("明代宫殿", "宫殿", "紫禁城")),
    )
    for label, tokens in hints:
        if any(token in text for token in tokens):
            return label + ("（闪回）" if "闪回" in text and "现代" in label
                            else "")
    return _text(fallback, "按场景基准图")


def build_physical_contract(shot):
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
    shot_contract = shot.get("shot_contract")
    shot_contract = shot_contract if isinstance(shot_contract, dict) else {}
    description = " ".join(_text(value) for value in (
        shot.get("description"), shot.get("action"),
        shot_contract.get("画面内容描述"),
        shot_contract.get("构图"),
    ) if _text(value))
    carrier = _text((shot.get("readable_text") or {}).get("carrier"))
    object_text = f"{description} {carrier}".lower()
    generic = (
        "人物、镜头与道具的前后左右关系必须真实成立；道具服从重力并与桌面/地面/手部"
        "保持自然接触；人物朝向、视线和手部动作必须指向实际使用对象；禁止漂浮、穿模、"
        "镜像反向、无支撑或无法完成动作的姿势。"
    )
    if generic not in rules:
        rules.insert(0, generic)
    if any(token in object_text for token in (
            "笔记本", "电脑", " laptop", "屏幕", "显示器")):
        rules.append(
            "电脑使用关系：屏幕正面、键盘和使用者必须位于同一使用侧；键盘朝向使用者，"
            "屏幕与底座由铰链连接并由桌面支撑；人物视线落在屏幕可见区域。若需要看清屏幕文字，"
            "镜头必须采用使用者同侧的越肩或侧面机位，禁止人物坐在屏幕背面却看到屏幕正面。"
        )
        objects.append("笔记本电脑：使用者↔键盘/屏幕正面↔桌面支撑")
    elif any(token in object_text for token in ("手机", "平板", "tablet")):
        rules.append(
            "手持屏幕关系：屏幕正面必须朝向正在查看或展示的人；手指与机身接触自然，"
            "手腕、手臂和视线方向一致，禁止屏幕朝后却被人物读取。"
        )
        objects.append("手持屏幕：使用者/观看者↔屏幕正面")
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
                pos = _text(actor.get("start") or actor.get("position"))
                direction = _text(actor.get("direction"))
                if name and (pos or direction):
                    positions.append(f"{name}:{pos or '原位'}{('，朝向' + direction) if direction else ''}")
            if positions:
                rules.append("人物站位与朝向：" + "；".join(positions) + "。")
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
    single_over_shoulder = "过肩" in framing_text and len(characters) == 1
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
    characters = list(shot.get("characters") or [])
    output_media, requested_mode = _normalize_mode(mode)
    target = _frame_target(shot, output_media, requested_mode)
    functional_figures, population_issues = _normalize_functional_figures(
        shot)
    registered_count = len(characters)
    functional_count = sum(
        int(item.get("count") or 0) for item in functional_figures)
    visible_count = registered_count + functional_count
    dialogue = shot.get("dialogue") or {}
    readable = shot.get("readable_text") or {}
    if readable_text_required(readable):
        whitelist = "、".join(sanitize_text_whitelist(
            readable.get("whitelist") or [])) or "白名单"
        carrier = _text(readable.get("carrier"), "指定载体")
        layout = _text(readable.get("layout"))
        text_style = _text(readable.get("style"))
        perspective = _text(readable.get("perspective"))
        presentation = "；".join(filter(None, (
            f"版式/位置:{layout}" if layout else "",
            f"字体/颜色/层级:{text_style}" if text_style else "",
            f"透视/反光:{perspective}" if perspective else "",
        )))
        if any(token in carrier for token in ("电脑", "笔记本", "屏幕", "显示器")):
            text_rule = (
                f"电脑屏幕必须打开并清晰显示白名单原文:{whitelist}；"
                + (presentation + "；" if presentation else "")
                + "屏幕不是冷白光效/空白占位面，禁止随机乱码、模糊色块和黑白占位；"
                + "屏幕外无字幕、Logo、水印和无关文字"
            )
        else:
            text_rule = (f"{carrier}内文字只保持原样:{whitelist}；"
                         + (presentation + "；" if presentation else "")
                         + "禁止新增文字")
    else:
        text_rule = "无画面文字、无字幕、无Logo、无水印"
    refs = []
    for item in references or []:
        if not isinstance(item, dict):
            continue
        refs.append(_normalize_reference(item))
    scene = shot_local_scene(shot, location)
    physical = build_physical_contract({
        **shot, "location": scene, "style": style,
    })
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
    declared_visible = shot.get("visible_figure_count")
    if declared_visible is not None:
        if (not isinstance(declared_visible, int)
                or isinstance(declared_visible, bool)):
            population_issues.append("visible_figure_count 必须是整数")
        elif declared_visible != visible_count:
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
    if (functional_count == 0 and any(
            token in population_text for token in VAGUE_POPULATION_TOKENS)):
        population_issues.append(
            "镜头使用了几名/数名/多名/一群等模糊人数，但未声明"
            " functional_figures 的明确 count")
    # Never fall back to the raw storyboard prompt here. It may contain the
    # whole episode bible and unrelated scenes, which makes the provider blend
    # facts from other shots into this image.
    action = _text(
        shot.get("description") or shot.get("action"),
        "环境保持稳定，只执行自然微动",
    )
    composition = (
        dict(shot.get("composition_contract"))
        if isinstance(shot.get("composition_contract"), dict)
        else build_composition_contract({
            **shot, "functional_figures": functional_figures,
        }))
    composition["expected_visible_figure_count"] = visible_count
    medium = _normalize_visual_medium(shot, style)
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
        "frame_kind": _text(shot.get("frame_kind")),
        "subject": {
            # v1/v2 compatibility: count remains the number of identity-locked
            # named characters, not every visible human body.
            "count": registered_count,
            "registered_count": registered_count,
            "functional_count": functional_count,
            "visible_count": visible_count,
            "actors": _character_lines(shot),
            "functional_figures": functional_figures,
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
            "declared_visible_figure_count": declared_visible,
            "issues": population_issues,
        },
        "composition": composition,
        "scene": scene,
        "script_reference": _text(shot.get("script_reference")),
        "era_context": _text(shot.get("era_context")),
        "era_object_constraints": build_era_object_constraints({
            **shot, "location": scene, "style": style,
        }),
        "style": _text(style or shot.get("style")),
        "visual_medium": medium["dimension"],
        "medium": medium,
        "start": _registered_state_value(
            shot, "start_state") or "保持首帧状态",
        "start_appearance": _appearance_map(shot.get("start_state")),
        "action": action,
        "performance": _text(
            (shot.get("performance") or {}).get("micro_expression"),
            "自然微表情、呼吸和重心变化",
        ),
        "camera": _camera(shot),
        "physical": physical,
        "spatial_relations": list(physical.get("spatial_relations") or []),
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
            "实体角色或空间站位"
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
    lines = [
        "【镜头合同v2.1】只执行下列事实，不自行补剧情。",
    ]
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
            f"（fallback=true；来源={source or 'description/action'}）"
            if fallback else "")
        lines.extend([
            f"【定格状态】{target_state}{fallback_note}。",
            f"【镜头】{camera_line}。",
        ])
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
    lines.append(f"【硬约束】{contract['hard']}。")
    return "\n".join(lines)


def compile_shot_prompt(shot, *, location="", style="", references=None, mode="image"):
    contract = build_shot_prompt_contract(
        shot, location=location, style=style, references=references,
        mode=mode)
    return contract, render_shot_prompt(contract)


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
    actor_names = []
    for actor in subject.get("actors") or []:
        value = _text(actor)
        if "=" in value:
            value = value.split("=", 1)[1].split("（", 1)[0].strip()
        if value:
            actor_names.append(value)
    if contract.get("appearance_state_required"):
        for name in actor_names:
            start_look = start_appearance.get(name) or {}
            end_look = end_appearance.get(name) or {}
            if not _text(
                    start_look.get("wardrobe") or end_look.get("wardrobe")):
                issues.append(f"{name}缺少当前镜头唯一服装状态")
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

    # Death/living-state checks are actor-local.  A surviving actor may still
    # react in the same shot where another character dies.
    for name in actor_names:
        local_action = _actor_semantic_clause(action, name, actor_names)
        state_clauses = []
        for value in (start, end):
            match = re.search(
                rf"(?:^|；){re.escape(name)}:([^；]*)", value)
            if match:
                state_clauses.append(match.group(1))
        actor_state = " ".join(state_clauses)
        terminal_state = any(
            token in f"{local_action} {actor_state}"
            for token in TERMINAL_DEATH_TOKENS)
        explicitly_living = any(
            token in local_action for token in LIVING_PERFORMANCE_TOKENS)
        if len(actor_names) == 1:
            explicitly_living = explicitly_living or any(
                token in f"{contract.get('performance', '')} {end}"
                for token in LIVING_PERFORMANCE_TOKENS)
        if terminal_state and explicitly_living:
            issues.append(
                f"{name}已死亡/呈尸身态，却仍被要求呼吸、眨眼或表演微表情")

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
                phase not in {"start", "end"}
                or policy != "terminal_only"):
            issues.append("静态图 output 必须指定 start/end 且只使用 terminal_only")
        if media == "video" and (
                phase != "timeline" or policy != "timeline"):
            issues.append("视频 output 必须指定 timeline")
    issues.extend(
        _text(value) for value in contract.get("output_issues") or []
        if _text(value))

    return {
        "passed": not issues,
        "issues": list(dict.fromkeys(issues)),
        "semantic_corrections": [
            dict(value) for value in (
                contract.get("semantic_corrections") or [])
            if isinstance(value, dict)
        ],
    }
