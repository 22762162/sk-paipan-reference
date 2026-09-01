"""Deterministic story-event graph contracts.

The graph is a clean-room AIFOS data contract.  It keeps source evidence,
world/realm state and visible dramatic beats close to the event that owns
them, while leaving model calls, persistence and production state to the
Director.  Every public function is pure: caller-owned script, graph and
storyboard objects are never mutated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA = "aifos.story-event-graph/v1"
HIGH_VALUE_SCHEMA = "aifos.high-value-events/v1"

EDGE_RELATIONS = (
    "causes",
    "enables",
    "reveals",
    "payoff_of",
    "continues",
    "contradicts",
    "temporal_next",
)

_CAUSAL_RELATIONS = frozenset(("causes", "enables"))
_SEQUENCE_RELATIONS = frozenset(("continues", "temporal_next"))
_HIGH_VALUE_CLASSES = frozenset(("high_value", "set_piece", "spectacle"))


class StoryEventGraphError(ValueError):
    """Raised when callers explicitly request a valid graph."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _positive_int(value: Any, default: int) -> int:
    number = _int(value, default)
    return number if number > 0 else default


def _json_value(value: Any) -> Any:
    """Return deterministic JSON data without falling back to ``repr``."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StoryEventGraphError("事件图不能包含 NaN 或 Infinity")
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StoryEventGraphError("事件图对象的 key 必须是字符串")
            result[key] = _json_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        rows = [_json_value(item) for item in value]
        return sorted(rows, key=_canonical_json)
    raise StoryEventGraphError(
        f"事件图只能包含 JSON 数据，收到 {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False)


def _stable_id(prefix: str, payload: Any) -> str:
    digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _unique_texts(value: Any) -> list[str]:
    if value in (None, ""):
        rows = []
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows = value
    else:
        rows = [value]
    return list(dict.fromkeys(
        text for text in (_text(item) for item in rows) if text))


def _evidence_rows(value: Any, *, fallback: Any = None) -> list[dict[str, Any]]:
    if value in (None, "", [], {}):
        value = fallback
    if value in (None, "", [], {}):
        rows: list[Any] = []
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows = list(value)
    else:
        rows = [value]
    result = []
    for raw in rows:
        if isinstance(raw, Mapping):
            row = {
                "source_id": _text(
                    raw.get("source_id") or raw.get("evidence_id")),
                "document_ref": _text(
                    raw.get("document_ref") or raw.get("source_document")),
                "chapter_id": _text(
                    raw.get("chapter_id") or raw.get("chapter")
                    or raw.get("chapter_index")),
                "scene_no": _int(raw.get("scene_no"), 0) or None,
                "span": copy.deepcopy(
                    raw.get("span") if isinstance(raw.get("span"), Mapping)
                    else {}),
                "quote": _text(
                    raw.get("quote") or raw.get("text")
                    or raw.get("excerpt") or raw.get("evidence")),
            }
        else:
            row = {
                "source_id": "",
                "document_ref": "",
                "chapter_id": "",
                "scene_no": None,
                "span": {},
                "quote": _text(raw),
            }
        if not row["source_id"]:
            row["source_id"] = _stable_id("evidence", {
                key: value for key, value in row.items()
                if key != "source_id"
            })
        result.append(row)
    result.sort(key=lambda row: (
        row["document_ref"], row["chapter_id"], row["scene_no"] or 0,
        _canonical_json(row["span"]), row["quote"], row["source_id"]))
    return result


def _normalize_beat(raw: Any, *, event_id: str, position: int) -> dict[str, Any]:
    beat = dict(raw) if isinstance(raw, Mapping) else {"visible_event": raw}
    description = _text(
        beat.get("visible_event") or beat.get("description")
        or beat.get("action") or beat.get("text"))
    order = _positive_int(beat.get("order"), position)
    beat_id = _text(beat.get("beat_id") or beat.get("event_beat_id"))
    if not beat_id:
        beat_id = _stable_id("beat", {
            "event_id": event_id,
            "order": order,
            "role": _text(beat.get("role") or beat.get("event_role")),
            "visible_event": description,
        })
    return {
        **copy.deepcopy(beat),
        "beat_id": beat_id,
        "order": order,
        "role": _text(beat.get("role") or beat.get("event_role")),
        "visible_event": description,
        "must_visualize": beat.get("must_visualize", True) is not False,
        "merge_allowed": bool(
            beat.get("merge_allowed") or beat.get("montage_merge_allowed")),
    }


def _node_identity_seed(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Use authored facts rather than list position so reordering is stable."""
    evidence = _evidence_rows(
        raw.get("source_evidence") or raw.get("evidence"))
    return {
        "source_evidence": evidence,
        "scene_no": _int(raw.get("scene_no"), 0) or None,
        "title": _text(raw.get("title") or raw.get("name")),
        "event_type": _text(raw.get("event_type") or raw.get("event_class")),
        "dramatic_question": _text(raw.get("dramatic_question")),
        "realm_id": _text(
            raw.get("realm_id") or raw.get("active_realm_id")),
        "era_context": _text(raw.get("era_context") or raw.get("era")),
        "participants": sorted(_unique_texts(
            raw.get("participants") or raw.get("characters"))),
        "props": sorted(_unique_texts(
            raw.get("props") or raw.get("prop_ids"))),
        "preconditions": copy.deepcopy(raw.get("preconditions") or []),
        "visible_beats": copy.deepcopy(
            raw.get("visible_beats") or raw.get("required_beats")
            or raw.get("event_beats") or []),
        "state_delta": copy.deepcopy(raw.get("state_delta") or {}),
    }


