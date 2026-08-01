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

SCHEMA = "aifos.scene-model/v2"
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

_DEFAULT_GEOMETRY = {
    "furniture": {"width_m": 0.8, "height_m": 0.9, "depth_ratio": 0.58,
                  "min_depth_m": 0.35, "max_depth_m": 1.2},
    "prop": {"width_m": 0.3, "height_m": 0.3, "depth_ratio": 0.72,
             "min_depth_m": 0.12, "max_depth_m": 0.55},
    "opening": {"width_m": 1.0, "height_m": 2.2, "depth_ratio": 0.08,
                "min_depth_m": 0.08, "max_depth_m": 0.18},
    "light": {"width_m": 0.35, "height_m": 1.5, "depth_ratio": 0.72,
              "min_depth_m": 0.18, "max_depth_m": 0.5},
    "decor": {"width_m": 0.5, "height_m": 1.2, "depth_ratio": 0.12,
              "min_depth_m": 0.08, "max_depth_m": 0.35},
}


def _semantic_height_cap(name, category):
    """Return a conservative real-world cap for visually ambiguous objects.

    A single panorama ray can overestimate an object's height when the visual
    annotator puts ``top_v`` on a tall background object instead of the small
    foreground object.  Desks in the production episode were consequently
    reconstructed as 1.5 m walls and blocked an eye-level camera.  Semantic
    caps are deliberately limited to unmistakable low object classes; tall
    cabinets and architectural elements remain purely measured.
    """
    text = str(name or "")
    if category == "furniture":
        if (any(word in text for word in ("书案", "画案", "木案", "桌", "台面"))
                and "档案" not in text):
            return 1.1
        if any(word in text for word in ("椅", "凳", "条凳")):
            return 1.25
    if category == "prop":
        if any(word in text for word in ("纸", "书", "册", "卷", "牒", "信")):
            return 0.45
    return None


