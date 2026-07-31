"""分镜可拍性预检:分镜出厂即"可拍",下游只做翻译,不再回头核对。

用户指出的病理(2026-07-28):"前后顺序如果不对,很容易出现前后命令
要求不一样,然后又要核对"。查证后确认——顺序本身是对的(空间服从
分镜、提示词翻译上游),真正的病是**上游发明了下游做不到的事,而
上游自己不知道**,一路拖到出图前才爆:

- 分镜写"85mm特写 + 三人分布在三个空间区域" → 几何不可能 → 熔断
- 分镜声明"特写",空间调度把机位摆在 5 米外 → 成片必然对不上
- 道具只写了 start/end 没写 freeze → 静态图合同校验拒绝生图
- 同场相邻镜头人物左右关系翻转(越轴) → 观众看不懂,质检也判不了

这些在分镜+空间阶段就能算出来,而空间调度是确定性计算、零额度成本。
本模块把这些校验前移到那里,不合格当场交编剧就地修——从"下游发现
再回头改"变成"上游根本不会写出来"。

零副作用叶子模块:只读入,只返回问题清单,不改任何文档。
"""

from .camera_language import scale_capacity
from .spatial_language import framing_conflict, screen_zone, _actor_line

SCHEMA = "aifos.storyboard-preflight/v1"

# 可拍性问题类型 → 谁能修
KIND_FIXERS = {
    "scale_capacity": "camera",
    "framing_distance": "camera",
    "prop_phase": "props",
    "axis_flip": "camera",
    "duration_short": "timing",
    "duration_long": "timing",
}

# Seedance 全家族时长硬下限 4 秒;2.0 世代上限 15 秒。低于下限的镜头
# 拖到视频提交层才会被拒收(白等一轮),超上限则被禁止静默截短——两者
# 都是分镜层就能算出来的废镜,必须在这里暴露。2.5 按镜升级(声明
# video_model_tier=seedance2_5)的镜头上限另由 seedance_policy 运行时
# 核验,预检只放行其 16-30 秒声明,不代替运行时闸门。
DURATION_MIN_SECONDS = 4.0
DURATION_MAX_BASELINE = 15.0
DURATION_MAX_UPGRADE = 30.0


def _scale_of(shot):
    camera = shot.get("camera")
    if isinstance(camera, dict):
        return str(camera.get("景别") or "").strip()
    text = str(camera or "")
    for token in ("大特写", "特写", "中近景", "近景", "膝上景", "七分身",
                  "中景", "大远景", "全景", "远景"):
        if token in text:
            return token
    return ""


def _visible_count(shot):
    try:
        declared = int(shot.get("visible_figure_count"))
        if declared > 0:
            return declared
    except (TypeError, ValueError):
        pass
    count = len(shot.get("characters") or [])
    for figure in (shot.get("functional_figures") or []):
        if isinstance(figure, dict):
            try:
                count += max(0, int(figure.get("count") or 0))
            except (TypeError, ValueError):
                continue
    return count


def _issue(shot, kind, detail, suggestion=""):
    return {
        "shot_no": shot.get("shot_no"),
        "scene_no": shot.get("scene_no"),
        "kind": kind,
        "fixer": KIND_FIXERS.get(kind, "camera"),
        "detail": detail,
        "suggestion": suggestion,
    }


def _check_capacity(shot):
    """景别容量装不下本镜宣称必须全部可见的人数。"""
    scale = _scale_of(shot)
    count = _visible_count(shot)
    if not scale or count <= 0:
        return None
    capacity = scale_capacity(scale)
    if capacity >= count:
        return None
    return _issue(
        shot, "scale_capacity",
        f"景别{scale}最多完整容纳{capacity}人,本镜要求{count}人全部可见",
        f"把景别放宽到能容纳{count}人的档位(中景/全景/远景),"
        "或改写为只框入部分人物、其余明确出画")


def _check_prop_phases(shot):
    """静态关键帧要求 transition 道具有 freeze 定格行(或当前相位行)。"""
    rows = shot.get("frame_props") or []
    transitions = shot.get("prop_transitions") or []
    if not transitions:
        return None
    declared = {
        (str(row.get("prop_id") or ""), str(row.get("phase") or "").lower())
        for row in rows if isinstance(row, dict)}
    missing = []
    for item in transitions:
        if not isinstance(item, dict):
            continue
        prop_id = str(item.get("prop_id") or "")
        if not prop_id:
            continue
        if not any((prop_id, phase) in declared
                   for phase in ("freeze", "start", "end")):
            missing.append(prop_id)
    if not missing:
        return None
    return _issue(
        shot, "prop_phase",
        "以下道具只写了状态变化没有定格记录: " + "、".join(missing[:5]),
        "为每件道具补 phase=freeze(或 start/end)的 frame_props 定格行;"
        "本镜内无变化就克隆同一状态")


def _shot_upgrade_tier(shot):
    tier = str(shot.get("video_model_tier") or "").strip().lower()
    return tier in ("seedance2_5", "seedance2.5")


