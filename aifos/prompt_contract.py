"""可执行的镜头提示词合同。

Seedance/图片模型更容易稳定执行“对象 → 场景 → 单一动作 → 摄影机 →
起止状态”这样的短结构。这个模块只做确定性的编译，不替模型补剧情，也不
把参考图的多个职责混在一条长提示词里。完整提示词仍由导演保存作审计，模型
请求优先使用这里编译出的短版。
"""

from __future__ import annotations

import re


PROMPT_CONTRACT_SCHEMA = "aifos.shot-prompt/v1"
NON_PICTURE_TEXT_CARRIERS = ("字幕", "对白字幕", "旁白字幕", "台词字幕")


def _text(value, fallback=""):
    value = "" if value is None else str(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or fallback


def _state_line(states):
    values = []
    for name, state in (states or {}).items():
        state = state if isinstance(state, dict) else {}
        values.append(
            f"{_text(name)}:{_text(state.get('position')) or '原位'},"
            f"{_text(state.get('pose')) or '自然状态'},"
            f"朝向{_text(state.get('direction')) or '按画面'}"
        )
    return "；".join(values)


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
    return bool([item for item in (value.get("whitelist") or [])
                 if _text(item)])


def _camera(shot):
    dimensions = shot.get("five_dimensions") or {}
    design = dimensions.get("camera_design") or {}
    contract = shot.get("shot_contract") or {}
    return {
        "景别": _text(design.get("shot_scale") or contract.get("景别"), "按分镜"),
        "角度": _text(design.get("angle") or contract.get("角度"), "保持轴线"),
        "焦段": _text(design.get("lens") or contract.get("焦段")),
        "机位": _text(design.get("camera_position") or contract.get("机位")),
        "运镜": _text(design.get("movement") or contract.get("运镜"), "固定"),
        "动机": _text(design.get("movement_motivation"), "服务主体动作"),
        "构图": _text(design.get("composition") or contract.get("构图"), "主体清楚"),
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
    over_shoulder = "过肩" in framing_text or (
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
        if over_shoulder:
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
            if over_shoulder else "每个可见人物只计一次"),
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
        whitelist = "、".join(dict.fromkeys(
            str(item).strip() for item in (readable.get("whitelist") or [])
            if str(item).strip())) or "白名单"
        carrier = _text(readable.get("carrier"), "指定载体")
        if any(token in carrier for token in ("电脑", "笔记本", "屏幕", "显示器")):
            text_rule = (
                f"电脑屏幕必须打开并清晰显示白名单原文:{whitelist}；"
                "屏幕不是冷白光效/空白占位面，禁止随机乱码、模糊色块和黑白占位；"
                "屏幕外无字幕、Logo、水印和无关文字"
            )
        else:
            text_rule = f"{carrier}内文字只保持原样:{whitelist}；禁止新增文字"
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
    scene = _text(location or shot.get("location"), "按场景基准图")
    action = _text(
        shot.get("description") or shot.get("action") or shot.get("prompt"),
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
        "action": action,
        "performance": _text(
            (shot.get("performance") or {}).get("micro_expression"),
            "自然微表情、呼吸和重心变化",
        ),
        "camera": _camera(shot),
        "end": _state_line(shot.get("end_state")) or "到达尾帧状态",
        "dialogue": _text(dialogue.get("dialogue")),
        "speaker": _text(dialogue.get("character")),
        "text": text_rule,
        "references": refs,
        "hard": (
            "只执行一个主动作和一个运镜；人物身份、服装、场景、构图分别服从"
            "对应参考图；不得重新设计人物，不得新增/复制人物或把参考图内容贴进成片"
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
    return "弱参考：不得覆盖已锁定身份和场景"


def render_shot_prompt(contract, *, mode="image"):
    """把合同渲染成短句；图片和视频共用同一组语义，只改变媒介边界。"""
    subject = "、".join(contract["subject"]["actors"]) or "无人"
    count = contract["subject"]["count"]
    camera = contract["camera"]
    camera_line = "；".join(
        value for value in (
            camera.get("景别"), camera.get("角度"), camera.get("焦段"),
            camera.get("机位"), f"{camera.get('运镜')}({camera.get('动机')})",
            f"构图{camera.get('构图')}",
        ) if value
    )
    lines = [
        "【镜头合同v1】只执行下列事实，不自行补剧情。",
        f"【主体】严格共{count}人：{subject}。",
        f"【场景】{contract['scene']}。",
        f"【起点】{contract['start']}。",
        f"【单一主动作】{contract['action']}。",
        f"【表演】{contract['performance']}。",
        f"【镜头】{camera_line}。",
        f"【终点】{contract['end']}。",
    ]
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
