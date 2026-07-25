"""可执行的镜头提示词合同。

Seedance/图片模型更容易稳定执行“对象 → 场景 → 单一动作 → 摄影机 →
起止状态”这样的短结构。这个模块只做确定性的编译，不替模型补剧情，也不
把参考图的多个职责混在一条长提示词里。完整提示词仍由导演保存作审计，模型
请求优先使用这里编译出的短版。
"""

from __future__ import annotations

import re


PROMPT_CONTRACT_SCHEMA = "aifos.shot-prompt/v2"
PHYSICAL_CONTRACT_SCHEMA = "aifos.physical-space/v1"
NON_PICTURE_TEXT_CARRIERS = ("字幕", "对白字幕", "旁白字幕", "台词字幕")
FORBIDDEN_ON_SCREEN_METADATA = (
    "镜头合同", "主体", "场景", "起点", "终点", "单一主动作", "TASK",
    "WORLD / STYLE", "质检原因", "自动优化修订", "参考图职责", "硬约束",
)


def _text(value, fallback=""):
    value = "" if value is None else str(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or fallback


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


def _state_line(states):
    values = []
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
        "expected_visible_figure_count": len(characters),
        "actors": actors,
        "count_rule": (
            "前景半身背影/肩膀是已登记的对话者本人，只计作该角色1人，"
            "不得另算成新增人物或人物复制"
            if over_shoulder else
            "近机位肩背、头部和可见侧脸必须属于同一具连续身体，"
            "同一人物只出现一次，不得生成第二具身体"
            if single_over_shoulder else "每个可见人物只计一次"),
    }


def build_shot_prompt_contract(shot, *, location="", style="", references=None):
    """从已通过五维分镜的镜头构造可审计的结构化合同。

    不读取故事背景长文；只有当镜头实际需要时才保留场景、动作和状态，避免
    全局风格/角色经历与参考图抢控制权。
    """
    characters = list(shot.get("characters") or [])
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
        if not isinstance(item, dict) or not item.get("index"):
            continue
        refs.append({
            "index": item.get("index"),
            "label": _text(item.get("label") or item.get("name"), "参考图"),
            "role": _text(item.get("role") or item.get("kind"), "reference"),
            "character": _text(item.get("character")),
        })
    scene = shot_local_scene(shot, location)
    physical = build_physical_contract(shot)
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
    # Never fall back to the raw storyboard prompt here. It may contain the
    # whole episode bible and unrelated scenes, which makes the provider blend
    # facts from other shots into this image.
    action = _text(
        shot.get("description") or shot.get("action"),
        "环境保持稳定，只执行自然微动",
    )
    contract = {
        "schema": PROMPT_CONTRACT_SCHEMA,
        "mode": "shot",
        "frame_kind": _text(shot.get("frame_kind")),
        "subject": {
            "count": len(characters),
            "actors": _character_lines(shot),
        },
        "composition": (
            shot.get("composition_contract")
            if isinstance(shot.get("composition_contract"), dict)
            else build_composition_contract(shot)),
        "scene": scene,
        "style": _text(style),
        "start": _state_line(shot.get("start_state")) or "保持首帧状态",
        "start_appearance": _appearance_map(shot.get("start_state")),
        "action": action,
        "performance": _text(
            (shot.get("performance") or {}).get("micro_expression"),
            "自然微表情、呼吸和重心变化",
        ),
        "camera": _camera(shot),
        "physical": physical,
        "end": _state_line(shot.get("end_state")) or "到达尾帧状态",
        "end_appearance": _appearance_map(shot.get("end_state")),
        "appearance_state_required": bool(
            shot.get("appearance_state_required")),
        "appearance_issues": [
            _text(value) for value in (
                shot.get("appearance_continuity_issues") or [])
            if _text(value)
        ],
        "dialogue": (
            "" if dialogue.get("inner_voice")
            else _text(dialogue.get("dialogue"))),
        "speaker": (
            "" if dialogue.get("inner_voice")
            else _text(dialogue.get("character"))),
        "inner_voice": bool(dialogue.get("inner_voice")),
        "text": text_rule,
        "narrative_overlays": overlays[:1],
        "references": refs,
        "hard": (
            "只执行一个主动作和一个运镜；人物身份、服装、场景、构图分别服从"
            "对应参考图；不得重新设计人物，不得新增/复制真实人物或把参考图内容"
            "贴进成片；服装、头饰、妆发必须逐人服从本镜起止状态，未写换装/"
            "摘戴/改妆动作时不得自行改变；非现实Q版叠层不得转化成真人、"
            "实体角色或空间站位"
        ),
    }
    return contract


def _reference_role(item):
    role = _text(item.get("role") or item.get("kind"))
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


