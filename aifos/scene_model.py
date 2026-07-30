"""从 720° 全景反解出**真实三维场景**:有位置、有尺寸、有落地点的物体表。

全景此前只被当贴图用——投影到房间盒内表面,看着像那间屋,但平台并不
知道「书案在哪、多大、人能不能站进去」。人物位置因此只能靠文字描述,
物理逻辑(遮挡、接触、可达、碰撞)全都无从校验。

**核心:落地物体的位置由它底部在全景里的位置唯一确定,是精确解不是估计。**
等距圆柱全景的拍摄点已知(房间中心、站立视平线 h),像素 (u,v) 对应唯一
视线方向 d;物体底部落在地面(y=0),于是

    t = h / (-d.y)        (d.y < 0,即视线朝下)
    地面点 = (t·d.x, 0, t·d.z)

不需要深度估计。物体高度同理:顶部视线与该 (x,z) 处的铅垂线求交。

坐标约定与 blocking/全景切片一致:右手系 y 上,+Z 为场景正向
(= 全景水平中心 u=0.5),yaw 自 +Z 起向 +X 为正。
"""
from __future__ import annotations

import math

SCHEMA = "aifos.scene-model/v1"
DEFAULT_CAPTURE_HEIGHT_M = 1.55
# 视线过于接近水平时,地面交点会跑到无穷远——超过这个距离一律判为
# 「看不出落地点」,而不是给一个荒谬的坐标。
MAX_FLOOR_DISTANCE_M = 30.0
_MIN_DOWNWARD = 1e-3


def direction_from_equirect(u, v):
    """等距圆柱归一化坐标 (u,v) → 单位视线方向。

    u∈[0,1) 自全景左缘起的水平比例,u=0.5 对应 +Z;v∈[0,1] 自顶向下。
    与 pano_slice / scene3d 着色器同一套映射,三处必须一致。
    """
    lon = (float(u) - 0.5) * 2.0 * math.pi          # +X 为正
    lat = (0.5 - float(v)) * math.pi                # 上为正
    cos_lat = math.cos(lat)
    return (cos_lat * math.sin(lon), math.sin(lat), cos_lat * math.cos(lon))


def equirect_from_direction(dx, dy, dz):
    """单位方向 → (u,v)。direction_from_equirect 的逆,用于自检与回投。"""
    length = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    dx, dy, dz = dx / length, dy / length, dz / length
    u = math.atan2(dx, dz) / (2.0 * math.pi) + 0.5
    v = 0.5 - math.asin(max(-1.0, min(1.0, dy))) / math.pi
    return (u % 1.0, min(1.0, max(0.0, v)))


def floor_point(u, v, capture_height=DEFAULT_CAPTURE_HEIGHT_M):
    """全景里的**落地点** → 地面世界坐标 (x, z)。看不到地面则返回 None。

    这是整个模型的地基:只要物体底部可见,位置就是解出来的,不是猜的。
    """
    dx, dy, dz = direction_from_equirect(u, v)
    if dy > -_MIN_DOWNWARD:          # 视线不朝下:打不到地面
        return None
    t = float(capture_height) / (-dy)
    if not math.isfinite(t) or t > MAX_FLOOR_DISTANCE_M:
        return None
    return (round(t * dx, 3), round(t * dz, 3))


def height_at(u_top, v_top, x, z, capture_height=DEFAULT_CAPTURE_HEIGHT_M):
    """已知物体所在 (x,z),由顶部像素解出它的高度(米)。

    顶部视线与该点的铅垂线求交:水平距离固定,高度 = h + 水平距离·tanφ。
    """
    dx, dy, dz = direction_from_equirect(u_top, v_top)
    horizontal = math.hypot(dx, dz)
    if horizontal < 1e-6:
        return None
    target_horizontal = math.hypot(float(x), float(z))
    height = float(capture_height) + target_horizontal * (dy / horizontal)
    return round(max(0.0, height), 3)


