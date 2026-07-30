"""空间语言:把 3D 空间调度的世界坐标翻译成可核验的画面文字合同。

2026-07-28 盘点发现的浪费:spatial_blocking 已经算出了完整的三维
调度——每个人的起终点世界坐标、身高、姿态、朝向,摄影机的三维位置、
瞄准点、焦段、视场角与朝向角——但这些数字只被渲染成一张 SVG/PNG
示意图上传当参考,一个数都没进提示词。于是模型必须"看懂示意图",
我们还得额外叮嘱它「不要把箭头色块画进成片」。

本模块把同一份三维事实翻译成文字条款:

- 屏幕定位:世界坐标 → 画面左/中/右第几分区、上下高度带
- 深度序与遮挡:按距摄影机远近排序,谁在谁前面、谁遮谁
- 朝向与视线:世界朝向 → "朝向画面左/右",与镜位自洽
- 行动路线:起点→终点的屏幕位移 + 远近变化(用户点名要的精准定位)
- 屏幕方向与轴线:同场跨镜不许左右翻转(180度法则)
- 运镜推导:机位起终点 → 推/拉/移/环绕,喂镜头语言的运镜维度
- 景别交叉校验:距离+焦段推算实际取景,与声明景别对不上就早报

文字与图各司其职:图给模型看整体关系,文字给模型和质检共用同一份
可逐条核验的判据。零依赖叶子模块。
"""

import math

# 画面横向分区(按归一化横坐标 -1..1)
_H_ZONES = (
    (-1.01, -0.6, "画面最左侧边缘"),
    (-0.6, -0.2, "画面左三分之一"),
    (-0.2, 0.2, "画面中央"),
    (0.2, 0.6, "画面右三分之一"),
    (0.6, 1.01, "画面最右侧边缘"),
)

# 距摄影机距离 → 该距离下的自然取景档(用于与声明景别交叉校验)
_DISTANCE_FRAMING = (
    (0.0, 1.2, "特写"),
    (1.2, 2.2, "近景"),
    (2.2, 3.5, "中景"),
    (3.5, 6.0, "全景"),
    (6.0, float("inf"), "远景"),
)


def _point(value):
    if not isinstance(value, dict):
        return None
    try:
        return (float(value.get("x", 0.0)), float(value.get("y", 0.0)),
                float(value.get("z", 0.0)))
    except (TypeError, ValueError):
        return None


def _distance(a, b):
    return math.dist(a, b) if a and b else None


def screen_zone(offset):
    """归一化横向偏移 → 画面分区名。"""
    for low, high, label in _H_ZONES:
        if low <= offset < high:
            return label
    return "画面中央"


def framing_for_distance(distance):
    """距摄影机多远 → 该距离的自然取景档。"""
    if distance is None:
        return ""
    for low, high, label in _DISTANCE_FRAMING:
        if low <= distance < high:
            return label
    return ""


# 被摄距离按"到演员胸眼高度"计,而不是到脚点:脚点会把机位高差
# (默认 1.55 米)整个算进距离,1 米的特写被报成 1.84 米,与按景别
# 摆位的机位对不上。摄影上说的被摄距离本就是到主体上半身。
SUBJECT_EYE_RATIO = 0.85
DEFAULT_SUBJECT_HEIGHT_M = 1.68


def subject_point(actor_point, height_m=None):
    """把演员的地面点抬到胸眼高度,用于距离与投影计算。"""
    if not isinstance(actor_point, dict):
        return actor_point
    try:
        height = float(height_m or DEFAULT_SUBJECT_HEIGHT_M)
    except (TypeError, ValueError):
        height = DEFAULT_SUBJECT_HEIGHT_M
    return {**actor_point, "y": round(height * SUBJECT_EYE_RATIO, 2)}


