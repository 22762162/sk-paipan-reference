"""动态预演的时间维度连贯性校验(确定性计算,零额度成本).

现有物理校验只看每镜的**端点**(起点/终点是否撞家具、能否放下机位)。
本模块把镜头内的运动按时间插值,检查**过程**——这些问题端点全合法,
动起来才暴露,现在要到成片才看得出来:

- 跨镜传送:同场相邻镜,角色上一镜终点与下一镜起点距离过大,剧情又
  没有交代移动——观众看到的就是"瞬移"穿帮;
- 中途碰撞:角色移动路径中途扫过家具盒体(端点合法,路径穿桌而过);
- 交叉相撞:两名角色同一时间窗内路径交汇,间距小于人身;
- 相机穿模:运镜路径中途穿过家具或人物头顶高度以下的实体。

全部输出 warning 级(导演检查项,不阻断生产):previz 播放器逐条可点
跳转,AI 导演审片时引用同一份结果。零副作用叶子模块。
"""

import math

SCHEMA = "aifos.previz-checks/v1"

TELEPORT_THRESHOLD_M = 1.2     # 同场相邻镜的站位跳变容忍
ACTOR_RADIUS_M = 0.22          # 人身半径(路径碰撞判定)
CROSSING_MIN_GAP_M = 0.35      # 两人路径最小安全间距
CAMERA_RADIUS_M = 0.12         # 相机体积半径
SOLID_MIN_HEIGHT_M = 0.4      # 低于此高度的物体(地毯/门槛)不算碰撞体
PATH_SAMPLES = 9               # 每条路径的插值采样数


def _point(value, default_y=0.0):
    if not isinstance(value, dict):
        return None
    try:
        return (float(value.get("x")), float(value.get("y", default_y)),
                float(value.get("z")))
    except (TypeError, ValueError):
        return None


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _dist_xz(a, b):
    return math.hypot(a[0] - b[0], a[2] - b[2])


def _boxes(scene_model):
    """场景模型 → 碰撞盒列表 (cx, cz, half_w, half_d, yaw_rad, height)。"""
    rows = []
    for obj in (scene_model or {}).get("objects") or []:
        if not isinstance(obj, dict):
            continue
        pos = obj.get("position_3d")
        if not isinstance(pos, dict):
            continue
        try:
            cx, cz = float(pos.get("x")), float(pos.get("z"))
            width = float(obj.get("width_m") or 0)
            depth = float(obj.get("depth_m") or obj.get("width_m") or 0)
            height = float(obj.get("height_m") or 0)
            yaw = math.radians(float(obj.get("rotation_y_deg") or 0.0))
        except (TypeError, ValueError):
            continue
        if width <= 0 or depth <= 0 or height < SOLID_MIN_HEIGHT_M:
            continue
        rows.append((cx, cz, width / 2.0, depth / 2.0, yaw,
                     height, str(obj.get("name") or "场景物体")))
    return rows


def _hits_box(point, box, radius):
    """点(带半径)是否落入旋转矩形足迹内。"""
    cx, cz, half_w, half_d, yaw, _height, _name = box
    dx, dz = point[0] - cx, point[2] - cz
    cos_y, sin_y = math.cos(yaw), math.sin(yaw)
    local_x = dx * cos_y - dz * sin_y
    local_z = dx * sin_y + dz * cos_y
    return (abs(local_x) <= half_w + radius
            and abs(local_z) <= half_d + radius)


def _closest_approach_xz(a_start, a_end, b_start, b_end):
    """两名匀速直线移动者的全程最近距离(解析解,不靠采样撞运气)。

    相对位移是 t 的线性函数,距离平方是二次函数——最小值点
    t* = -p·v / v·v,夹到 [0,1] 后取值即全程最近点。"""
    px = a_start[0] - b_start[0]
    pz = a_start[2] - b_start[2]
    vx = (a_end[0] - a_start[0]) - (b_end[0] - b_start[0])
    vz = (a_end[2] - a_start[2]) - (b_end[2] - b_start[2])
    vv = vx * vx + vz * vz
    t = 0.0 if vv <= 1e-9 else max(0.0, min(1.0, -(px * vx + pz * vz) / vv))
    return math.hypot(px + vx * t, pz + vz * t)


def _actor_states(block):
    states = {}
    for actor in (block or {}).get("actors") or []:
        if not isinstance(actor, dict):
            continue
        name = str(actor.get("name") or "").strip()
        start = _point(actor.get("start_3d"))
        end = _point(actor.get("end_3d")) or start
        if name and start:
            states[name] = (start, end)
    return states


def _issue(shot_no, scene_no, kind, detail):
    return {"shot_no": shot_no, "scene_no": scene_no,
            "kind": kind, "severity": "warn", "detail": detail}


def describe_for_repair(report, shot_no):
    """某镜的时间维度问题 → 交给编剧的修复理由(带改法指引)。"""
    items = [item for item in (report or {}).get("issues") or []
             if str(item.get("shot_no")) == str(shot_no)]
    return "；".join(str(item.get("detail") or "") for item in items)


