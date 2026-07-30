"""导演调度器:机位由导演意图在三维度量空间里求解。

在此之前,机位是**画布坐标的副产品**——`_camera_block` 把机位放在画布
(500,625) 这个固定点上,按机位词做几个像素偏移,再朝主体拉到景别距离。
后果实测(《长夏记事》EP1 八镜):

* 景别→机距只有 2/8 生效(声明近景 1.7m,实际 4.73m):距离在画布像素里
  校正,换算成三维后不守恒;
* 镜高恒定 1.55m:`_camera_height` 只读「机位」与「运动」两个字段,而
  俯拍/仰拍写在**「角度」**字段里,函数从来看不到它;
* 方位逐镜乱跳(134°→168°→-93°→15°→…):人物站着没动,机位却每镜在屋里
  瞬移——因为方位是「画布固定点→主体连线」的方向,主体一动方位就变,
  不是任何导演选择。

本模块把这三件事倒过来:**先有导演意图,再有三维机位,画布图从三维反导**。

坐标约定与全景/blocking 一致:右手系 y 轴向上,+Z 为场景正向
(= 720° 全景水平中心),方位角 yaw 自 +Z 起向 +X 为正。
"""
from __future__ import annotations

import math

# 景别 → 被摄距离(米)。与 spatial_blocking.SCALE_TARGET_DISTANCE_M 同源,
# 这里是求解器的权威副本(三维度量,不经画布)。
SHOT_SIZE_DISTANCE_M = {
    "大特写": 0.8, "特写": 1.0, "近景": 1.7, "中近景": 1.9,
    "中景": 2.8, "膝上景": 3.0, "七分身": 3.2,
    "全景": 4.6, "远景": 7.5, "大远景": 10.0,
}

# 角度 → 俯仰角(度,正=机位高于主体向下拍)。此前整个维度是死的。
ANGLE_PITCH_DEG = {
    "顶拍": 72.0, "顶视": 72.0, "鸟瞰": 65.0, "航拍": 65.0,
    "大俯": 40.0, "俯拍": 22.0, "高机位": 18.0, "微俯": 8.0,
    "平视": 0.0, "水平": 0.0,
    "微仰": -8.0, "仰拍": -22.0, "低机位": -18.0, "大仰": -40.0,
}

# 机位 → 相对「主体正前方」的方位偏移(度)。0 = 正对主体面部。
POSITION_AZIMUTH_DEG = {
    "正面": 0.0, "正": 0.0,
    "斜侧": 35.0, "四分之三": 35.0, "三分之二": 35.0,
    "侧面": 90.0, "侧": 90.0,
    "过肩": 155.0, "反打": 180.0, "背面": 180.0, "背": 180.0,
}

SUBJECT_EYE_HEIGHT_M = 1.43       # 1.68m 主体的胸眼高度(与空间语言同口径)
MIN_CAMERA_HEIGHT_M = 0.35
MAX_CAMERA_HEIGHT_M = 4.6
# 相邻镜方位变化的「无效剪辑」下限:小于此值又同景别,剪起来像跳帧
MIN_MEANINGFUL_AZIMUTH_DELTA_DEG = 12.0


def _text(value):
    return str(value or "").strip()


def _match_token(text, table):
    """在文本里找表中最长的匹配词(长词优先,避免「中景」吃掉「中近景」)。"""
    text = _text(text)
    if not text:
        return ""
    for token in sorted(table, key=len, reverse=True):
        if token in text:
            return token
    return ""


def declared_shot_size(shot):
    design = ((shot or {}).get("five_dimensions") or {}).get(
        "camera_design") or {}
    for source in (design.get("shot_size"), design.get("scale"),
                   (shot or {}).get("camera")):
        token = _match_token(source, SHOT_SIZE_DISTANCE_M)
        if token:
            return token
    return ""


