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

# scene_model 现有类别中只有 furniture/prop 作为家具碰撞体。
# decor/opening/light 是外观或开口参考，不能因为默认高度较大就阻挡路线。
_SOLID_CATEGORIES = frozenset({"furniture", "prop", "structure", "structural"})
_NON_SOLID_NAMES = (
    "地毯", "地垫", "窗帘", "纱帘", "门帘", "帘幕", "帷幔", "纱帐",
    "挂毯", "床单", "床罩", "被褥", "抱枕", "靠垫", "软垫", "桌布",
)


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


def _dist_3d(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _explicit_bool(value):
    """只解析明确的布尔值；None 表示 schema 未声明。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1", "on"}:
            return True
        if text in {"false", "no", "0", "off"}:
            return False
    return None


def _solid_obstacle(obj):
    """scene_model 物体是否可作为路线碰撞体。

    显式 ``blocking`` 是最高优先级，与可编辑调度图 schema 一致；
    未声明时再沿用 scene_model 的 category 语义。对无 category 的
    存量文档保持兼容，但排除明确可踩踏/可穿过的软饰名称。
    """
    declared = _explicit_bool(obj.get("blocking"))
    if declared is not None:
        return declared
    category = str(obj.get("category") or "").strip().lower()
    if category and category not in _SOLID_CATEGORIES:
        return False
    name = str(obj.get("name") or "")
    if any(token in name for token in _NON_SOLID_NAMES):
        return False
    return True


def _boxes(scene_model):
    """场景模型 → 碰撞盒列表 (cx, cz, half_w, half_d, yaw_rad, height)。"""
    rows = []
    for obj in (scene_model or {}).get("objects") or []:
        if not isinstance(obj, dict):
            continue
        if not _solid_obstacle(obj):
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
        explicitly_blocking = _explicit_bool(obj.get("blocking")) is True
        if (width <= 0 or depth <= 0
                or (height < SOLID_MIN_HEIGHT_M and not explicitly_blocking)):
            continue
        rows.append((cx, cz, width / 2.0, depth / 2.0, yaw,
                     height, str(obj.get("name") or "场景物体")))
    return rows


def _segment_box_interval_xz(start, end, box, radius):
    """线段与旋转盒扩张足迹的参数交集 [enter, exit]。

    比每段固定采样更稳定：窄桌腿/薄隔断也不会从采样点之间漏过。
    """
    cx, cz, half_w, half_d, yaw, _height, _name = box
    cos_y, sin_y = math.cos(yaw), math.sin(yaw)

    def local(point):
        dx, dz = point[0] - cx, point[2] - cz
        return (dx * cos_y - dz * sin_y,
                dx * sin_y + dz * cos_y)

    start_xz, end_xz = local(start), local(end)
    lower = (-half_w - radius, -half_d - radius)
    upper = (half_w + radius, half_d + radius)
    enter, exit_ = 0.0, 1.0
    for axis in range(2):
        origin = start_xz[axis]
        delta = end_xz[axis] - origin
        if abs(delta) <= 1e-12:
            if origin < lower[axis] or origin > upper[axis]:
                return None
            continue
        first = (lower[axis] - origin) / delta
        second = (upper[axis] - origin) / delta
        if first > second:
            first, second = second, first
        enter, exit_ = max(enter, first), min(exit_, second)
        if enter > exit_:
            return None
    return (max(0.0, enter), min(1.0, exit_))


def _route_points(entity, default_y=0.0):
    """route_3d 优先，存量数据回退 start_3d→end_3d。"""
    route = [point for point in (
        _point(value, default_y) for value in (entity or {}).get("route_3d") or [])
             if point is not None]
    start = _point((entity or {}).get("start_3d"), default_y)
    end = _point((entity or {}).get("end_3d"), default_y) or start
    if len(route) >= 2:
        return route
    if start is not None:
        if end is not None and _dist_3d(start, end) > 1e-9:
            return [start, end]
        return [start]
    return route


def _route_length(route, distance=_dist_xz):
    return sum(distance(left, right) for left, right in zip(route, route[1:]))


def _timed_route(route):
    """把无时标 route_3d 按路程归一化，等速参与交叉检查。"""
    if not route:
        return []
    if len(route) == 1:
        return [(0.0, route[0]), (1.0, route[0])]
    lengths = [_dist_xz(left, right)
               for left, right in zip(route, route[1:])]
    total = sum(lengths)
    if total <= 1e-9:
        return [(index / (len(route) - 1), point)
                for index, point in enumerate(route)]
    elapsed = 0.0
    result = [(0.0, route[0])]
    for length, point in zip(lengths, route[1:]):
        elapsed += length
        result.append((min(1.0, elapsed / total), point))
    result[-1] = (1.0, result[-1][1])
    return result


def _point_at(timed_route, t):
    if not timed_route:
        return None
    for (left_t, left), (right_t, right) in zip(
            timed_route, timed_route[1:]):
        if t <= right_t + 1e-9:
            span = right_t - left_t
            ratio = 0.0 if span <= 1e-9 else (t - left_t) / span
            return _lerp(left, right, max(0.0, min(1.0, ratio)))
    return timed_route[-1][1]


def _closest_routes_xz(route_a, route_b):
    """按同一归一化时间轴检查两条折线的最近距离。"""
    timed_a, timed_b = _timed_route(route_a), _timed_route(route_b)
    if not timed_a or not timed_b:
        return math.inf
    breakpoints = sorted({t for t, _point_value in (*timed_a, *timed_b)})
    closest = math.inf
    for start_t, end_t in zip(breakpoints, breakpoints[1:]):
        a_start, a_end = _point_at(timed_a, start_t), _point_at(timed_a, end_t)
        b_start, b_end = _point_at(timed_b, start_t), _point_at(timed_b, end_t)
        closest = min(closest, _closest_approach_xz(
            a_start, a_end, b_start, b_end))
    return closest


def _route_box_hit(route, box, radius, *, camera=False):
    """返回折线首个穿盒点；全路线首末点留给端点校验。"""
    if len(route) < 2:
        return None
    segment_lengths = [_dist_3d(left, right) if camera else _dist_xz(left, right)
                       for left, right in zip(route, route[1:])]
    total = sum(segment_lengths)
    elapsed = 0.0
    last_segment = len(route) - 2
    for index, (start, end) in enumerate(zip(route, route[1:])):
        interval = _segment_box_interval_xz(start, end, box, radius)
        if interval is None:
            elapsed += segment_lengths[index]
            continue
        enter, exit_ = interval
        # 旧逻辑不复审全路线的首末站位，但中间转角必须审。
        if index == 0:
            enter = max(enter, 1e-7)
        if index == last_segment:
            exit_ = min(exit_, 1.0 - 1e-7)
        if enter > exit_:
            elapsed += segment_lengths[index]
            continue
        hit_t = enter
        if camera:
            height = box[5]
            y_enter = start[1] + (end[1] - start[1]) * enter
            y_exit = start[1] + (end[1] - start[1]) * exit_
            if min(y_enter, y_exit) > height:
                elapsed += segment_lengths[index]
                continue
            if y_enter > height and abs(end[1] - start[1]) > 1e-12:
                hit_t = max(enter, min(exit_,
                    (height - start[1]) / (end[1] - start[1])))
        point = _lerp(start, end, hit_t)
        distance_at_hit = elapsed + segment_lengths[index] * hit_t
        percent = 0 if total <= 1e-9 else int(round(
            100 * distance_at_hit / total))
        return {"point": point, "percent": percent,
                "segment": index + 1, "segments": len(route) - 1}
    return None


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
        route = _route_points(actor)
        if name and route:
            states[name] = {"start": route[0], "end": route[-1],
                            "route": route}
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
            for name, state in states.items():
                prev_state = previous[2].get(name)
                if not prev_state:
                    continue
                start = state["start"]
                previous_end = prev_state["end"]
                gap = _dist_xz(previous_end, start)
                if gap > TELEPORT_THRESHOLD_M:
                    issues.append(_issue(
                        shot_no, scene_no, "teleport",
                        f"{name}上一镜(镜{previous[0]})结束在"
                        f"({previous_end[0]:.1f},{previous_end[2]:.1f}),"
                        f"本镜开始在({start[0]:.1f},{start[2]:.1f}),"
                        f"瞬移{gap:.1f}米;若剧情确有走位,应在上一镜"
                        "写出移动或在本镜开头交代"))

        moving = {
            name: state for name, state in states.items()
            if _route_length(state["route"]) > 0.05}

        # 2) 中途碰撞:按 route_3d 的每个折线段扫过家具。
        # 禁止再用 start→end 弦线代替真实绕行路线。
        for name, state in moving.items():
            route = state["route"]
            for box in boxes:
                hit = _route_box_hit(route, box, ACTOR_RADIUS_M)
                if hit:
                    issues.append(_issue(
                        shot_no, scene_no, "path_collision",
                        f"{name}的 route_3d 第{hit['segment']}/"
                        f"{hit['segments']}段在全程{hit['percent']}%处穿过"
                        f"「{box[6]}」;起终点都合法,但该分段走不过去,"
                        "需要绕行或改起终点"))
                    break

        # 3) 交叉相撞:两人同一时间窗的分段路线交汇。
        # 无显式时标时按各自路程归一化,视为全镜等速。
        names = sorted(moving)
        for i, name_a in enumerate(names):
            for name_b in names[i + 1:]:
                closest = _closest_routes_xz(
                    moving[name_a]["route"], moving[name_b]["route"])
                if closest < CROSSING_MIN_GAP_M:
                    issues.append(_issue(
                        shot_no, scene_no, "crossing",
                        f"{name_a}与{name_b}的 route_3d 分段在同一时间窗交汇,"
                        f"最近仅{closest:.2f}米;错开出发时机或改走位"))

        # 4) 相机穿模:运镜 route_3d 每段中途撞实体
        camera = block.get("camera") or {}
        camera_route = _route_points(camera, 1.55)
        if _route_length(camera_route, _dist_3d) > 0.05:
            for box in boxes:
                hit = _route_box_hit(
                    camera_route, box, CAMERA_RADIUS_M, camera=True)
                if hit:
                    point = hit["point"]
                    issues.append(_issue(
                        shot_no, scene_no, "camera_through",
                        f"运镜 route_3d 第{hit['segment']}/"
                        f"{hit['segments']}段在全程{hit['percent']}%处以"
                        f"{point[1]:.1f}米高度穿过「{box[6]}」"
                        f"(高{box[5]:.1f}米);"
                        "抬高机位、绕行或缩短运动距离"))
                    break

        previous = (shot_no, scene_no, states)
    return {"schema": SCHEMA, "issues": issues,
            "shots": len(order), "passed": not issues}