def _normalize_node(raw: Any) -> dict[str, Any]:
    node = dict(raw) if isinstance(raw, Mapping) else {"title": raw}
    event_id = _text(
        node.get("event_id") or node.get("id")
        or node.get("sequence_id") or node.get("dramatic_sequence_id"))
    if not event_id:
        event_id = _stable_id("event", _node_identity_seed(node))
    evidence = _evidence_rows(
        node.get("source_evidence") or node.get("evidence")
        or node.get("sources"))
    raw_beats = (
        node.get("visible_beats") or node.get("required_beats")
        or node.get("event_beats") or [])
    if not isinstance(raw_beats, Sequence) or isinstance(raw_beats, (str, bytes)):
        raw_beats = []
    beats = [
        _normalize_beat(beat, event_id=event_id, position=position)
        for position, beat in enumerate(raw_beats, 1)
    ]
    beats.sort(key=lambda beat: (beat["order"], beat["beat_id"]))
    event_class = _text(
        node.get("event_class") or node.get("event_type") or "story_event")
    high_value = bool(node.get("high_value")) or event_class.lower() in (
        _HIGH_VALUE_CLASSES)
    minimum = _positive_int(
        node.get("minimum_independent_shots") or node.get("minimum_shots"),
        3 if high_value else 1)
    return {
        **copy.deepcopy(node),
        "event_id": event_id,
        "title": _text(node.get("title") or node.get("name")),
        "event_class": event_class,
        "story_value": _text(node.get("story_value")),
        "dramatic_question": _text(node.get("dramatic_question")),
        "scene_no": _int(node.get("scene_no"), 0) or None,
        "sequence": _int(
            node.get("sequence") or node.get("event_order")
            or node.get("order"), 0) or None,
        "source_evidence": evidence,
        "realm_id": _text(
            node.get("realm_id") or node.get("active_realm_id")),
        "era_context": _text(node.get("era_context") or node.get("era")),
        "participants": sorted(_unique_texts(
            node.get("participants") or node.get("characters")
            or node.get("entities"))),
        "props": sorted(_unique_texts(
            node.get("props") or node.get("prop_ids")
            or node.get("objects"))),
        "preconditions": copy.deepcopy(
            node.get("preconditions")
            if isinstance(node.get("preconditions"), list) else []),
        "visible_beats": beats,
        "state_delta": copy.deepcopy(
            node.get("state_delta")
            if isinstance(node.get("state_delta"), Mapping) else {}),
        "high_value": high_value,
        "must_visualize": node.get("must_visualize", high_value) is not False,
        "minimum_independent_shots": minimum,
        "routine_montage_allowed": node.get(
            "routine_montage_allowed", True) is not False,
    }


