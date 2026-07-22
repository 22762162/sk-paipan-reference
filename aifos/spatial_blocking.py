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
from pathlib import Path


SCHEMA = "aifos.spatial-blocking/v1"
WIDTH, HEIGHT = 1000, 700
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


def _lens_and_fov(shot):
    camera = ((shot.get("five_dimensions") or {}).get("camera_design") or {})
    lens = _number(camera.get("lens") or shot.get("camera"), 50)
    lens = max(8, min(200, lens))
    # 以全画幅水平视角做规划近似；只用于构图示意。
    fov = math.degrees(2 * math.atan(36 / (2 * lens)))
    return int(round(lens)), round(fov, 1)


def _camera_block(shot, target):
    design = ((shot.get("five_dimensions") or {}).get("camera_design") or {})
    movement = str(design.get("movement") or "固定")
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
    elif "升降" in movement:
        end_y -= 90
    lens, fov = _lens_and_fov(shot)
    return {
        "start": _point(start_x, start_y),
        "end": _point(end_x, end_y),
        "target": target,
        "movement": movement,
        "position": position,
        "lens_mm": lens,
        "fov_degrees": fov,
        "axis_offset_degrees": offset,
    }


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
    if any(word in str(s.get("camera") or "")
           for s in shots for word in ("跟", "移", "摇", "环绕", "推", "拉")):
        reasons.append("包含镜头运动")
    return bool(reasons), reasons or ["连续性参考"]


