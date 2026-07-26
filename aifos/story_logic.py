"""Deterministic screenplay logic and shootability checks.

The writing model is responsible for creative adaptation.  This module is the
fail-closed production gate that makes sure a generated or imported script has
enough concrete information for a director to stage it without inventing
physics, props, entrances, exits or character motivation downstream.
"""

from __future__ import annotations

import copy
import hashlib
import re

from .speaker_labels import is_non_person_label


SCRIPT_LOGIC_SCHEMA = "aifos.script-logic/v2"
PROP_CONTRACT_SCHEMA = "aifos.prop-contract/v2.2"
PROP_VISIBILITY_STATES = {
    "visible", "occluded", "hidden", "absent",
}
PROP_REPRESENTATIONS = {
    "physical", "reflection", "screen", "painting", "overlay",
}
PROP_FRAME_PHASES = {"start", "end", "freeze"}
PROP_DISCLOSURE_POLICIES = {
    "explicit_frame_only",
    "conceal_until_introduced",
    "available_after_introduction",
    "never_visualize",
}
ADAPTATION_REVIEW_FIELDS = (
    "source_to_screen_strategy",
    "source_material_policy",
    "causal_chain",
    "character_motivation",
    "information_continuity",
    "physical_reality",
    "spatial_continuity",
    "temporal_continuity",
    "prop_lifecycle",
    "missing_detail_completion",
    "story_density",
    "shootability",
    "local_rewrite_policy",
)
SCENE_LOGIC_FIELDS = (
    "dramatic_function",
    "entry_state",
    "information_state",
    "physical_actions",
    "prop_continuity",
    "spatial_logic",
    "time_continuity",
    "missing_details_completed",
    "exit_state",
    "director_intent",
)
CONTINUITY_CONTRACT_FIELDS = (
    "entry_boundary",
    "exit_boundary",
    "immutable_facts",
    "prop_ledger",
    "knowledge_state",
    "time_state",
    "local_rewrite_scope",
)
VAGUE_ACTIONS = {
    "", "推进剧情", "发生冲突", "展开故事", "人物互动", "继续对话",
    "按剧情发展", "营造氛围", "情绪变化", "自然表演",
}
PLACEHOLDER_CUES = (
    "待编剧", "按剧情决定", "按剧情发展", "自由发挥", "自行决定",
    "后续补充", "暂未明确",
)
PHYSICAL_CUES = (
    "站", "坐", "跪", "躺", "卧", "走", "跑", "转身", "抬手", "伸手",
    "拿", "放", "递", "推", "拉", "扶", "趴", "起身", "进入", "离开",
    "看向", "低头", "抬头", "打开", "关闭", "触碰",
)


def _text(value, fallback="") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def _stable_prop_id(name: str, kind: str = "core") -> str:
    """Create a deterministic legacy ID without treating a display name as ID."""
    identity = f"{_text(kind).lower()}|{_text(name).lower()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"prop-{digest}"


def _valid_prop_id(value) -> bool:
    text = _text(value)
    return bool(
        text and len(text) <= 128
        and not re.search(r"[\s/\\]", text))


# phase 枚举的无歧义别名:模型偶发写 begin/opening/开场 等同义词,
# 语义唯一时本地归一,不值得为几个枚举字段丢弃整份剧本重新生成。
_PROP_PHASE_ALIASES = {
    "begin": "start", "beginning": "start", "opening": "start",
    "open": "start", "first": "start", "intro": "start",
    "introduce": "start", "introduced": "start", "appear": "start",
    "appears": "start", "entry": "start", "enter": "start",
    "起点": "start", "开始": "start", "开场": "start", "首帧": "start",
    "入场": "start",
    "finish": "end", "finished": "end", "final": "end", "close": "end",
    "closing": "end", "last": "end", "exit": "end", "retire": "end",
    "retired": "end", "leave": "end", "departure": "end",
    "终点": "end", "结束": "end", "收场": "end", "尾帧": "end",
    "离场": "end",
    "hold": "freeze", "held": "freeze", "static": "freeze",
    "frozen": "freeze", "still": "freeze", "pause": "freeze",
    "定格": "freeze", "静止": "freeze", "冻结": "freeze",
}
# 按字母段切词(scene_start/at-end/startFrame 里的下划线、连字符
# 都是分隔),再与合法枚举取交集;恰好命中一个才归一。
_PROP_PHASE_WORD_RE = re.compile(r"[a-z]+")


def _normalize_phase(phase):
    """phase 同义词/复合写法(scene_start、at_end…)归一到合法枚举。

    只在语义无歧义时归一;无法确定的值原样保留,交给校验与就地修复。
    """
    phase = _text(phase).lower()
    if not phase or phase in PROP_FRAME_PHASES:
        return phase
    alias = _PROP_PHASE_ALIASES.get(phase)
    if alias:
        return alias
    words = set(_PROP_PHASE_WORD_RE.findall(phase)) & PROP_FRAME_PHASES
    if len(words) == 1:
        return words.pop()
    return phase