def _normalize_edge(raw: Any) -> dict[str, Any]:
    edge = dict(raw) if isinstance(raw, Mapping) else {}
    source = _text(
        edge.get("from_event_id") or edge.get("from")
        or edge.get("source_event_id") or edge.get("source"))
    target = _text(
        edge.get("to_event_id") or edge.get("to")
        or edge.get("target_event_id") or edge.get("target"))
    relation = _text(edge.get("relation") or edge.get("type")).lower()
    edge_id = _text(edge.get("edge_id") or edge.get("id"))
    if not edge_id:
        edge_id = _stable_id("edge", {
            "from_event_id": source,
            "to_event_id": target,
            "relation": relation,
        })
    return {
        **copy.deepcopy(edge),
        "edge_id": edge_id,
        "from_event_id": source,
        "to_event_id": target,
        "relation": relation,
        "source_evidence": _evidence_rows(
            edge.get("source_evidence") or edge.get("evidence")),
    }


def _graph_fingerprint(graph: Mapping[str, Any]) -> str:
    payload = {
        key: copy.deepcopy(value) for key, value in graph.items()
        if key not in {"fingerprint", "validation"}
    }
    return "sha256:" + hashlib.sha256(
        _canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_story_event_graph(value: Any) -> dict[str, Any]:
    """Normalize aliases, ordering and IDs without inventing story content."""
    source = copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}
    raw_nodes = source.get("nodes") or source.get("events") or []
    raw_edges = source.get("edges") or source.get("relations") or []
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raw_nodes = []
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
        raw_edges = []
    nodes = [_normalize_node(node) for node in raw_nodes]
    nodes.sort(key=lambda node: (
        node.get("sequence") is None,
        node.get("sequence") or 0,
        node.get("scene_no") is None,
        node.get("scene_no") or 0,
        node["event_id"],
    ))
    edges = [_normalize_edge(edge) for edge in raw_edges]
    edges.sort(key=lambda edge: (
        edge["from_event_id"], edge["to_event_id"], edge["relation"],
        edge["edge_id"]))
    metadata = {
        key: copy.deepcopy(item)
        for key, item in source.items()
        if key not in {
            "edges", "events", "fingerprint", "graph_id", "nodes",
            "relations", "schema", "source", "validation",
        }
    }
    graph = {
        **metadata,
        "schema": SCHEMA,
        "graph_id": _text(source.get("graph_id")),
        "source": copy.deepcopy(
            source.get("source") if isinstance(source.get("source"), Mapping)
            else {}),
        "nodes": nodes,
        "edges": edges,
    }
    if not graph["graph_id"]:
        graph["graph_id"] = _stable_id("story-event-graph", {
            "source": graph["source"],
            "event_ids": sorted(node["event_id"] for node in nodes),
        })
    graph["fingerprint"] = _graph_fingerprint(graph)
    return graph


def _scene_evidence(scene: Mapping[str, Any], *, document_ref: str) -> dict[str, Any]:
    return {
        "document_ref": document_ref,
        "chapter_id": _text(
            scene.get("chapter_id") or scene.get("chapter_index")),
        "scene_no": _int(scene.get("scene_no"), 0) or None,
        "quote": _text(
            scene.get("action") or scene.get("description")
            or scene.get("story_function")),
    }