def _room_height(room):
    room = room if isinstance(room, dict) else {}
    try:
        return float(room.get("wall_height_m") or 4.2)
    except (TypeError, ValueError):
        return 4.2


WALL_TOLERANCE_M = 0.6   # 贴墙家具解到墙外一点是常态,不当异常报


def _clamp_room(x, z, room):
    """钳进房间;inside 用容差判定——书架/帷幔本来就贴墙,解到墙外
    几十厘米是标注误差的正常范围,当异常报只会淹没真正越界的条目。"""
    room = room if isinstance(room, dict) else {}
    try:
        half_w = float(room.get("floor_width_m") or 10.0) / 2
        half_d = float(room.get("floor_depth_m") or 7.0) / 2
    except (TypeError, ValueError):
        half_w, half_d = 5.0, 3.5
    inside = (abs(x) <= half_w + WALL_TOLERANCE_M
              and abs(z) <= half_d + WALL_TOLERANCE_M)
    return (max(-half_w, min(half_w, x)), max(-half_d, min(half_d, z)),
            inside)


def build_object(annotation, *, capture_height=DEFAULT_CAPTURE_HEIGHT_M,
                 room=None):
    """一条视觉标注 → 一个带位置尺寸的三维物体。解不出来就返回 None。

    标注字段(视觉模型只需给出「在全景图里的比例位置」,不必估距离):
      name           物体名
      category       furniture/prop/opening/light/decor
      base_u, base_v 底部着地点在全景里的归一化坐标
      top_v          顶部在全景里的纵向比例(可选,用于解高度)
      width_u        物体在全景里的水平跨度比例(可选,用于解宽度)
    """
    annotation = annotation if isinstance(annotation, dict) else {}
    name = str(annotation.get("name") or "").strip()
    if not name:
        return None
    try:
        base_u = float(annotation["base_u"])
        base_v = float(annotation["base_v"])
    except (KeyError, TypeError, ValueError):
        return None
    ground = floor_point(base_u, base_v, capture_height)
    if ground is None:
        return None
    x, z = ground
    x, z, inside = _clamp_room(x, z, room)
    distance = math.hypot(x, z)

    height = None
    height_overflow = False
    top_v = annotation.get("top_v")
    if top_v is not None:
        try:
            height = height_at(base_u, float(top_v), x, z, capture_height)
        except (TypeError, ValueError):
            height = None
    if height is not None:
        # 顶部视线接近水平时 height_at 会发散(实测「木柱」解出 10.6 米,
        # 而层高只有 4.2)。顶到天花板的物体本来就该按层高截断。
        ceiling = _room_height(room)
        if height > ceiling:
            height_overflow = True
            height = ceiling

    width = None
    span = annotation.get("width_u")
    if span is not None:
        try:
            # 水平跨度比例 → 张角 → 该距离上的实际宽度
            angle = abs(float(span)) * 2.0 * math.pi
            width = round(2.0 * distance * math.tan(min(angle, math.pi * 0.9)
                                                    / 2.0), 3)
        except (TypeError, ValueError):
            width = None

    return {
        "name": name,
        "category": str(annotation.get("category") or "furniture"),
        "position_3d": {"x": round(x, 2), "y": 0.0, "z": round(z, 2)},
        "distance_m": round(distance, 2),
        "yaw_deg": round(math.degrees(math.atan2(x, z)), 1),
        "height_m": height,
        "width_m": width,
        "inside_room": inside,
        "height_clamped": height_overflow,
        "source": {"base_u": round(base_u, 4), "base_v": round(base_v, 4),
                   "top_v": top_v, "width_u": span},
    }


