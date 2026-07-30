"""确定性 3D 空间调度（blocking）。

把五维分镜中的人物站位与摄影机设计转换成可校验的三维世界坐标、交互
查看数据和固定透视参考图。参考图只服务预生产与模型约束，不会作为最终
关键帧交付；旧版二维坐标继续保留，供历史调用方平滑升级。
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .director_camera import (primary_actor, solve_camera,
                              solve_camera_motion)


SCHEMA = "aifos.spatial-blocking/v3"
DIALOGUE_SCHEMA = "aifos.dialogue-continuity/v1"
WIDTH, HEIGHT = 1000, 700
WORLD_WIDTH_M, WORLD_DEPTH_M = 10.0, 7.0
DEFAULT_ACTOR_HEIGHT_M = 1.68
DEFAULT_CAMERA_HEIGHT_M = 1.55
MAX_SOLVED_CAMERA_HEIGHT_M = 4.6
MIN_ACTOR_SEPARATION = 72
MIN_CAMERA_SEPARATION = 90
ACTOR_COLORS = (
    "#ff5d8f", "#52b8ff", "#ffc857", "#69db9d", "#ad8cff", "#ff8c5a",
)
MOTION_WORDS = (
    "走", "跑", "冲", "追", "进入", "进门", "离开", "起身", "靠近",
    "后退", "转身", "移动", "绕", "穿过", "上前", "退到", "跟随",
)
DIALOGUE_WORDS = (
    "对话", "交谈", "谈话", "对视", "问话", "回答", "质问", "争辩",
    "试探", "谈判", "寒暄", "告白",
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _source_fingerprint(script, storyboard, continuity):
    # 可读文字锁定会在关键帧之后回写 storyboard；它不改变空间关系，
    # 因此只纳入会影响人物/机位坐标的字段，避免门禁误判为调度图过期。
    blocking_shots = [{
        "shot_no": shot.get("shot_no"),
        "scene_no": shot.get("scene_no"),
        "unit_id": shot.get("unit_id"),
        "characters": shot.get("characters", []),
        "character_count": shot.get("character_count"),
        "description": shot.get("description"),
        "prompt": shot.get("prompt"),
        "camera": shot.get("camera"),
        "start_state": shot.get("start_state", {}),
        "end_state": shot.get("end_state", {}),
        "camera_design": ((shot.get("five_dimensions") or {}).get(
            "camera_design") or {}),
    } for shot in storyboard.get("shots", [])]
    payload = {
        "blocking_schema": SCHEMA,
        "pose_contract_version": 2,
        "script_version": script.get("script_version"),
        "scenes": script.get("scenes", []),
        "storyboard": blocking_shots,
        "continuity": {
            "characters": continuity.get("characters", []),
            "scenes": continuity.get("scenes", []),
        },
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _number(value, default=0):
    found = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(found.group()) if found else default


def _position_x(value, fallback):
    text = str(value or "")
    if "左" in text:
        return 300
    if "右" in text:
        return 700
    if "中" in text or "中心" in text:
        return 500
    return fallback


def _point(x, y):
    return {"x": int(max(90, min(WIDTH - 90, x))),
            "y": int(max(115, min(HEIGHT - 115, y)))}


def _world_point(point, height=0.0):
    """把兼容二维画布坐标转换为右手系 Y-up 米制世界坐标。"""
    return {
        "x": round(
            (float(point.get("x", WIDTH / 2)) - WIDTH / 2)
            / (WIDTH - 180) * WORLD_WIDTH_M, 2),
        "y": round(float(height), 2),
        "z": round(
            (float(point.get("y", HEIGHT / 2)) - HEIGHT / 2)
            / (HEIGHT - 230) * WORLD_DEPTH_M, 2),
    }


def _point_3d_valid(point):
    return (
        isinstance(point, dict)
        and all(isinstance(point.get(axis), (int, float))
                and math.isfinite(float(point[axis]))
                for axis in ("x", "y", "z"))
    )


def _distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _spread_point(preferred, occupied):
    """把同一区域的多人摊开，同时尽量尊重左/中/右站位。"""
    preferred = _point(preferred["x"], preferred["y"])
    candidates = [preferred]
    candidates.extend(
        _point(x, y)
        for y in (245, 330, 415, 500)
        for x in (180, 260, 340, 420, 500, 580, 660, 740, 820)
    )
    candidates = sorted(
        {(_candidate["x"], _candidate["y"]): _candidate
         for _candidate in candidates}.values(),
        key=lambda candidate: (
            _distance(candidate, preferred), candidate["y"], candidate["x"]),
    )
    for candidate in candidates:
        if all(_distance(candidate, point) >= MIN_ACTOR_SEPARATION
               for point in occupied):
            return candidate
    return preferred


def _direction(start, end):
    dx, dy = end["x"] - start["x"], end["y"] - start["y"]
    if not dx and not dy:
        return "静止"
    horizontal = "向右" if dx > 0 else "向左"
    vertical = "向下" if dy > 0 else "向上"
    if abs(dx) < 24:
        return vertical
    if abs(dy) < 24:
        return horizontal
    return f"{horizontal}{vertical}"


def _axis_side(a, b, point):
    """Return the signed half-plane of point around directed line a→b."""
    try:
        ax, ay = float(a["x"]), float(a["y"])
        bx, by = float(b["x"]), float(b["y"])
        px, py = float(point["x"]), float(point["y"])
    except (KeyError, TypeError, ValueError):
        return 0
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) < 1e-6:
        return 0
    return 1 if cross > 0 else -1


def _reflect_across_axis(point, a, b):
    """Reflect a 2D canvas point across the actor-to-actor axis."""
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-6:
        return dict(point)
    ratio = (
        (point["x"] - a["x"]) * dx
        + (point["y"] - a["y"]) * dy) / length_sq
    projection = {
        "x": a["x"] + ratio * dx,
        "y": a["y"] + ratio * dy,
    }
    return _point(
        2 * projection["x"] - point["x"],
        2 * projection["y"] - point["y"])


def _dialogue_shot(shot, positions):
    if len(positions) != 2:
        return False
    if shot.get("kind") == "dialogue":
        return True
    dialogue = shot.get("dialogue")
    if isinstance(dialogue, dict) and dialogue.get("dialogue"):
        return True
    text = " ".join(str(shot.get(key) or "") for key in (
        "description", "prompt", "action"))
    return any(word in text for word in DIALOGUE_WORDS)


def _dialogue_pair_key(positions):
    return "|".join(sorted(
        str(actor.get("actor_id") or "") for actor in positions))


def _force_camera_half_plane(camera, axis, side, actor_points):
    """Keep both camera endpoints on one side of the dialogue axis."""
    a, b = axis["a"], axis["b"]
    perimeter = (
        [_point(x, 585) for x in (120, 240, 360, 500, 640, 760, 880)]
        + [_point(x, 115) for x in (120, 240, 360, 500, 640, 760, 880)]
        + [_point(90, y) for y in (190, 300, 410, 520)]
        + [_point(910, y) for y in (190, 300, 410, 520)]
    )

    def clear_on_side(original, occupied):
        reflected = _reflect_across_axis(original, a, b)
        candidates = [original, reflected, *perimeter]
        candidates = sorted(
            {(_candidate["x"], _candidate["y"]): _candidate
             for _candidate in candidates}.values(),
            key=lambda candidate: _distance(candidate, original))
        return next((
            candidate for candidate in candidates
            if _axis_side(a, b, candidate) == side
            and all(_distance(candidate, actor) >= MIN_CAMERA_SEPARATION
                    for actor in occupied)
        ), original)

    original_moving = bool(camera.get("moving"))
    start = clear_on_side(camera["start"], actor_points)
    end = (
        clear_on_side(camera["end"], actor_points + [start])
        if original_moving else start)
    moving = original_moving and start != end
    direction = _direction(start, end)
    camera.update({
        "start": start,
        "end": end,
        "moving": moving,
        "route": (
            [dict(start, phase="start"), dict(end, phase="end")]
            if moving else [dict(start, phase="fixed")]),
        "direction": direction,
        "direction_label": (
            f"镜头{camera.get('movement') or '移动'}：起点→终点，{direction}"
            if moving else "静止机位：起点=终点"),
        "dialogue_axis_side": side,
    })
    return camera


def _dialogue_contract(shot, positions, camera, memory, scene_no):
    """Build one cross-shot contract for a two-person dialogue pair."""
    if not _dialogue_shot(shot, positions):
        return {}
    pair_key = _dialogue_pair_key(positions)
    previous = memory.get(pair_key) or {}
    by_x = sorted(
        positions,
        key=lambda actor: (
            actor["end"]["x"], actor["end"]["y"], actor["actor_id"]))
    left_id = previous.get("screen_left_actor_id") or by_x[0]["actor_id"]
    right_id = previous.get("screen_right_actor_id") or by_x[-1]["actor_id"]
    actor_by_id = {actor["actor_id"]: actor for actor in positions}
    if left_id not in actor_by_id or right_id not in actor_by_id:
        left_id, right_id = by_x[0]["actor_id"], by_x[-1]["actor_id"]
    left, right = actor_by_id[left_id], actor_by_id[right_id]
    axis = {
        "a": dict(left["end"]),
        "b": dict(right["end"]),
        "a_3d": dict(left["end_3d"]),
        "b_3d": dict(right["end_3d"]),
        "rule": "摄影机起点与终点保持在演员连线同一侧；越轴须可见重建",
    }
    side = int(previous.get("camera_side_sign") or 0)
    if side not in (-1, 1):
        side = _axis_side(axis["a"], axis["b"], camera["start"]) or 1
    axis_id = previous.get("axis_id") or (
        f"S{int(scene_no):02d}-{pair_key.replace('|', '-')}-A01")
    camera = _force_camera_half_plane(
        camera, axis, side,
        [point for actor in positions
         for point in (actor["start"], actor["end"])])
    # 轴线校正会把机位弹到画布边缘的固定环上,距离随之失真;守住轴线
    # 所在的半平面方向不变,只把距离重新拉回声明景别应有的值。
    if camera.get("scale_distance_m"):
        target = _point(
            sum(p["end"]["x"] for p in positions) / max(1, len(positions)),
            sum(p["end"]["y"] for p in positions) / max(1, len(positions)))
        _rescale_to_declared_distance(
            camera, target, camera["scale_distance_m"])
    for actor, other in ((left, right), (right, left)):
        # 双人对话镜刻意覆写朝向以锁 180° 轴线。三个键必须一起写:
        # 下游 staging_clause 优先读 facing_{phase},只改 facing 会让
        # 轴线覆写被静默绕过,双人镜退回分镜原文、越轴。
        actor["facing"] = f"面向{other['name']}"
        actor["facing_start"] = actor["facing"]
        actor["facing_end"] = actor["facing"]
        actor["gaze_target_actor_id"] = other["actor_id"]
        actor["gaze_target_name"] = other["name"]
        actor["eyeline_screen_direction"] = (
            "向右" if actor["actor_id"] == left_id else "向左")
        actor["spatial_anchor"] = (
            "left" if actor["actor_id"] == left_id else "right")
    camera_design = (
        (shot.get("five_dimensions") or {}).get("camera_design") or {})
    scale = str(camera_design.get("shot_scale") or "")
    position = str(
        camera.get("position")
        or camera_design.get("camera_position") or "")
    if "过肩" in position:
        coverage = "同侧过肩正反打"
    elif scale in ("远景", "全景", "中景"):
        coverage = "双人建立镜头"
    else:
        coverage = "同侧情绪近景"
    speaker = str((shot.get("dialogue") or {}).get("character") or "")
    subject = next(
        (actor for actor in positions if actor["name"] == speaker), None)
    foreground = (
        next((actor for actor in positions if actor is not subject), None)
        if coverage == "同侧过肩正反打" and subject else None)
    contract = {
        "schema": DIALOGUE_SCHEMA,
        "pair_id": pair_key,
        "pair_actor_ids": [left_id, right_id],
        "screen_left_actor_id": left_id,
        "screen_right_actor_id": right_id,
        "screen_left_name": left["name"],
        "screen_right_name": right["name"],
        "axis_id": axis_id,
        "axis": axis,
        "camera_side_sign": side,
        "camera_side": "positive" if side > 0 else "negative",
        "coverage": coverage,
        "foreground_actor_id": (
            foreground.get("actor_id") if foreground else ""),
        "subject_actor_id": subject.get("actor_id") if subject else "",
        "eyelines": {
            left_id: {
                "target_actor_id": right_id,
                "target_name": right["name"],
                "screen_direction": "向右",
            },
            right_id: {
                "target_actor_id": left_id,
                "target_name": left["name"],
                "screen_direction": "向左",
            },
        },
        "rules": [
            "左右锚点跨镜稳定，不得交换两人方位",
            "摄影机起点与终点保持在演员连线同一侧",
            "双方身体朝向彼此，画内视线方向互补",
            "过肩前景只能属于另一位对话者，不得生成随机第三人",
            "越轴必须通过可见运镜、人物走位或中性机位重建新轴线",
        ],
    }
    memory[pair_key] = {
        key: contract[key] for key in (
            "screen_left_actor_id", "screen_right_actor_id",
            "axis_id", "camera_side_sign")
    }
    return contract


def _actor_is_moving(name, people, shot, state_start, state_end):
    if (state_start.get("position") and state_end.get("position")
            and state_start.get("position") != state_end.get("position")):
        return True
    state_text = " ".join((str(state_start.get("pose") or ""),
                           str(state_end.get("pose") or "")))
    if any(word in state_text for word in MOTION_WORDS):
        return True
    action_text = " ".join((str(shot.get("description") or ""),
                            str(shot.get("prompt") or "")))
    if not any(word in action_text for word in MOTION_WORDS):
        return False
    clauses = re.split(r"[，。；,;]", action_text)
    named_people = [person for person in people if person in action_text]
    if named_people:
        return any(
            name in clause and any(word in clause for word in MOTION_WORDS)
            for clause in clauses
        )
    return True


def build_character_number_map(continuity, storyboard=None):
    """建立整集稳定编号；编号仅用于生产引用，不作为成片画面文字。"""
    characters = []
    roles = {}
    for character in continuity.get("characters", []):
        name = character.get("name")
        if not name or name in characters:
            continue
        characters.append(name)
        roles[name] = str(character.get("role") or "角色")
    for shot in (storyboard or {}).get("shots", []):
        for name in shot.get("characters", []):
            if name and name not in characters:
                characters.append(name)
                roles[name] = "角色"
    result = {}
    for index, name in enumerate(characters, 1):
        actor_id = f"P{index:02d}"
        role = roles.get(name) or "角色"
        result[actor_id] = {
            "actor_id": actor_id,
            "name": name,
            "role": role,
            "is_protagonist": "主角" in role,
            "display_label": f"{actor_id} {role}·{name}",
            "color": ACTOR_COLORS[(index - 1) % len(ACTOR_COLORS)],
        }
    return result


# 景别 → 摄影机到主体的目标距离(米)。与 spatial_language 的距离分档
# 同源(取各档中值),保证"声明景别"与"三维机位"从一开始就一致。
# 此前机位距离是画布布局的副产品(固定 start_y=625 换算而来),与声明
# 景别完全无关——实测 ep24 12 镜里 4 镜声明特写/中近景却把机位摆在
# 4~5 米外,成片必然对不上,改图也改不动(输入本身矛盾)。
SUBJECT_EYE_HEIGHT_M = 1.43   # 1.68m 主体的胸眼高度
SCALE_TARGET_DISTANCE_M = {
    "大特写": 0.8, "特写": 1.0, "近景": 1.7, "中近景": 1.9,
    "中景": 2.8, "膝上景": 3.0, "七分身": 3.2,
    "全景": 4.6, "远景": 7.5, "大远景": 10.0,
}
_SCALE_TOKENS = tuple(SCALE_TARGET_DISTANCE_M)


def declared_scale(shot):
    """镜头声明的景别(五维优先,退回镜头原文);未命中返回空串。"""
    design = ((shot.get("five_dimensions") or {}).get("camera_design") or {})
    for source in (design.get("shot_size"), design.get("scale"),
                   shot.get("camera")):
        text = str(source or "")
        for token in _SCALE_TOKENS:
            if token in text:
                return token
    return ""


def _scale_camera_distance(camera, target, shot):
    """把机位沿现有方向拉到声明景别应有的距离(2D 与 3D 同步)。

    只改距离、不改方向与高度:机位角度是分镜的创作选择,距离才是被
    画布布局意外决定的。2D 坐标同步修正,保证示意图与文字合同一致。
    """
    scale = declared_scale(shot)
    desired = SCALE_TARGET_DISTANCE_M.get(scale)
    if not desired:
        return camera
    camera["scale_distance_m"] = desired
    camera["scale_for_distance"] = scale
    return _rescale_to_declared_distance(camera, target, desired)


def _rescale_to_declared_distance(camera, target, desired):
    """沿现有方向把机位拉到 desired 米(只改距离,不改方向与高度)。"""
    # 目标点抬到主体胸眼高度:与 spatial_language 的被摄距离同口径,
    # 否则"按水平距离摆位、按三维距离核验"两边永远对不上。
    world_target = _world_point(target, SUBJECT_EYE_HEIGHT_M)
    for key in ("start", "end"):
        point = camera.get(key)
        if not isinstance(point, dict):
            continue
        world_cam = _world_point(
            point, _camera_height(
                str(camera.get("position") or ""),
                str(camera.get("movement") or ""),
                "start" if key == "start" else "end"))
        current = math.dist(
            (world_cam["x"], world_cam["y"], world_cam["z"]),
            (world_target["x"], world_target["y"], world_target["z"]))
        if current < 1e-3:
            continue
        ratio = desired / current
        if abs(ratio - 1.0) < 0.05:
            continue
        camera[key] = _point(
            target["x"] + (point["x"] - target["x"]) * ratio,
            target["y"] + (point["y"] - target["y"]) * ratio)
    start, end = camera["start"], camera["end"]
    camera["moving"] = start != end
    camera["route"] = ([dict(start, phase="start"), dict(end, phase="end")]
                       if camera["moving"] else [dict(start, phase="fixed")])
    return camera


def _lens_and_fov(shot):
    camera = ((shot.get("five_dimensions") or {}).get("camera_design") or {})
    lens = _number(camera.get("lens") or shot.get("camera"), 50)
    lens = max(8, min(200, lens))
    # 以全画幅水平视角做规划近似；只用于构图示意。
    fov = math.degrees(2 * math.atan(36 / (2 * lens)))
    return int(round(lens)), round(fov, 1)


def _camera_height(position, movement, phase="start"):
    text = f"{position} {movement}"
    if any(word in text for word in ("顶视", "顶拍", "航拍", "鸟瞰")):
        height = 4.8
    elif any(word in text for word in ("俯拍", "高机位", "高角度")):
        height = 2.8
    elif any(word in text for word in ("低机位", "低角度", "仰拍")):
        height = .7
    else:
        height = DEFAULT_CAMERA_HEIGHT_M
    if phase == "end" and "升" in movement:
        height += 1.2
    elif phase == "end" and "降" in movement:
        height = max(.35, height - .9)
    return round(height, 2)


def _camera_orientation(camera_point, target_point):
    dx = target_point["x"] - camera_point["x"]
    dy = target_point["y"] - camera_point["y"]
    dz = target_point["z"] - camera_point["z"]
    horizontal = max(.001, math.hypot(dx, dz))
    return {
        "heading_degrees": round(math.degrees(math.atan2(dx, dz)), 1),
        "pitch_degrees": round(math.degrees(math.atan2(dy, horizontal)), 1),
        "roll_degrees": 0.0,
    }


def _pose_profile(state, action="", phase="start"):
    """Resolve support and eye/torso target height from visible body state."""
    state = state if isinstance(state, dict) else {}
    explicit = " ".join(str(state.get(key) or "") for key in (
        "pose", "position", "support", "action"))
    pose_tokens = (
        "仰卧", "俯卧", "侧卧", "卧床", "卧榻", "躺", "平卧",
        "伏案", "趴桌", "趴在", "趴向", "坐", "落座", "跪", "蹲",
        "蜷缩", "站立", "站姿",
    )
    text = (
        explicit if any(token in explicit for token in pose_tokens)
        else f"{explicit} {action}")
    if any(word in text for word in (
            "仰卧", "俯卧", "侧卧", "卧床", "卧榻", "躺", "平卧")):
        support = "床榻" if any(
            word in text for word in ("床", "榻", "卧榻")) else "地面/已声明支撑面"
        return {
            "pose": "lying", "pose_label": "卧姿",
            "support": support, "height_m": .55, "target_height_m": .42,
        }
    if any(word in text for word in ("伏案", "趴桌", "趴在", "趴向")):
        return {
            "pose": "leaning_seated", "pose_label": "伏案/前倾坐姿",
            "support": "座椅与桌面", "height_m": 1.05,
            "target_height_m": .82,
        }
    if any(word in text for word in ("坐", "落座")):
        return {
            "pose": "sitting", "pose_label": "坐姿",
            "support": "座椅/已声明坐面", "height_m": 1.22,
            "target_height_m": 1.0,
        }
    if any(word in text for word in ("跪", "半跪")):
        return {
            "pose": "kneeling", "pose_label": "跪姿",
            "support": "地面", "height_m": 1.12, "target_height_m": .9,
        }
    if any(word in text for word in ("蹲", "蜷缩")):
        return {
            "pose": "crouching", "pose_label": "蹲姿",
            "support": "双脚/地面", "height_m": .98, "target_height_m": .78,
        }
    return {
        "pose": "standing", "pose_label": "站姿",
        "support": "双脚/地面", "height_m": DEFAULT_ACTOR_HEIGHT_M,
        "target_height_m": 1.25,
    }


def _attach_camera_3d(
        camera, start_target, end_target,
        start_target_height=1.25, end_target_height=1.25):
    position = str(camera.get("position") or "")
    movement = str(camera.get("movement") or "")
    # 调度器解出的镜高是按「角度」维度算的(俯拍抬高、仰拍压低);
    # 旧启发式只读机位/运动,读不到角度,镜高因此恒为默认值。
    solved_h = camera.get("director_height_m")
    solved_end_h = camera.get("director_end_height_m")
    start_h = (float(solved_h) if solved_h is not None
               else _camera_height(position, movement, "start"))
    # 终点高度由运镜求解给出(升/降已在其中算好),不再二次加减
    end_h = (float(solved_end_h) if solved_end_h is not None
             else (float(solved_h) if solved_h is not None
                   else _camera_height(position, movement, "end")))
    start = _world_point(camera["start"], start_h)
    end = _world_point(camera["end"], end_h)
    target_start = _world_point(start_target, start_target_height)
    target_end = _world_point(end_target, end_target_height)
    # 摇/移/跟 的终点瞄准点由运镜求解决定:摇是机位不动只转机身,
    # 用「人物终点」当瞄准点会把它错算成机位平移。
    solved_target = camera.get("director_end_target_3d")
    if isinstance(solved_target, dict) and solved_target:
        target_end = {"x": round(float(solved_target.get("x", 0.0)), 2),
                      "y": round(float(solved_target.get(
                          "y", end_target_height)), 2),
                      "z": round(float(solved_target.get("z", 0.0)), 2)}
    horizontal_fov = float(camera.get("fov_degrees") or 39.6)
    vertical_fov = math.degrees(
        2 * math.atan(math.tan(math.radians(horizontal_fov) / 2) * 16 / 9))
    camera.update({
        "start_3d": start,
        "end_3d": end,
        "target_start_3d": target_start,
        "target_end_3d": target_end,
        "target_3d": target_end,
        "route_3d": (
            [dict(start, phase="start"), dict(end, phase="end")]
            if camera.get("moving") or start != end
            else [dict(start, phase="fixed")]
        ),
        "orientation_start": _camera_orientation(start, target_start),
        "orientation_end": _camera_orientation(end, target_end),
        "horizontal_fov_degrees": round(horizontal_fov, 1),
        "vertical_fov_degrees": round(vertical_fov, 1),
        "frustum": {"near_m": .1, "far_m": 18.0, "aspect_ratio": "9:16"},
    })
    return camera


def director_camera_issues(blocking):
    """从已求解的机位里汇总导演调度问题(跨镜连续性/缺声明/贴墙)。

    机位在 _apply_director_camera 里逐镜求解;这里只做**跨镜**判定,
    避免重复求解。写进 blocking.validation,供分镜出厂预检在文本层拦住
    ——机位问题到出图层才发现就已经浪费一次生成。
    """
    issues = []
    by_scene = {}
    for key, block in sorted((blocking.get("shot_index") or {}).items(),
                             key=lambda kv: int(kv[0]) if str(kv[0]).isdigit()
                             else 0):
        dc = (block.get("camera") or {}).get("director_camera") or {}
        if not dc:
            continue
        by_scene.setdefault(block.get("scene_no"), []).append((key, block, dc))
    for scene_no, rows in by_scene.items():
        previous = None
        for key, _block, dc in rows:
            shot_no = key
            declared = dc.get("declared") or {}
            if not declared.get("shot_size"):
                issues.append({
                    "scene_no": scene_no, "shot_no": shot_no,
                    "severity": "warning", "field": "shot_size",
                    "message": (f"镜{shot_no} 未声明景别,机位按中景兜底;"
                                "景别是机距的唯一依据,请在分镜写明"),
                })
            if dc.get("movement_wall_clamped"):
                issues.append({
                    "scene_no": scene_no, "shot_no": shot_no,
                    "severity": "warning", "field": "movement",
                    "message": (
                        f"镜{shot_no} 的「{dc.get('movement')}」运镜会把机位"
                        f"推出墙外,已钳在室内({dc.get('movement_amount')});"
                        "请缩短运镜幅度或改机位方向"),
                })
            if dc.get("wall_clamped"):
                issues.append({
                    "scene_no": scene_no, "shot_no": shot_no,
                    "severity": "warning", "field": "distance",
                    "message": (
                        f"镜{shot_no} 按声明景别应退到 "
                        f"{dc.get('desired_distance_m')}m 但会穿墙,已贴墙"
                        f"摆位(实际 {dc.get('distance_m')}m);"
                        "请改景别或把人物挪离墙面"),
                })
            if previous is not None:
                delta = abs(_wrap_deg(
                    float(dc.get("yaw_deg") or 0)
                    - float(previous[2].get("yaw_deg") or 0)))
                same = (declared.get("shot_size")
                        == (previous[2].get("declared") or {}).get("shot_size"))
                if same and delta < 12.0:
                    issues.append({
                        "scene_no": scene_no, "shot_no": shot_no,
                        "severity": "warning", "field": "coverage",
                        "message": (
                            f"镜{shot_no} 与镜{previous[0]} 机位方位仅差 "
                            f"{delta:.0f}°、景别相同,剪在一起近似跳帧;"
                            "请换景别或换机位"),
                    })
            previous = (shot_no, _block, dc)
    return issues


def _wrap_deg(deg):
    return ((float(deg) + 180.0) % 360.0) - 180.0


def _canvas_from_world(world_point):
    """世界坐标(米) → 二维画布坐标。_world_point 的逆,让示意图与三维同源。"""
    try:
        wx = float(world_point.get("x", 0.0))
        wz = float(world_point.get("z", 0.0))
    except (TypeError, ValueError):
        return _point(WIDTH / 2, HEIGHT / 2)
    return _point(
        WIDTH / 2 + wx / WORLD_WIDTH_M * (WIDTH - 180),
        HEIGHT / 2 + wz / WORLD_DEPTH_M * (HEIGHT - 230))


def _apply_director_camera(camera, shot, positions, world=None):
    """用导演调度器求出的三维机位覆盖画布副产品(三维为权威)。

    此前机位是画布(500,625)固定点朝主体拉出来的:方位是「画布点→主体」
    连线的方向(主体一动方位就变,不是导演选择),镜高只读机位/运动两个
    字段(俯拍写在「角度」里永远读不到),距离在像素里校正、换算成三维后
    不守恒——EP1 实测八镜只有 2 镜吻合声明景别,镜高恒定 1.55m,
    方位逐镜乱跳 134°→168°→-93°→15°。
    """
    world_dict = world if isinstance(world, dict) else {}
    solved = solve_camera(
        shot, positions, axis_side=1, scene_center=(0.0, 0.0),
        world=world_dict)
    if not solved:
        return camera
    # 运镜也求解:此前终点只是画布像素位移(end_x += 150 之类),既没有米制
    # 依据,也和 MOVEMENT_GEOMETRY 写死的几何对不上——提示词说「沿视线
    # 推近」,三维里可能是斜着平移。现在每个运镜词有确定的几何解。
    subject = primary_actor(positions) or {}
    start_xz = (float((subject.get("start_3d") or {}).get("x", 0.0)),
                float((subject.get("start_3d") or {}).get("z", 0.0)))
    end_xz = (float((subject.get("end_3d") or {}).get("x", start_xz[0])),
              float((subject.get("end_3d") or {}).get("z", start_xz[1])))
    solved = solve_camera_motion(
        solved, shot, subject_start=start_xz, subject_end=end_xz,
        world=world_dict)
    camera["start"] = _canvas_from_world(solved["position_3d"])
    camera["end"] = _canvas_from_world(solved["end_position_3d"])
    camera["moving"] = bool(solved.get("moving"))
    camera["movement"] = solved.get("movement") or camera.get("movement")
    camera["director_camera"] = {
        "distance_m": solved["distance_m"],
        "desired_distance_m": solved["desired_distance_m"],
        "height_m": solved["height_m"],
        "yaw_deg": solved["yaw_deg"],
        "pitch_deg": solved["pitch_deg"],
        "declared": solved["declared"],
        "wall_clamped": solved["wall_clamped"],
        "movement": solved.get("movement"),
        "movement_amount": solved.get("movement_amount"),
        "movement_wall_clamped": solved.get("movement_wall_clamped"),
        "end_position_3d": solved.get("end_position_3d"),
        "end_target_3d": solved.get("end_target_3d"),
    }
    camera["director_end_height_m"] = (
        solved.get("end_position_3d") or {}).get("y")
    camera["director_end_target_3d"] = solved.get("end_target_3d")
    camera["scale_distance_m"] = solved["desired_distance_m"]
    camera["scale_for_distance"] = solved["declared"]["shot_size"]
    camera["director_height_m"] = solved["height_m"]
    return camera


def _camera_block(shot, target):
    design = ((shot.get("five_dimensions") or {}).get("camera_design") or {})
    movement = str(design.get("movement") or "").strip()
    if not movement:
        camera_text = str(shot.get("camera") or "")
        movement = next((word for word in (
            "急推", "手持跟拍", "环绕", "跟", "推", "拉", "移", "摇",
            "升", "降") if word in camera_text), "固定")
    position = str(design.get("camera_position") or "正面")
    offset = _number(design.get("axis_offset_degrees"), 0)
    start_x = 500 + max(-1, min(1, offset / 60)) * 250
    start_y = 625
    if "侧" in position:
        start_x = 160 if offset <= 0 else 840
        start_y = 465
    elif "背" in position:
        start_y = 150
    elif "过肩" in position:
        start_x += 130
        start_y = 560
    end_x, end_y = start_x, start_y
    if any(word in movement for word in ("推", "跟")):
        end_x += (target["x"] - start_x) * .28
        end_y += (target["y"] - start_y) * .28
    elif "拉" in movement:
        end_x -= (target["x"] - start_x) * .18
        end_y -= (target["y"] - start_y) * .18
    elif any(word in movement for word in ("移", "摇", "环绕")):
        end_x += 150 if offset <= 0 else -150
    elif any(word in movement for word in ("升", "降")):
        end_y -= 90
    lens, fov = _lens_and_fov(shot)
    start, end = _point(start_x, start_y), _point(end_x, end_y)
    block = {"start": start, "end": end}
    # 机位距离按声明景别校正:此前它只是画布布局的副产品,与景别无关。
    _scale_camera_distance(block, target, shot)
    start, end = block["start"], block["end"]
    moving = start != end
    direction = _direction(start, end)
    return {
        "start": start,
        "end": end,
        "scale_distance_m": block.get("scale_distance_m"),
        "scale_for_distance": block.get("scale_for_distance", ""),
        "target": target,
        "movement": movement,
        "moving": moving,
        "route": [dict(start, phase="start"), dict(end, phase="end")]
                 if moving else [dict(start, phase="fixed")],
        "direction": direction,
        "direction_label": (f"镜头{movement}：起点→终点，{direction}"
                            if moving else "静止机位：起点=终点"),
        "position": position,
        "lens_mm": lens,
        "fov_degrees": fov,
        "axis_offset_degrees": offset,
    }


def _clear_camera_icons(camera, actor_points):
    """把摄像机起终点留在人物标记之外，避免三角机位压住人物。"""
    perimeter = (
        [_point(x, 585) for x in (120, 240, 360, 500, 640, 760, 880)]
        + [_point(x, 115) for x in (120, 240, 360, 500, 640, 760, 880)]
        + [_point(90, y) for y in (190, 300, 410, 520)]
        + [_point(910, y) for y in (190, 300, 410, 520)]
    )

    def clear(point, occupied):
        if all(_distance(point, actor) >= MIN_CAMERA_SEPARATION
               for actor in occupied):
            return point
        candidates = sorted(perimeter, key=lambda item: _distance(item, point))
        return next((item for item in candidates
                     if all(_distance(item, actor) >= MIN_CAMERA_SEPARATION
                            for actor in occupied)), point)

    original_moving = bool(camera.get("moving"))
    start = clear(camera["start"], actor_points)
    end = (clear(camera["end"], actor_points + [start])
           if original_moving else start)
    moving = original_moving and start != end
    direction = _direction(start, end)
    camera.update({
        "start": start, "end": end, "moving": moving,
        "route": ([dict(start, phase="start"), dict(end, phase="end")]
                  if moving else [dict(start, phase="fixed")]),
        "direction": direction,
        "direction_label": (
            f"镜头{camera.get('movement') or '移动'}：起点→终点，{direction}"
            if moving else "静止机位：起点=终点"),
    })
    return camera


def _scene_location(script, continuity, scene_no):
    for scene in script.get("scenes", []):
        if int(scene.get("scene_no", 0)) == int(scene_no):
            return scene.get("location") or scene.get("name") or f"第{scene_no}场"
    scenes = continuity.get("scenes", [])
    if 0 < int(scene_no) <= len(scenes):
        return scenes[int(scene_no) - 1].get("name") or f"第{scene_no}场"
    return f"第{scene_no}场"


def _needs_map(shots, group_threshold):
    max_people = max((int(s.get("character_count", 0)) for s in shots),
                     default=0)
    text = " ".join(str(s.get("description") or s.get("prompt") or "")
                    for s in shots)
    reasons = []
    if max_people >= group_threshold:
        reasons.append(f"多人场景（最高 {max_people} 人）")
    if any(
            len(list(dict.fromkeys(shot.get("characters", [])))) == 2
            and (
                shot.get("kind") == "dialogue"
                or (isinstance(shot.get("dialogue"), dict)
                    and shot["dialogue"].get("dialogue"))
                or any(word in " ".join(str(shot.get(key) or "")
                                        for key in ("description", "prompt"))
                       for word in DIALOGUE_WORDS))
            for shot in shots):
        reasons.append("双人对话需要固定左右锚点、180°轴线与互锁视线")
    if any(word in text for word in MOTION_WORDS):
        reasons.append("包含人物走位")
    if any(word in " ".join((
            str(s.get("camera") or ""),
            str(((s.get("five_dimensions") or {}).get(
                "camera_design") or {}).get("movement") or "")))
           for s in shots
           for word in ("跟", "移", "摇", "环绕", "推", "拉", "升", "降")):
        reasons.append("包含镜头运动")
    return bool(reasons), reasons or ["连续性参考"]


def build_spatial_plan(script, storyboard, continuity, group_threshold=3):
    """从剧本/分镜/连续性圣经构建可复现的空间调度计划。"""
    character_number_map = build_character_number_map(continuity, storyboard)
    character_by_name = {
        character["name"]: character
        for character in character_number_map.values()
    }
    cast_names = list(character_by_name)
    scene_numbers = []
    for shot in storyboard.get("shots", []):
        number = int(shot.get("scene_no", 0))
        if number not in scene_numbers:
            scene_numbers.append(number)
    scenes = []
    shot_index = {}
    for scene_no in scene_numbers:
        shots = [s for s in storyboard.get("shots", [])
                 if int(s.get("scene_no", 0)) == scene_no]
        required, reasons = _needs_map(shots, int(group_threshold or 3))
        previous_end = {}
        dialogue_memory = {}
        scene_shots = []
        for local_index, shot in enumerate(shots):
            people = list(dict.fromkeys(shot.get("characters", [])))
            positions = []
            occupied_starts = []
            occupied_ends = []
            reserved_inherited = [previous_end[name] for name in people
                                  if name in previous_end]
            starts = {}
            for actor_index, name in enumerate(people):
                state_start = (shot.get("start_state") or {}).get(name, {})
                fallback_x = 300 + (actor_index * 400 // max(1, len(people) - 1))
                requested_x = _position_x(
                    state_start.get("position"), fallback_x)
                inherited = previous_end.get(name)
                start = inherited or _spread_point(
                    _point(requested_x, 330 + (actor_index % 2) * 90),
                    occupied_starts + reserved_inherited)
                # 同镜中首次出现人物如果恰好占用继承人物位置，也必须避让。
                if not inherited and any(
                        _distance(start, point) < MIN_ACTOR_SEPARATION
                        for point in occupied_starts):
                    start = _spread_point(start, occupied_starts)
                occupied_starts.append(start)
                starts[name] = start
            # 终点在全员起点锁定后再计算，避免 A 的终点压住稍后出现的 B 起点。
            for actor_index, name in enumerate(people):
                state_start = (shot.get("start_state") or {}).get(name, {})
                state_end = (shot.get("end_state") or {}).get(name, {})
                start = starts[name]
                end_x = _position_x(state_end.get("position"), start["x"])
                end_y = start["y"]
                moving = _actor_is_moving(
                    name, people, shot, state_start, state_end)
                if moving and abs(end_x - start["x"]) < 30:
                    direction = 1 if (local_index + actor_index) % 2 == 0 else -1
                    end_x = start["x"] + direction * 110
                    end_y = start["y"] - 35 + (actor_index % 2) * 70
                preferred_end = _point(end_x, end_y)
                end = (start if not moving else
                       _spread_point(
                           preferred_end, occupied_ends + occupied_starts))
                if any(_distance(end, point) < MIN_ACTOR_SEPARATION
                       for point in occupied_ends):
                    end = _spread_point(end, occupied_ends)
                occupied_ends.append(end)
                previous_end[name] = end
                character = character_by_name[name]
                action_text = " ".join((
                    str(shot.get("description") or ""),
                    str(shot.get("prompt") or "")))
                start_pose = _pose_profile(
                    state_start, action_text, "start")
                end_pose = _pose_profile(state_end, action_text, "end")
                route_direction = _direction(start, end)
                start_3d = _world_point(start)
                end_3d = _world_point(end)
                positions.append({
                    "actor_id": character["actor_id"], "name": name,
                    "role": character["role"],
                    "display_label": character["display_label"],
                    "is_protagonist": character["is_protagonist"],
                    "color": character["color"], "start": start, "end": end,
                    "route": [dict(start, phase="start"),
                              dict(end, phase="end")]
                             if start != end
                             else [dict(start, phase="fixed")],
                    "start_3d": start_3d, "end_3d": end_3d,
                    "route_3d": (
                        [dict(start_3d, phase="start"),
                         dict(end_3d, phase="end")]
                        if start != end
                        else [dict(start_3d, phase="fixed")]
                    ),
                    "height_m": end_pose["height_m"],
                    "start_height_m": start_pose["height_m"],
                    "end_height_m": end_pose["height_m"],
                    "target_start_height_m":
                        start_pose["target_height_m"],
                    "target_end_height_m": end_pose["target_height_m"],
                    "pose_start": start_pose["pose"],
                    "pose_end": end_pose["pose"],
                    "pose_label_start": start_pose["pose_label"],
                    "pose_label_end": end_pose["pose_label"],
                    "support_start": start_pose["support"],
                    "support_end": end_pose["support"],
                    "moving": start != end,
                    "route_direction": route_direction,
                    "route_label": (f"起点→终点，{route_direction}"
                                    if start != end else "原地静止"),
                    # 朝向必须分相位。只留一个 facing 会让首帧合同拿到
                    # 尾帧视线——实测本集 7/8 镜命中:镜6 首帧一边写
                    # 「银铃仍由木面承托、右拳尚未成形」,一边写「视线仍落
                    # 在右拳」,要求模型注视一个此刻还不存在的东西。
                    # facing 保留为尾帧值,供 1138 行的对视审计与存量文档。
                    "facing_start": state_start.get("direction")
                                    or state_end.get("direction") or "面向主体",
                    "facing_end": state_end.get("direction")
                                  or state_start.get("direction") or "面向主体",
                    "facing": state_end.get("direction")
                              or state_start.get("direction") or "面向主体",
                })
            target = _point(
                sum(p["end"]["x"] for p in positions) / max(1, len(positions)),
                sum(p["end"]["y"] for p in positions) / max(1, len(positions)))
            start_target = _point(
                sum(p["start"]["x"] for p in positions)
                / max(1, len(positions)),
                sum(p["start"]["y"] for p in positions)
                / max(1, len(positions)))
            actor_markers = [point for position in positions
                             for point in (position["start"], position["end"])]
            # 先避让人物标记(防图标压住人物),再按声明景别定距——
            # 顺序反了的话避让会把机位弹到画布边缘的固定环上,
            # 距离校正被整个抹掉(实测:声明特写仍在 4~5 米外)。
            camera_2d = _apply_director_camera(
                _scale_camera_distance(
                    _clear_camera_icons(
                        _camera_block(shot, target), actor_markers),
                    target, shot),
                shot, positions,
                {"floor_width_m": WORLD_WIDTH_M,
                 "floor_depth_m": WORLD_DEPTH_M})
            dialogue_continuity = _dialogue_contract(
                shot, positions, camera_2d, dialogue_memory, scene_no)
            camera = _attach_camera_3d(
                camera_2d,
                start_target, target,
                sum(p["target_start_height_m"] for p in positions)
                / max(1, len(positions)),
                sum(p["target_end_height_m"] for p in positions)
                / max(1, len(positions)))
            compact = ";".join(
                f"{p['display_label']}"
                f"({p['start_3d']['x']},{p['start_3d']['y']},"
                f"{p['start_3d']['z']})m"
                f"→({p['end_3d']['x']},{p['end_3d']['y']},"
                f"{p['end_3d']['z']})m/{p['route_direction']}/"
                f"{p['pose_label_start']}→{p['pose_label_end']}/"
                f"支撑:{p['support_end']}"
                for p in positions)
            constraint = (
                f"空间调度锁：本镜严格 {len(people)} 人；{compact}；"
                f"机位({camera['start_3d']['x']},{camera['start_3d']['y']},"
                f"{camera['start_3d']['z']})m"
                f"→({camera['end_3d']['x']},{camera['end_3d']['y']},"
                f"{camera['end_3d']['z']})m，"
                f"瞄准({camera['target_3d']['x']},"
                f"{camera['target_3d']['y']},"
                f"{camera['target_3d']['z']})m，"
                f"{camera['lens_mm']}mm/{camera['movement']}，"
                f"水平视角{camera['horizontal_fov_degrees']}°，保持屏幕轴线。"
                "编号、坐标和路线仅供生产约束引用，最终画面不得出现人物编号、"
                "姓名标签、坐标、箭头或调度图符号。"
            )
            if dialogue_continuity:
                left_id = dialogue_continuity["screen_left_actor_id"]
                right_id = dialogue_continuity["screen_right_actor_id"]
                constraint += (
                    f" 双人对话轴线锁：axis_id="
                    f"{dialogue_continuity['axis_id']}；"
                    f"{left_id}固定空间左锚点，{right_id}固定空间右锚点；"
                    f"摄影机起点与终点保持演员连线"
                    f"{dialogue_continuity['camera_side']}半平面；"
                    f"{left_id}看{right_id}，{right_id}看{left_id}，"
                    "画内视线方向互补；禁止左右互换、并排同向、看空气、"
                    "随机第三人过肩或无重建越轴。"
                )
            block = {
                "shot_no": int(shot.get("shot_no", 0)),
                "scene_no": scene_no,
                "unit_id": shot.get("unit_id"),
                "character_count": len(people),
                "character_number_map": {
                    p["actor_id"]: {
                        key: p[key] for key in (
                            "actor_id", "name", "role", "display_label")
                    } for p in positions
                },
                "actors": positions,
                "camera": camera,
                "axis": (
                    dialogue_continuity["axis"]
                    if dialogue_continuity else {
                        "a": _point(120, target["y"]),
                        "b": _point(880, target["y"]),
                        "a_3d": _world_point(_point(120, target["y"])),
                        "b_3d": _world_point(_point(880, target["y"])),
                        "rule": "机位保持在同一轴线侧，越轴须另建镜头",
                    }),
                "dialogue_continuity": dialogue_continuity,
                "constraint": constraint,
            }
            scene_shots.append(block)
            shot_index[str(block["shot_no"])] = block
        scenes.append({
            "scene_no": scene_no,
            "location": _scene_location(script, continuity, scene_no),
            "required": required,
            "reasons": reasons,
            "canvas": {"width": WIDTH, "height": HEIGHT,
                       "orientation": "交互3D",
                       "projection": "orbit"},
            "world": {
                "coordinate_system": "right-handed-y-up",
                "unit": "meter",
                "floor_width_m": WORLD_WIDTH_M,
                "floor_depth_m": WORLD_DEPTH_M,
                "floor_y_m": 0.0,
                "default_actor_height_m": DEFAULT_ACTOR_HEIGHT_M,
                "default_camera_height_m": DEFAULT_CAMERA_HEIGHT_M,
            },
            "actors": [dict(character_by_name[name])
                       for name in cast_names
                       if any(name in s.get("characters", []) for s in shots)],
            "shots": scene_shots,
        })
    plan = {
        "schema": SCHEMA,
        "source_fingerprint": _source_fingerprint(
            script, storyboard, continuity),
        "group_threshold": int(group_threshold or 3),
        "character_number_map": character_number_map,
        "character_ids_by_name": {
            character["name"]: actor_id
            for actor_id, character in character_number_map.items()
        },
        "character_prompt_reference": "人物编号映射（仅提示词引用，不生成画面文字）：" +
            "；".join(
                f"{actor_id}={character['role']}·{character['name']}"
                for actor_id, character in character_number_map.items()),
        "summary": {
            "scenes": len(scenes),
            "required_scenes": sum(1 for scene in scenes if scene["required"]),
            "shots": len(shot_index),
            "actors": len(character_number_map),
        },
        "scenes": scenes,
        "shot_index": shot_index,
    }
    plan["validation"] = validate_spatial_plan(plan, storyboard)
    return plan


def validate_spatial_plan(plan, storyboard):
    issues = []
    if plan.get("schema") != SCHEMA:
        issues.append(f"空间调度版本不是 {SCHEMA}")
    for scene in plan.get("scenes", []):
        world = scene.get("world") or {}
        if (world.get("coordinate_system") != "right-handed-y-up"
                or world.get("unit") != "meter"):
            issues.append(
                f"第 {scene.get('scene_no')} 场缺少米制 Y-up 三维世界")
    index = plan.get("shot_index") or {
        str(shot.get("shot_no")): shot
        for scene in plan.get("scenes", []) for shot in scene.get("shots", [])
    }
    previous = {}
    previous_dialogue = {}
    for shot in storyboard.get("shots", []):
        shot_no = str(shot.get("shot_no"))
        block = index.get(shot_no)
        if not block:
            issues.append(f"镜头 {shot_no} 缺少空间调度")
            continue
        expected = list(dict.fromkeys(shot.get("characters", [])))
        actual = [actor.get("name") for actor in block.get("actors", [])]
        if actual != expected or block.get("character_count") != len(expected):
            issues.append(f"镜头 {shot_no} 人物名单/数量与分镜不一致")
        numbered = block.get("character_number_map") or {}
        stable_map = plan.get("character_number_map") or {}
        actor_points = block.get("actors", [])
        for left_index, left in enumerate(actor_points):
            actor_id = left.get("actor_id")
            canonical = stable_map.get(actor_id) or {}
            if (canonical.get("name") != left.get("name")
                    or not left.get("display_label")
                    or actor_id not in numbered):
                issues.append(
                    f"镜头 {shot_no} 的 {left.get('name')} 缺少稳定人物编号映射")
            route = left.get("route") or []
            route_3d = left.get("route_3d") or []
            if (not _point_3d_valid(left.get("start_3d"))
                    or not _point_3d_valid(left.get("end_3d"))
                    or not route_3d
                    or not all(_point_3d_valid(point)
                               for point in route_3d)
                    or float(left.get("height_m") or 0) <= 0
                    or not left.get("pose_start")
                    or not left.get("pose_end")
                    or not left.get("support_start")
                    or not left.get("support_end")):
                issues.append(
                    f"镜头 {shot_no} 的 "
                    f"{left.get('display_label') or left.get('name')}"
                    " 缺少合法三维站位/路线")
            if (left.get("pose_end") == "lying"
                    and float(left.get("target_end_height_m") or 99) > .8):
                issues.append(
                    f"镜头 {shot_no} 的 {left.get('name')} 为卧姿，"
                    "但镜头瞄准高度仍按站姿计算")
            if left.get("moving") and (
                    len(route) < 2 or route[0].get("phase") != "start"
                    or route[-1].get("phase") != "end"
                    or left.get("route_direction") in (None, "静止")):
                issues.append(
                    f"镜头 {shot_no} 的 {left.get('display_label') or left.get('name')}"
                    " 缺少清晰行动路线")
            for right in actor_points[left_index + 1:]:
                if (_distance(left.get("start") or {}, right.get("start") or {})
                        < MIN_ACTOR_SEPARATION):
                    issues.append(
                        f"镜头 {shot_no} 人物起点重叠："
                        f"{left.get('actor_id')}/{right.get('actor_id')}")
                if (_distance(left.get("end") or {}, right.get("end") or {})
                        < MIN_ACTOR_SEPARATION):
                    issues.append(
                        f"镜头 {shot_no} 人物终点重叠："
                        f"{left.get('actor_id')}/{right.get('actor_id')}")
                if min(
                        _distance(left.get("start") or {},
                                  right.get("end") or {}),
                        _distance(left.get("end") or {},
                                  right.get("start") or {}),
                ) < MIN_ACTOR_SEPARATION:
                    issues.append(
                        f"镜头 {shot_no} 人物起终点交叉重叠："
                        f"{left.get('actor_id')}/{right.get('actor_id')}")
        scene_no = shot.get("scene_no")
        for actor in block.get("actors", []):
            key = (scene_no, actor.get("name"))
            if key in previous and actor.get("start") != previous[key]:
                issues.append(f"镜头 {shot_no} 的 {actor.get('name')} 起点未继承上一镜终点")
            previous[key] = actor.get("end")
        camera = block.get("camera") or {}
        if (not camera.get("start") or not camera.get("end")
                or not camera.get("target") or not camera.get("route")
                or not camera.get("direction_label")):
            issues.append(f"镜头 {shot_no} 缺少机位或视线目标")
        elif (
                not _point_3d_valid(camera.get("start_3d"))
                or not _point_3d_valid(camera.get("end_3d"))
                or not _point_3d_valid(camera.get("target_3d"))
                or not camera.get("route_3d")
                or not all(_point_3d_valid(point)
                           for point in camera.get("route_3d", []))
                or not camera.get("orientation_start")
                or not camera.get("orientation_end")
                or float(camera.get("horizontal_fov_degrees") or 0) <= 0
                or float(camera.get("vertical_fov_degrees") or 0) <= 0):
            issues.append(f"镜头 {shot_no} 缺少合法三维机位/视锥")
        elif camera.get("moving") and len(camera.get("route", [])) < 2:
            issues.append(f"镜头 {shot_no} 缺少摄影机移动起终点")
        elif (not camera.get("moving")
              and camera.get("start") != camera.get("end")):
            issues.append(f"镜头 {shot_no} 静止机位起终点不一致")
        dialogue = block.get("dialogue_continuity") or {}
        if dialogue:
            if dialogue.get("schema") != DIALOGUE_SCHEMA:
                issues.append(
                    f"镜头 {shot_no} 双人对话合同版本错误")
            pair_ids = list(dialogue.get("pair_actor_ids") or [])
            actual_ids = [actor.get("actor_id") for actor in actor_points]
            if len(pair_ids) != 2 or set(pair_ids) != set(actual_ids):
                issues.append(
                    f"镜头 {shot_no} 双人对话角色编号与空间人物不一致")
            left_id = dialogue.get("screen_left_actor_id")
            right_id = dialogue.get("screen_right_actor_id")
            if (not left_id or not right_id or left_id == right_id
                    or {left_id, right_id} != set(actual_ids)):
                issues.append(
                    f"镜头 {shot_no} 缺少稳定的双人左右锚点")
            actor_by_id = {
                actor.get("actor_id"): actor for actor in actor_points}
            for actor_id, other_id in (
                    (left_id, right_id), (right_id, left_id)):
                actor = actor_by_id.get(actor_id) or {}
                other = actor_by_id.get(other_id) or {}
                if (actor.get("gaze_target_actor_id") != other_id
                        or other.get("name") not in str(
                            actor.get("facing") or "")):
                    issues.append(
                        f"镜头 {shot_no} 的 {actor_id} 未与"
                        f"{other_id} 建立互锁视线/相向站位")
            axis = dialogue.get("axis") or {}
            axis_a, axis_b = axis.get("a") or {}, axis.get("b") or {}
            expected_side = int(dialogue.get("camera_side_sign") or 0)
            if expected_side not in (-1, 1):
                issues.append(
                    f"镜头 {shot_no} 双人对话未声明许可机位半平面")
            elif any(
                    _axis_side(axis_a, axis_b, camera.get(phase) or {})
                    != expected_side
                    for phase in ("start", "end")):
                issues.append(
                    f"镜头 {shot_no} 摄影机跨越双人表演轴或落在轴线上")
            dialogue_key = (
                scene_no, str(dialogue.get("pair_id") or ""))
            prior = previous_dialogue.get(dialogue_key)
            current = {
                key: dialogue.get(key) for key in (
                    "screen_left_actor_id", "screen_right_actor_id",
                    "axis_id", "camera_side_sign")
            }
            if prior and current != prior:
                issues.append(
                    f"镜头 {shot_no} 双人左右锚点/轴线侧未继承上一镜")
            previous_dialogue[dialogue_key] = current
        for point in {
                ((camera.get(phase) or {}).get("x"),
                 (camera.get(phase) or {}).get("y"))
                for phase in ("start", "end") if camera.get(phase)}:
            camera_point = {"x": point[0], "y": point[1]}
            if any(_distance(camera_point, actor_point) < MIN_CAMERA_SEPARATION
                   for actor in actor_points
                   for actor_point in (actor.get("start"), actor.get("end"))):
                issues.append(f"镜头 {shot_no} 摄像机图标与人物标记重叠")
    return {"passed": not issues, "issues": issues,
            "checked_shots": len(storyboard.get("shots", []))}


def shot_blocking(plan, shot_no):
    if not plan:
        return None
    return (plan.get("shot_index") or {}).get(str(shot_no))


def mark_spatial_reference_requirements(plan):
    """标出必须随 Seedance 提交的空间图。

    多人镜头、镜头内摄影机移动，以及同场相邻镜头发生机位变化时都必须
    带空间图。这个标记同时写入 scene.shots 与 shot_index，兼容从 JSON
    重新加载后两处对象不再共享引用的情况。
    """
    if not plan:
        return plan
    index = plan.setdefault("shot_index", {})
    for scene in plan.get("scenes", []):
        previous_camera = None
        for shot in scene.get("shots", []):
            camera = shot.get("camera") or {}
            start = camera.get("start") or {}
            end = camera.get("end") or start
            moving = bool(camera.get("moving")) or start != end
            camera_changed = (
                previous_camera is not None and start != previous_camera)
            people = int(shot.get("character_count") or 0)
            reasons = []
            if people > 1:
                reasons.append(f"{people}人同框")
            if moving:
                reasons.append("镜头内机位移动")
            if camera_changed:
                reasons.append("相邻镜头机位变化")
            required = bool(reasons)
            # 本机没有 SVG→PNG 工具时豁免"必传":空间图是辅助参考,
            # 不能因环境缺一个转换器把整条生产线卡死(macOS 自带 sips
            # 不受影响;Linux 装 rsvg-convert/ImageMagick 即恢复强制)
            waived = ""
            if required and not spatial_png_supported():
                required = False
                waived = NO_SVG_CONVERTER
            shot["spatial_reference_required"] = required
            shot["spatial_reference_reason"] = "、".join(reasons)
            shot["spatial_reference_waived"] = waived
            indexed = index.get(str(shot.get("shot_no")))
            if isinstance(indexed, dict):
                indexed["spatial_reference_required"] = required
                indexed["spatial_reference_reason"] = "、".join(reasons)
                indexed["spatial_reference_waived"] = waived
            previous_camera = end or start
    return plan


def requires_spatial_reference(block):
    """单镜是否必须给 Seedance 空间图；旧文档没有新标记时安全兜底。"""
    if not block:
        return False
    if "spatial_reference_required" in block:
        return bool(block.get("spatial_reference_required"))
    camera = block.get("camera") or {}
    start = camera.get("start") or {}
    end = camera.get("end") or start
    return (int(block.get("character_count") or 0) > 1
            or bool(camera.get("moving")) or start != end)


def _line(x1, y1, x2, y2, **attrs):
    extra = " ".join(f'{key.replace("_", "-")}="{html.escape(str(value))}"'
                     for key, value in attrs.items())
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" {extra}/>'


def _project_3d(point, plot_x, plot_y, plot_width, plot_height):
    """固定等距投影；交互查看器会使用同一坐标但允许用户旋转。"""
    x = float(point.get("x", 0))
    y = float(point.get("y", 0))
    z = float(point.get("z", 0))
    scale = min(plot_width / 13.0, plot_height / 8.5)
    screen_x = plot_x + plot_width / 2 + (x - z) * .68 * scale
    screen_y = (
        plot_y + plot_height * .64
        + (x + z) * .29 * scale
        - y * .95 * scale
    )
    return round(screen_x, 1), round(screen_y, 1)


def _stick_figure_geometry(anchor, height, pose):
    """Return a small 3D stick skeleton for the actor's resolved body pose.

    ``anchor`` remains the authoritative blocking position.  The skeleton is
    only a clearer visualization of that point; it must never change routing
    coordinates or be interpreted as character appearance.
    """
    anchor = anchor or {}
    base_x = float(anchor.get("x", 0))
    base_y = float(anchor.get("y", 0))
    base_z = float(anchor.get("z", 0))
    height = max(.55, float(height or DEFAULT_ACTOR_HEIGHT_M))
    pose = str(pose or "standing")

    def point(x=0, y=0, z=0):
        return {
            "x": round(base_x + x, 3),
            "y": round(base_y + y, 3),
            "z": round(base_z + z, 3),
        }

    if pose == "lying":
        body_length = max(1.4, height * 2.8)
        joints = {
            "head": point(body_length * .45, height * .72),
            "neck": point(body_length * .32, height * .61),
            "shoulder_l": point(body_length * .24, height * .58, -.18),
            "shoulder_r": point(body_length * .24, height * .58, .18),
            "hip": point(-body_length * .12, height * .50),
            "elbow_l": point(body_length * .05, height * .45, -.28),
            "elbow_r": point(body_length * .05, height * .45, .28),
            "hand_l": point(-body_length * .10, height * .36, -.34),
            "hand_r": point(-body_length * .10, height * .36, .34),
            "knee_l": point(-body_length * .31, height * .38, -.10),
            "knee_r": point(-body_length * .31, height * .38, .10),
            "foot_l": point(-body_length * .48, height * .26, -.16),
            "foot_r": point(-body_length * .48, height * .26, .16),
        }
    else:
        profiles = {
            "standing": {
                "head_x": 0, "neck_x": 0, "shoulder_x": 0,
                "hip_x": 0, "hip_y": .43,
                "knee_x": .11, "knee_y": .22, "foot_x": .18,
            },
            "sitting": {
                "head_x": .05, "neck_x": .03, "shoulder_x": .02,
                "hip_x": 0, "hip_y": .48,
                "knee_x": .25, "knee_y": .30, "foot_x": .25,
            },
            "leaning_seated": {
                "head_x": .18, "neck_x": .13, "shoulder_x": .09,
                "hip_x": 0, "hip_y": .46,
                "knee_x": .25, "knee_y": .29, "foot_x": .26,
            },
            "kneeling": {
                "head_x": .03, "neck_x": .02, "shoulder_x": 0,
                "hip_x": 0, "hip_y": .45,
                "knee_x": .21, "knee_y": .09, "foot_x": .31,
            },
            "crouching": {
                "head_x": .10, "neck_x": .07, "shoulder_x": .03,
                "hip_x": -.04, "hip_y": .39,
                "knee_x": .28, "knee_y": .16, "foot_x": .35,
            },
        }
        profile = profiles.get(pose, profiles["standing"])
        shoulder_center = profile["shoulder_x"]
        arm_drop = .42 if pose == "standing" else .38
        joints = {
            "head": point(profile["head_x"], height * .91),
            "neck": point(profile["neck_x"], height * .80),
            "shoulder_l": point(shoulder_center - .20, height * .75),
            "shoulder_r": point(shoulder_center + .20, height * .75),
            "hip": point(profile["hip_x"], height * profile["hip_y"]),
            "elbow_l": point(shoulder_center - .29, height * .58),
            "elbow_r": point(shoulder_center + .29, height * .58),
            "hand_l": point(shoulder_center - .33, height * arm_drop),
            "hand_r": point(shoulder_center + .33, height * arm_drop),
            "knee_l": point(
                profile["hip_x"] - profile["knee_x"],
                height * profile["knee_y"]),
            "knee_r": point(
                profile["hip_x"] + profile["knee_x"],
                height * profile["knee_y"]),
            "foot_l": point(-profile["foot_x"], 0),
            "foot_r": point(profile["foot_x"], 0),
        }
    segments = (
        ("neck", "shoulder_l"), ("neck", "shoulder_r"),
        ("neck", "hip"),
        ("shoulder_l", "elbow_l"), ("elbow_l", "hand_l"),
        ("shoulder_r", "elbow_r"), ("elbow_r", "hand_r"),
        ("hip", "knee_l"), ("knee_l", "foot_l"),
        ("hip", "knee_r"), ("knee_r", "foot_r"),
    )
    return {
        "anchor": point(),
        "head": joints["head"],
        "joints": joints,
        "segments": segments,
    }


def _render_stick_figure_svg(
        parts, anchor, height, pose, color, phase,
        plot_x, plot_y, plot_width, plot_height, actor_id=""):
    """Append one projected, pose-aware stick figure to an SVG panel."""
    figure = _stick_figure_geometry(anchor, height, pose)
    ghost = phase == "start"
    attrs = (
        f'data-actor-model="stick-figure" '
        f'data-actor-id="{html.escape(str(actor_id or ""))}" '
        f'data-pose="{html.escape(str(pose or "standing"))}" '
        f'data-phase="{html.escape(str(phase))}"')
    parts.append(
        f'<g {attrs} opacity="{".52" if ghost else ".96"}">')
    for start_name, end_name in figure["segments"]:
        start = _project_3d(
            figure["joints"][start_name],
            plot_x, plot_y, plot_width, plot_height)
        end = _project_3d(
            figure["joints"][end_name],
            plot_x, plot_y, plot_width, plot_height)
        parts.append(_line(
            *start, *end, stroke=color,
            stroke_width="2.2" if ghost else "3.6",
            stroke_dasharray="4 4" if ghost else "",
            stroke_linecap="round"))
    head = _project_3d(
        figure["head"], plot_x, plot_y, plot_width, plot_height)
    anchor_point = _project_3d(
        figure["anchor"], plot_x, plot_y, plot_width, plot_height)
    parts.extend([
        f'<circle cx="{head[0]}" cy="{head[1]}" r="7" '
        f'fill="{"#111827" if ghost else color}" stroke="'
        f'{color if ghost else "#f8fafc"}" stroke-width="2" '
        'data-stick-head="true"/>',
        f'<circle cx="{anchor_point[0]}" cy="{anchor_point[1]}" r="3" '
        f'fill="{color}" stroke="#f8fafc" stroke-width="1" '
        'data-stick-anchor="true"/>',
        '</g>',
    ])
    return figure


def _svg_points(points):
    return " ".join(f"{x},{y}" for x, y in points)


def _render_floor_grid(parts, plot_x, plot_y, plot_width, plot_height):
    floor = [
        _project_3d({"x": x, "y": 0, "z": z},
                    plot_x, plot_y, plot_width, plot_height)
        for x, z in (
            (-WORLD_WIDTH_M / 2, -WORLD_DEPTH_M / 2),
            (WORLD_WIDTH_M / 2, -WORLD_DEPTH_M / 2),
            (WORLD_WIDTH_M / 2, WORLD_DEPTH_M / 2),
            (-WORLD_WIDTH_M / 2, WORLD_DEPTH_M / 2),
        )
    ]
    parts.append(
        f'<polygon points="{_svg_points(floor)}" fill="#0d1728" '
        'stroke="#475569" stroke-width="1.5" data-world-floor="true"/>')
    for x in range(-5, 6):
        a = _project_3d({"x": x, "y": 0, "z": -3.5},
                        plot_x, plot_y, plot_width, plot_height)
        b = _project_3d({"x": x, "y": 0, "z": 3.5},
                        plot_x, plot_y, plot_width, plot_height)
        parts.append(_line(*a, *b, stroke="#25354d", stroke_width=".7"))
    for index in range(-7, 8):
        z = index / 2
        a = _project_3d({"x": -5, "y": 0, "z": z},
                        plot_x, plot_y, plot_width, plot_height)
        b = _project_3d({"x": 5, "y": 0, "z": z},
                        plot_x, plot_y, plot_width, plot_height)
        parts.append(_line(*a, *b, stroke="#25354d", stroke_width=".7"))
    origin = _project_3d({"x": 0, "y": 0, "z": 0},
                         plot_x, plot_y, plot_width, plot_height)
    axes = (
        ("X", {"x": 1.4, "y": 0, "z": 0}, "#f87171"),
        ("Y", {"x": 0, "y": 1.4, "z": 0}, "#4ade80"),
        ("Z", {"x": 0, "y": 0, "z": 1.4}, "#60a5fa"),
    )
    for label, point, color in axes:
        end = _project_3d(
            point, plot_x, plot_y, plot_width, plot_height)
        parts.extend([
            _line(*origin, *end, stroke=color, stroke_width="2"),
            f'<text x="{end[0] + 3}" y="{end[1] - 2}" fill="{color}" '
            f'font-size="8" font-family="sans-serif">{label}</text>',
        ])


def render_scene_svg(scene):
    """渲染逐镜固定 3D 透视图，供人审与图片/视频模型共同参考。"""
    title = html.escape(str(scene.get("location") or "空间调度图"))
    scene_actors = scene.get("actors", [])
    shots = scene.get("shots", [])
    columns = 2 if len(shots) > 1 else 1
    panel_width, panel_height, panel_gap = 580, 440, 20
    canvas_width = 1220 if columns == 2 else 620
    legend_rows = max(1, math.ceil(len(scene_actors) / 2))
    header_height = 104 + legend_rows * 26
    shot_rows = max(1, math.ceil(len(shots) / columns))
    canvas_height = header_height + shot_rows * (panel_height + panel_gap) + 20
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {canvas_width} {canvas_height}" '
        f'data-layout="per-shot-panels" data-projection="isometric-3d" '
        f'data-overlap-policy="separate-label-lanes">',
        "<defs><marker id=\"arrow\" markerWidth=\"10\" markerHeight=\"10\" "
        "refX=\"8\" refY=\"3\" orient=\"auto\"><path d=\"M0,0 L0,6 L9,3 z\" "
        "fill=\"#f8fafc\"/></marker>"
        "<marker id=\"camera-arrow\" markerWidth=\"10\" markerHeight=\"10\" "
        "refX=\"8\" refY=\"3\" orient=\"auto\"><path d=\"M0,0 L0,6 L9,3 z\" "
        "fill=\"#38bdf8\"/></marker></defs>",
        f'<rect width="{canvas_width}" height="{canvas_height}" rx="28" '
        'fill="#111827"/>',
        f'<text x="55" y="48" fill="#f8fafc" font-size="26" '
        f'font-family="sans-serif" font-weight="700">第{scene.get("scene_no")}场 · {title}</text>',
        '<text x="55" y="76" fill="#94a3b8" font-size="15" '
        'font-family="sans-serif">逐镜 3D 透视 · 姿态火柴人/路线/机位高度/瞄准线/视锥 · 单位：米</text>',
    ]
    for index, actor in enumerate(scene_actors):
        col, row = index % 2, index // 2
        x, y = 55 + col * 575, 110 + row * 26
        label = html.escape(str(actor.get("display_label") or
                                f"{actor.get('actor_id')} {actor.get('name')}"))
        parts.extend([
            f'<circle cx="{x + 8}" cy="{y - 5}" r="7" '
            f'fill="{actor.get("color", "#fff")}"/>',
            f'<text x="{x + 22}" y="{y}" fill="#cbd5e1" font-size="14" '
            f'font-family="sans-serif">{label}</text>',
        ])
    for shot_index, shot in enumerate(shots):
        no = shot.get("shot_no")
        panel_col, panel_row = shot_index % columns, shot_index // columns
        panel_x = 20 + panel_col * (panel_width + panel_gap)
        panel_y = header_height + panel_row * (panel_height + panel_gap)
        plot_x, plot_y = panel_x + 18, panel_y + 52
        plot_width, plot_height = 350, 282
        roster_x = panel_x + 386
        camera_y = panel_y + 350
        parts.extend([
            f'<g data-shot="{html.escape(str(no))}" '
            'data-layout="isolated-panel" data-world-axis="y-up">',
            f'<rect x="{panel_x}" y="{panel_y}" width="{panel_width}" '
            f'height="{panel_height}" rx="18" fill="#172033" '
            'stroke="#44506a" stroke-width="2"/>',
            f'<text x="{panel_x + 18}" y="{panel_y + 32}" fill="#f8fafc" '
            f'font-size="18" font-family="sans-serif" font-weight="700">'
            f'S{html.escape(str(no))} · {shot.get("character_count", 0)}人</text>',
            f'<rect x="{plot_x}" y="{plot_y}" width="{plot_width}" '
            f'height="{plot_height}" rx="12" fill="#111827" '
            'stroke="#334155"/>',
            f'<text x="{roster_x}" y="{plot_y + 15}" fill="#94a3b8" '
            'font-size="12" font-family="sans-serif">人物火柴人 3D 站位（稳定编号）</text>',
        ])
        _render_floor_grid(parts, plot_x, plot_y, plot_width, plot_height)
        axis = shot.get("axis") or {}
        a = axis.get("a_3d") or _world_point(axis.get("a") or {})
        b = axis.get("b_3d") or _world_point(axis.get("b") or {})
        ax, ay = _project_3d(a, plot_x, plot_y, plot_width, plot_height)
        bx, by = _project_3d(b, plot_x, plot_y, plot_width, plot_height)
        parts.append(_line(ax, ay, bx, by, stroke="#94a3b8",
                           stroke_width="1.5", stroke_dasharray="8 7"))
        for actor_index, actor in enumerate(shot.get("actors", [])):
            start = actor.get("start_3d") or _world_point(actor["start"])
            end = actor.get("end_3d") or _world_point(actor["end"])
            start_height = float(
                actor.get("start_height_m")
                or actor.get("height_m") or DEFAULT_ACTOR_HEIGHT_M)
            end_height = float(
                actor.get("end_height_m")
                or actor.get("height_m") or DEFAULT_ACTOR_HEIGHT_M)
            color = actor.get("color", "#fff")
            sx, sy = _project_3d(
                start, plot_x, plot_y, plot_width, plot_height)
            ex, ey = _project_3d(
                end, plot_x, plot_y, plot_width, plot_height)
            if actor.get("moving"):
                parts.append(_line(
                    sx, sy, ex, ey, stroke=color, stroke_width="4",
                    marker_end="url(#arrow)", opacity=".9"))
                _render_stick_figure_svg(
                    parts, start, start_height,
                    actor.get("pose_start") or "standing",
                    color, "start", plot_x, plot_y, plot_width, plot_height,
                    actor.get("actor_id"))
            _render_stick_figure_svg(
                parts, end, end_height,
                actor.get("pose_end") or actor.get("pose_start")
                or "standing",
                color, "end" if actor.get("moving") else "fixed",
                plot_x, plot_y, plot_width, plot_height,
                actor.get("actor_id"))
            actor_label = html.escape(str(actor.get("display_label") or
                                          f"{actor.get('actor_id')} {actor.get('name')}"))
            route_label = html.escape(
                f"{actor.get('pose_label_start', '姿态未标注')}→"
                f"{actor.get('pose_label_end', '姿态未标注')}；"
                f"{actor.get('route_label') or '原地静止'}")
            roster_y = plot_y + 42 + actor_index * 30
            parts.extend([
                f'<circle cx="{roster_x + 7}" cy="{roster_y - 5}" r="6" '
                f'fill="{color}"/>',
                f'<text x="{roster_x + 18}" y="{roster_y}" fill="#f8fafc" '
                f'font-size="12" font-family="sans-serif">{actor_label}</text>',
                f'<text x="{roster_x + 18}" y="{roster_y + 14}" fill="#94a3b8" '
                f'font-size="9" font-family="sans-serif">{route_label} · '
                f'({end.get("x")},{end.get("z")})m</text>',
            ])
        camera = shot.get("camera") or {}
        cs = camera.get("start_3d") or _world_point(
            camera.get("start") or {}, DEFAULT_CAMERA_HEIGHT_M)
        ce = camera.get("end_3d") or _world_point(
            camera.get("end") or {}, DEFAULT_CAMERA_HEIGHT_M)
        target = camera.get("target_3d") or _world_point(
            camera.get("target") or {}, 1.25)
        if cs and target:
            csx, csy = _project_3d(
                cs, plot_x, plot_y, plot_width, plot_height)
            cex, cey = _project_3d(
                ce, plot_x, plot_y, plot_width, plot_height)
            tx, ty = _project_3d(
                target, plot_x, plot_y, plot_width, plot_height)
            target_left = _project_3d(
                dict(target, x=target["x"] - 1.0, y=0),
                plot_x, plot_y, plot_width, plot_height)
            target_right = _project_3d(
                dict(target, x=target["x"] + 1.0, y=2.4),
                plot_x, plot_y, plot_width, plot_height)
            parts.extend([
                f'<polygon points="{_svg_points([(cex, cey), target_left, target_right])}" '
                'fill="#38bdf8" opacity=".11" stroke="#38bdf8" '
                'stroke-width="1" data-camera-frustum="true"/>',
                _line(cex, cey, tx, ty, stroke="#7dd3fc",
                      stroke_width="1.5", stroke_dasharray="5 4"),
            ])
            if camera.get("moving"):
                parts.extend([
                    _line(csx, csy, cex, cey, stroke="#38bdf8",
                          stroke_width="4", marker_end="url(#camera-arrow)"),
                    f'<circle cx="{csx}" cy="{csy}" r="8" fill="#111827" '
                    'stroke="#38bdf8" stroke-width="3" '
                    'data-camera-phase="start"/>',
                    f'<path d="M {cex} {cey - 12} L {cex - 12} {cey + 10} '
                    f'L {cex + 12} {cey + 10} Z" fill="#38bdf8" '
                    'data-camera-phase="end"/>',
                ])
            else:
                parts.append(
                    f'<path d="M {csx} {csy - 13} L {csx - 13} {csy + 11} '
                    f'L {csx + 13} {csy + 11} Z" fill="#38bdf8" '
                    'stroke="#e0f2fe" stroke-width="2" '
                    'data-camera-phase="fixed"/>')
        camera_label = html.escape(str(camera.get("direction_label") or
                                       "静止机位：起点=终点"))
        orientation = camera.get("orientation_end") or {}
        parts.extend([
            f'<rect x="{panel_x + 18}" y="{camera_y}" width="{panel_width - 36}" '
            'height="72" rx="10" fill="#0f2940"/>',
            f'<text x="{panel_x + 32}" y="{camera_y + 22}" fill="#bae6fd" '
            f'font-size="13" font-family="sans-serif" font-weight="700">C{no} '
            f'{camera.get("lens_mm")}mm · '
            f'{html.escape(str(camera.get("movement") or "固定"))} · '
            f'FOV {camera.get("horizontal_fov_degrees", camera.get("fov_degrees"))}°</text>',
            f'<text x="{panel_x + 32}" y="{camera_y + 43}" fill="#e0f2fe" '
            f'font-size="11" font-family="sans-serif">{camera_label}</text>',
            f'<text x="{panel_x + 32}" y="{camera_y + 61}" fill="#7dd3fc" '
            f'font-size="9" font-family="sans-serif">'
            f'机位({ce.get("x")},{ce.get("y")},{ce.get("z")})m · '
            f'目标({target.get("x")},{target.get("y")},{target.get("z")})m · '
            f'朝向{orientation.get("heading_degrees", "-")}° / '
            f'俯仰{orientation.get("pitch_degrees", "-")}°</text>',
            '</g>',
        ])
    parts.append("</svg>")
    return "".join(parts)


NO_SVG_CONVERTER = ("本机没有 SVG 转 PNG 工具(sips/rsvg-convert/"
                    "inkscape/ImageMagick 任一),空间图仅供人审,"
                    "本机不强制随 Seedance 提交")


def _svg_converter_command(svg_path, png_path):
    """按平台可用性挑选 SVG→PNG 转换器(macOS sips 优先,Linux 常见
    转换器兜底);全都没有返回 None。"""
    sips = shutil.which("sips")
    if sips:
        return [sips, "-s", "format", "png", str(svg_path),
                "--out", str(png_path)]
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        return [rsvg, "-o", str(png_path), str(svg_path)]
    inkscape = shutil.which("inkscape")
    if inkscape:
        return [inkscape, str(svg_path),
                "--export-type=png",
                f"--export-filename={png_path}"]
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        return [magick, str(svg_path), str(png_path)]
    return None


def spatial_png_supported():
    """本机是否具备把空间 SVG 转成 PNG 的任一工具。"""
    return any(shutil.which(tool) for tool in
               ("sips", "rsvg-convert", "inkscape", "magick", "convert"))


def _render_svg_png(svg_path, png_path):
    """把空间 SVG 变成 Seedance 可上传的 PNG(多转换器自动择一)。"""
    command = _svg_converter_command(svg_path, png_path)
    if command is None:
        return NO_SVG_CONVERTER
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"空间图转 PNG 失败:{exc}"
    if completed.returncode != 0:
        return ("空间图转 PNG 失败:"
                + (completed.stderr or completed.stdout or "未知错误")[:240])
    try:
        valid = (png_path.exists() and png_path.stat().st_size > 64
                 and png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")
    except OSError:
        valid = False
    return "" if valid else "空间图转 PNG 后文件无效"


def write_spatial_reference_pngs(plan, out_dir):
    """为需要空间约束的镜头生成独立 PNG，供 Seedance 真实上传。

    场景总览 SVG 继续用于人审；每镜 PNG 只保留当前镜的稳定人物编号、
    行动路线和摄影机起终点，避免把其他镜头的调度误喂给视频模型。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mark_spatial_reference_requirements(plan)
    paths = {}
    index = (plan or {}).get("shot_index") or {}
    for scene in (plan or {}).get("scenes", []):
        for shot in scene.get("shots", []):
            no = int(shot.get("shot_no") or 0)
            indexed = index.get(str(no))
            required = requires_spatial_reference(shot)
            shot["spatial_reference_required"] = required
            if isinstance(indexed, dict):
                indexed["spatial_reference_required"] = required
            if not required:
                shot.pop("spatial_reference_uri", None)
                shot.pop("spatial_reference_error", None)
                if isinstance(indexed, dict):
                    indexed.pop("spatial_reference_uri", None)
                    indexed.pop("spatial_reference_error", None)
                continue
            svg_path = out_dir / f"shot_{no:03d}_space.svg"
            png_path = out_dir / f"shot_{no:03d}_space.png"
            isolated = dict(scene)
            isolated["shots"] = [shot]
            # 逐镜参考图只列出本镜人物，不能把同场其他角色的编号带给
            # 图片/视频模型，避免把场景总演员表误读成当前镜头人数。
            isolated["actors"] = [
                {
                    key: actor.get(key)
                    for key in (
                        "actor_id", "name", "role", "display_label",
                        "is_protagonist", "color")
                }
                for actor in shot.get("actors", [])
            ]
            svg_path.write_text(
                render_scene_svg(isolated), encoding="utf-8")
            error = _render_svg_png(svg_path, png_path)
            if error:
                shot["spatial_reference_error"] = error
                shot.pop("spatial_reference_uri", None)
                if isinstance(indexed, dict):
                    indexed["spatial_reference_error"] = error
                    indexed.pop("spatial_reference_uri", None)
                continue
            uri = str(png_path.resolve())
            shot["spatial_reference_uri"] = uri
            shot.pop("spatial_reference_error", None)
            if isinstance(indexed, dict):
                indexed["spatial_reference_uri"] = uri
                indexed.pop("spatial_reference_error", None)
            paths[no] = uri
    return paths