def project(camera_point, target_point, actor_point, fov_degrees=54.4):
    """把演员世界坐标投影到画面横向偏移(-1..1)与距离。

    以摄影机→瞄准点为光轴,计算演员相对光轴的水平夹角,再按半视场角
    归一化:±1 即画面左右边缘。返回 (offset, distance);数据不足为
    (None, None),调用方按"该项不可推导"处理,绝不猜。
    """
    cam = _point(camera_point)
    aim = _point(target_point)
    actor = _point(actor_point)
    if not (cam and aim and actor):
        return None, None
    axis = (aim[0] - cam[0], aim[2] - cam[2])
    to_actor = (actor[0] - cam[0], actor[2] - cam[2])
    axis_len = math.hypot(*axis)
    actor_len = math.hypot(*to_actor)
    distance = math.dist(cam, actor)
    if axis_len < 1e-6 or actor_len < 1e-6:
        return None, distance
    # 有符号夹角:正=光轴右侧(画面右),负=左侧
    cross = axis[0] * to_actor[1] - axis[1] * to_actor[0]
    dot = axis[0] * to_actor[0] + axis[1] * to_actor[1]
    angle = math.degrees(math.atan2(cross, dot))
    try:
        half_fov = max(5.0, float(fov_degrees) / 2.0)
    except (TypeError, ValueError):
        half_fov = 27.2
    # 屏幕右为正。已按两种机位朝向验证:机位在 +z 朝原点(视线朝 -z)时
    # 摄影机右手方向是世界 +x,世界 -x 的人物落在画面左;机位在 -z 时
    # 两者同时翻转,符号自洽,不需要额外取负。
    offset = max(-1.2, min(1.2, angle / half_fov))
    return offset, distance


def _facing_screen_direction(facing_text, offset_self, offset_target):
    """朝向的屏幕表述:优先用"看向谁"推屏幕方向,退回原文。"""
    if offset_self is not None and offset_target is not None:
        if offset_target > offset_self + 0.05:
            return "朝向画面右侧"
        if offset_target < offset_self - 0.05:
            return "朝向画面左侧"
        return "正朝摄影机方向"
    text = str(facing_text or "").strip()
    return text or ""


def _actor_line(actor, camera, phase):
    """单个演员在某相位的屏幕定位句(含分区、距离档、高度带)。"""
    cam_point = camera.get(f"{phase}_3d") or camera.get("start_3d")
    aim = camera.get(f"target_{phase}_3d") or camera.get("target_3d")
    fov = camera.get("fov_degrees")
    actor_point = subject_point(
        actor.get(f"{phase}_3d") or actor.get("start_3d"),
        actor.get("height_m"))
    offset, distance = project(cam_point, aim, actor_point, fov)
    parts = [str(actor.get("name") or actor.get("actor_id") or "角色")]
    if offset is not None:
        parts.append(screen_zone(offset))
    if distance is not None:
        framing = framing_for_distance(distance)
        parts.append(
            f"距摄影机约{distance:.1f}米"
            + (f"(该距离自然落在{framing}带)" if framing else ""))
    pose = str(actor.get(f"pose_label_{phase}")
               or actor.get("pose_label_start") or "").strip()
    if pose:
        parts.append(pose)
    support = str(actor.get(f"support_{phase}")
                  or actor.get("support_start") or "").strip()
    if support:
        parts.append(f"支撑={support}")
    return "、".join(parts), offset, distance


def staging_clause(block, *, phase="start"):
    """【空间站位】条款:屏幕定位 + 深度遮挡序(可逐条核验)。"""
    block = block if isinstance(block, dict) else {}
    camera = block.get("camera") or {}
    actors = [a for a in (block.get("actors") or []) if isinstance(a, dict)]
    if not actors:
        return ""
    rows = []
    for actor in actors:
        line, offset, distance = _actor_line(actor, camera, phase)
        rows.append({"actor": actor, "line": line,
                     "offset": offset, "distance": distance})
    parts = ["；".join(row["line"] for row in rows)]
    ranked = [r for r in rows if r["distance"] is not None]
    if len(ranked) > 1:
        ranked.sort(key=lambda row: row["distance"])
        names = [str(r["actor"].get("name") or "") for r in ranked]
        parts.append("由近及远的遮挡顺序:" + "→".join(names)
                     + ";前者与后者重叠处必须由前者遮挡后者,"
                     "禁止把远处人物画在近处人物之前")
    # 朝向与视线(用屏幕方向表述,和镜位自洽)
    by_name = {str(r["actor"].get("name") or ""): r for r in rows}
    facing_parts = []
    for row in rows:
        actor = row["actor"]
        # 与同函数内 pose_label_{phase} / support_{phase} 取法一致:
        # 朝向必须跟着相位走,否则首帧会拿到尾帧视线。
        # `or actor.get("facing")` 保证存量 blocking 文档(只有 facing)
        # 渲染结果与改动前完全相同。
        facing_text = str(actor.get(f"facing_{phase}")
                          or actor.get("facing") or "")
        target_offset = None
        for other_name, other in by_name.items():
            if other_name and other_name != str(actor.get("name") or "") \
                    and other_name in facing_text:
                target_offset = other["offset"]
                break
        direction = _facing_screen_direction(
            facing_text, row["offset"], target_offset)
        if direction:
            name = str(actor.get("name") or "")
            note = f"{name}{direction}"
            if target_offset is not None:
                note += "(视线落在对方身上,双方视线高度对齐)"
            facing_parts.append(note)
    if facing_parts:
        parts.append("朝向与视线:" + "；".join(facing_parts))
    return "；".join(parts)