def build_scene_model(annotations, *, location="",
                      capture_height=DEFAULT_CAPTURE_HEIGHT_M, room=None,
                      panorama_uri=""):
    """一组标注 → 场景物体表 + 自检问题清单。"""
    objects, issues = [], []
    for index, annotation in enumerate(annotations or [], 1):
        built = build_object(annotation, capture_height=capture_height,
                             room=room)
        if built is None:
            issues.append({
                "severity": "warning", "field": "annotation",
                "message": (f"第 {index} 条标注解不出落地点(缺 base_u/base_v "
                            "或视线未朝下),已跳过;该物体在三维场景里不存在"),
            })
            continue
        # 门窗帷幔壁灯本来就长在墙上,解到墙面附近是正确结果而不是异常;
        # 只有该待在屋里的家具/道具跑出去才值得报。
        if not built["inside_room"] and built["category"] in (
                "furniture", "prop"):
            issues.append({
                "severity": "warning", "field": "bounds",
                "object": built["name"],
                "message": (f"「{built['name']}」解出的位置在房间外,已钳到"
                            "墙内;可能是标注点没落在物体与地面的接触处"),
            })
        objects.append(built)
    issues.extend(overlap_issues(objects))
    return {
        "schema": SCHEMA,
        "location": location,
        "panorama_uri": panorama_uri,
        "capture": {"x": 0.0, "y": round(float(capture_height), 2), "z": 0.0},
        "room": dict(room or {}),
        "objects": objects,
        "issues": issues,
    }


def overlap_issues(objects, min_gap_m=0.25):
    """同类家具占位重叠 = 标注把两件东西解到了同一处,或房间尺寸不对。"""
    issues = []
    furniture = [o for o in objects
                 if o.get("category") in ("furniture", "prop")]
    for i, a in enumerate(furniture):
        for b in furniture[i + 1:]:
            gap = math.dist(
                (a["position_3d"]["x"], a["position_3d"]["z"]),
                (b["position_3d"]["x"], b["position_3d"]["z"]))
            if gap < min_gap_m:
                issues.append({
                    "severity": "warning", "field": "overlap",
                    "object": a["name"],
                    "message": (f"「{a['name']}」与「{b['name']}」解出的位置"
                                f"相距仅 {gap:.2f}m,几乎重叠;请检查标注的"
                                "落地点是否指向了同一处"),
                })
    return issues


def find_object(scene_model, name):
    name = str(name or "").strip()
    if not name:
        return None
    for obj in (scene_model or {}).get("objects", []):
        if obj.get("name") == name:
            return obj
    for obj in (scene_model or {}).get("objects", []):
        if name in str(obj.get("name") or ""):
            return obj
    return None


def actor_placement_issues(scene_model, actors, *, clearance_m=0.35):
    """人物站位与真实家具的物理校验:站进家具里、贴墙穿模,都在这里暴露。

    这是「真实三维场景」的兑现点——有了物体的实际位置尺寸,人物位置
    才不再只是一串文字,物理逻辑才真正可判。
    """
    issues = []
    objects = [o for o in (scene_model or {}).get("objects", [])
               if o.get("category") in ("furniture", "prop")]
    room = (scene_model or {}).get("room") or {}
    for actor in (actors or []):
        if not isinstance(actor, dict):
            continue
        start = actor.get("start_3d") or {}
        try:
            ax, az = float(start.get("x", 0.0)), float(start.get("z", 0.0))
        except (TypeError, ValueError):
            continue
        name = str(actor.get("name") or actor.get("actor_id") or "角色")
        _cx, _cz, inside = _clamp_room(ax, az, room)
        if not inside:
            issues.append({
                "severity": "block", "field": "actor_bounds",
                "actor": name,
                "message": f"「{name}」站位在房间外,画面上会是穿墙",
            })
        for obj in objects:
            gap = math.dist((ax, az), (obj["position_3d"]["x"],
                                       obj["position_3d"]["z"]))
            footprint = (float(obj.get("width_m") or 0.6)) / 2
            if gap < footprint + clearance_m:
                issues.append({
                    "severity": "warning", "field": "actor_furniture",
                    "actor": name, "object": obj["name"],
                    "message": (f"「{name}」站位距「{obj['name']}」中心仅 "
                                f"{gap:.2f}m,小于该家具半宽 {footprint:.2f}m "
                                f"加 {clearance_m}m 余量——画面上会站进家具里"),
                })
    return issues