def _normalize_event_ref(value):
    """Preserve stable event references; a bare shot number is intentionally invalid."""
    if isinstance(value, str):
        event_id = _text(value)
        return {"event_id": event_id} if event_id else None
    if not isinstance(value, dict):
        return None
    event_id = _text(value.get("event_id") or value.get("unit_id"))
    if not event_id:
        return copy.deepcopy(value)
    normalized = copy.deepcopy(value)
    normalized["event_id"] = event_id
    normalized.pop("unit_id", None)
    phase = _normalize_phase(normalized.get("phase"))
    if phase:
        normalized["phase"] = phase
    else:
        normalized.pop("phase", None)
    return normalized


def _event_ref_id(value) -> str:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return ""
    return _text(value.get("event_id") or value.get("unit_id"))


def _event_ref_phase(value, fallback="start") -> str:
    if not isinstance(value, dict):
        return fallback
    return _text(value.get("phase"), fallback).lower()


def _event_ref_issues(value, label, *, optional=False) -> list[str]:
    if value in (None, ""):
        return [] if optional else [f"{label} 缺少稳定事件引用"]
    event_id = _event_ref_id(value)
    if not event_id:
        return [f"{label} 必须含 event_id，不能只写 shot_no"]
    if len(event_id) > 160 or re.search(r"[\r\n]", event_id):
        return [f"{label}.event_id 非法"]
    phase = _event_ref_phase(value)
    if phase not in PROP_FRAME_PHASES:
        return [f"{label}.phase 必须是 start/end/freeze"]
    return []


def normalize_prop_contract(script: dict) -> dict:
    """Normalize only registered story props; ordinary set dressing stays out."""
    if not isinstance(script, dict):
        return script
    raw_registry = script.get("prop_registry")
    registry = raw_registry if isinstance(raw_registry, list) else []
    normalized_registry = []
    by_id = {}
    ids_by_name = {}
    for raw in registry:
        if not isinstance(raw, dict):
            normalized_registry.append(raw)
            continue
        item = copy.deepcopy(raw)
        name = _text(item.get("name"))
        kind = _text(item.get("kind"), "core").lower()
        prop_id = _text(item.get("prop_id"))
        if not prop_id and name:
            prop_id = _stable_prop_id(name, kind)
        item["prop_id"] = prop_id
        item["name"] = name
        item["kind"] = kind
        if "instance_count" not in item:
            item["instance_count"] = 1
        normalization_issues = []
        if (item.get("availability_start_event") not in (None, "")
                and item.get("introduced_at") not in (None, "")
                and _normalize_event_ref(
                    item.get("availability_start_event"))
                != _normalize_event_ref(item.get("introduced_at"))):
            normalization_issues.append(
                "introduced_at 与 availability_start_event 冲突")
        if (item.get("availability_end_event") not in (None, "")
                and item.get("retired_at") not in (None, "")
                and _normalize_event_ref(
                    item.get("availability_end_event"))
                != _normalize_event_ref(item.get("retired_at"))):
            normalization_issues.append(
                "retired_at 与 availability_end_event 冲突")
        if normalization_issues:
            item["_prop_contract_normalization_issues"] = (
                normalization_issues)
        start = (
            item.get("availability_start_event")
            if item.get("availability_start_event") not in (None, "")
            else item.get("introduced_at"))
        normalized_start = _normalize_event_ref(start)
        if normalized_start is not None:
            item["availability_start_event"] = normalized_start
            item["introduced_at"] = copy.deepcopy(normalized_start)
        end = (
            item.get("availability_end_event")
            if item.get("availability_end_event") not in (None, "")
            else item.get("retired_at"))
        normalized_end = _normalize_event_ref(end)
        if normalized_end is not None:
            item["availability_end_event"] = normalized_end
            item["retired_at"] = copy.deepcopy(normalized_end)
        item.setdefault("disclosure_policy", "explicit_frame_only")
        normalized_registry.append(item)
        if prop_id:
            by_id.setdefault(prop_id, item)
        if name:
            ids_by_name.setdefault(name, []).append(prop_id)

    # Existing core_props are plot-sensitive by definition. Give legacy records a
    # stable ID and an explicit episode-start boundary, without registering any
    # incidental scene dressing.
    for raw in script.get("core_props") or []:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"))
        prop_id = _text(raw.get("prop_id"))
        if not prop_id and name and len(ids_by_name.get(name, [])) == 1:
            prop_id = ids_by_name[name][0]
        if not prop_id and name:
            prop_id = _stable_prop_id(name, "core")
        if prop_id:
            raw["prop_id"] = prop_id
        if prop_id in by_id:
            continue
        start = (
            raw.get("availability_start_event")
            or raw.get("introduced_at")
            or {"event_id": "episode-start", "phase": "start"})
        item = {
            "prop_id": prop_id,
            "name": name,
            "kind": _text(raw.get("kind"), "core").lower(),
            "instance_count": raw.get("instance_count", 1),
            "availability_start_event": _normalize_event_ref(start),
            "introduced_at": _normalize_event_ref(start),
            "disclosure_policy": _text(
                raw.get("disclosure_policy"), "explicit_frame_only"),
        }
        end = (
            raw.get("availability_end_event")
            or raw.get("retired_at"))
        if end not in (None, ""):
            item["availability_end_event"] = _normalize_event_ref(end)
            item["retired_at"] = _normalize_event_ref(end)
        normalized_registry.append(item)
        by_id[prop_id] = item
        ids_by_name.setdefault(name, []).append(prop_id)

    script["prop_contract_schema"] = PROP_CONTRACT_SCHEMA
    script["prop_registry"] = normalized_registry
    return script