def render_shot_prompt(contract, *, mode="image"):
    """把合同渲染成短句；图片和视频共用同一组语义，只改变媒介边界。"""
    subject = "、".join(contract["subject"]["actors"]) or "无人"
    count = contract["subject"]["count"]
    camera = contract["camera"]
    camera_values = [
        camera.get("景别"), camera.get("角度"), camera.get("焦段"),
        camera.get("机位"),
    ]
    if mode == "video":
        camera_values.append(
            f"{camera.get('运镜')}({camera.get('动机')})")
    else:
        camera_values.append("静态关键帧只定格最终可见机位与构图")
    camera_values.append(f"构图{camera.get('构图')}")
    camera_line = "；".join(value for value in camera_values if value)
    lines = [
        "【镜头合同v2】只执行下列事实，不自行补剧情。",
        f"【主体】严格共{count}人：{subject}（均为真实人物）。",
        f"【场景】{contract['scene']}。",
        f"【起点】{contract['start']}。",
        f"【单一主动作】{contract['action']}。",
        f"【表演】{contract['performance']}。",
        f"【镜头】{camera_line}。",
        f"【终点】{contract['end']}。",
    ]
    overlays = contract.get("narrative_overlays") or []
    if overlays:
        overlay = overlays[0]
        inner_dialogue = (
            f"；内心说「{overlay['dialogue']}」"
            if overlay.get("dialogue") else "")
        lines.insert(
            2,
            "【非现实内心Q版叠层】"
            f"{overlay['name']}是{overlay['host_character']}的内心人格，"
            f"用途={overlay['function']}；{overlay['expression']}；"
            f"{overlay['action']}{inner_dialogue}。它不是真实人物，不计入"
            f"上述{count}名真实主体，不参与物理站位、遮挡、空间调度或真实"
            "连续性；只有宿主内心感知，其他人物不得看见、回应、触碰、对视；"
            "严格保持大头小身：约1.8头身，头占总高约58%，身体、肩宽、"
            "躯干、四肢、手脚都明显小于头部；"
            "继承锁定的当前衣着，不继承默认道具；内心发声时宿主闭口，"
            "不画旁白/吐槽字幕。")
    physical = contract.get("physical") or {}
    physical_rules = "；".join(physical.get("rules") or [])
    if physical_rules:
        lines.insert(
            6,
            f"【物理/空间逻辑】{physical_rules}"
            + (f"；对象关系：{'；'.join(physical.get('objects') or [])}。"
               if physical.get("objects") else "。"))
    composition = contract.get("composition") or {}
    if composition.get("composition_type") == "over_shoulder_dialogue":
        duties = "；".join(
            f"{item.get('character')}={item.get('role')}/"
            f"{item.get('expected_view')}"
            for item in composition.get("actors") or [])
        lines.insert(
            3,
            "【过肩构图】"
            f"主体{composition.get('expected_primary_count', 1)}人，"
            f"实际可见人形{composition.get('expected_visible_figure_count', count)}人；"
            f"{duties}；{composition.get('count_rule', '')}。")
    elif composition.get(
            "composition_type") == "single_subject_over_shoulder":
        duties = "；".join(
            f"{item.get('character')}={item.get('role')}/"
            f"{item.get('expected_view')}"
            for item in composition.get("actors") or [])
        lines.insert(
            3,
            "【单人过肩构图】"
            f"严格只有1名人物、1具连续身体；{duties}；"
            f"{composition.get('count_rule', '')}。")
    if contract.get("style"):
        lines.append(f"【画风】{contract['style']}（只沿用项目基准，不改媒介）。")
    if contract.get("dialogue"):
        speaker = contract.get("speaker") or "说话人"
        lines.append(f"【对白】{speaker}说出「{contract['dialogue']}」，自然口型；不画字幕。")
    lines.append(f"【文字】{contract['text']}。")
    if mode == "video":
        lines.insert(1, "【输入】图1是唯一动作起点，图2是唯一动作终点；只让已锁定画面动起来。")
    if contract.get("frame_kind") in {"first_frame", "last_frame"}:
        label = "首帧" if contract["frame_kind"] == "first_frame" else "尾帧"
        state = contract["start"] if label == "首帧" else contract["end"]
        lines.insert(
            1,
            f"【单帧修改】只生成{label}这一张；目标状态={state}；"
            "保持待修改基底中未被反馈点名的内容不变。")
    if contract.get("references"):
        refs = "；".join(
            f"图{item['index']}={item['label']}({_reference_role(item)})"
            for item in contract["references"]
        )
        lines.append(f"【参考图职责】{refs}。")
    lines.append(f"【硬约束】{contract['hard']}。")
    return "\n".join(lines)


def compile_shot_prompt(shot, *, location="", style="", references=None, mode="image"):
    contract = build_shot_prompt_contract(
        shot, location=location, style=style, references=references)
    return contract, render_shot_prompt(contract, mode=mode)


def validate_shot_prompt_contract(contract):
    """Fail before generation when a shot contract cannot be executed."""
    contract = contract if isinstance(contract, dict) else {}
    issues = []
    subject = contract.get("subject") or {}
    count = int(subject.get("count") or 0)
    composition = contract.get("composition") or {}
    if composition.get("expected_visible_figure_count", count) != count:
        issues.append("构图可见人数与镜头人物名单不一致")
    if (composition.get("composition_type") == "over_shoulder_dialogue"
            and count < 2):
        issues.append("单人镜头不得使用双人过肩对话合同")

    refs = contract.get("references") or []
    indexes = [item.get("index") for item in refs if isinstance(item, dict)]
    if indexes and indexes != list(range(1, len(indexes) + 1)):
        issues.append("参考图编号不是与提交顺序一致的连续编号")

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

    camera = contract.get("camera") or {}
    angle = _text(camera.get("角度"))
    physical = " ".join(contract.get("physical", {}).get("rules") or [])
    if angle in {"顶拍", "俯拍"} and any(
            token in physical for token in ("低机位", "仰拍")):
        issues.append("镜头要求俯拍/顶拍，但空间合同要求低机位/仰拍")
    if angle == "仰拍" and any(
            token in physical for token in ("顶拍", "摄影机在人物上方")):
        issues.append("镜头要求仰拍，但空间合同要求人物上方俯拍")

    return {
        "passed": not issues,
        "issues": list(dict.fromkeys(issues)),
    }