def motion_clause(block):
    """【行动路线】条款:起点→终点的屏幕位移与远近变化。

    用户点名要"人物精准定位和行动路线":光说"向右走"没有约束力,
    要说清从画面哪个分区走到哪个分区、距摄影机是走近还是走远。
    """
    block = block if isinstance(block, dict) else {}
    camera = block.get("camera") or {}
    moving = []
    for actor in (block.get("actors") or []):
        if not isinstance(actor, dict) or not actor.get("moving"):
            continue
        _line, start_offset, start_distance = _actor_line(
            actor, camera, "start")
        _line2, end_offset, end_distance = _actor_line(actor, camera, "end")
        name = str(actor.get("name") or actor.get("actor_id") or "角色")
        segs = []
        if start_offset is not None and end_offset is not None:
            segs.append(f"自{screen_zone(start_offset)}"
                        f"移动到{screen_zone(end_offset)}")
        if start_distance is not None and end_distance is not None:
            delta = end_distance - start_distance
            if abs(delta) >= 0.3:
                segs.append(
                    ("走近摄影机" if delta < 0 else "远离摄影机")
                    + f"约{abs(delta):.1f}米"
                    + f"(由{start_distance:.1f}米到{end_distance:.1f}米)")
            else:
                segs.append("与摄影机距离基本不变")
        pose_start = str(actor.get("pose_label_start") or "").strip()
        pose_end = str(actor.get("pose_label_end") or "").strip()
        if pose_start and pose_end and pose_start != pose_end:
            segs.append(f"姿态由{pose_start}变为{pose_end}")
        if segs:
            moving.append(f"{name}:" + "、".join(segs))
    # 运镜的米制运动量:导演调度器解出来的,与三维起止机位同源。
    # 此前提示词只有「推」「摇」这类词,幅度全靠模型自己想——同一个
    # 「推」可能推半米也可能推三米,和空间调度对不上。
    director = camera.get("director_camera") or {}
    amount = str(director.get("movement_amount") or "").strip()
    movement_line = ""
    if amount and str(director.get("movement") or "") not in ("", "固定"):
        movement_line = f"摄影机运镜:{amount}"
    if not moving:
        return (movement_line + "；本镜人物不位移,只有摄影机在动"
                if movement_line else "")
    body = "；".join(moving)
    if movement_line:
        body = f"{movement_line}；{body}"
    return (body
            + "；静态关键帧只定格所属相位的位置,不表现移动过程;"
            "视频单元中该位移必须连续且脚步与地面接触真实")


def screen_direction_clause(block, *, phase="start"):
    """【屏幕方向】条款:轴线一致性(180度法则),治跨镜左右翻转。

    必须与【空间站位】用同一相位,否则会出现"站位说都在中央、方向说
    一左一右"的自相矛盾(实测 ep24 镜头1)。左右分区相同时不下发——
    锁一个不存在的左右关系只会制造互斥合同。
    """
    block = block if isinstance(block, dict) else {}
    axis = block.get("axis") or {}
    camera = block.get("camera") or {}
    actors = [a for a in (block.get("actors") or []) if isinstance(a, dict)]
    if len(actors) < 2:
        return ""
    ordered = []
    for actor in actors:
        _line, offset, _distance = _actor_line(actor, camera, phase)
        if offset is not None:
            ordered.append((offset, str(actor.get("name") or "")))
    if len(ordered) < 2:
        return ""
    ordered.sort()
    # 分区名不同还不够:相距几厘米却恰好跨了分桶边界的两个人,锁成
    # "一左一右"就是凭空造互斥合同。要求真实横向间距达到半幅的四分
    # 之一以上,才认为左右关系是可执行、可核验的事实。
    if (screen_zone(ordered[0][0]) == screen_zone(ordered[-1][0])
            or (ordered[-1][0] - ordered[0][0]) < 0.25):
        return ""
    left, right = ordered[0][1], ordered[-1][1]
    clause = (
        f"本镜屏幕左右关系锁定:{left}在画面左侧、{right}在画面右侧;"
        "同一场内所有镜头必须保持这一左右关系(180度轴线法则),"
        "反打镜头只允许越过同一侧机位,不得让两人左右互换")
    axis_note = str(axis.get("label") or axis.get("note") or "").strip()
    if axis_note:
        clause += f";轴线依据:{axis_note}"
    return clause