def _check_duration(shot):
    """时长越界在分镜层就是废镜:短于4秒提交必拒,超上限禁止静默截短。"""
    raw = shot.get("duration")
    if raw is None:
        return None  # 缺失由分镜结构校验负责,预检不重复报
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        return None
    if duration < DURATION_MIN_SECONDS:
        return _issue(
            shot, "duration_short",
            f"本镜声明{duration:g}秒,低于 Seedance 硬下限4秒,"
            "视频提交必被拒收",
            "用反应拍/呼吸感/情绪留白把表演拉满到4秒以上,"
            "或与相邻镜头合并叙事")
    ceiling = (DURATION_MAX_UPGRADE if _shot_upgrade_tier(shot)
               else DURATION_MAX_BASELINE)
    if duration > ceiling:
        label = ("Seedance 2.5 升级档上限30秒" if _shot_upgrade_tier(shot)
                 else "Seedance 2.0 上限15秒")
        return _issue(
            shot, "duration_long",
            f"本镜声明{duration:g}秒,超过{label},提交层禁止静默截短",
            "拆分为多镜;确属不可分割长镜头且在16-30秒内,"
            "按镜声明 video_model_tier=seedance2_5 并写明升级理由")
    return None


def _screen_order(block, phase="start"):
    """本镜人物的画面左右次序(按投影偏移升序)。"""
    if not isinstance(block, dict):
        return []
    camera = block.get("camera") or {}
    ordered = []
    for actor in (block.get("actors") or []):
        if not isinstance(actor, dict):
            continue
        _line, offset, _distance = _actor_line(actor, camera, phase)
        if offset is None:
            continue
        ordered.append((offset, str(actor.get("name") or "")))
    ordered.sort()
    return ordered


def _check_axis(shot, block, previous):
    """同场相邻镜头的人物左右关系不得翻转(180度轴线法则)。

    只在两镜共有同一对人物、且两镜的左右分区都成立时判定——同区、
    单人、换人都不算越轴,避免把正常剪辑误判成错误。
    """
    if not previous:
        return None
    prev_shot, prev_block = previous
    if prev_shot.get("scene_no") != shot.get("scene_no"):
        return None
    current = _screen_order(block)
    earlier = _screen_order(prev_block)
    if len(current) < 2 or len(earlier) < 2:
        return None
    shared = {name for _o, name in current} & {name for _o, name in earlier}
    if len(shared) < 2:
        return None
    def side_map(rows):
        rows = [(offset, name) for offset, name in rows if name in shared]
        if len(rows) < 2 or screen_zone(rows[0][0]) == screen_zone(
                rows[-1][0]):
            return None
        return rows[0][1], rows[-1][1]
    now, before = side_map(current), side_map(earlier)
    if not now or not before:
        return None
    if now == before:
        return None
    return _issue(
        shot, "axis_flip",
        f"越轴:上一镜{before[0]}在画面左、{before[1]}在右,"
        f"本镜变成{now[0]}在左、{now[1]}在右",
        f"把机位移回轴线同一侧,保持{before[0]}在画面左、{before[1]}在右;"
        "确需跨轴请先安排一个中性过渡镜")


def preflight_storyboard(script, storyboard, blocking=None):
    """返回本集分镜的可拍性问题清单(不改任何文档)。

    {"schema","passed","issues":[...],"by_shot":{shot_no:[...]}}
    """
    shots = [s for s in (storyboard or {}).get("shots") or []
             if isinstance(s, dict)]
    index = ((blocking or {}).get("shot_index") or {})
    issues = []
    previous = None
    for shot in shots:
        block = index.get(str(shot.get("shot_no"))) or {}
        for issue in (_check_capacity(shot), _check_prop_phases(shot),
                      _check_duration(shot)):
            if issue:
                issues.append(issue)
        if block:
            conflict = framing_conflict(block, _scale_of(shot))
            if conflict:
                issues.append(_issue(
                    shot, "framing_distance", conflict,
                    "调整机位距离或改写景别,使两者落在同一取景档"))
            axis = _check_axis(shot, block, previous)
            if axis:
                issues.append(axis)
            previous = (shot, block)
    by_shot = {}
    for issue in issues:
        by_shot.setdefault(str(issue["shot_no"]), []).append(issue)
    return {
        "schema": SCHEMA,
        "passed": not issues,
        "shots": len(shots),
        "issues": issues,
        "by_shot": by_shot,
    }


def repairable_shots(report, limit=8):
    """需要交编剧就地修的镜头号(按问题数降序,限量避免一次改太多)。"""
    counted = sorted(
        ((int(shot_no), len(items))
         for shot_no, items in (report.get("by_shot") or {}).items()
         if str(shot_no).isdigit()),
        key=lambda row: (-row[1], row[0]))
    return [shot_no for shot_no, _count in counted[:limit]]


def describe_issues(report, shot_no):
    """某镜的问题清单 → 交给编剧的一段人话。"""
    items = (report.get("by_shot") or {}).get(str(shot_no)) or []
    return "；".join(
        f"{item['detail']}(建议:{item['suggestion']})" if item.get("suggestion")
        else item["detail"] for item in items)