def _scene_node(scene: Mapping[str, Any], *, position: int,
                document_ref: str) -> dict[str, Any]:
    design = (
        scene.get("production_design")
        if isinstance(scene.get("production_design"), Mapping) else {})
    scene_no = _int(scene.get("scene_no"), position)
    return {
        "event_id": _text(scene.get("event_id")) or f"scene:{scene_no}",
        "title": _text(scene.get("title") or scene.get("location")),
        "event_class": "scene_event",
        "story_value": _text(
            scene.get("story_value") or design.get("story_function")),
        "dramatic_question": _text(scene.get("dramatic_question")),
        "scene_no": scene_no,
        "sequence": position,
        "source_evidence": [_scene_evidence(
            scene, document_ref=document_ref)],
        "realm_id": _text(
            scene.get("active_realm_id") or scene.get("realm_id")),
        "era_context": _text(
            scene.get("era_context") or scene.get("era")
            or design.get("era_context")),
        "participants": _unique_texts(scene.get("characters")),
        "props": _unique_texts(
            scene.get("props") or scene.get("prop_ids")),
        "preconditions": copy.deepcopy(
            scene.get("preconditions")
            if isinstance(scene.get("preconditions"), list) else []),
        "visible_beats": copy.deepcopy(
            scene.get("visible_beats")
            if isinstance(scene.get("visible_beats"), list) else []),
        "state_delta": copy.deepcopy(
            scene.get("state_delta")
            if isinstance(scene.get("state_delta"), Mapping) else {}),
        "must_visualize": bool(scene.get("must_visualize", False)),
    }


def _high_value_node(event: Mapping[str, Any], *, scene: Mapping[str, Any],
                     document_ref: str) -> dict[str, Any]:
    scene_no = _int(event.get("scene_no"), 0) or None
    evidence = event.get("source_evidence") or event.get("evidence")
    if not evidence and scene:
        evidence = [_scene_evidence(scene, document_ref=document_ref)]
    return {
        **copy.deepcopy(dict(event)),
        "event_class": _text(event.get("event_class")) or "high_value",
        "high_value": True,
        "must_visualize": event.get("must_visualize", True) is not False,
        "scene_no": scene_no,
        "source_evidence": evidence or [],
        "realm_id": _text(
            event.get("realm_id") or event.get("active_realm_id")
            or scene.get("active_realm_id") or scene.get("realm_id")),
        "era_context": _text(
            event.get("era_context") or event.get("era")
            or scene.get("era_context") or scene.get("era")),
        "participants": (
            event.get("participants") or event.get("characters")
            or scene.get("characters") or []),
        "props": (
            event.get("props") or event.get("prop_ids")
            or scene.get("props") or scene.get("prop_ids") or []),
        "visible_beats": (
            event.get("visible_beats") or event.get("required_beats") or []),
    }


def _derived_temporal_edges(nodes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        (node for node in nodes if node.get("sequence") is not None),
        key=lambda node: (node["sequence"], node["event_id"]))
    result = []
    for left, right in zip(ordered, ordered[1:]):
        if left["sequence"] == right["sequence"]:
            continue
        result.append({
            "from_event_id": left["event_id"],
            "to_event_id": right["event_id"],
            "relation": "temporal_next",
        })
    return result