def audit_prop_contract(script: dict) -> dict:
    """Validate stable IDs and event-addressable lifecycles without NLP scans."""
    issues = []
    registry = script.get("prop_registry")
    if not isinstance(registry, list):
        return {
            "schema": PROP_CONTRACT_SCHEMA,
            "passed": False,
            "issues": ["prop_registry 必须是数组"],
            "registered_prop_ids": [],
        }
    seen_ids = set()
    seen_names = set()
    registered = {}
    for position, item in enumerate(registry, 1):
        prefix = f"prop_registry[{position}]"
        if not isinstance(item, dict):
            issues.append(f"{prefix} 必须是对象")
            continue
        prop_id = _text(item.get("prop_id"))
        if not _valid_prop_id(prop_id):
            issues.append(f"{prefix}.prop_id 缺失或非法")
        elif prop_id in seen_ids:
            issues.append(f"prop_id 重复：{prop_id}")
        else:
            seen_ids.add(prop_id)
            registered[prop_id] = item
        name = _text(item.get("name"))
        issues.extend(
            f"{prefix}.{_text(value)}"
            for value in (
                item.get("_prop_contract_normalization_issues") or [])
            if _text(value))
        if not name:
            issues.append(f"{prefix}.name 缺失")
        elif name in seen_names:
            issues.append(
                f"{prefix}.name 重复：{name}；同款多件物品也必须使用"
                "可区分的实例名称与不同 prop_id")
        else:
            seen_names.add(name)
        if not _text(item.get("kind")):
            issues.append(f"{prefix}.kind 缺失")
        instance_count = item.get("instance_count")
        if type(instance_count) is not int or instance_count != 1:
            issues.append(
                f"{prefix}.instance_count 必须为1；多件同款物品必须拆成"
                "不同 prop_id")
        policy = _text(item.get("disclosure_policy")).lower()
        if policy not in PROP_DISCLOSURE_POLICIES:
            issues.append(
                f"{prefix}.disclosure_policy 必须是 "
                + "/".join(sorted(PROP_DISCLOSURE_POLICIES)))
        start = item.get("availability_start_event")
        introduced = item.get("introduced_at")
        issues.extend(_event_ref_issues(
            start or introduced, f"{prefix}.availability_start_event"))
        if (start not in (None, "") and introduced not in (None, "")
                and _normalize_event_ref(start) != _normalize_event_ref(introduced)):
            issues.append(f"{prefix}.introduced_at 与 availability_start_event 冲突")
        end = item.get("availability_end_event")
        retired = item.get("retired_at")
        issues.extend(_event_ref_issues(
            end or retired, f"{prefix}.availability_end_event", optional=True))
        if (end not in (None, "") and retired not in (None, "")
                and _normalize_event_ref(end) != _normalize_event_ref(retired)):
            issues.append(f"{prefix}.retired_at 与 availability_end_event 冲突")

    for position, item in enumerate(script.get("core_props") or [], 1):
        if not isinstance(item, dict):
            continue
        prop_id = _text(item.get("prop_id"))
        if not prop_id:
            issues.append(f"core_props[{position}] 缺少 prop_id")
        elif prop_id not in registered:
            issues.append(
                f"core_props[{position}].prop_id 未登记到 prop_registry：{prop_id}")
    return {
        "schema": PROP_CONTRACT_SCHEMA,
        "passed": not issues,
        "issues": list(dict.fromkeys(issues)),
        "registered_prop_ids": sorted(registered),
    }