def _effective_height(obj):
    """Collision/prompt height, including migration for saved v2 models."""
    try:
        height = float((obj or {}).get("height_m") or 0.0)
    except (TypeError, ValueError):
        height = 0.0
    cap = _semantic_height_cap(
        (obj or {}).get("name"), (obj or {}).get("category"))
    return min(height, cap) if cap is not None else height


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _normal_angle(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return round(((number + 180.0) % 360.0) - 180.0, 1)


def _object_footprint(x, z, width, depth, rotation_y_deg):
    """返回旋转盒体的四个地面角点；本地宽轴=X，本地深度轴=Z。"""
    half_w, half_d = float(width) / 2.0, float(depth) / 2.0
    yaw = math.radians(float(rotation_y_deg))
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    points = []
    for local_x, local_z in (
            (-half_w, -half_d), (half_w, -half_d),
            (half_w, half_d), (-half_w, half_d)):
        points.append({
            "x": round(x + local_x * cos_yaw + local_z * sin_yaw, 3),
            "y": 0.0,
            "z": round(z - local_x * sin_yaw + local_z * cos_yaw, 3),
        })
    return points


def _footprint_inside_room(points, room):
    room = room if isinstance(room, dict) else {}
    try:
        half_w = float(room.get("floor_width_m") or 10.0) / 2.0
        half_d = float(room.get("floor_depth_m") or 7.0) / 2.0
    except (TypeError, ValueError):
        half_w, half_d = 5.0, 3.5
    return all(
        abs(float(point["x"])) <= half_w + WALL_TOLERANCE_M
        and abs(float(point["z"])) <= half_d + WALL_TOLERANCE_M
        for point in points)


def _clamp_footprint_center(x, z, width, depth, rotation_y_deg, room):
    """把完整旋转盒体钳进房间，返回中心与是否无需修正。"""
    room = room if isinstance(room, dict) else {}
    try:
        half_room_w = float(room.get("floor_width_m") or 10.0) / 2.0
        half_room_d = float(room.get("floor_depth_m") or 7.0) / 2.0
    except (TypeError, ValueError):
        half_room_w, half_room_d = 5.0, 3.5
    yaw = math.radians(float(rotation_y_deg))
    half_w, half_d = float(width) / 2.0, float(depth) / 2.0
    extent_x = abs(math.cos(yaw)) * half_w + abs(math.sin(yaw)) * half_d
    extent_z = abs(math.sin(yaw)) * half_w + abs(math.cos(yaw)) * half_d
    limit_x = max(0.0, half_room_w - extent_x)
    limit_z = max(0.0, half_room_d - extent_z)
    clamped_x = max(-limit_x, min(limit_x, float(x)))
    clamped_z = max(-limit_z, min(limit_z, float(z)))
    unchanged = (
        abs(clamped_x - float(x)) < 1e-9
        and abs(clamped_z - float(z)) < 1e-9)
    return clamped_x, clamped_z, unchanged


def _footprints_overlap(left, right, gap=0.0):
    """二维旋转矩形 SAT；gap>0 时把安全余量计入投影。"""
    if len(left) != 4 or len(right) != 4:
        return False
    axes = []
    for points in (left, right):
        for index in (0, 1):
            a, b = points[index], points[index + 1]
            edge_x = float(b["x"]) - float(a["x"])
            edge_z = float(b["z"]) - float(a["z"])
            length = math.hypot(edge_x, edge_z)
            if length > 1e-9:
                axes.append((-edge_z / length, edge_x / length))
    for axis_x, axis_z in axes:
        left_values = [
            float(point["x"]) * axis_x + float(point["z"]) * axis_z
            for point in left]
        right_values = [
            float(point["x"]) * axis_x + float(point["z"]) * axis_z
            for point in right]
        if (max(left_values) + gap < min(right_values)
                or max(right_values) + gap < min(left_values)):
            return False
    return True


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
      base_u, base_v 底边中心与地面接触点在全景里的归一化坐标
      top_v          顶部在全景里的纵向比例(可选,用于解高度)
      width_u        物体在全景里的水平跨度比例(可选,用于解宽度)
      depth_m        物体真实前后深度(可选,视觉模型按房间尺度给出)
      rotation_y_deg 物体绕竖直轴朝向(可选,角度约定同 blocking)

    单张全景能精确反解落地点、宽度和高度，不能凭几何唯一反解深度与
    朝向。因此 depth/rotation 优先使用视觉标注；缺失时使用明确记录的
    类别兜底值，绝不把估计冒充成测量值。
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

    category = str(annotation.get("category") or "furniture")
    defaults = _DEFAULT_GEOMETRY.get(
        category, _DEFAULT_GEOMETRY["furniture"])
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

    measured_width = _positive_number(width)
    measured_height = _positive_number(height)
    final_width = measured_width or float(defaults["width_m"])
    final_height = measured_height or float(defaults["height_m"])
    semantic_height_cap = _semantic_height_cap(name, category)
    semantic_height_clamped = bool(
        semantic_height_cap is not None
        and final_height > semantic_height_cap)
    if semantic_height_clamped:
        final_height = semantic_height_cap
    annotated_depth = _positive_number(annotation.get("depth_m"))
    final_depth = annotated_depth
    if final_depth is None:
        final_depth = max(
            float(defaults["min_depth_m"]),
            min(float(defaults["max_depth_m"]),
                final_width * float(defaults["depth_ratio"])))
    annotated_rotation = (
        annotation.get("rotation_y_deg")
        if annotation.get("rotation_y_deg") is not None
        else annotation.get("orientation_deg"))
    # 缺朝向时让盒体正面朝全景拍摄点：宽轴与观察射线相切。这个回退
    # 至少能让门窗、帷幔、书架贴墙，而不是全部沿世界 X 轴硬排。
    measured_position = {
        "x": round(x, 3), "y": 0.0, "z": round(z, 3)}
    position_yaw = math.degrees(math.atan2(x, z))
    rotation_y = _normal_angle(
        annotated_rotation
        if annotated_rotation is not None else position_yaw)
    footprint_clamped = False
    if category in ("furniture", "prop"):
        x, z, unchanged = _clamp_footprint_center(
            x, z, final_width, final_depth, rotation_y, room)
        footprint_clamped = not unchanged
    distance = math.hypot(x, z)
    footprint = _object_footprint(
        x, z, final_width, final_depth, rotation_y)

    return {
        "name": name,
        "category": category,
        # 毫米级保留是为了钳位后的盒体不因二位小数回舍再次越墙。
        "position_3d": {"x": round(x, 3), "y": 0.0, "z": round(z, 3)},
        "distance_m": round(distance, 2),
        "yaw_deg": round(math.degrees(math.atan2(x, z)), 1),
        "rotation_y_deg": rotation_y,
        "height_m": round(final_height, 3),
        "width_m": round(final_width, 3),
        "depth_m": round(final_depth, 3),
        "footprint_3d": footprint,
        "inside_room": inside,
        "footprint_inside_room": _footprint_inside_room(footprint, room),
        "footprint_clamped": footprint_clamped,
        "height_clamped": bool(height_overflow or semantic_height_clamped),
        "semantic_height_cap_m": semantic_height_cap,
        "geometry_sources": {
            "position": "panorama_floor_intersection",
            "width": (
                "panorama_angular_span"
                if measured_width is not None else "category_default"),
            "height": (
                "semantic_cap_over_panorama_vertical_ray"
                if semantic_height_clamped
                else ("panorama_vertical_ray"
                      if measured_height is not None else "category_default")),
            "depth": (
                "visual_annotation"
                if annotated_depth is not None else "category_default"),
            "rotation": (
                "visual_annotation"
                if annotated_rotation is not None else "radial_fallback"),
        },
        "source": {"base_u": round(base_u, 4), "base_v": round(base_v, 4),
                   "top_v": top_v, "width_u": span,
                   "depth_m": annotation.get("depth_m"),
                   "rotation_y_deg": annotated_rotation,
                   "measured_position_3d": measured_position},
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
        elif (built["footprint_clamped"]
              and built["category"] in ("furniture", "prop")):
            issues.append({
                "severity": "warning", "field": "footprint_bounds",
                "object": built["name"],
                "message": (
                    f"「{built['name']}」原始底边中心虽在房间内,但按宽"
                    f"{built['width_m']:.2f}m×深{built['depth_m']:.2f}m"
                    "画出的完整盒体会越墙,已整体钳回室内;"
                    "请复核深度、朝向或底边中心"),
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
    """同类家具盒体重叠 = 标注、朝向、尺寸或房间尺度至少一项有误。"""
    issues = []
    furniture = [o for o in objects
                 if o.get("category") in ("furniture", "prop")]
    for i, a in enumerate(furniture):
        for b in furniture[i + 1:]:
            gap = math.dist(
                (a["position_3d"]["x"], a["position_3d"]["z"]),
                (b["position_3d"]["x"], b["position_3d"]["z"]))
            overlap = _footprints_overlap(
                a.get("footprint_3d") or [],
                b.get("footprint_3d") or [],
                gap=float(min_gap_m))
            if overlap:
                issues.append({
                    "severity": "warning", "field": "overlap",
                    "object": a["name"],
                    "message": (
                        f"「{a['name']}」与「{b['name']}」的真实盒体"
                        f"相交或间距小于 {min_gap_m:.2f}m"
                        f"(中心距 {gap:.2f}m);请检查底边中心、深度与朝向"),
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


def _point_box_gap(x, z, obj):
    """点到旋转盒体占地的距离；在盒内返回 0。"""
    center = obj.get("position_3d") or {}
    ox, _oy, oz = _xyz_of(center)
    yaw = math.radians(float(obj.get("rotation_y_deg") or 0.0))
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    dx, dz = float(x) - ox, float(z) - oz
    local_x = dx * cos_yaw - dz * sin_yaw
    local_z = dx * sin_yaw + dz * cos_yaw
    half_w = float(obj.get("width_m") or 0.6) / 2.0
    half_d = float(obj.get("depth_m") or 0.4) / 2.0
    outside_x = max(abs(local_x) - half_w, 0.0)
    outside_z = max(abs(local_z) - half_d, 0.0)
    return math.hypot(outside_x, outside_z)


def _declared_actor_support(actor, obj):
    """人物与桌椅的接触是否是分镜明确声明的表演关系。"""
    pose_values = [
        str(actor.get("pose_start") or ""),
        str(actor.get("pose_end") or ""),
    ]
    support_values = [
        str(actor.get("support_start") or ""),
        str(actor.get("support_end") or ""),
    ]
    seated = any(
        word in " ".join(pose_values).lower()
        for word in ("sitting", "seated", "leaning"))
    supported_object = any(
        word in str(obj.get("name") or "")
        for word in ("案", "桌", "椅", "凳"))
    return bool(
        supported_object and (
            seated or any(
                word in " ".join(support_values)
                for word in ("座椅", "桌面", "案面"))))


def _push_outside_box(x, z, obj, room, clearance_m):
    """把盒体内的点移到最近的安全边，保留原来的表演位移最小。"""
    center = obj.get("position_3d") or {}
    ox, _oy, oz = _xyz_of(center)
    yaw = math.radians(float(obj.get("rotation_y_deg") or 0.0))
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    dx, dz = float(x) - ox, float(z) - oz
    local_x = dx * cos_yaw - dz * sin_yaw
    local_z = dx * sin_yaw + dz * cos_yaw
    half_w = float(obj.get("width_m") or 0.6) / 2.0
    half_d = float(obj.get("depth_m") or 0.4) / 2.0
    clearance = float(clearance_m)
    candidates = (
        (-half_w - clearance, max(-half_d, min(half_d, local_z))),
        (half_w + clearance, max(-half_d, min(half_d, local_z))),
        (max(-half_w, min(half_w, local_x)), -half_d - clearance),
        (max(-half_w, min(half_w, local_x)), half_d + clearance),
    )
    ranked = []
    for candidate_x, candidate_z in candidates:
        world_x = ox + candidate_x * cos_yaw + candidate_z * sin_yaw
        world_z = oz - candidate_x * sin_yaw + candidate_z * cos_yaw
        clamped_x, clamped_z, inside = _clamp_room(world_x, world_z, room)
        # 优先留在房间内，再选择位移最小的一侧，避免角色瞬移。
        ranked.append((
            0 if inside and abs(clamped_x - world_x) < 1e-6
            and abs(clamped_z - world_z) < 1e-6 else 1,
            math.hypot(world_x - float(x), world_z - float(z)),
            round(clamped_x, 3), round(clamped_z, 3),
        ))
    _room_penalty, _distance, safe_x, safe_z = min(ranked)
    return safe_x, safe_z


def repair_actor_furniture_collisions(
        scene_model, actors, *, clearance_m=0.35):
    """确定性修复站进已测量家具盒体的人物锚点。

    只自动修复原本会成为硬阻断的物理穿模；合理的坐姿/伏案接触与仅因
    深度估计产生的警告不动。相同坐标使用同一修复结果，因此相邻镜头的
    end/start 连续锚点不会被随机拆开。
    """
    model = scene_model if isinstance(scene_model, dict) else {}
    room = model.get("room") or {}
    objects = [
        obj for obj in (model.get("objects") or [])
        if isinstance(obj, dict)
        and obj.get("category") in ("furniture", "prop")
        and (obj.get("geometry_sources") or {}).get("depth")
        == "visual_annotation"
        and (obj.get("geometry_sources") or {}).get("rotation")
        == "visual_annotation"
    ]
    adjustments = []
    cache = {}
    for actor in (actors or []):
        if not isinstance(actor, dict):
            continue
        actor_name = str(actor.get("name") or actor.get("actor_id") or "角色")
        points = []
        for field in ("start_3d", "end_3d"):
            point = actor.get(field)
            if isinstance(point, dict):
                points.append((field, point))
        for index, point in enumerate(actor.get("route_3d") or []):
            if isinstance(point, dict):
                points.append((f"route_3d[{index}]", point))
        for field, point in points:
            try:
                original_x = float(point.get("x"))
                original_z = float(point.get("z"))
            except (TypeError, ValueError):
                continue
            original_key = (round(original_x, 4), round(original_z, 4))
            if original_key in cache:
                safe_x, safe_z, object_names = cache[original_key]
            else:
                safe_x, safe_z = original_x, original_z
                object_names = []
                # 修复点若落到第二个家具旁，做有限轮确定性消解。
                for _iteration in range(max(1, len(objects) * 2)):
                    changed = False
                    for obj in objects:
                        if (_declared_actor_support(actor, obj)
                                or _point_box_gap(safe_x, safe_z, obj) > 0.02):
                            continue
                        safe_x, safe_z = _push_outside_box(
                            safe_x, safe_z, obj, room, clearance_m)
                        object_names.append(str(obj.get("name") or "家具"))
                        changed = True
                    if not changed:
                        break
                cache[original_key] = (safe_x, safe_z, object_names)
            if (abs(safe_x - original_x) < 1e-6
                    and abs(safe_z - original_z) < 1e-6):
                continue
            point["x"], point["z"] = safe_x, safe_z
            adjustments.append({
                "actor": actor_name,
                "field": field,
                "phase": str(point.get("phase") or field),
                "from": {"x": round(original_x, 3),
                         "z": round(original_z, 3)},
                "to": {"x": safe_x, "z": safe_z},
                "objects": list(dict.fromkeys(object_names)),
                "clearance_m": float(clearance_m),
            })
    return adjustments


def _orientation_to(camera_point, target_point):
    """重定位机位后重算朝向，保持瞄准点不漂移。"""
    cx, cy, cz = _xyz_of(camera_point)
    tx, ty, tz = _xyz_of(target_point)
    dx, dy, dz = tx - cx, ty - cy, tz - cz
    horizontal = max(0.001, math.hypot(dx, dz))
    return {
        "heading_degrees": round(math.degrees(math.atan2(dx, dz)), 1),
        "pitch_degrees": round(math.degrees(math.atan2(dy, horizontal)), 1),
        "roll_degrees": 0.0,
    }


def repair_camera_furniture_collisions(
        scene_model, camera, *, clearance_m=0.15):
    """确定性把穿入已测量家具的机位移到最近安全边。

    相机路线是 blocking 的派生几何，不应把一个可以精确求解的盒体碰撞
    推回人工。修复只移动真正穿入“深度+朝向均由视觉标注测得”的盒体的
    机位点；估算家具仍只警告。相同三维点统一映射，固定机位和运镜首尾
    不会因字段副本不同而被拆开。瞄准点、焦段与运镜类型均保持不变。
    """
    model = scene_model if isinstance(scene_model, dict) else {}
    camera = camera if isinstance(camera, dict) else {}
    room = model.get("room") or {}
    objects = [
        obj for obj in (model.get("objects") or [])
        if isinstance(obj, dict)
        and obj.get("category") in ("furniture", "prop")
        and (obj.get("geometry_sources") or {}).get("depth")
        == "visual_annotation"
        and (obj.get("geometry_sources") or {}).get("rotation")
        == "visual_annotation"
    ]
    if not objects or not camera:
        return []
    points = []
    for field in ("position_3d", "start_3d", "end_3d"):
        point = camera.get(field)
        if _valid_xyz_point(point):
            points.append((field, point))
    for index, point in enumerate(camera.get("route_3d") or []):
        if _valid_xyz_point(point):
            points.append((f"route_3d[{index}]", point))
    adjustments = []
    cache = {}
    for field, point in points:
        original_x, original_y, original_z = _xyz_of(point)
        original_key = (
            round(original_x, 4), round(original_y, 4),
            round(original_z, 4))
        if original_key in cache:
            safe_x, safe_y, safe_z, object_names = cache[original_key]
        else:
            safe_x, safe_y, safe_z = original_x, original_y, original_z
            object_names = []
            # 一个安全边可能紧贴第二件家具；有限轮逐一消解，逻辑与人物
            # 锚点修复相同，但相机保留原镜高和目标点。
            for _iteration in range(max(1, len(objects) * 2)):
                changed = False
                for obj in objects:
                    base_y = float(
                        (obj.get("position_3d") or {}).get("y") or 0.0)
                    top_y = base_y + _effective_height(obj)
                    if safe_y > top_y + float(clearance_m):
                        continue
                    if _point_box_gap(safe_x, safe_z, obj) > 0.02:
                        continue
                    next_x, next_z = _push_outside_box(
                        safe_x, safe_z, obj, room, clearance_m)
                    if (abs(next_x - safe_x) < 1e-6
                            and abs(next_z - safe_z) < 1e-6):
                        # 房间边界使水平避让无进展时才抬高，避免死循环。
                        safe_y = round(top_y + float(clearance_m) + 0.01, 3)
                    else:
                        safe_x, safe_z = next_x, next_z
                    object_names.append(str(obj.get("name") or "家具"))
                    changed = True
                if not changed:
                    break
            cache[original_key] = (
                safe_x, safe_y, safe_z, object_names)
        if (abs(safe_x - original_x) < 1e-6
                and abs(safe_y - original_y) < 1e-6
                and abs(safe_z - original_z) < 1e-6):
            continue
        point.update({"x": safe_x, "y": safe_y, "z": safe_z})
        adjustments.append({
            "field": field,
            "phase": str(point.get("phase") or field),
            "from": {"x": round(original_x, 3),
                     "y": round(original_y, 3),
                     "z": round(original_z, 3)},
            "to": {"x": safe_x, "y": safe_y, "z": safe_z},
            "objects": list(dict.fromkeys(object_names)),
            "clearance_m": float(clearance_m),
        })
    start = camera.get("start_3d") or camera.get("position_3d")
    end = camera.get("end_3d") or start
    target_start = camera.get("target_start_3d") or camera.get("target_3d")
    target_end = camera.get("target_end_3d") or camera.get("target_3d")
    if _valid_xyz_point(start):
        camera["position_3d"] = dict(start)
        camera["director_height_m"] = float(start["y"])
        if _valid_xyz_point(target_start):
            camera["orientation_start"] = _orientation_to(
                start, target_start)
    if _valid_xyz_point(end):
        camera["director_end_height_m"] = float(end["y"])
        if _valid_xyz_point(target_end):
            camera["orientation_end"] = _orientation_to(end, target_end)
    director_camera = camera.get("director_camera")
    if isinstance(director_camera, dict) and _valid_xyz_point(start):
        director_camera["height_m"] = float(start["y"])
        if _valid_xyz_point(end):
            director_camera["end_position_3d"] = dict(end)
    return adjustments


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
        name = str(actor.get("name") or actor.get("actor_id") or "角色")
        points = actor.get("route_3d") or []
        if not points:
            points = [
                actor.get("start_3d") or {},
                actor.get("end_3d") or actor.get("start_3d") or {},
            ]
        seen = set()
        for index, point in enumerate(points):
            try:
                ax = float((point or {}).get("x", 0.0))
                az = float((point or {}).get("z", 0.0))
            except (TypeError, ValueError):
                continue
            point_key = (round(ax, 4), round(az, 4))
            if point_key in seen:
                continue
            seen.add(point_key)
            phase = str((point or {}).get("phase") or f"route_{index}")
            _cx, _cz, inside = _clamp_room(ax, az, room)
            if not inside:
                issues.append({
                    "severity": "block", "field": "actor_bounds",
                    "actor": name, "phase": phase,
                    "message": (
                        f"「{name}」在 {phase} 的站位({ax:.2f},{az:.2f})"
                        "位于房间外,画面上会是穿墙"),
                })
            for obj in objects:
                gap = _point_box_gap(ax, az, obj)
                if gap < clearance_m:
                    sources = obj.get("geometry_sources") or {}
                    measured_box = (
                        sources.get("depth") == "visual_annotation"
                        and sources.get("rotation") == "visual_annotation")
                    collision = gap <= 0.02
                    # A seated/leaning performer at a declared desk or chair
                    # is an intentional support relationship, not a person
                    # standing inside solid furniture.  Keep it visible as a
                    # warning for review, while standing/kneeling collisions
                    # remain a hard block.
                    declared_support = _declared_actor_support(actor, obj)
                    severity = (
                        "block"
                        if collision and measured_box and not declared_support
                        else "warning")
                    issues.append({
                        "severity": severity, "field": "actor_furniture",
                        "actor": name, "object": obj["name"], "phase": phase,
                        "message": (
                            f"「{name}」在 {phase} 距「{obj['name']}」"
                            f"盒体边缘仅 {gap:.2f}m,小于 {clearance_m:.2f}m"
                            "安全余量——"
                            + ("人物会站进已测量家具盒体"
                               if severity == "block"
                               else ("坐姿/伏案与已声明支撑物接触，属于表演"
                                     "关系，仅记录不阻断"
                                     if declared_support
                                     else "可能是合理接触，也可能擦碰；"
                                     "深度/朝向未完全测准时只警告，需人工看图"))),
                    })
    return issues


def camera_placement_issues(scene_model, camera, *, clearance_m=0.15):
    """摄影机路线不能穿墙或穿过真实家具盒体。

    高机位允许从家具上方通过；只有镜头中心高度仍落在物体盒体高度内
    才判碰撞，避免把摇臂/俯拍误杀成地面摄影机。
    """
    issues = []
    camera = camera if isinstance(camera, dict) else {}
    points = camera.get("route_3d") or []
    if not points:
        points = [
            camera.get("start_3d") or camera.get("position_3d") or {},
            camera.get("end_3d") or camera.get("start_3d")
            or camera.get("position_3d") or {},
        ]
    # This validator only checks geometry that actually exists.  During the
    # storyboard stage blocking has not been built yet; treating the missing
    # camera as {} used to manufacture a camera at (0, 0, 0), which could then
    # collide with furniture near the panorama capture point and abort a run
    # before any real camera had been solved.  Missing/malformed blocking is
    # owned by the spatial-contract validator, not by furniture collision.
    points = [point for point in points if _valid_xyz_point(point)]
    objects = [o for o in (scene_model or {}).get("objects", [])
               if o.get("category") in ("furniture", "prop")]
    room = (scene_model or {}).get("room") or {}
    seen = set()
    for index, point in enumerate(points):
        cx, cy, cz = _xyz_of(point)
        phase = str((point or {}).get("phase") or f"route_{index}")
        marker = (round(cx, 4), round(cy, 4), round(cz, 4), phase)
        if marker in seen:
            continue
        seen.add(marker)
        _room_x, _room_z, inside = _clamp_room(cx, cz, room)
        if not inside:
            issues.append({
                "severity": "block", "field": "camera_bounds",
                "phase": phase,
                "message": (
                    f"摄影机在 {phase} 的位置({cx:.2f},{cy:.2f},"
                    f"{cz:.2f})位于房间外，运镜会穿墙"),
            })
        for obj in objects:
            base_y = float((obj.get("position_3d") or {}).get("y") or 0.0)
            top_y = base_y + _effective_height(obj)
            if cy > top_y + clearance_m:
                continue
            gap = _point_box_gap(cx, cz, obj)
            if gap < clearance_m:
                sources = obj.get("geometry_sources") or {}
                measured_box = (
                    sources.get("depth") == "visual_annotation"
                    and sources.get("rotation") == "visual_annotation")
                collision = gap <= 0.02
                severity = (
                    "block" if collision and measured_box else "warning")
                issues.append({
                    "severity": severity, "field": "camera_furniture",
                    "object": obj.get("name"), "phase": phase,
                    "message": (
                        f"摄影机在 {phase} 距「{obj.get('name')}」盒体"
                        f"仅 {gap:.2f}m(镜高 {cy:.2f}m)；"
                        + ("已穿入测量盒体，请移动机位或提高镜头"
                           if severity == "block"
                           else "几何置信度不足，只警告并交人工复核")),
                })
    return issues


# ---------------------------------------------------------------------------
# 把三维场景写成提示词条款
#
# 用户实测的穿帮:「说在东廊纱帐后面,实际画在镜前桌子很近的位置,
# 后面纱帐还经常变」。根因是提示词里【场景】只有一句地名,物体一个坐标
# 都没有——模型每张图重新想象家具在哪,位置当然每次都不一样。
# 有了三维场景表,就能把每件东西钉在固定坐标上,并按本镜机位换算成
# 「在画面里的哪一侧、离镜头多远」这种模型真正能执行的说法。

def _screen_side(relative_yaw_deg):
    """物体相对机位视线的方位 → 画面左右描述。"""
    yaw = ((float(relative_yaw_deg) + 180.0) % 360.0) - 180.0
    if abs(yaw) <= 12:
        return "画面正中"
    if abs(yaw) >= 150:
        return "机位背后(画面外)"
    side = "右" if yaw > 0 else "左"
    if abs(yaw) <= 45:
        return f"画面中偏{side}"
    if abs(yaw) <= 90:
        return f"画面{side}侧"
    return f"画面{side}后方(可能出画)"


def _depth_word(distance_m):
    if distance_m <= 1.2:
        return "紧贴镜头的前景"
    if distance_m <= 2.5:
        return "近景层"
    if distance_m <= 4.5:
        return "中景层"
    return "背景层"


def scene_layout_clause(scene_model, camera=None, *, max_items=8):
    """【场景陈设定位】条款:每件东西钉死在坐标上,并换算成本镜画面位置。

    没有机位时只给世界坐标(适用于场景概念图);给了机位则同时给出
    「在画面哪一侧、属于前景还是背景」——这才是模型能执行的说法。
    """
    objects = [o for o in (scene_model or {}).get("objects", [])
               if isinstance(o, dict)]
    if not objects:
        return ""
    cam = camera if isinstance(camera, dict) else {}
    cam_pos = cam.get("position_3d") or cam.get("start_3d")
    cam_target = cam.get("target_3d") or cam.get("target_start_3d")
    view_yaw = None
    if isinstance(cam_pos, dict) and isinstance(cam_target, dict):
        cx, _cy, cz = _xyz_of(cam_pos)
        tx, _ty, tz = _xyz_of(cam_target)
        view_yaw = math.degrees(math.atan2(tx - cx, tz - cz))

    ranked = []
    for obj in objects:
        pos = obj.get("position_3d") or {}
        ox, _oy, oz = _xyz_of(pos)
        if view_yaw is None:
            ranked.append((obj.get("distance_m") or 0.0, obj, None, None))
            continue
        cx, _cy, cz = _xyz_of(cam_pos)
        rel_distance = math.dist((ox, oz), (cx, cz))
        obj_yaw = math.degrees(math.atan2(ox - cx, oz - cz))
        ranked.append((rel_distance, obj, rel_distance, obj_yaw - view_yaw))
    ranked.sort(key=lambda row: row[0])

    parts = []
    for _key, obj, rel_distance, rel_yaw in ranked[:max_items]:
        pos = obj.get("position_3d") or {}
        ox, _oy, oz = _xyz_of(pos)
        bits = [f"{obj.get('name')}"]
        size = []
        if obj.get("width_m"):
            size.append(f"宽{float(obj['width_m']):.1f}米")
        effective_height = _effective_height(obj)
        if effective_height:
            size.append(f"高{effective_height:.1f}米")
        if obj.get("depth_m"):
            size.append(f"深{float(obj['depth_m']):.1f}米")
        if obj.get("rotation_y_deg") is not None:
            size.append(f"朝向{float(obj['rotation_y_deg']):.0f}度")
        bits.append(f"固定在({ox:+.1f},{oz:+.1f})米"
                    + ("、" + "、".join(size) if size else ""))
        if rel_distance is not None:
            bits.append(f"距本镜机位{rel_distance:.1f}米、"
                        f"{_screen_side(rel_yaw)}、{_depth_word(rel_distance)}")
        parts.append("".join([bits[0], "(", "；".join(bits[1:]), ")"]))
    return ("【场景陈设定位】本场家具陈设的位置是固定事实,每一镜都相同,"
            "不得挪动、增删或换款式:" + "；".join(parts)
            + "。画面里这些物体必须出现在上述相对位置上——"
            "被本镜取景裁掉的可以不画,但不得改到别处。")


def _xyz_of(point):
    point = point if isinstance(point, dict) else {}
    def num(key):
        try:
            return float(point.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0
    return num("x"), num("y"), num("z")


def _valid_xyz_point(point):
    """True only for an explicit finite 3D point; never invent the origin."""
    if not isinstance(point, dict):
        return False
    try:
        values = [float(point[key]) for key in ("x", "y", "z")]
    except (KeyError, TypeError, ValueError):
        return False
    return all(math.isfinite(value) for value in values)


def occlusion_issues(scene_model, camera, actors, *, near_margin_m=0.25):
    """遮挡穿帮检测:声明「在 X 后面」,几何上却在 X 前面。

    用户实测:「说在东廊纱帐后面,却在镜头前桌子很近的位置」——这类
    前后关系错乱以前只能靠肉眼在成片里发现。
    """
    issues = []
    cam = camera if isinstance(camera, dict) else {}
    cam_pos = cam.get("position_3d") or cam.get("start_3d")
    if not isinstance(cam_pos, dict):
        return issues
    cx, _cy, cz = _xyz_of(cam_pos)
    objects = [o for o in (scene_model or {}).get("objects", [])
               if isinstance(o, dict)]
    for actor in (actors or []):
        if not isinstance(actor, dict):
            continue
        ax, _ay, az = _xyz_of(actor.get("start_3d"))
        actor_distance = math.dist((ax, az), (cx, cz))
        name = str(actor.get("name") or actor.get("actor_id") or "角色")
        for obj in objects:
            ox, _oy, oz = _xyz_of(obj.get("position_3d"))
            obj_distance = math.dist((ox, oz), (cx, cz))
            relation = " ".join(str(actor.get(key) or "") for key in (
                "facing", "facing_start", "facing_end", "occluded_by"))
            object_name = str(obj.get("name") or "")
            declared_behind = (
                object_name in relation
                and any(word in relation for word in (
                    "后面", "之后", "后方", "背后", "遮挡", "挡住")))
            if not declared_behind:
                continue
            if obj_distance > actor_distance + near_margin_m:
                issues.append({
                    "severity": "warning", "field": "occlusion",
                    "actor": name, "object": obj.get("name"),
                    "message": (
                        f"合同说「{name}」在「{obj.get('name')}」之后,但本镜"
                        f"机位下 {name} 距镜头 {actor_distance:.1f}米、"
                        f"{obj.get('name')} 距镜头 {obj_distance:.1f}米——"
                        "几何上是人在前、物在后,画出来必然穿帮"),
                })
    return issues