def build_story_event_graph(
    script: Any,
    *,
    project_id: Any = "",
    episode_id: Any = "",
    source_document_ref: Any = "script",
    source_version: Any = "",
    derive_temporal_edges: bool = True,
) -> dict[str, Any]:
    """Build a graph from explicit events, scenes and high-value contracts.

    Explicit ``story_events``/``events`` are preferred.  Scenes are used as
    event nodes only when no general event list exists.  High-value contracts
    are then added or merged by stable ``event_id``.  No unseen dramatic beat
    is generated.
    """
    value = copy.deepcopy(dict(script)) if isinstance(script, Mapping) else {}
    embedded = value.get("story_event_graph")
    if isinstance(embedded, Mapping):
        graph = copy.deepcopy(dict(embedded))
    else:
        document_ref = _text(source_document_ref) or "script"
        scenes = [
            scene for scene in value.get("scenes") or []
            if isinstance(scene, Mapping)
        ]
        scene_by_no = {
            _int(scene.get("scene_no"), position): scene
            for position, scene in enumerate(scenes, 1)
        }
        raw_events = value.get("story_events") or value.get("events") or []
        if isinstance(raw_events, Sequence) and not isinstance(
                raw_events, (str, bytes)) and raw_events:
            nodes = [copy.deepcopy(dict(event)) for event in raw_events
                     if isinstance(event, Mapping)]
        else:
            nodes = [
                _scene_node(scene, position=position,
                            document_ref=document_ref)
                for position, scene in enumerate(scenes, 1)
            ]
        temporal_nodes = [_normalize_node(node) for node in nodes]
        node_by_id = {
            _text(node.get("event_id") or node.get("id")): index
            for index, node in enumerate(nodes)
            if _text(node.get("event_id") or node.get("id"))
        }
        for event in value.get("high_value_events") or []:
            if not isinstance(event, Mapping):
                continue
            event_id = _text(
                event.get("event_id") or event.get("sequence_id")
                or event.get("dramatic_sequence_id"))
            scene = scene_by_no.get(_int(event.get("scene_no"), 0), {})
            high_value = _high_value_node(
                event, scene=scene, document_ref=document_ref)
            if event_id and event_id in node_by_id:
                existing = nodes[node_by_id[event_id]]
                nodes[node_by_id[event_id]] = {**existing, **high_value}
            else:
                nodes.append(high_value)
                if event_id:
                    node_by_id[event_id] = len(nodes) - 1
        raw_edges = (
            value.get("story_event_edges") or value.get("event_edges") or [])
        edges = [copy.deepcopy(dict(edge)) for edge in raw_edges
                 if isinstance(edge, Mapping)]
        normalized_nodes = [_normalize_node(node) for node in nodes]
        if derive_temporal_edges:
            existing = {
                (_text(edge.get("from_event_id") or edge.get("from")),
                 _text(edge.get("to_event_id") or edge.get("to")),
                 _text(edge.get("relation") or edge.get("type")).lower())
                for edge in edges
            }
            for edge in _derived_temporal_edges(temporal_nodes):
                signature = (
                    edge["from_event_id"], edge["to_event_id"],
                    edge["relation"])
                if signature not in existing:
                    edges.append(edge)
        graph = {"nodes": normalized_nodes, "edges": edges}
    graph["source"] = {
        **(copy.deepcopy(graph.get("source"))
           if isinstance(graph.get("source"), Mapping) else {}),
        "project_id": _text(project_id),
        "episode_id": _text(episode_id),
        "document_ref": _text(source_document_ref),
        "document_version": _text(source_version),
    }
    if project_id or episode_id:
        graph.setdefault(
            "graph_id",
            "story-event-graph:"
            f"{_text(project_id) or 'project'}:{_text(episode_id) or 'episode'}")
    return normalize_story_event_graph(graph)