def write_spatial_svgs(plan, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mark_spatial_reference_requirements(plan)
    paths = []
    for scene in plan.get("scenes", []):
        path = out_dir / f"scene_{int(scene.get('scene_no', 0)):03d}.svg"
        path.write_text(render_scene_svg(scene), encoding="utf-8")
        scene["svg_uri"] = str(path.resolve())
        paths.append(str(path.resolve()))
    return paths


# ---------------------------------------------------------------------------
# 信息状态 × 空间事实交叉校验(警示级)
# v7 实证漏洞:全景重排把纱幔窗挪到人物正前方,人影从镜1就站在她两米外,
# 而她镜7才「惊疑发现」——发现拍点穿帮。空间一变,「谁能看见谁」必须重推。
_DISCOVERY_RE = re.compile(
    r"发现|惊觉|察觉|注意到|惊疑|抬眸.{0,6}(?:望|看)|回头.{0,6}(?:望|看)")


def awareness_sightline_issues(storyboard, blocking, max_distance=6.0):
    """「迟到的发现」穿帮检测:X 在镜 N 才发现 Y,但更早的镜里 Y 已与 X
    近距同场——要么 Y 晚点出现,要么给出遮挡/注意力理由,要么删掉发现拍点。

    只做警示不阻断:近距同场可以有合法理由(遮挡物、背对、注意力),
    但必须是**有意选择**而不是空间重排后的无意穿帮。
    """
    issues = []
    shot_index = (blocking or {}).get("shot_index") or {}
    shots = list((storyboard or {}).get("shots") or [])
    for shot in shots:
        text = " ".join(
            str(shot.get(key) or "")
            for key in ("action", "prompt", "performance", "camera_notes"))
        if not _DISCOVERY_RE.search(text):
            continue
        shot_no = shot.get("shot_no")
        block = shot_index.get(str(shot_no)) or {}
        actors = block.get("actors") or []
        if len(actors) < 2:
            continue
        observer = actors[0]
        discovered = [a for a in actors[1:] if a.get("name")]
        for target in discovered:
            for earlier in shots:
                early_no = earlier.get("shot_no")
                if (early_no is None or shot_no is None
                        or early_no >= shot_no
                        or earlier.get("scene_no") != shot.get("scene_no")):
                    continue
                early_block = shot_index.get(str(early_no)) or {}
                early_names = {
                    str(a.get("name") or "")
                    for a in (early_block.get("actors") or [])}
                if (str(observer.get("name")) not in early_names
                        or str(target.get("name")) not in early_names):
                    continue
                by_name = {str(a.get("name")): a
                           for a in early_block.get("actors") or []}
                obs_early = by_name[str(observer.get("name"))]
                tgt_early = by_name[str(target.get("name"))]
                try:
                    distance = math.hypot(
                        float(tgt_early["start_3d"]["x"])
                        - float(obs_early["start_3d"]["x"]),
                        float(tgt_early["start_3d"]["z"])
                        - float(obs_early["start_3d"]["z"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if distance > max_distance:
                    continue
                issues.append({
                    "shot_no": shot_no,
                    "earlier_shot_no": early_no,
                    "observer": observer.get("name"),
                    "target": target.get("name"),
                    "distance_m": round(distance, 1),
                    "severity": "warning",
                    "message": (
                        f"镜{shot_no}「{observer.get('name')}」才发现"
                        f"「{target.get('name')}」,但镜{early_no}里两人已"
                        f"近距同场(约{distance:.1f}米)——发现拍点可能穿帮。"
                        "要么让对方更晚出现,要么声明遮挡/背对/注意力理由。"),
                })
                break        # 每对报最早一处即可
    return issues
