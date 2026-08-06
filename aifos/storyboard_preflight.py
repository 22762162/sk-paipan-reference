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

import re

from .camera_language import (allows_partial_multi_subject_scale,
                              scale_capacity)
from .high_value_events import audit_high_value_event_coverage
from .spatial_language import framing_conflict, screen_zone, _actor_line
from .spatial_blocking import director_camera_issues

SCHEMA = "aifos.storyboard-preflight/v1"

# 可拍性问题类型 → 谁能修
KIND_FIXERS = {
    "scale_capacity": "camera",
    "framing_distance": "camera",
    "camera_wall_clamped": "camera",
    "camera_movement_wall_clamped": "camera",
    "prop_phase": "props",
    "axis_flip": "camera",
    "duration_short": "timing",
    "duration_under_preferred": "timing",
    "duration_long": "timing",
    "temporal_phases": "timing",
    "high_value_event": "story",
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
    # Composite scales must precede their shorter substring: ``中全景``
    # contains ``全景`` and otherwise silently degrades into the wider tier.
    for token in ("大特写", "特写", "中近景", "近景", "膝上景", "七分身",
                  "中景", "中全景", "大远景", "全景", "远景"):
        if token in text:
            return token
    return ""


def _visible_count(shot):
    try:
        declared = int(shot.get("visible_figure_count"))
        if declared >= 0:
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
    # 中全景 sits between medium and full: it can comfortably carry a small
    # group of complete bodies, but is not the unlimited crowd-wide fallback.
    # Keep this local until camera_language publishes the same independent
    # tier; six is wider than 中景(4) while remaining finite.
    capacity = 6 if scale == "中全景" else scale_capacity(scale)
    if capacity >= count:
        return None
    frame_targets = shot.get("frame_targets")
    frame_targets = frame_targets if isinstance(frame_targets, dict) else {}
    framing_text = "；".join(str(value or "") for value in (
        shot.get("camera"), shot.get("description"),
        frame_targets.get("keyframe"), frame_targets.get("first_frame"),
        frame_targets.get("last_frame")))
    if allows_partial_multi_subject_scale(framing_text, count):
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


def _check_duration(shot, preferred_floor=None, temporal_required=False):
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
    if (preferred_floor and duration < preferred_floor
            and not str(shot.get("duration_exception_reason") or (
                shot.get("long_take_contract") or {}).get(
                    "short_shot_exception_reason") or "").strip()):
        return _issue(
            shot, "duration_under_preferred",
            f"本镜声明{duration:g}秒,低于长镜头优选下限"
            f"{preferred_floor:g}秒且没有不可合并原因",
            "把场景建立、必要动作、听者反应或情绪收束折入本镜;"
            f"确需短切则写 duration_exception_reason")
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
    if temporal_required:
        beats = shot.get("temporal_beats") or []
        phases = [
            item.get("phase") for item in beats if isinstance(item, dict)]
        if phases != ["setup", "main", "settle"]:
            return _issue(
                shot, "temporal_phases",
                "8-15秒长镜头缺少连续的setup/main/settle三阶段时间节拍",
                "把起势、唯一主动作/对白、听者反应/情绪收束写入"
                "temporal_beats，并连续覆盖完整duration")
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
        if offset is None or offset < -1.01 or offset >= 1.01:
            continue
        ordered.append((offset, str(actor.get("name") or "")))
    ordered.sort()
    return ordered


def _normalized_blocking_phase(value):
    """Map an authored static-frame phase to the 3D blocking boundary."""
    value = str(value or "").strip().lower()
    if value in {"start", "first", "first_frame", "首帧", "起点"}:
        return "start"
    if value in {"end", "last", "last_frame", "尾帧", "终点"}:
        return "end"
    return ""


def _framing_phase(shot):
    """Return the truthful blocking phase for this shot's static framing.

    A keyframe may deliberately bind its freeze to the take's end geometry.
    Using ``framing_conflict``'s legacy start default in that case reports a
    correct end-frame close-up as a seven-metre wide shot.  Explicit frame
    targets take precedence; legacy shots with no phase declaration retain
    the historical start check.
    """
    shot = shot if isinstance(shot, dict) else {}
    target = shot.get("frame_target")
    candidates = []
    if isinstance(target, dict):
        candidates.extend((target.get("blocking_phase"),
                           target.get("phase"), target.get("frame_phase")))
    targets = shot.get("frame_targets")
    if isinstance(targets, dict):
        keyframe = targets.get("keyframe")
        if isinstance(keyframe, dict):
            candidates.extend((keyframe.get("blocking_phase"),
                               keyframe.get("phase"),
                               keyframe.get("frame_phase")))
    candidates.extend((shot.get("blocking_phase"), shot.get("frame_phase")))
    for value in candidates:
        phase = _normalized_blocking_phase(value)
        if phase:
            return phase
    return "start"


def _axis_phase_text(shot, phase):
    targets = shot.get("frame_targets")
    targets = targets if isinstance(targets, dict) else {}
    key = "first_frame" if phase == "start" else "last_frame"
    target = targets.get(key)
    if not isinstance(target, dict):
        target = shot.get("frame_target")
    state = target.get("state") if isinstance(target, dict) else target
    return "；".join(str(value or "") for value in (
        shot.get("camera"), shot.get("description"), state,
        shot.get("shot_contract")))


def _explicit_axis_pair(shot, names, phase):
    """Return visible left/right identities, including named body fragments."""
    text = _axis_phase_text(shot, phase)
    sides = {}
    for clause in re.split(r"[，,。；;\n]+", text):
        for name in names:
            escaped = re.escape(name)
            for side in ("左", "右"):
                # Real shot prose commonly inserts a body part between the
                # identity and side: ``虞寻欢右腕固定在画面左侧`` or
                # ``虞寻歌两指从画面右侧伸入``.
                relation = re.compile(
                    rf"{escaped}[^，,。；;\n]{{0,14}}"
                    rf"(?:位于|处于|固定在|保持在|保持|始终在|在|从)\s*"
                    rf"(?:画面|屏幕|成片)?\s*{side}"
                    rf"(?:侧|边|锚点|前层|后层)?")
                for match in relation.finditer(clause):
                    # A prohibited position is not an observed position.
                    # Previously ``甲不在画面右侧`` was parsed as 甲=right,
                    # manufacturing the exact axis reversal the sentence
                    # forbids.  Only reject negation attached to the spatial
                    # verb; ``甲不移动但始终在左侧`` remains a positive lock.
                    if re.search(
                            r"(?:不在|未在|没有在|并非在|禁止在|不得在|"
                            r"不能在|不可在|不应在|不要在|不许在|不准在|"
                            r"严禁在|避免在|切勿在|未处于|并非处于|"
                            r"不得处于|禁止处于|不可处于|未位于|并非位于|"
                            r"不得位于|禁止位于|不可位于)",
                            match.group(0)):
                        continue
                    sides[side] = name
    if (sides.get("左") and sides.get("右")
            and sides["左"] != sides["右"]):
        return sides["左"], sides["右"]
    return None


def _axis_observable(shot, phase):
    """Whether this phase exposes a truthful two-person screen direction."""
    names = {str(name) for name in (shot.get("characters") or []) if name}
    if len(names) < 2 or _visible_count(shot) == 0:
        return False
    if _explicit_axis_pair(shot, names, phase):
        return True
    text = _axis_phase_text(shot, phase)
    partial_part = any(token in text for token in (
        "手部", "腕部", "手腕", "手指", "两指", "指腹", "脚部", "局部特写"))
    excludes_people = any(token in text for token in (
        "不出现完整人形", "不完整呈现任何人物", "完整人物出画",
        "面部、躯干出画", "面部躯干出画", "仅框入", "只框入",
        "严格只容纳"))
    shows_faces = any(token in text for token in (
        "两张脸", "双脸", "双人面孔", "两人面孔", "头肩", "面部同时"))
    if partial_part and excludes_people and not shows_faces:
        return False
    if (partial_part and not shows_faces
            and _scale_of(shot) in {"大特写", "特写", "近景"}):
        return False
    return True


def _scene_location(script, shot):
    scene_no = str(shot.get("scene_no") or "").strip()
    for scene in (script or {}).get("scenes") or []:
        if (isinstance(scene, dict)
                and str(scene.get("scene_no") or "").strip() == scene_no):
            return str(scene.get("location") or "").strip()
    return str(shot.get("location") or shot.get("scene_location") or "").strip()


def _scene_record(script, shot):
    scene_no = str((shot or {}).get("scene_no") or "").strip()
    for scene in (script or {}).get("scenes") or []:
        if (isinstance(scene, dict)
                and str(scene.get("scene_no") or "").strip() == scene_no):
            return scene
    return {}


def _continuity_value(script, shot, keys):
    """Resolve a realm/phase/era value from shot first, then its scene."""
    scene = _scene_record(script, shot)
    for source in (shot or {}, scene):
        for key in keys:
            value = re.sub(r"\s+", "", str(source.get(key) or "")).lower()
            if value:
                return value
    return ""


def _space_root(value):
    value = re.sub(r"\s+", "", str(value or ""))
    parts = value.split("·")
    room = parts[-1] if parts else value
    room = re.split(
        r"(?:床侧|床边|床尾|至|入口|门内|门外|走廊|近景|远景)",
        room, maxsplit=1)[0]
    return (parts[0] + "·" + room) if len(parts) > 1 else room


def _same_continuous_space(script, earlier, current):
    # Identical furniture coordinates do not make two different worlds one
    # continuous set.  Reality/game, present/flashback and era transitions
    # reset the audience's 180-degree baseline even when the location label is
    # deliberately reused (for example the same bedroom in two timelines).
    dimensions = (
        ("active_realm_id", "realm_id", "active_realm"),
        ("active_story_phase", "story_phase", "timeline_state"),
        ("era_context", "era", "时代"),
    )
    for keys in dimensions:
        before = _continuity_value(script, earlier, keys)
        after = _continuity_value(script, current, keys)
        if before and after and before != after:
            return False
    earlier_no = str(earlier.get("scene_no") or "").strip()
    current_no = str(current.get("scene_no") or "").strip()
    if earlier_no and current_no and earlier_no == current_no:
        return True
    earlier_location = _space_root(_scene_location(script, earlier))
    current_location = _space_root(_scene_location(script, current))
    return bool(
        earlier_location and current_location
        and earlier_location == current_location)


def _check_axis(shot, block, previous):
    """同场相邻镜头的人物左右关系不得翻转(180度轴线法则)。

    只在两镜共有同一对人物、且两镜的左右分区都成立时判定——同区、
    单人、换人都不算越轴,避免把正常剪辑误判成错误。
    """
    if not previous:
        return None
    prev_shot, prev_block = previous
    shared = {
        str(name) for name in (prev_shot.get("characters") or []) if name
    } & {str(name) for name in (shot.get("characters") or []) if name}
    if len(shared) < 2:
        return None

    explicit_now = _explicit_axis_pair(shot, shared, "start")
    explicit_before = _explicit_axis_pair(prev_shot, shared, "end")
    # Explicit visible left/right locks remain observable even in a hand,
    # wrist or face insert.  Only hidden 3D actor centres are unsafe for a
    # partial shot; never let the partial-shot exemption suppress an authored
    # and visibly contradictory reversal.
    if explicit_now and explicit_before:
        now, before = explicit_now, explicit_before
        if now == before:
            return None
        return _issue(
            shot, "axis_flip",
            f"越轴:上一镜{before[0]}在画面左、{before[1]}在右,"
            f"本镜变成{now[0]}在左、{now[1]}在右",
            f"把机位移回轴线同一侧,保持{before[0]}在画面左、{before[1]}在右;"
            "确需跨轴请先安排一个中性过渡镜")

    if (not _axis_observable(prev_shot, "end")
            or not _axis_observable(shot, "start")):
        return None

    current = _screen_order(block, phase="start")
    # A cut joins the previous *end* frame to the current *start* frame.
    earlier = _screen_order(prev_block, phase="end")
    if len(current) < 2 or len(earlier) < 2:
        return None

    def side_map(rows):
        rows = [(offset, name) for offset, name in rows if name in shared]
        if len(rows) < 2 or screen_zone(rows[0][0]) == screen_zone(
                rows[-1][0]):
            return None
        return rows[0][1], rows[-1][1]
    now = explicit_now or side_map(current)
    before = explicit_before or side_map(earlier)
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
    profile = (storyboard or {}).get("profile") or {}
    rules = profile.get("rules") or {}
    production = rules.get("production") or {}
    policy = (profile.get("long_take_policy")
              or production.get("long_take_policy") or {})
    long_take = bool(policy.get("enabled", False))
    try:
        preferred_floor = float((
            policy.get("preferred_seconds")
            or profile.get("preferred_segment_seconds")
            or production.get("preferred_segment_seconds")
            or [8, 15])[0]) if long_take else None
    except (IndexError, TypeError, ValueError):
        preferred_floor = 8.0 if long_take else None
    issues = []
    # Last shot whose visible phase actually carries a left/right relation.
    # A hand-only insert is transparent to the 180-degree state machine and
    # must not erase the last audience-observable axis.
    previous = None
    for shot in shots:
        block = index.get(str(shot.get("shot_no"))) or {}
        for issue in (_check_capacity(shot), _check_prop_phases(shot),
                      _check_duration(
                          shot, preferred_floor,
                          temporal_required=bool(
                              long_take and policy.get(
                                  "temporal_phases_required", True)))):
            if issue:
                issues.append(issue)
        if block:
            declared_scale = _scale_of(shot)
            # spatial_blocking solves 中全景 at 3.8m, which falls in the
            # current distance validator's 全景 band (3.5–6.0m).  Validate it
            # against that boundary while preserving its independent label in
            # the issue shown to the director.
            conflict_scale = (
                "全景" if declared_scale == "中全景" else declared_scale)
            conflict = framing_conflict(
                block, conflict_scale, phase=_framing_phase(shot))
            if conflict and declared_scale == "中全景":
                conflict = conflict.replace("合同声明全景", "合同声明中全景")
            if conflict:
                issues.append(_issue(
                    shot, "framing_distance", conflict,
                    "调整机位距离或改写景别,使两者落在同一取景档"))
            if (previous
                    and not _same_continuous_space(
                        script, previous[0], shot)):
                previous = None
            axis = _check_axis(shot, block, previous)
            if axis:
                issues.append(axis)
            if _axis_observable(shot, "end"):
                previous = (shot, block)
    shots_by_no = {
        str(shot.get("shot_no")): shot for shot in shots
        if shot.get("shot_no") is not None
    }
    camera_kinds = {
        "distance": "camera_wall_clamped",
        "movement": "camera_movement_wall_clamped",
    }
    for camera_issue in director_camera_issues(blocking or {}):
        kind = camera_kinds.get(str(camera_issue.get("field") or ""))
        if not kind:
            continue
        shot_no = str(camera_issue.get("shot_no") or "")
        owner = shots_by_no.get(shot_no) or {
            "shot_no": camera_issue.get("shot_no"),
            "scene_no": camera_issue.get("scene_no"),
        }
        suggestion = (
            "改景别或把人物挪离墙面，重新求解真实机位"
            if kind == "camera_wall_clamped"
            else "缩短运镜幅度或改变机位方向，重新求解完整运动路线")
        issues.append(_issue(
            owner, kind, str(camera_issue.get("message") or ""), suggestion))
    by_shot = {}
    for issue in issues:
        by_shot.setdefault(str(issue["shot_no"]), []).append(issue)
    event_coverage = audit_high_value_event_coverage(script, storyboard)
    for detail in event_coverage["issues"]:
        issue = {
            "shot_no": None,
            "scene_no": None,
            "kind": "high_value_event",
            "fixer": "story",
            "detail": detail,
            "suggestion": "只重分该高价值事件所在场，补足独立可见节拍；禁止用长镜头折叠",
        }
        issues.append(issue)
        by_shot.setdefault("None", []).append(issue)
    return {
        "schema": SCHEMA,
        "passed": not issues,
        "shots": len(shots),
        "issues": issues,
        "by_shot": by_shot,
        "high_value_event_coverage": event_coverage,
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