def _event_order(storyboard: dict) -> tuple[dict, list[str]]:
    order = {("episode-start", "start"): -2,
             ("episode-start", "end"): -1}
    issues = []
    reserved_events = {"episode-start", "episode-end"}
    scene_event_by_no = {}
    scene_no_by_event = {}
    shot_event_ids = []

    # Validate the namespace and the one-to-one scene binding before building
    # the time axis.  Otherwise an early shot can claim a later scene_event_id
    # and move that later scene's availability boundary forwards.
    for position, shot in enumerate(storyboard.get("shots") or [], 1):
        if not isinstance(shot, dict):
            continue
        scene_no = _text(shot.get("scene_no"))
        if not scene_no:
            issues.append(f"镜头{position}缺少 scene_no，无法绑定场次事件")
            continue
        canonical_scene_event = f"scene:{scene_no}"
        scene_event_id = _text(
            shot.get("scene_event_id") or canonical_scene_event)
        if not scene_event_id:
            issues.append(f"镜头{position}缺少稳定 scene_event_id")
            continue
        if scene_event_id in reserved_events or scene_event_id.startswith(
                "shot:"):
            issues.append(
                f"镜头{position}.scene_event_id 命名空间非法："
                f"{scene_event_id}")
        previous_event = scene_event_by_no.setdefault(
            scene_no, scene_event_id)
        if previous_event != scene_event_id:
            issues.append(
                f"scene_no={scene_no} 绑定了多个 scene_event_id："
                f"{previous_event}、{scene_event_id}")
        previous_scene = scene_no_by_event.setdefault(
            scene_event_id, scene_no)
        if previous_scene != scene_no:
            issues.append(
                f"scene_event_id={scene_event_id} 被多个场次复用："
                f"{previous_scene}、{scene_no}")
        event_id = _text(shot.get("event_id") or shot.get("unit_id"))
        if event_id:
            shot_event_ids.append((position, event_id))

    scene_event_ids = set(scene_no_by_event)
    for position, event_id in shot_event_ids:
        if (event_id in reserved_events or event_id in scene_event_ids
                or event_id.startswith("scene:")):
            issues.append(
                f"镜头{position}.event_id 与场次/保留事件命名空间冲突："
                f"{event_id}")

    cursor = 0
    seen = set()
    for position, shot in enumerate(storyboard.get("shots") or [], 1):
        if not isinstance(shot, dict):
            continue
        event_id = _text(shot.get("event_id") or shot.get("unit_id"))
        if not event_id:
            issues.append(f"镜头{position}缺少稳定 event_id")
            continue
        if event_id in seen:
            issues.append(f"镜头 event_id 重复：{event_id}")
            continue
        seen.add(event_id)
        order[(event_id, "start")] = cursor
        order[(event_id, "freeze")] = cursor + 1
        order[(event_id, "end")] = cursor + 2
        # The script registry is authored before shots exist, so its stable
        # lifecycle boundary normally points at a scene event.  Map that scene
        # event onto the first/last shot in the scene without rewriting the
        # authoritative registry to fragile shot numbers.
        scene_event_id = _text(
            shot.get("scene_event_id")
            or (f"scene:{shot.get('scene_no')}"
                if shot.get("scene_no") is not None else ""))
        if scene_event_id:
            order.setdefault((scene_event_id, "start"), cursor)
            order.setdefault((scene_event_id, "freeze"), cursor + 1)
            order[(scene_event_id, "end")] = cursor + 2
        cursor += 3
    order[("episode-end", "start")] = cursor
    order[("episode-end", "freeze")] = cursor + 1
    order[("episode-end", "end")] = cursor + 2
    return order, issues


