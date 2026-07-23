"""确定性空间调度图（blocking map）。

把五维分镜中的人物站位与摄影机设计转换成可校验的俯视坐标和 SVG。
该产物只服务预生产规划，不调用图片模型，也不会作为最终关键帧交付。
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


SCHEMA = "aifos.spatial-blocking/v2"
WIDTH, HEIGHT = 1000, 700
MIN_ACTOR_SEPARATION = 72
MIN_CAMERA_SEPARATION = 90
ACTOR_COLORS = (
    "#ff5d8f", "#52b8ff", "#ffc857", "#69db9d", "#ad8cff", "#ff8c5a",
)
MOTION_WORDS = (
    "走", "跑", "冲", "追", "进入", "进门", "离开", "起身", "靠近",
    "后退", "转身", "移动", "绕", "穿过", "上前", "退到", "跟随",
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


def _lens_and_fov(shot):
    camera = ((shot.get("five_dimensions") or {}).get("camera_design") or {})
    lens = _number(camera.get("lens") or shot.get("camera"), 50)
    lens = max(8, min(200, lens))
    # 以全画幅水平视角做规划近似；只用于构图示意。
    fov = math.degrees(2 * math.atan(36 / (2 * lens)))
    return int(round(lens)), round(fov, 1)


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
    moving = start != end
    direction = _direction(start, end)
    return {
        "start": start,
        "end": end,
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
                route_direction = _direction(start, end)
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
                    "moving": start != end,
                    "route_direction": route_direction,
                    "route_label": (f"起点→终点，{route_direction}"
                                    if start != end else "原地静止"),
                    "facing": state_end.get("direction")
                              or state_start.get("direction") or "面向主体",
                })
            target = _point(
                sum(p["end"]["x"] for p in positions) / max(1, len(positions)),
                sum(p["end"]["y"] for p in positions) / max(1, len(positions)))
            actor_markers = [point for position in positions
                             for point in (position["start"], position["end"])]
            camera = _clear_camera_icons(
                _camera_block(shot, target), actor_markers)
            compact = ";".join(
                f"{p['display_label']}({p['start']['x']},{p['start']['y']})"
                f"→({p['end']['x']},{p['end']['y']})/{p['route_direction']}"
                for p in positions)
            constraint = (
                f"空间调度锁：本镜严格 {len(people)} 人；{compact}；"
                f"机位({camera['start']['x']},{camera['start']['y']})"
                f"→({camera['end']['x']},{camera['end']['y']})，"
                f"{camera['lens_mm']}mm/{camera['movement']}，保持屏幕轴线。"
                "编号、坐标和路线仅供生产约束引用，最终画面不得出现人物编号、"
                "姓名标签、坐标、箭头或调度图符号。"
            )
            block = {
                "shot_no": int(shot.get("shot_no", 0)),
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
                "axis": {"a": _point(120, target["y"]),
                         "b": _point(880, target["y"]),
                         "rule": "机位保持在同一轴线侧，越轴须另建镜头"},
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
                       "orientation": "俯视"},
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
    index = plan.get("shot_index") or {
        str(shot.get("shot_no")): shot
        for scene in plan.get("scenes", []) for shot in scene.get("shots", [])
    }
    previous = {}
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
        elif camera.get("moving") and len(camera.get("route", [])) < 2:
            issues.append(f"镜头 {shot_no} 缺少摄影机移动起终点")
        elif (not camera.get("moving")
              and camera.get("start") != camera.get("end")):
            issues.append(f"镜头 {shot_no} 静止机位起终点不一致")
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


def _panel_point(point, plot_x, plot_y, plot_width, plot_height):
    x = plot_x + (point.get("x", 500) - 90) / (WIDTH - 180) * plot_width
    y = plot_y + (point.get("y", 350) - 115) / (HEIGHT - 230) * plot_height
    return round(x, 1), round(y, 1)


def render_scene_svg(scene):
    """渲染单场俯视空间图；逐镜分面，避免跨镜标记互相覆盖。"""
    title = html.escape(str(scene.get("location") or "空间调度图"))
    scene_actors = scene.get("actors", [])
    shots = scene.get("shots", [])
    columns = 2
    panel_width, panel_height, panel_gap = 580, 420, 20
    canvas_width = 1220
    legend_rows = max(1, math.ceil(len(scene_actors) / 2))
    header_height = 100 + legend_rows * 26
    shot_rows = max(1, math.ceil(len(shots) / columns))
    canvas_height = header_height + shot_rows * (panel_height + panel_gap) + 20
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {canvas_width} {canvas_height}" '
        f'data-layout="per-shot-panels" data-overlap-policy="separate-label-lanes">',
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
        'font-family="sans-serif">每镜独立俯视图 · 空心=起点 · 实心=终点 · 箭头=方向 · 三角形=摄影机</text>',
    ]
    for index, actor in enumerate(scene_actors):
        col, row = index % 2, index // 2
        x, y = 55 + col * 575, 108 + row * 26
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
        plot_width, plot_height = 350, 262
        roster_x = panel_x + 386
        camera_y = panel_y + 332
        parts.extend([
            f'<g data-shot="{html.escape(str(no))}" data-layout="isolated-panel">',
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
            'font-size="12" font-family="sans-serif">镜头前人物（稳定编号）</text>',
        ])
        axis = shot.get("axis") or {}
        a, b = axis.get("a") or {}, axis.get("b") or {}
        ax, ay = _panel_point(a, plot_x, plot_y, plot_width, plot_height)
        bx, by = _panel_point(b, plot_x, plot_y, plot_width, plot_height)
        parts.append(_line(ax, ay, bx, by, stroke="#64748b",
                           stroke_width="1.5", stroke_dasharray="8 7"))
        for actor_index, actor in enumerate(shot.get("actors", [])):
            start, end = actor["start"], actor["end"]
            color = actor.get("color", "#fff")
            sx, sy = _panel_point(start, plot_x, plot_y,
                                  plot_width, plot_height)
            ex, ey = _panel_point(end, plot_x, plot_y,
                                  plot_width, plot_height)
            if actor.get("moving"):
                parts.append(_line(sx, sy, ex, ey,
                                   stroke=color, stroke_width="5",
                                   marker_end="url(#arrow)", opacity="0.86"))
                parts.extend([
                    f'<circle cx="{sx}" cy="{sy}" r="14" fill="#111827" '
                    f'stroke="{color}" stroke-width="4" data-phase="start"/>',
                    f'<circle cx="{ex}" cy="{ey}" r="15" fill="{color}" '
                    'data-phase="end"/>',
                    f'<text x="{sx}" y="{sy + 4}" text-anchor="middle" '
                    f'fill="#f8fafc" font-size="8" font-family="sans-serif">起</text>',
                    f'<text x="{ex}" y="{ey + 4}" text-anchor="middle" '
                    f'fill="#111827" font-size="8" font-family="sans-serif">终</text>',
                ])
            else:
                parts.append(
                    f'<circle cx="{sx}" cy="{sy}" r="15" fill="{color}" '
                    f'stroke="#f8fafc" stroke-width="2" data-phase="fixed"/>')
            actor_label = html.escape(str(actor.get("display_label") or
                                          f"{actor.get('actor_id')} {actor.get('name')}"))
            route_label = html.escape(str(actor.get("route_label") or "原地静止"))
            roster_y = plot_y + 42 + actor_index * 28
            parts.extend([
                f'<circle cx="{roster_x + 7}" cy="{roster_y - 5}" r="6" '
                f'fill="{color}"/>',
                f'<text x="{roster_x + 18}" y="{roster_y}" fill="#f8fafc" '
                f'font-size="12" font-family="sans-serif">{actor_label}</text>',
                f'<text x="{roster_x + 18}" y="{roster_y + 14}" fill="#94a3b8" '
                f'font-size="9" font-family="sans-serif">{route_label}</text>',
            ])
        camera = shot.get("camera") or {}
        cs, ce, target = camera.get("start") or {}, camera.get("end") or {}, camera.get("target") or {}
        if cs and target:
            csx, csy = _panel_point(cs, plot_x, plot_y,
                                    plot_width, plot_height)
            cex, cey = _panel_point(ce, plot_x, plot_y,
                                    plot_width, plot_height)
            tx, ty = _panel_point(target, plot_x, plot_y,
                                  plot_width, plot_height)
            parts.append(
                f'<path d="M {cex} {cey} L {tx - 28} {ty + 15} '
                f'L {tx + 28} {ty + 15} Z" fill="#38bdf8" opacity="0.08" '
                f'stroke="#38bdf8" stroke-width="1"/>')
            if camera.get("moving"):
                parts.extend([
                    _line(csx, csy, cex, cey, stroke="#38bdf8",
                          stroke_width="4", marker_end="url(#camera-arrow)"),
                    f'<path d="M {csx} {csy - 13} L {csx - 13} {csy + 11} '
                    f'L {csx + 13} {csy + 11} Z" fill="#111827" '
                    'stroke="#38bdf8" stroke-width="3" data-camera-phase="start"/>',
                    f'<path d="M {cex} {cey - 13} L {cex - 13} {cey + 11} '
                    f'L {cex + 13} {cey + 11} Z" fill="#38bdf8" '
                    'data-camera-phase="end"/>',
                ])
            else:
                parts.append(
                    f'<path d="M {csx} {csy - 14} L {csx - 14} {csy + 12} '
                    f'L {csx + 14} {csy + 12} Z" fill="#38bdf8" '
                    'stroke="#e0f2fe" stroke-width="2" data-camera-phase="fixed"/>')
        camera_label = html.escape(str(camera.get("direction_label") or
                                       "静止机位：起点=终点"))
        parts.extend([
            f'<rect x="{panel_x + 18}" y="{camera_y}" width="{panel_width - 36}" '
            'height="68" rx="10" fill="#0f2940"/>',
            f'<text x="{panel_x + 32}" y="{camera_y + 22}" fill="#bae6fd" '
            f'font-size="13" font-family="sans-serif" font-weight="700">C{no} '
            f'{camera.get("lens_mm")}mm · '
            f'{html.escape(str(camera.get("movement") or "固定"))}</text>',
            f'<text x="{panel_x + 32}" y="{camera_y + 43}" fill="#e0f2fe" '
            f'font-size="11" font-family="sans-serif">{camera_label}</text>',
            f'<text x="{panel_x + 32}" y="{camera_y + 59}" fill="#7dd3fc" '
            f'font-size="9" font-family="sans-serif">起点({cs.get("x")},{cs.get("y")}) '
            f'→ 终点({ce.get("x")},{ce.get("y")}) · 视线目标('
            f'{target.get("x")},{target.get("y")})</text>',
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
