"""可执行的镜头提示词合同。

Seedance/图片模型更容易稳定执行“对象 → 场景 → 单一动作 → 摄影机 →
起止状态”这样的短结构。这个模块只做确定性的编译，不替模型补剧情，也不
把参考图的多个职责混在一条长提示词里。完整提示词仍由导演保存作审计，模型
请求优先使用这里编译出的短版。
"""

from __future__ import annotations

import re


PROMPT_CONTRACT_SCHEMA = "aifos.shot-prompt/v1"


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
    actor_by_name = {
        item.get("name"): actor_id
        for actor_id, item in number_map.items()
        if isinstance(item, dict) and item.get("name")
    }
    return [
        f"{actor_by_name.get(name) or f'P{index:02d}'}={name}"
        for index, name in enumerate(characters, 1)
    ]


def build_shot_prompt_contract(shot, *, location="", style="", references=None):
    """从已通过五维分镜的镜头构造可审计的结构化合同。

    不读取故事背景长文；只有当镜头实际需要时才保留场景、动作和状态，避免
    全局风格/角色经历与参考图抢控制权。
    """
    characters = list(shot.get("characters") or [])
    dialogue = shot.get("dialogue") or {}
    readable = shot.get("readable_text") or {}
    text_rule = (
        f"首帧文字只保持原样:{'、'.join(readable.get('whitelist') or []) or '白名单'}"
        if readable.get("required") else "无画面文字、无字幕、无Logo、无水印"
    )
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
        "subject": {
            "count": len(characters),
            "actors": _character_lines(shot),
        },
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
    if contract.get("style"):
        lines.append(f"【画风】{contract['style']}（只沿用项目基准，不改媒介）。")
    if contract.get("dialogue"):
        speaker = contract.get("speaker") or "说话人"
        lines.append(f"【对白】{speaker}说出「{contract['dialogue']}」，自然口型；不画字幕。")
    lines.append(f"【文字】{contract['text']}。")
    if mode == "video":
        lines.insert(1, "【输入】图1是唯一动作起点，图2是唯一动作终点；只让已锁定画面动起来。")
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