def audit_storyboard_prop_contract(storyboard: dict) -> dict:
    """Validate frame-local prop instances and event-order availability."""
    issues = []
    registry = storyboard.get("prop_registry")
    if not isinstance(registry, list):
        registry = []
    registry_by_id = {
        _text(item.get("prop_id")): item
        for item in registry if isinstance(item, dict)
        and _valid_prop_id(item.get("prop_id"))
    }
    order, order_issues = _event_order(storyboard)
    any_structured_props = any(
        isinstance(shot, dict) and (
            shot.get("frame_props") or shot.get("prop_transitions"))
        for shot in storyboard.get("shots") or [])
    if any_structured_props or registry_by_id:
        issues.extend(order_issues)
    if any_structured_props:
        if not registry_by_id:
            issues.append("分镜使用 frame_props/prop_transitions，但缺少 prop_registry")

    # Availability is a registry invariant even when the prop never appears in
    # this episode.  Validate every boundary globally so an impossible
    # start-after-end lifecycle cannot hide behind an empty frame_props list.
    for prop_id, prop in registry_by_id.items():
        start_ref = (
            prop.get("availability_start_event")
            or prop.get("introduced_at"))
        start_key = (
            _event_ref_id(start_ref),
            _event_ref_phase(start_ref, "start"))
        start_order = order.get(start_key)
        if start_ref and start_order is None:
            issues.append(
                f"{prop_id} 无法解析 availability_start_event："
                f"{start_key[0]}#{start_key[1]}")
        end_ref = (
            prop.get("availability_end_event")
            or prop.get("retired_at"))
        if not end_ref:
            continue
        end_key = (
            _event_ref_id(end_ref),
            _event_ref_phase(end_ref, "end"))
        end_order = order.get(end_key)
        if end_order is None:
            issues.append(
                f"{prop_id} 无法解析 availability_end_event："
                f"{end_key[0]}#{end_key[1]}")
        elif (start_order is not None and start_order > end_order):
            issues.append(
                f"{prop_id} 的 availability_start_event 晚于 "
                "availability_end_event")

    for shot_position, shot in enumerate(storyboard.get("shots") or [], 1):
        if not isinstance(shot, dict):
            continue
        event_id = _text(shot.get("event_id") or shot.get("unit_id"))
        frame_props = shot.get("frame_props") or []
        transitions = shot.get("prop_transitions") or []
        if not isinstance(frame_props, list):
            issues.append(f"镜头{shot_position}.frame_props 必须是数组")
            frame_props = []
        if not isinstance(transitions, list):
            issues.append(f"镜头{shot_position}.prop_transitions 必须是数组")
            transitions = []
        phase_rows = {}
        physical_counts = {}
        for row_position, item in enumerate(frame_props, 1):
            prefix = f"镜头{shot_position}.frame_props[{row_position}]"
            if not isinstance(item, dict):
                issues.append(f"{prefix} 必须是对象")
                continue
            prop_id = _text(item.get("prop_id"))
            phase = _text(item.get("phase")).lower()
            visibility = _text(item.get("visibility")).lower()
            representation = _text(item.get("representation")).lower()
            if not _valid_prop_id(prop_id):
                issues.append(f"{prefix}.prop_id 缺失或非法")
            elif prop_id not in registry_by_id:
                issues.append(f"{prefix}.prop_id 未登记：{prop_id}")
            if phase not in PROP_FRAME_PHASES:
                issues.append(f"{prefix}.phase 必须是 start/end/freeze")
            if visibility not in PROP_VISIBILITY_STATES:
                issues.append(
                    f"{prefix}.visibility 必须是 visible/occluded/hidden/absent")
            if representation not in PROP_REPRESENTATIONS:
                issues.append(
                    f"{prefix}.representation 必须是 physical/reflection/"
                    "screen/painting/overlay")
            for field in ("physical_state", "holder", "location", "support"):
                if not _text(item.get(field)):
                    issues.append(f"{prefix}.{field} 必须显式填写，无则写 none")
            key = (prop_id, phase)
            phase_rows.setdefault(key, []).append(item)
            if (representation == "physical"
                    and visibility != "absent" and prop_id):
                physical_counts[key] = physical_counts.get(key, 0) + 1

            prop = registry_by_id.get(prop_id) or {}
            is_disclosure = visibility in {"visible", "occluded"}
            is_physical_presence = (
                representation == "physical" and visibility != "absent")
            if is_disclosure:
                policy = _text(prop.get("disclosure_policy")).lower()
                if policy == "never_visualize":
                    issues.append(f"{prefix} 违反 never_visualize 披露策略")
            if is_disclosure or is_physical_presence:
                current_order = order.get((event_id, phase))
                start_ref = (
                    prop.get("availability_start_event")
                    or prop.get("introduced_at"))
                start_key = (
                    _event_ref_id(start_ref),
                    _event_ref_phase(start_ref, "start"))
                start_order = order.get(start_key)
                if start_ref and start_order is None:
                    issues.append(
                        f"{prefix} 无法解析 availability_start_event："
                        f"{start_key[0]}#{start_key[1]}")
                elif (current_order is not None and start_order is not None
                      and current_order < start_order):
                    issues.append(f"{prefix} 在首次可披露事件之前出现")
                end_ref = (
                    prop.get("availability_end_event")
                    or prop.get("retired_at"))
                if end_ref:
                    end_key = (
                        _event_ref_id(end_ref),
                        _event_ref_phase(end_ref, "end"))
                    end_order = order.get(end_key)
                    if end_order is None:
                        issues.append(
                            f"{prefix} 无法解析 availability_end_event："
                            f"{end_key[0]}#{end_key[1]}")
                    elif (current_order is not None
                          and current_order > end_order):
                        issues.append(f"{prefix} 在退场事件之后出现")

        # One registered instance may have several carrier representations in a
        # phase (for example physical + reflection), but the same representation
        # cannot declare two locations or mutually exclusive visibility states.
        for (prop_id, phase), rows in phase_rows.items():
            representations = {}
            for item in rows:
                representation = _text(
                    item.get("representation")).lower()
                representations.setdefault(representation, []).append(item)
            for representation, representation_rows in representations.items():
                if len(representation_rows) > 1:
                    issues.append(
                        f"镜头{shot_position}的 {prop_id} 在 {phase} 相位"
                        f"重复声明 representation={representation}；"
                        "同一实例同一呈现方式只能有一个状态")

        for (prop_id, phase), count in physical_counts.items():
            prop = registry_by_id.get(prop_id) or {}
            instance_count = prop.get("instance_count")
            if type(instance_count) is int and count > instance_count:
                issues.append(
                    f"镜头{shot_position}的 {prop_id} 在 {phase} 相位出现"
                    f"{count}个实体位置，超过登记实例数{instance_count}")

        transition_keys = set()
        for row_position, item in enumerate(transitions, 1):
            prefix = f"镜头{shot_position}.prop_transitions[{row_position}]"
            if not isinstance(item, dict):
                issues.append(f"{prefix} 必须是对象")
                continue
            prop_id = _text(item.get("prop_id"))
            from_phase = _text(item.get("from_phase")).lower()
            to_phase = _text(item.get("to_phase")).lower()
            if prop_id not in registry_by_id:
                issues.append(f"{prefix}.prop_id 未登记：{prop_id}")
            if from_phase not in PROP_FRAME_PHASES:
                issues.append(f"{prefix}.from_phase 非法")
            if to_phase not in PROP_FRAME_PHASES:
                issues.append(f"{prefix}.to_phase 非法")
            if from_phase == to_phase:
                issues.append(f"{prefix} 起止 phase 不能相同")
            if not _text(item.get("action")):
                issues.append(f"{prefix}.action 缺失")
            if (prop_id, from_phase) not in phase_rows:
                issues.append(f"{prefix} 缺少 from_phase 对应的 frame_props")
            if (prop_id, to_phase) not in phase_rows:
                issues.append(f"{prefix} 缺少 to_phase 对应的 frame_props")
            transition_keys.add((prop_id, from_phase, to_phase))

        prop_ids = {key[0] for key in phase_rows}
        for prop_id in prop_ids:
            starts = phase_rows.get((prop_id, "start")) or []
            ends = phase_rows.get((prop_id, "end")) or []
            freezes = phase_rows.get((prop_id, "freeze")) or []
            if bool(starts) != bool(ends) or (
                    freezes and not starts and not ends):
                if not starts and not ends:
                    missing_phase = "start 与 end"
                else:
                    missing_phase = "end" if starts else "start"
                issues.append(
                    f"镜头{shot_position}的 {prop_id} 参加视频时间线，"
                    f"但缺少 phase={missing_phase} 的 frame_props；"
                    "未出现时也必须显式写 visibility=absent")
                continue
            if starts and ends:
                state_fields = (
                    "physical_state", "holder", "location", "support",
                    "visibility", "representation",
                )
                start_signature = sorted(
                    tuple(_text(item.get(field)).lower()
                          for field in state_fields)
                    for item in starts)
                end_signature = sorted(
                    tuple(_text(item.get(field)).lower()
                          for field in state_fields)
                    for item in ends)
                changed = start_signature != end_signature
                if changed and (prop_id, "start", "end") not in transition_keys:
                    issues.append(
                        f"镜头{shot_position}的 {prop_id} 起止状态变化但缺少 "
                        "start→end prop_transitions")
    return {
        "schema": PROP_CONTRACT_SCHEMA,
        "passed": not issues,
        "issues": list(dict.fromkeys(issues)),
    }