def shots_with_issues(report, limit=6):
    """按问题数降序给出需要修的镜头号(限量,避免一轮改太多)。"""
    counts = {}
    for item in (report or {}).get("issues") or []:
        try:
            counts[int(item.get("shot_no"))] = counts.get(
                int(item.get("shot_no")), 0) + 1
        except (TypeError, ValueError):
            continue
    ranked = sorted(counts.items(), key=lambda row: (-row[1], row[0]))
    return [shot_no for shot_no, _count in ranked[:limit]]


def previz_report(blocking, storyboard=None, scene_models=None):
    """时间维度连贯性报告 {"schema","issues":[...],"shots":N}。

    blocking: 空间调度文档(shot_index + scenes);
    storyboard: 分镜(镜头次序与时长的权威来源,可缺省按镜号排序);
    scene_models: {location: 场景模型} 提供碰撞盒体。
    """
    index = (blocking or {}).get("shot_index") or {}
    scene_of = {}
    location_of = {}
    for scene in (blocking or {}).get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for shot in scene.get("shots") or []:
            key = str(shot.get("shot_no"))
            scene_of[key] = scene.get("scene_no")
            location_of[key] = str(scene.get("location") or "")
    order = []
    for shot in ((storyboard or {}).get("shots") or []):
        try:
            order.append(int(shot.get("shot_no")))
        except (TypeError, ValueError):
            continue
    if not order:
        order = sorted(int(no) for no in index if str(no).isdigit())
    issues = []
    previous = None  # (shot_no, scene_no, actor_states)
    for shot_no in order:
        key = str(shot_no)
        block = index.get(key)
        if not isinstance(block, dict):
            previous = None
            continue
        scene_no = block.get("scene_no", scene_of.get(key))
        states = _actor_states(block)
        boxes = _boxes((scene_models or {}).get(location_of.get(key, "")))

        # 1) 跨镜传送:同场相邻镜共有角色的站位跳变
        if previous and previous[1] == scene_no:
            for name, (start, _end) in states.items():
                prev_state = previous[2].get(name)
                if not prev_state:
                    continue
                gap = _dist_xz(prev_state[1], start)
                if gap > TELEPORT_THRESHOLD_M:
                    issues.append(_issue(
                        shot_no, scene_no, "teleport",
                        f"{name}上一镜(镜{previous[0]})结束在"
                        f"({prev_state[1][0]:.1f},{prev_state[1][2]:.1f}),"
                        f"本镜开始在({start[0]:.1f},{start[2]:.1f}),"
                        f"瞬移{gap:.1f}米;若剧情确有走位,应在上一镜"
                        "写出移动或在本镜开头交代"))

        moving = {
            name: (start, end) for name, (start, end) in states.items()
            if _dist_xz(start, end) > 0.05}

        # 2) 中途碰撞:移动路径扫过家具
        for name, (start, end) in moving.items():
            for step in range(1, PATH_SAMPLES):
                t = step / PATH_SAMPLES
                point = _lerp(start, end, t)
                hit = next((box for box in boxes
                            if _hits_box(point, box, ACTOR_RADIUS_M)), None)
                if hit is not None:
                    issues.append(_issue(
                        shot_no, scene_no, "path_collision",
                        f"{name}的移动路径在{int(t * 100)}%处穿过"
                        f"「{hit[6]}」;起终点都合法,但直线走不过去,"
                        "需要绕行或改起终点"))
                    break

        # 3) 交叉相撞:两人同一时间窗路径交汇
        names = sorted(moving)
        for i, name_a in enumerate(names):
            for name_b in names[i + 1:]:
                a_start, a_end = moving[name_a]
                b_start, b_end = moving[name_b]
                closest = _closest_approach_xz(
                    a_start, a_end, b_start, b_end)
                if closest < CROSSING_MIN_GAP_M:
                    issues.append(_issue(
                        shot_no, scene_no, "crossing",
                        f"{name_a}与{name_b}的移动路径在同一时间窗交汇,"
                        f"最近仅{closest:.2f}米;错开出发时机或改走位"))

        # 4) 相机穿模:运镜路径中途撞实体
        camera = block.get("camera") or {}
        cam_start = _point(camera.get("start_3d"), 1.55)
        cam_end = _point(camera.get("end_3d"), 1.55) or cam_start
        if cam_start and _dist_xz(cam_start, cam_end) > 0.05:
            for step in range(1, PATH_SAMPLES):
                t = step / PATH_SAMPLES
                point = _lerp(cam_start, cam_end, t)
                hit = next(
                    (box for box in boxes
                     if point[1] <= box[5]
                     and _hits_box(point, box, CAMERA_RADIUS_M)), None)
                if hit is not None:
                    issues.append(_issue(
                        shot_no, scene_no, "camera_through",
                        f"运镜路径在{int(t * 100)}%处以{point[1]:.1f}米"
                        f"高度穿过「{hit[6]}」(高{hit[5]:.1f}米);"
                        "抬高机位、绕行或缩短运动距离"))
                    break

        previous = (shot_no, scene_no, states)
    return {"schema": SCHEMA, "issues": issues,
            "shots": len(order), "passed": not issues}