def declared_angle(shot):
    """角度维度——此前从没有人读过它,镜高因此永远是默认值。"""
    design = ((shot or {}).get("five_dimensions") or {}).get(
        "camera_design") or {}
    for source in (design.get("angle"), design.get("camera_angle"),
                   (shot or {}).get("camera")):
        token = _match_token(source, ANGLE_PITCH_DEG)
        if token:
            return token
    return ""


def declared_position(shot):
    design = ((shot or {}).get("five_dimensions") or {}).get(
        "camera_design") or {}
    for source in (design.get("camera_position"), design.get("position"),
                   (shot or {}).get("camera")):
        token = _match_token(source, POSITION_AZIMUTH_DEG)
        if token:
            return token
    return ""


def _xyz(point, default_y=0.0):
    point = point if isinstance(point, dict) else {}
    def num(key, fallback):
        try:
            return float(point.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
    return num("x", 0.0), num("y", default_y), num("z", 0.0)


def _yaw_between(from_xz, to_xz):
    """自 from 指向 to 的方位角(度):0=+Z,向 +X 为正。"""
    return math.degrees(math.atan2(to_xz[0] - from_xz[0],
                                   to_xz[1] - from_xz[1]))


def _wrap180(deg):
    return ((float(deg) + 180.0) % 360.0) - 180.0


def subject_facing_yaw(actor, actors, scene_center=(0.0, 0.0)):
    """主体面朝的方位角。

    优先级:注视对象(双人镜互锁) → 行动方向(移动镜) → 面向场景中心。
    facing 是自然语言("面向本镜主体，视线不越轴"),不可解析成角度,
    因此只用几何事实,不去猜文字。
    """
    ax, _ay, az = _xyz(actor.get("start_3d"))
    gaze_id = _text(actor.get("gaze_target_actor_id"))
    if gaze_id:
        other = next((a for a in actors
                      if _text(a.get("actor_id")) == gaze_id), None)
        if other is not None:
            ox, _oy, oz = _xyz(other.get("start_3d"))
            if (ox, oz) != (ax, az):
                return _yaw_between((ax, az), (ox, oz))
    ex, _ey, ez = _xyz(actor.get("end_3d"))
    if (ex, ez) != (ax, az):
        return _yaw_between((ax, az), (ex, ez))
    if (scene_center[0], scene_center[1]) != (ax, az):
        return _yaw_between((ax, az), scene_center)
    return 0.0


def primary_actor(actors):
    """主体 = 主角优先,其次第一个有坐标的角色。

    不用质心:双人镜里若一方是远处剪影,按质心摆位会把机位放在两人中间,
    声明的特写因此永远拍不到脸(EP1 镜1 实测偏差 2.4 米)。
    """
    actors = [a for a in (actors or []) if isinstance(a, dict)]
    if not actors:
        return None
    for actor in actors:
        if actor.get("is_protagonist"):
            return actor
    return actors[0]


def solve_camera(shot, actors, *, axis_side=1, scene_center=(0.0, 0.0),
                 world=None):
    """按导演意图求解本镜三维机位。

    返回 dict:position_3d / target_3d / height_m / distance_m / yaw_deg /
    pitch_deg / axis_side / 以及各维度实际采用的声明词(供审计与闸门)。
    """
    actor = primary_actor(actors)
    if actor is None:
        return None
    sx, _sy, sz = _xyz(actor.get("start_3d"))
    try:
        subject_height = float(actor.get("height_m") or 1.68)
    except (TypeError, ValueError):
        subject_height = 1.68
    eye = subject_height * (SUBJECT_EYE_HEIGHT_M / 1.68)

    size = declared_shot_size(shot)
    distance = SHOT_SIZE_DISTANCE_M.get(size, SHOT_SIZE_DISTANCE_M["中景"])
    angle = declared_angle(shot)
    pitch = ANGLE_PITCH_DEG.get(angle, 0.0)
    position = declared_position(shot)
    offset = POSITION_AZIMUTH_DEG.get(position, 0.0)

    facing = subject_facing_yaw(actor, actors, scene_center)
    # 机位在主体正前方 = 与朝向相反;机位词只决定绕主体转多少度,
    # 转向哪一侧由本场轴线锁定(axis_side),保证同场不越轴。
    yaw = _wrap180(facing + 180.0 + offset * (1 if axis_side >= 0 else -1))

    horizontal = distance * math.cos(math.radians(pitch))
    height = eye + distance * math.sin(math.radians(pitch))
    height = max(MIN_CAMERA_HEIGHT_M, min(MAX_CAMERA_HEIGHT_M, height))

    cx = sx + horizontal * math.sin(math.radians(yaw))
    cz = sz + horizontal * math.cos(math.radians(yaw))

    world = world if isinstance(world, dict) else {}
    try:
        half_w = float(world.get("floor_width_m") or 10.0) / 2 - 0.15
        half_d = float(world.get("floor_depth_m") or 7.0) / 2 - 0.15
    except (TypeError, ValueError):
        half_w, half_d = 4.85, 3.35
    clamped = False
    if abs(cx) > half_w or abs(cz) > half_d:
        clamped = True
        cx = max(-half_w, min(half_w, cx))
        cz = max(-half_d, min(half_d, cz))

    actual = math.dist((cx, height, cz), (sx, eye, sz))
    return {
        "position_3d": {"x": round(cx, 2), "y": round(height, 2),
                        "z": round(cz, 2)},
        "target_3d": {"x": round(sx, 2), "y": round(eye, 2),
                      "z": round(sz, 2)},
        "height_m": round(height, 2),
        "distance_m": round(actual, 2),
        "desired_distance_m": distance,
        "yaw_deg": round(yaw, 1),
        "pitch_deg": round(pitch, 1),
        "axis_side": 1 if axis_side >= 0 else -1,
        "declared": {"shot_size": size, "angle": angle,
                     "position": position},
        "subject_actor_id": _text(actor.get("actor_id")),
        "wall_clamped": clamped,
    }


def scene_axis_side(shots_with_actors, scene_center=(0.0, 0.0)):
    """本场轴线侧:由第一镜的机位方位定,全场沿用——同场不越轴。"""
    for shot, actors in shots_with_actors:
        actor = primary_actor(actors)
        if actor is None:
            continue
        offset = POSITION_AZIMUTH_DEG.get(declared_position(shot), 0.0)
        if offset:
            return 1
        return 1
    return 1


def solve_scene(shots_with_actors, *, scene_center=(0.0, 0.0), world=None):
    """求解一场内全部镜头的机位,并给出跨镜连续性问题清单。"""
    side = scene_axis_side(shots_with_actors, scene_center)
    solved, issues = [], []
    previous = None
    for shot, actors in shots_with_actors:
        camera = solve_camera(shot, actors, axis_side=side,
                              scene_center=scene_center, world=world)
        if camera is None:
            continue
        shot_no = shot.get("shot_no")
        camera["shot_no"] = shot_no
        if not camera["declared"]["shot_size"]:
            issues.append({
                "shot_no": shot_no, "severity": "warning",
                "field": "shot_size",
                "message": (f"镜{shot_no} 未声明景别,机位按中景兜底;"
                            "景别是机距的唯一依据,请在分镜里写明"),
            })
        if camera["wall_clamped"]:
            issues.append({
                "shot_no": shot_no, "severity": "warning",
                "field": "distance",
                "message": (f"镜{shot_no} 按声明景别应退到 "
                            f"{camera['desired_distance_m']}m,但会穿墙,"
                            f"已贴墙摆位(实际 {camera['distance_m']}m)。"
                            "请改景别或把人物挪离墙面"),
            })
        if previous is not None:
            delta = abs(_wrap180(camera["yaw_deg"] - previous["yaw_deg"]))
            same_size = (camera["declared"]["shot_size"]
                         == previous["declared"]["shot_size"])
            if delta < MIN_MEANINGFUL_AZIMUTH_DELTA_DEG and same_size:
                issues.append({
                    "shot_no": shot_no, "severity": "warning",
                    "field": "coverage",
                    "message": (f"镜{shot_no} 与镜{previous['shot_no']} "
                                f"机位方位仅差 {delta:.0f}°、景别相同,"
                                "剪在一起近似跳帧;请换景别或换机位"),
                })
            camera["azimuth_delta_deg"] = round(delta, 1)
        solved.append(camera)
        previous = camera
    return {"cameras": solved, "issues": issues, "axis_side": side}


# ---------------------------------------------------------------------------
# 运镜求解:把运镜词解成米制的起止机位
#
# 此前运镜只改画布像素(end_x += 150 之类),换算成三维后既没有米制依据,
# 也和 MOVEMENT_GEOMETRY 里写死的几何描述对不上——提示词说"沿视线推近",
# 三维里却可能是斜着平移。现在每个词有确定的几何解与可核验的运动量。

MOVEMENT_KINDS = (
    "急推", "推", "拉", "摇", "移", "跟", "升", "降", "环绕", "手持", "固定")
PUSH_RATIO = 0.62          # 推:机距收到 62%(约紧一档景别)
FAST_PUSH_RATIO = 0.45     # 急推:更狠
PULL_RATIO = 1.55          # 拉:机距放到 155%
PAN_SWEEP_DEG = 25.0       # 摇:机身扫过的角度(机位不动)
ORBIT_SWEEP_DEG = 30.0     # 环绕:绕主体扫过的角度
TRACK_SHIFT_M = 1.2        # 移:横向平移量
CRANE_UP_M = 1.2
CRANE_DOWN_M = 0.9


def declared_movement(shot):
    design = ((shot or {}).get("five_dimensions") or {}).get(
        "camera_design") or {}
    for source in (design.get("movement"), (shot or {}).get("camera")):
        text = _text(source)
        if not text:
            continue
        for token in MOVEMENT_KINDS:      # 急推 先于 推,长词优先
            if token in text:
                return token
    return "固定"


def _polar(subject_xz, distance, yaw_deg, height):
    return {
        "x": round(subject_xz[0] + distance * math.sin(math.radians(yaw_deg)),
                   2),
        "y": round(height, 2),
        "z": round(subject_xz[1] + distance * math.cos(math.radians(yaw_deg)),
                   2),
    }


def _clamp_room(point, world):
    world = world if isinstance(world, dict) else {}
    try:
        half_w = float(world.get("floor_width_m") or 10.0) / 2 - 0.15
        half_d = float(world.get("floor_depth_m") or 7.0) / 2 - 0.15
    except (TypeError, ValueError):
        half_w, half_d = 4.85, 3.35
    clamped = abs(point["x"]) > half_w or abs(point["z"]) > half_d
    point["x"] = round(max(-half_w, min(half_w, point["x"])), 2)
    point["z"] = round(max(-half_d, min(half_d, point["z"])), 2)
    return point, clamped


def solve_camera_motion(camera, shot, *, subject_start, subject_end=None,
                        world=None):
    """按运镜词求解终点机位与终点瞄准点(米制)。

    camera 是 solve_camera 的返回值。返回新增字段:
    end_position_3d / end_target_3d / movement / movement_amount(可核验的
    运动量文字,给视频提示词直接引用)。
    """
    if not camera:
        return camera
    movement = declared_movement(shot)
    start = dict(camera["position_3d"])
    target = dict(camera["target_3d"])
    sx, sz = float(subject_start[0]), float(subject_start[1])
    ex, ez = ((float(subject_end[0]), float(subject_end[1]))
              if subject_end else (sx, sz))
    distance = float(camera["distance_m"]) or 1.0
    yaw = float(camera["yaw_deg"])
    height = float(camera["height_m"])
    side = 1 if int(camera.get("axis_side", 1)) >= 0 else -1
    end_pos, end_target, amount = dict(start), dict(target), ""
    clamped = False

    if movement in ("推", "急推"):
        ratio = FAST_PUSH_RATIO if movement == "急推" else PUSH_RATIO
        new_d = max(0.45, distance * ratio)
        end_pos = _polar((sx, sz), new_d, yaw, height)
        amount = f"沿视线推近约 {distance - new_d:.1f} 米(机距 {distance:.1f}→{new_d:.1f}m)"
    elif movement == "拉":
        new_d = distance * PULL_RATIO
        end_pos = _polar((sx, sz), new_d, yaw, height)
        end_pos, clamped = _clamp_room(end_pos, world)
        actual = math.dist((end_pos["x"], end_pos["z"]), (sx, sz))
        amount = f"沿视线后拉约 {actual - distance:.1f} 米(机距 {distance:.1f}→{actual:.1f}m)"
    elif movement == "摇":
        # 机位不动,只转机身:终点瞄准点绕机位扫过 PAN_SWEEP_DEG
        sweep = PAN_SWEEP_DEG * side
        base = _yaw_between((start["x"], start["z"]), (target["x"], target["z"]))
        reach = math.dist((start["x"], start["z"]), (target["x"], target["z"])) or 1.0
        end_target = {
            "x": round(start["x"] + reach * math.sin(math.radians(base + sweep)), 2),
            "y": target["y"],
            "z": round(start["z"] + reach * math.cos(math.radians(base + sweep)), 2),
        }
        amount = f"机位不动,机身水平摇过约 {abs(sweep):.0f}°"
    elif movement == "环绕":
        sweep = ORBIT_SWEEP_DEG * side
        end_pos = _polar((sx, sz), distance, yaw + sweep, height)
        end_pos, clamped = _clamp_room(end_pos, world)
        amount = f"以主体为圆心绕行约 {abs(sweep):.0f}°(机距保持 {distance:.1f}m)"
    elif movement == "移":
        lateral = math.radians(yaw + 90.0 * side)
        end_pos = {"x": round(start["x"] + TRACK_SHIFT_M * math.sin(lateral), 2),
                   "y": start["y"],
                   "z": round(start["z"] + TRACK_SHIFT_M * math.cos(lateral), 2)}
        end_pos, clamped = _clamp_room(end_pos, world)
        end_target = {"x": round(target["x"] + (end_pos["x"] - start["x"]), 2),
                      "y": target["y"],
                      "z": round(target["z"] + (end_pos["z"] - start["z"]), 2)}
        amount = f"横向平移约 {TRACK_SHIFT_M:.1f} 米(前后景产生视差)"
    elif movement == "跟":
        # 与主体同速同向:相机随主体位移平移,画面里主体位置基本不变
        dx, dz = ex - sx, ez - sz
        end_pos = {"x": round(start["x"] + dx, 2), "y": start["y"],
                   "z": round(start["z"] + dz, 2)}
        end_pos, clamped = _clamp_room(end_pos, world)
        end_target = {"x": round(ex, 2), "y": target["y"], "z": round(ez, 2)}
        moved = math.hypot(dx, dz)
        amount = (f"与主体同速同向跟移约 {moved:.1f} 米"
                  if moved > 0.05 else "主体未位移,跟拍退化为固定机位")
    elif movement in ("升", "降"):
        delta = CRANE_UP_M if movement == "升" else -CRANE_DOWN_M
        new_h = max(MIN_CAMERA_HEIGHT_M,
                    min(MAX_CAMERA_HEIGHT_M, height + delta))
        end_pos = {"x": start["x"], "y": round(new_h, 2), "z": start["z"]}
        amount = f"机位{'升' if delta > 0 else '降'}约 {abs(new_h - height):.1f} 米"
    elif movement == "手持":
        amount = "机位无净位移,只有手持呼吸感的轻微晃动"
    else:
        amount = "机位与焦距全程不变"

    camera.update({
        "movement": movement,
        "end_position_3d": end_pos,
        "end_target_3d": end_target,
        "movement_amount": amount,
        "movement_wall_clamped": clamped,
        "moving": end_pos != start or end_target != target,
    })
    return camera