def _scene_action(scene) -> str:
    return _text(scene.get("action"))


def _default_scene_logic(scene: dict) -> dict:
    """Conservative legacy/import fallback without fabricating new plot."""
    action = _scene_action(scene)
    people = "、".join(
        str(name) for name in (scene.get("characters") or []) if name)
    return {
        "dramatic_function": (
            f"通过本场可见行动推进：{action}" if action
            else "待编剧明确本场可见事件与状态变化"),
        "entry_state": (
            f"{people or '场景'}进入本场时的位置、姿态、持有道具和情绪"
            "必须承接上一场或在本场开头明确建立"),
        "information_state": (
            f"逐一明确{people or '本场重要人物'}在本场开始时已经知道、"
            "尚不知道和通过何种可见证据新获知的信息，禁止无来源知情"),
        "physical_actions": (
            action if action else
            "待编剧改写为能被摄影机直接拍到的单一动作链"),
        "prop_continuity": (
            "本场使用的关键道具必须说明来源、持有人、接触方式和离场去向；"
            "没有关键道具时明确为无"),
        "spatial_logic": (
            f"在{_text(scene.get('location')) or '已声明地点'}内明确人物入口、"
            "相对站位、视线对象、动作路径与出口，禁止瞬移或无支撑动作"),
        "time_continuity": (
            "明确本场与上一场的时间关系、动作耗时和必要过渡，"
            "禁止人物、道具或事件无时间成本地跳到结果"),
        "missing_details_completed": (
            "补齐原素材未写但实际拍摄必需的入口、支撑面、交接动作、"
            "环境反应和道具状态；不得新增改变主线的无依据事件"),
        "exit_state": (
            "明确本场结束时每名重要人物的位置、姿态、情绪、伤势和持有道具，"
            "供下一场继承"),
        "director_intent": (
            "把叙述性概括外化为观众可见的行动、反应或状态改变；"
            "不使用旁白替代核心事件"),
    }