def derive_movement_term(camera):
    """由机位起终点三维坐标推导运镜术语(喂镜头语言的运镜维度)。"""
    camera = camera if isinstance(camera, dict) else {}
    declared = str(camera.get("movement") or "").strip()
    start = _point(camera.get("start_3d"))
    end = _point(camera.get("end_3d"))
    aim_start = _point(camera.get("target_start_3d")
                       or camera.get("target_3d"))
    if not (start and end):
        return declared or "固定"
    if math.dist(start, end) < 0.15:
        return "固定"
    # 垂直位移先判:纯升降也会改变到瞄准点的距离,先判推拉会把
    # "机位升高"误读成"拉"(实测)。
    vertical = abs(end[1] - start[1])
    horizontal = math.hypot(end[0] - start[0], end[2] - start[2])
    if vertical > 0.4 and vertical >= horizontal:
        return "升降"
    if aim_start:
        before = math.dist(start, aim_start)
        after = math.dist(end, aim_start)
        if before - after > 0.4:
            return "推"
        if after - before > 0.4:
            return "拉"
    return "移"


def framing_conflict(block, declared_scale, *, phase="start"):
    """距离+焦段推算的取景档与声明景别是否矛盾(出图前抓)。

    只在明显跨档时报(相邻档不报):特写声明配 6 米机位这种才是真错。
    """
    block = block if isinstance(block, dict) else {}
    scale = str(declared_scale or "").strip()
    if not scale:
        return ""
    camera = block.get("camera") or {}
    actors = [a for a in (block.get("actors") or []) if isinstance(a, dict)]
    if not actors:
        return ""
    # 必须与空间站位同相位:人物在镜头内移动时,拿 start 位置去核对
    # 定格在 end 的画面,会报出根本不存在的矛盾(实测 ep24 镜头5)。
    cam = _point(camera.get(f"{phase}_3d") or camera.get("start_3d"))
    nearest = None
    for actor in actors:
        point = _point(subject_point(
            actor.get(f"{phase}_3d") or actor.get("start_3d"),
            actor.get("height_m")))
        distance = _distance(cam, point)
        if distance is not None and (nearest is None or distance < nearest):
            nearest = distance
    natural = framing_for_distance(nearest)
    if not natural or natural == scale:
        return ""
    order = ["特写", "近景", "中景", "全景", "远景"]
    aliases = {"大特写": "特写", "中近景": "近景", "膝上景": "中景",
               "七分身": "中景", "大远景": "远景"}
    declared = aliases.get(scale, scale)
    if declared not in order or natural not in order:
        return ""
    gap = abs(order.index(declared) - order.index(natural))
    if gap < 2:
        return ""
    return (f"景别与空间调度不一致:合同声明{scale},但最近人物距摄影机"
            f"约{nearest:.1f}米,该距离自然落在{natural};"
            "请调整机位距离或改写景别,否则成片必然对不上")


def spatial_lines(block, *, phase="start", declared_scale=""):
    """返回本镜应进提示词的空间条款(按需给出,不可推导时留空)。"""
    lines = []
    staging = staging_clause(block, phase=phase)
    if staging:
        lines.append(("空间站位", staging))
    motion = motion_clause(block)
    if motion:
        lines.append(("行动路线", motion))
    direction = screen_direction_clause(block, phase=phase)
    if direction:
        lines.append(("屏幕方向", direction))
    conflict = framing_conflict(block, declared_scale, phase=phase)
    if conflict:
        lines.append(("空间冲突", conflict))
    return lines