def build_spatial_plan(script, storyboard, continuity, group_threshold=3):
    """从剧本/分镜/连续性圣经构建可复现的空间调度计划。"""
    cast_names = [c.get("name") for c in continuity.get("characters", [])
                  if c.get("name")]
    for shot in storyboard.get("shots", []):
        for name in shot.get("characters", []):
            if name and name not in cast_names:
                cast_names.append(name)
    actor_ids = {name: f"P{index:02d}"
                 for index, name in enumerate(cast_names, 1)}
    colors = {name: ACTOR_COLORS[(index - 1) % len(ACTOR_COLORS)]
              for index, name in enumerate(cast_names, 1)}
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
            for actor_index, name in enumerate(people):
                state_start = (shot.get("start_state") or {}).get(name, {})
                state_end = (shot.get("end_state") or {}).get(name, {})
                fallback_x = 300 + (actor_index * 400 // max(1, len(people) - 1))
                requested_x = _position_x(
                    state_start.get("position"), fallback_x)
                start = previous_end.get(name) or _point(
                    requested_x, 330 + (actor_index % 2) * 90)
                end_x = _position_x(state_end.get("position"), start["x"])
                end_y = start["y"]
                action_text = " ".join((
                    str(shot.get("description") or ""),
                    str(shot.get("prompt") or ""),
                    str(state_end.get("pose") or ""),
                ))
                moving = any(word in action_text for word in MOTION_WORDS)
                if moving and abs(end_x - start["x"]) < 30:
                    direction = 1 if (local_index + actor_index) % 2 == 0 else -1
                    end_x = start["x"] + direction * 110
                    end_y = start["y"] - 35 + (actor_index % 2) * 70
                end = _point(end_x, end_y)
                previous_end[name] = end
                positions.append({
                    "actor_id": actor_ids[name], "name": name,
                    "color": colors[name], "start": start, "end": end,
                    "route": [start, end] if start != end else [start],
                    "moving": start != end,
                    "facing": state_end.get("direction")
                              or state_start.get("direction") or "面向主体",
                })
            target = _point(
                sum(p["end"]["x"] for p in positions) / max(1, len(positions)),
                sum(p["end"]["y"] for p in positions) / max(1, len(positions)))
            camera = _camera_block(shot, target)
            compact = ";".join(
                f"{p['actor_id']} {p['name']}({p['start']['x']},{p['start']['y']})"
                f"→({p['end']['x']},{p['end']['y']})" for p in positions)
            constraint = (
                f"空间调度锁：本镜严格 {len(people)} 人；{compact}；"
                f"机位({camera['start']['x']},{camera['start']['y']})"
                f"→({camera['end']['x']},{camera['end']['y']})，"
                f"{camera['lens_mm']}mm/{camera['movement']}，保持屏幕轴线。"
            )
            block = {
                "shot_no": int(shot.get("shot_no", 0)),
                "unit_id": shot.get("unit_id"),
                "character_count": len(people),
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
            "actors": [{"actor_id": actor_ids[name], "name": name,
                        "color": colors[name]}
                       for name in cast_names
                       if any(name in s.get("characters", []) for s in shots)],
            "shots": scene_shots,
        })
    plan = {
        "schema": SCHEMA,
        "source_fingerprint": _source_fingerprint(
            script, storyboard, continuity),
        "group_threshold": int(group_threshold or 3),
        "summary": {
            "scenes": len(scenes),
            "required_scenes": sum(1 for scene in scenes if scene["required"]),
            "shots": len(shot_index),
            "actors": len(actor_ids),
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
        scene_no = shot.get("scene_no")
        for actor in block.get("actors", []):
            key = (scene_no, actor.get("name"))
            if key in previous and actor.get("start") != previous[key]:
                issues.append(f"镜头 {shot_no} 的 {actor.get('name')} 起点未继承上一镜终点")
            previous[key] = actor.get("end")
        camera = block.get("camera") or {}
        if not camera.get("start") or not camera.get("target"):
            issues.append(f"镜头 {shot_no} 缺少机位或视线目标")
    return {"passed": not issues, "issues": issues,
            "checked_shots": len(storyboard.get("shots", []))}


def shot_blocking(plan, shot_no):
    if not plan:
        return None
    return (plan.get("shot_index") or {}).get(str(shot_no))


def _line(x1, y1, x2, y2, **attrs):
    extra = " ".join(f'{key.replace("_", "-")}="{html.escape(str(value))}"'
                     for key, value in attrs.items())
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" {extra}/>'


def render_scene_svg(scene):
    """渲染单场俯视空间图；输出 SVG 文本。"""
    title = html.escape(str(scene.get("location") or "空间调度图"))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<defs><marker id=\"arrow\" markerWidth=\"10\" markerHeight=\"10\" "
        "refX=\"8\" refY=\"3\" orient=\"auto\"><path d=\"M0,0 L0,6 L9,3 z\" "
        "fill=\"#f8fafc\"/></marker></defs>",
        '<rect width="1000" height="700" rx="28" fill="#111827"/>',
        '<rect x="55" y="92" width="890" height="540" rx="24" '
        'fill="#172033" stroke="#44506a" stroke-width="2"/>',
        f'<text x="55" y="48" fill="#f8fafc" font-size="26" '
        f'font-family="sans-serif" font-weight="700">第{scene.get("scene_no")}场 · {title}</text>',
        '<text x="55" y="76" fill="#94a3b8" font-size="15" '
        'font-family="sans-serif">俯视图 · 实线箭头=人物走位 · 虚线=180°轴线 · 三角形=摄影机</text>',
        '<rect x="70" y="112" width="190" height="76" rx="12" fill="#26324a"/>',
        '<text x="88" y="140" fill="#94a3b8" font-size="14" font-family="sans-serif">入口 / 出口</text>',
        '<text x="88" y="169" fill="#e2e8f0" font-size="18" font-family="sans-serif">场景左侧通道</text>',
        '<rect x="745" y="112" width="180" height="76" rx="12" fill="#26324a"/>',
        '<text x="763" y="140" fill="#94a3b8" font-size="14" font-family="sans-serif">主动作区</text>',
        '<text x="763" y="169" fill="#e2e8f0" font-size="18" font-family="sans-serif">主体 / 对手戏</text>',
    ]
    for shot in scene.get("shots", []):
        no = shot.get("shot_no")
        axis = shot.get("axis") or {}
        if no == (scene.get("shots") or [{}])[0].get("shot_no"):
            a, b = axis.get("a") or {}, axis.get("b") or {}
            parts.append(_line(a.get("x", 120), a.get("y", 350),
                               b.get("x", 880), b.get("y", 350),
                               stroke="#64748b", stroke_width="2",
                               stroke_dasharray="10 8"))
        for actor in shot.get("actors", []):
            start, end = actor["start"], actor["end"]
            color = actor.get("color", "#fff")
            if actor.get("moving"):
                parts.append(_line(start["x"], start["y"], end["x"], end["y"],
                                   stroke=color, stroke_width="5",
                                   marker_end="url(#arrow)", opacity="0.86"))
            parts.extend([
                f'<circle cx="{start["x"]}" cy="{start["y"]}" r="17" '
                f'fill="#111827" stroke="{color}" stroke-width="4"/>',
                f'<circle cx="{end["x"]}" cy="{end["y"]}" r="19" fill="{color}"/>',
                f'<text x="{end["x"] + 24}" y="{end["y"] + 6}" fill="#f8fafc" '
                f'font-size="15" font-family="sans-serif">S{no} {html.escape(actor["actor_id"])} '
                f'{html.escape(actor["name"])}</text>',
            ])
        camera = shot.get("camera") or {}
        cs, ce, target = camera.get("start") or {}, camera.get("end") or {}, camera.get("target") or {}
        if cs and target:
            parts.append(
                f'<path d="M {cs["x"]} {cs["y"]} L {target["x"] - 80} {target["y"] + 35} '
                f'L {target["x"] + 80} {target["y"] + 35} Z" fill="#38bdf8" opacity="0.08" '
                f'stroke="#38bdf8" stroke-width="1"/>')
            parts.append(_line(cs["x"], cs["y"], ce.get("x", cs["x"]),
                               ce.get("y", cs["y"]), stroke="#38bdf8",
                               stroke_width="4", marker_end="url(#arrow)"))
            x, y = cs["x"], cs["y"]
            parts.extend([
                f'<path d="M {x} {y - 18} L {x - 18} {y + 16} L {x + 18} {y + 16} Z" '
                'fill="#38bdf8"/>',
                f'<text x="{x + 24}" y="{y + 5}" fill="#bae6fd" font-size="14" '
                f'font-family="sans-serif">C{no} {camera.get("lens_mm")}mm·'
                f'{html.escape(str(camera.get("movement") or "固定"))}</text>',
            ])
    legend_x = 55
    for actor in scene.get("actors", []):
        parts.extend([
            f'<circle cx="{legend_x + 10}" cy="666" r="8" fill="{actor["color"]}"/>',
            f'<text x="{legend_x + 24}" y="672" fill="#cbd5e1" font-size="14" '
            f'font-family="sans-serif">{html.escape(actor["actor_id"])} '
            f'{html.escape(actor["name"])}</text>',
        ])
        legend_x += min(260, 70 + len(actor["name"]) * 15)
    parts.append("</svg>")
    return "".join(parts)


def write_spatial_svgs(plan, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for scene in plan.get("scenes", []):
        path = out_dir / f"scene_{int(scene.get('scene_no', 0)):03d}.svg"
        path.write_text(render_scene_svg(scene), encoding="utf-8")
        scene["svg_uri"] = str(path.resolve())
        paths.append(str(path.resolve()))
    return paths