def _default_continuity_contract(logic: dict) -> dict:
    return {
        "entry_boundary": _text(logic.get("entry_state")),
        "exit_boundary": _text(logic.get("exit_state")),
        "immutable_facts": (
            "人物身份、世界规则、已经发生的事件、已建立的伏笔和后续必达结果"
            "不可在局部返编中静默改变"),
        "prop_ledger": _text(logic.get("prop_continuity")),
        "knowledge_state": _text(logic.get("information_state")),
        "time_state": _text(logic.get("time_continuity")),
        "local_rewrite_scope": (
            "默认只修改本场；若改变前一场出口、后一场入口、伏笔、后续结果或"
            "已生产资产，必须先列出影响范围并等待确认"),
    }


def normalize_script_logic(script: dict) -> dict:
    """Add an auditable director layer to new, imported and legacy scripts."""
    if not isinstance(script, dict):
        return script
    normalize_prop_contract(script)
    review = script.get("adaptation_review")
    if not isinstance(review, dict):
        review = {}
        script["adaptation_review"] = review
    review.setdefault(
        "source_to_screen_strategy",
        "保留原作因果与人物核心动机，把抽象叙述改写为可见行动、反应和状态变化")
    review.setdefault(
        "source_material_policy",
        "小说、梗概和导入文本是可改编素材，不是已锁定正式剧本；"
        "正式剧本经第一道门禁锁定后才保护台词和场次合同")
    review.setdefault(
        "causal_chain",
        "逐场明确触发事件→人物选择→可见行动→直接结果→下一场钩子")
    review.setdefault(
        "character_motivation",
        "每个关键行动必须由人物目标、已知信息和当前风险驱动")
    review.setdefault(
        "information_continuity",
        "逐场核对人物知道什么、从哪里得知、何时得知，禁止无来源知情或遗忘")
    review.setdefault(
        "physical_reality",
        "人物动作可达，道具来源与去向明确，重力、支撑、接触和设备使用方向成立")
    review.setdefault(
        "spatial_continuity",
        "人物入口、出口、相对站位、视线、运动路径与场景布局连续")
    review.setdefault(
        "temporal_continuity",
        "逐场明确时间顺序、动作耗时和场间过渡，禁止时间跳跃造成状态断裂")
    review.setdefault(
        "prop_lifecycle",
        "逐件关键道具明确出现来源、初始状态、持有人、使用与交接、状态变化和去向")
    review.setdefault(
        "missing_detail_completion",
        "主动补齐原素材省略的服化道、入口出口、交接动作、环境反应、"
        "支撑接触和可见结果，但不得凭空改变核心剧情")
    review.setdefault(
        "story_density",
        "每场必须产生新信息、选择、冲突、反应或结果；不以空镜和泛化旁白凑时长")
    review.setdefault(
        "shootability",
        "核心剧情必须能由镜头直接拍出，不依赖泛化旁白或不可见心理概括")
    review.setdefault(
        "local_rewrite_policy",
        "后续发现剧本根因时只返编问题场，锁定前场出口和后场入口；"
        "先做影响分析，只使受影响分镜与资产失效")
    review.setdefault("self_reviewed", False)

    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        defaults = _default_scene_logic(scene)
        logic = scene.get("director_logic")
        if not isinstance(logic, dict):
            logic = {}
            scene["director_logic"] = logic
        auto_filled = []
        for key, value in defaults.items():
            if not _text(logic.get(key)):
                logic[key] = value
                auto_filled.append(key)
        contract = logic.get("continuity_contract")
        if not isinstance(contract, dict):
            contract = {}
            logic["continuity_contract"] = contract
        for key, value in _default_continuity_contract(logic).items():
            if not _text(contract.get(key)):
                contract[key] = value
                auto_filled.append(f"continuity_contract.{key}")
        if auto_filled:
            logic["_auto_filled_fields"] = auto_filled
        else:
            logic.pop("_auto_filled_fields", None)
    script["script_logic_audit"] = audit_script_logic(script)
    return script