def _duplicate_values(values: Sequence[str]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return sorted(value for value, count in counts.items() if count > 1)


def _has_cycle(nodes: Sequence[str], edges: Sequence[tuple[str, str]]) -> bool:
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {node: 0 for node in nodes}
    for source, target in edges:
        if source not in indegree or target not in indegree:
            continue
        outgoing[source].append(target)
        indegree[target] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        node = ready.pop(0)
        visited += 1
        for target in sorted(outgoing.get(node, ())):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return visited != len(nodes)


def validate_story_event_graph(value: Any) -> list[str]:
    """Return stable authoring errors; an empty list means structurally valid."""
    graph = normalize_story_event_graph(value)
    issues: list[str] = []
    nodes = graph["nodes"]
    edges = graph["edges"]
    event_ids = [node["event_id"] for node in nodes]
    for event_id in _duplicate_values(event_ids):
        issues.append(f"事件 event_id 重复: {event_id}")
    known = set(event_ids)
    for node in nodes:
        event_id = node["event_id"]
        if not event_id:
            issues.append("事件缺少 event_id")
        if not node["source_evidence"]:
            issues.append(f"事件 {event_id} 缺少 source_evidence")
        else:
            for evidence in node["source_evidence"]:
                if not any((
                        evidence.get("document_ref"),
                        evidence.get("chapter_id"),
                        evidence.get("scene_no"),
                        evidence.get("quote"))):
                    issues.append(
                        f"事件 {event_id} 的来源证据"
                        f" {evidence['source_id']} 无定位信息")
        beat_ids = [beat["beat_id"] for beat in node["visible_beats"]]
        for beat_id in _duplicate_values(beat_ids):
            issues.append(f"事件 {event_id} 的 beat_id 重复: {beat_id}")
        orders = [beat["order"] for beat in node["visible_beats"]]
        for order in _duplicate_values([str(item) for item in orders]):
            issues.append(f"事件 {event_id} 的节拍顺序重复: {order}")
        if orders and orders != list(range(1, len(orders) + 1)):
            issues.append(f"事件 {event_id} 的 visible_beats.order 必须从1连续递增")
        for beat in node["visible_beats"]:
            if beat["must_visualize"] and not beat["visible_event"]:
                issues.append(
                    f"事件 {event_id} 的必看节拍 {beat['beat_id']} 缺少可见事件")
        if node["high_value"] and node["must_visualize"]:
            must_beats = [
                beat for beat in node["visible_beats"]
                if beat["must_visualize"]
            ]
            if not node["dramatic_question"]:
                issues.append(f"高价值事件 {event_id} 缺少 dramatic_question")
            if len(must_beats) < node["minimum_independent_shots"]:
                issues.append(
                    f"高价值事件 {event_id} 至少需要"
                    f"{node['minimum_independent_shots']}个必看节拍，"
                    f"当前只有{len(must_beats)}个")
    edge_ids = [edge["edge_id"] for edge in edges]
    for edge_id in _duplicate_values(edge_ids):
        issues.append(f"事件 edge_id 重复: {edge_id}")
    for edge in edges:
        edge_id = edge["edge_id"]
        source = edge["from_event_id"]
        target = edge["to_event_id"]
        relation = edge["relation"]
        if relation not in EDGE_RELATIONS:
            issues.append(f"事件边 {edge_id} 使用未知关系: {relation or '空'}")
        if not source or not target:
            issues.append(f"事件边 {edge_id} 缺少起点或终点")
            continue
        if source == target:
            issues.append(f"事件边 {edge_id} 不允许自环: {source}")
        if source not in known:
            issues.append(f"事件边 {edge_id} 起点悬空: {source}")
        if target not in known:
            issues.append(f"事件边 {edge_id} 终点悬空: {target}")
    causal_edges = [
        (edge["from_event_id"], edge["to_event_id"])
        for edge in edges if edge["relation"] in _CAUSAL_RELATIONS
    ]
    if _has_cycle(sorted(known), causal_edges):
        issues.append("事件图 causes/enables 存在非法因果环")
    sequence_edges = [
        (edge["from_event_id"], edge["to_event_id"])
        for edge in edges if edge["relation"] in _SEQUENCE_RELATIONS
    ]
    if _has_cycle(sorted(known), sequence_edges):
        issues.append("事件图 continues/temporal_next 存在非法时序环")
    return list(dict.fromkeys(issues))


def require_valid_story_event_graph(value: Any) -> dict[str, Any]:
    """Return a normalized graph or raise one concise contract error."""
    graph = normalize_story_event_graph(value)
    issues = validate_story_event_graph(graph)
    if issues:
        raise StoryEventGraphError("；".join(issues))
    return graph


def project_high_value_events(value: Any) -> list[dict[str, Any]]:
    """Project graph nodes into the existing high-value authoring vocabulary."""
    graph = normalize_story_event_graph(value)
    events = []
    for node in graph["nodes"]:
        if not node["high_value"]:
            continue
        events.append({
            "event_id": node["event_id"],
            "scene_no": node["scene_no"],
            "event_class": "high_value",
            "story_value": node["story_value"],
            "dramatic_question": node["dramatic_question"],
            "must_visualize": node["must_visualize"],
            "minimum_independent_shots": node["minimum_independent_shots"],
            "routine_montage_allowed": node["routine_montage_allowed"],
            "required_beats": [
                {
                    "beat_id": beat["beat_id"],
                    "order": beat["order"],
                    "role": beat["role"],
                    "visible_event": beat["visible_event"],
                    "must_visualize": beat["must_visualize"],
                    "merge_allowed": beat["merge_allowed"],
                }
                for beat in node["visible_beats"]
            ],
            "source_evidence": copy.deepcopy(node["source_evidence"]),
            "realm_id": node["realm_id"],
            "era_context": node["era_context"],
            "participants": list(node["participants"]),
            "props": list(node["props"]),
            "preconditions": copy.deepcopy(node["preconditions"]),
            "state_delta": copy.deepcopy(node["state_delta"]),
        })
    return events


def _shot_number(shot: Mapping[str, Any], position: int) -> int:
    return _positive_int(shot.get("shot_no"), position)


def _shot_event_id(shot: Mapping[str, Any]) -> str:
    return _text(
        shot.get("high_value_event_id") or shot.get("dramatic_sequence_id")
        or shot.get("special_sequence_id"))


def _shot_beat_ids(shot: Mapping[str, Any]) -> list[str]:
    values = shot.get("event_beat_ids")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = []
    singular = shot.get("event_beat_id") or shot.get("sequence_beat_id")
    if singular:
        values = [singular, *values]
    return _unique_texts(values)


def supervise_story_event_coverage(
    graph: Any,
    storyboard: Any,
) -> dict[str, Any]:
    """Supervise authored high-value beats against independent shots.

    This function reports repair facts only.  It neither mutates the
    storyboard nor decides whether the wider production should stop.
    """
    normalized = normalize_story_event_graph(graph)
    graph_issues = validate_story_event_graph(normalized)
    shots = [
        shot for shot in (
            storyboard.get("shots") if isinstance(storyboard, Mapping) else []
        ) or [] if isinstance(shot, Mapping)
    ]
    rows = []
    issues = list(graph_issues)
    for event in project_high_value_events(normalized):
        if not event["must_visualize"]:
            continue
        event_id = event["event_id"]
        matched = [
            (position, shot) for position, shot in enumerate(shots, 1)
            if _shot_event_id(shot) == event_id
        ]
        event_issues: list[str] = []
        shot_nos = [_shot_number(shot, position) for position, shot in matched]
        if len(set(shot_nos)) != len(shot_nos):
            event_issues.append(f"高价值事件 {event_id} 存在重复 shot_no")
        required = [
            beat for beat in event["required_beats"]
            if beat["must_visualize"]
        ]
        required_ids = [beat["beat_id"] for beat in required]
        known_ids = {beat["beat_id"] for beat in event["required_beats"]}
        beat_to_shots: dict[str, list[int]] = defaultdict(list)
        unknown_beat_ids: list[str] = []
        for position, shot in matched:
            shot_no = _shot_number(shot, position)
            beat_ids = _shot_beat_ids(shot)
            for beat_id in beat_ids:
                if beat_id not in known_ids:
                    unknown_beat_ids.append(beat_id)
                else:
                    beat_to_shots[beat_id].append(shot_no)
            if event["scene_no"] is not None and _int(
                    shot.get("scene_no"), 0) != event["scene_no"]:
                event_issues.append(
                    f"高价值事件 {event_id} 的镜头{shot_no}落在错误场次")
            if shot.get("must_visualize") is not True:
                event_issues.append(
                    f"高价值事件 {event_id} 的镜头{shot_no}未标 must_visualize:true")
            if shot.get("must_preserve") is not True:
                event_issues.append(
                    f"高价值事件 {event_id} 的镜头{shot_no}未标 must_preserve:true")
            if shot.get("foldable_into_long_take") is not False:
                event_issues.append(
                    f"高价值事件 {event_id} 的镜头{shot_no}仍允许折入长镜头")
            if shot.get("folded_into_long_take") is True:
                event_issues.append(
                    f"高价值事件 {event_id} 的镜头{shot_no}已被折入长镜头")
            if len(beat_ids) > 1 and not event["routine_montage_allowed"]:
                event_issues.append(
                    f"高价值事件 {event_id} 禁止把多个节拍合入镜头{shot_no}")
        missing = [beat_id for beat_id in required_ids if not beat_to_shots[beat_id]]
        if missing:
            event_issues.append(
                f"高价值事件 {event_id} 缺少必看节拍:"
                + "、".join(missing))
        if unknown_beat_ids:
            event_issues.append(
                f"高价值事件 {event_id} 引用了未知节拍:"
                + "、".join(sorted(set(unknown_beat_ids))))
        for beat in required:
            if beat["merge_allowed"]:
                continue
            shared = set(beat_to_shots[beat["beat_id"]])
            for other in required:
                if other["beat_id"] == beat["beat_id"] or other["merge_allowed"]:
                    continue
                if shared.intersection(beat_to_shots[other["beat_id"]]):
                    event_issues.append(
                        f"高价值事件 {event_id} 的必看节拍"
                        f"{beat['beat_id']}与{other['beat_id']}必须使用独立镜头")
        covered_in_order = [
            min(beat_to_shots[beat_id])
            for beat_id in required_ids if beat_to_shots[beat_id]
        ]
        if covered_in_order != sorted(covered_in_order):
            event_issues.append(f"高价值事件 {event_id} 的必看节拍镜头顺序错误")
        independent_count = len(set(shot_nos))
        if independent_count < event["minimum_independent_shots"]:
            event_issues.append(
                f"高价值事件 {event_id} 至少需要"
                f"{event['minimum_independent_shots']}个独立镜头，"
                f"当前只有{independent_count}个")
        issues.extend(event_issues)
        rows.append({
            "event_id": event_id,
            "scene_no": event["scene_no"],
            "realm_id": event["realm_id"],
            "minimum_independent_shots": event["minimum_independent_shots"],
            "shot_nos": shot_nos,
            "required_beat_ids": required_ids,
            "covered_beat_ids": [
                beat_id for beat_id in required_ids if beat_to_shots[beat_id]
            ],
            "missing_beat_ids": missing,
            "unknown_beat_ids": sorted(set(unknown_beat_ids)),
            "passed": not event_issues,
            "issues": list(dict.fromkeys(event_issues)),
            "repair_scope": {
                "event_id": event_id,
                "scene_no": event["scene_no"],
                "missing_beat_ids": missing,
                "preserve_other_events": True,
            },
        })
    issues = list(dict.fromkeys(issues))
    return {
        "schema": "aifos.story-event-supervision/v1",
        "kind": "review",
        "production_blocking": False,
        "graph_schema": SCHEMA,
        "graph_fingerprint": normalized["fingerprint"],
        "passed": not issues,
        "declared_high_value_event_count": len(rows),
        "events": rows,
        "issues": issues,
    }


audit_story_event_coverage = supervise_story_event_coverage


__all__ = [
    "EDGE_RELATIONS",
    "HIGH_VALUE_SCHEMA",
    "SCHEMA",
    "StoryEventGraphError",
    "audit_story_event_coverage",
    "build_story_event_graph",
    "normalize_story_event_graph",
    "project_high_value_events",
    "require_valid_story_event_graph",
    "supervise_story_event_coverage",
    "validate_story_event_graph",
]