def audit_script_logic(script: dict) -> dict:
    """Check causal, physical, spatial and shootability facts before art."""
    issues: list[str] = []
    scene_reports: list[dict] = []
    prop_contract = audit_prop_contract(script)
    issues.extend(
        f"结构化道具合同：{issue}"
        for issue in prop_contract.get("issues") or [])
    declared = {
        str(item.get("name"))
        for item in (script.get("characters") or [])
        if isinstance(item, dict) and item.get("name")
    }
    review = script.get("adaptation_review")
    strict_director_review = bool(
        isinstance(review, dict) and review.get("self_reviewed") is True)
    previous_exit = ""
    seen_numbers = set()
    for position, scene in enumerate(script.get("scenes") or [], 1):
        if not isinstance(scene, dict):
            issues.append(f"第{position}场不是结构化场次")
            continue
        number = scene.get("scene_no", position)
        prefix = f"第{number}场"
        scene_issues: list[str] = []
        if number in seen_numbers:
            scene_issues.append("场次编号重复")
        seen_numbers.add(number)
        location = _text(scene.get("location"))
        action = _scene_action(scene)
        if not location:
            scene_issues.append("缺少可定位的场景地点")
        if strict_director_review and (
                action in VAGUE_ACTIONS or len(action) < 4):
            scene_issues.append("动作过于泛化，未写出可被镜头拍到的事件")
        lines = [
            line for line in (scene.get("lines") or [])
            if isinstance(line, dict)
        ]
        scene_people = {
            str(name) for name in (scene.get("characters") or []) if name
        }
        # 旁白/音效/字幕是声音来源,不是人物实体:不参与“角色必须已声明”
        # 与“台词人物须在本场名单内”的核对。否则模型把旁白正确写成声音
        # 来源反而校验失败,下一轮就会把旁白补进人物表来迎合校验。
        speakers = {
            str(line.get("character")) for line in lines
            if line.get("character")
            and not line.get("non_person_voice")
            and not is_non_person_label(line.get("character"))
        }
        unknown = sorted(
            name for name in ((scene_people | speakers) - declared)
            if not is_non_person_label(name))
        if unknown:
            scene_issues.append("出现未声明角色：" + "、".join(unknown))
        if not speakers <= scene_people:
            scene_issues.append("台词人物未列入本场人物名单")
        logic = scene.get("director_logic")
        if not isinstance(logic, dict):
            scene_issues.append("缺少导演级物理/空间改编")
            logic = {}
        for field in SCENE_LOGIC_FIELDS:
            if not _text(logic.get(field)):
                scene_issues.append(f"导演改编字段缺失：{field}")
        contract = logic.get("continuity_contract")
        if not isinstance(contract, dict):
            scene_issues.append("缺少局部返编连续性边界合同")
            contract = {}
        for field in CONTINUITY_CONTRACT_FIELDS:
            if not _text(contract.get(field)):
                scene_issues.append(f"连续性边界字段缺失：{field}")
        if strict_director_review and logic.get("_auto_filled_fields"):
            scene_issues.append(
                "AI 声明已完成导演自审，但导演改编字段仍由平台兜底："
                + "、".join(logic["_auto_filled_fields"]))
        if strict_director_review:
            placeholder_fields = [
                field for field in SCENE_LOGIC_FIELDS
                if any(cue in _text(logic.get(field))
                       for cue in PLACEHOLDER_CUES)
            ]
            if placeholder_fields:
                scene_issues.append(
                    "编剧仍留下未完成占位内容："
                    + "、".join(placeholder_fields))
        physical_actions = _text(logic.get("physical_actions"))
        if physical_actions in VAGUE_ACTIONS:
            scene_issues.append("物理动作仍是概括，没有动作链")
        abstract_only = any(word in f"{action} {physical_actions}" for word in (
            "觉得", "认为", "意识到", "思考", "回忆", "陷入", "决定",
            "感到", "局势紧张", "发生冲突", "展开交锋"))
        if (strict_director_review and abstract_only
                and not any(cue in f"{action} {physical_actions}"
                            for cue in PHYSICAL_CUES)):
            scene_issues.append("抽象心理/局势未外化为人物或道具的可见动作")
        entry = _text(logic.get("entry_state"))
        exit_state = _text(logic.get("exit_state"))
        if position > 1 and not entry:
            scene_issues.append("未说明如何承接上一场结束状态")
        if previous_exit and entry and entry == previous_exit:
            # Exact equality is allowed but should still be explicit; no issue.
            pass
        previous_exit = exit_state
        scene_reports.append({
            "scene_no": number,
            "passed": not scene_issues,
            "issues": scene_issues,
            "location": location,
            "action": action,
        })
        issues.extend(f"{prefix}：{issue}" for issue in scene_issues)

    if not isinstance(review, dict):
        issues.append("缺少 adaptation_review 编剧/导演自审")
    else:
        for field in ADAPTATION_REVIEW_FIELDS:
            if not _text(review.get(field)):
                issues.append(f"编剧/导演自审字段缺失：{field}")
    return {
        "schema": SCRIPT_LOGIC_SCHEMA,
        "prop_contract_schema": PROP_CONTRACT_SCHEMA,
        "prop_contract": prop_contract,
        "passed": not issues,
        "issues": issues,
        "scene_reports": scene_reports,
        "strict_director_review": strict_director_review,
        "legacy_compatible": not strict_director_review,
        "summary": (
            f"{len(scene_reports)}场均具备因果、人物信息、物理、时间、空间、"
            "道具生命周期和局部返编边界合同"
            if not issues else
            f"{len(scene_reports)}场中发现{len(issues)}项编剧/导演逻辑问题"),
    }


def script_logic_snapshot(script: dict) -> dict:
    """Return a detached report for UI/preflight storage."""
    normalized = copy.deepcopy(script or {})
    normalize_script_logic(normalized)
    return copy.deepcopy(normalized.get("script_logic_audit") or {})
