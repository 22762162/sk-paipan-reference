import copy
import unittest

from aifos.story_event_graph import (
    EDGE_RELATIONS,
    SCHEMA,
    StoryEventGraphError,
    build_story_event_graph,
    normalize_story_event_graph,
    project_high_value_events,
    require_valid_story_event_graph,
    supervise_story_event_coverage,
    validate_story_event_graph,
)


def _evidence(scene_no=1, quote="事件发生"):
    return {
        "document_ref": "episode-1-script",
        "chapter_id": "chapter-1",
        "scene_no": scene_no,
        "quote": quote,
    }


def _event(event_id, *, sequence=1, scene_no=1):
    return {
        "event_id": event_id,
        "sequence": sequence,
        "scene_no": scene_no,
        "title": event_id,
        "source_evidence": [_evidence(scene_no, event_id)],
    }


def _high_value_event(
    event_id="game-draw",
    *,
    minimum_independent_shots=3,
    routine_montage_allowed=False,
):
    return {
        **_event(event_id, scene_no=2),
        "event_class": "high_value",
        "dramatic_question": "她能否抽到改变命运的天赋？",
        "must_visualize": True,
        "minimum_independent_shots": minimum_independent_shots,
        "routine_montage_allowed": routine_montage_allowed,
        "realm_id": "reality:hotel-room",
        "era_context": "2078年现代现实",
        "participants": ["虞寻歌"],
        "props": ["同一部手机"],
        "preconditions": [{"fact": "02:22:00后游戏开启"}],
        "state_delta": {"talent": {"from": None, "to": "SS级盗神"}},
        "visible_beats": [
            {
                "beat_id": "create",
                "order": 1,
                "role": "setup",
                "visible_event": "创建亡灵青年男性角色",
                "must_visualize": True,
            },
            {
                "beat_id": "draw",
                "order": 2,
                "role": "reveal",
                "visible_event": "抽取界面揭示SS级盗神",
                "must_visualize": True,
            },
            {
                "beat_id": "react",
                "order": 3,
                "role": "payoff",
                "visible_event": "虞寻歌确认结果并作出反应",
                "must_visualize": True,
            },
        ],
    }


def _shot(shot_no, beat_id, *, scene_no=2, event_id="game-draw"):
    return {
        "shot_no": shot_no,
        "scene_no": scene_no,
        "high_value_event_id": event_id,
        "event_beat_id": beat_id,
        "must_visualize": True,
        "must_preserve": True,
        "foldable_into_long_take": False,
    }


class StoryEventGraphNormalizationTests(unittest.TestCase):
    def test_normalization_is_pure_stable_and_order_independent(self):
        raw = {
            "events": [
                {
                    "title": "结果",
                    "sequence": 2,
                    "source_evidence": [_evidence(2, "结果出现")],
                },
                {
                    "title": "选择",
                    "sequence": 1,
                    "source_evidence": [_evidence(1, "作出选择")],
                },
            ],
        }
        untouched = copy.deepcopy(raw)

        first = normalize_story_event_graph(raw)
        reversed_graph = normalize_story_event_graph(
            {"events": list(reversed(raw["events"]))}
        )

        self.assertEqual(raw, untouched)
        self.assertEqual(first["schema"], SCHEMA)
        self.assertEqual(first["fingerprint"], reversed_graph["fingerprint"])
        self.assertEqual(first["graph_id"], reversed_graph["graph_id"])
        self.assertEqual(
            [node["sequence"] for node in first["nodes"]],
            [1, 2],
        )
        self.assertTrue(
            all(node["event_id"].startswith("event:") for node in first["nodes"])
        )

    def test_aliases_and_all_relationships_normalize(self):
        nodes = [_event("a"), _event("b", sequence=2)]
        graph = {
            "events": nodes,
            "relations": [
                {"from": "a", "to": "b", "type": relation}
                for relation in EDGE_RELATIONS
            ],
        }

        normalized = normalize_story_event_graph(graph)

        self.assertEqual(
            {edge["relation"] for edge in normalized["edges"]},
            set(EDGE_RELATIONS),
        )
        self.assertEqual(validate_story_event_graph(normalized), [])

    def test_generated_ids_distinguish_events_with_different_visible_beats(self):
        shared = {
            "title": "同场事件",
            "scene_no": 1,
            "source_evidence": [_evidence(1, "同一段来源")],
        }
        graph = normalize_story_event_graph({"nodes": [
            {**shared, "visible_beats": [{"visible_event": "角色举起手机"}]},
            {**shared, "visible_beats": [{"visible_event": "屏幕揭示结果"}]},
        ]})

        self.assertEqual(len({node["event_id"] for node in graph["nodes"]}), 2)

    def test_must_visualize_alone_does_not_reclassify_routine_event(self):
        event = {
            **_event("routine"),
            "must_visualize": True,
            "visible_beats": [
                {"visible_event": "拿起杯子"},
                {"visible_event": "喝一口水"},
            ],
        }

        graph = normalize_story_event_graph({"nodes": [event]})

        self.assertFalse(graph["nodes"][0]["high_value"])
        self.assertEqual(project_high_value_events(graph), [])

    def test_require_valid_returns_normalized_graph_or_raises(self):
        valid = require_valid_story_event_graph({"nodes": [_event("a")]})
        self.assertEqual(valid["schema"], SCHEMA)

        with self.assertRaisesRegex(StoryEventGraphError, "source_evidence"):
            require_valid_story_event_graph(
                {"nodes": [{"event_id": "a", "source_evidence": []}]}
            )


class StoryEventGraphBuildTests(unittest.TestCase):
    def test_builds_scene_events_and_high_value_projection(self):
        script = {
            "scenes": [
                {
                    "scene_no": 1,
                    "event_id": "arrival",
                    "location": "酒店房间",
                    "action": "虞寻歌抵达房间并等待游戏开启。",
                    "active_realm_id": "reality:hotel-room",
                    "era_context": "2078年现代现实",
                    "characters": ["虞寻歌"],
                },
                {
                    "scene_no": 2,
                    "event_id": "game-start",
                    "location": "酒店房间",
                    "action": "02:22:00到达，手机游戏正式开启。",
                    "active_realm_id": "reality:hotel-room",
                    "era_context": "2078年现代现实",
                    "characters": ["虞寻歌"],
                    "props": ["同一部手机"],
                },
            ],
            "high_value_events": [_high_value_event()],
        }

        graph = build_story_event_graph(
            script,
            project_id="p1",
            episode_id="e1",
            source_document_ref="episode-1-script",
            source_version="v3",
        )

        self.assertEqual(graph["graph_id"], "story-event-graph:p1:e1")
        self.assertEqual(graph["source"]["document_version"], "v3")
        self.assertEqual(validate_story_event_graph(graph), [])
        self.assertIn(
            ("arrival", "game-start", "temporal_next"),
            {
                (
                    edge["from_event_id"],
                    edge["to_event_id"],
                    edge["relation"],
                )
                for edge in graph["edges"]
            },
        )

        projected = project_high_value_events(graph)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["event_id"], "game-draw")
        self.assertEqual(projected[0]["realm_id"], "reality:hotel-room")
        self.assertEqual(projected[0]["participants"], ["虞寻歌"])
        self.assertEqual(projected[0]["props"], ["同一部手机"])
        self.assertEqual(
            projected[0]["state_delta"]["talent"]["to"], "SS级盗神"
        )
        self.assertEqual(
            [beat["beat_id"] for beat in projected[0]["required_beats"]],
            ["create", "draw", "react"],
        )

    def test_explicit_events_win_over_implicit_scene_nodes(self):
        script = {
            "scenes": [{"scene_no": 1, "action": "只作为证据来源的场景"}],
            "story_events": [_event("authored-event")],
        }

        graph = build_story_event_graph(script)

        self.assertEqual(
            [node["event_id"] for node in graph["nodes"]],
            ["authored-event"],
        )

    def test_build_does_not_mutate_script(self):
        script = {
            "scenes": [{"scene_no": 1, "action": "开场"}],
            "high_value_events": [_high_value_event()],
        }
        untouched = copy.deepcopy(script)

        build_story_event_graph(script)

        self.assertEqual(script, untouched)


class StoryEventGraphValidationTests(unittest.TestCase):
    def test_reports_dangling_self_loop_and_unknown_relation(self):
        graph = {
            "nodes": [_event("a"), _event("b", sequence=2)],
            "edges": [
                {"from_event_id": "a", "to_event_id": "a", "relation": "causes"},
                {"from_event_id": "missing", "to_event_id": "b", "relation": "reveals"},
                {"from_event_id": "a", "to_event_id": "b", "relation": "teleports"},
            ],
        }

        issues = validate_story_event_graph(graph)

        self.assertTrue(any("不允许自环" in issue for issue in issues))
        self.assertTrue(any("起点悬空" in issue for issue in issues))
        self.assertTrue(any("未知关系" in issue for issue in issues))

    def test_reports_causal_and_temporal_cycles(self):
        graph = {
            "nodes": [_event("a"), _event("b", sequence=2)],
            "edges": [
                {"from_event_id": "a", "to_event_id": "b", "relation": "causes"},
                {"from_event_id": "b", "to_event_id": "a", "relation": "enables"},
                {"from_event_id": "a", "to_event_id": "b", "relation": "continues"},
                {"from_event_id": "b", "to_event_id": "a", "relation": "temporal_next"},
            ],
        }

        issues = validate_story_event_graph(graph)

        self.assertIn("事件图 causes/enables 存在非法因果环", issues)
        self.assertIn("事件图 continues/temporal_next 存在非法时序环", issues)

    def test_reports_high_value_beat_order_and_minimum_expansion(self):
        event = _high_value_event(minimum_independent_shots=4)
        event["visible_beats"][1]["order"] = 3
        event["visible_beats"][2]["order"] = 5

        issues = validate_story_event_graph({"nodes": [event]})

        self.assertTrue(any("从1连续递增" in issue for issue in issues))
        self.assertTrue(any("至少需要4个必看节拍" in issue for issue in issues))

    def test_reports_duplicate_event_and_beat_identifiers(self):
        event = _high_value_event()
        event["visible_beats"][1]["beat_id"] = "create"
        graph = {"nodes": [event, _event("game-draw", sequence=2)]}

        issues = validate_story_event_graph(graph)

        self.assertTrue(any("event_id 重复" in issue for issue in issues))
        self.assertTrue(any("beat_id 重复" in issue for issue in issues))


class StoryEventGraphCoverageTests(unittest.TestCase):
    def setUp(self):
        self.graph = normalize_story_event_graph(
            {"nodes": [_high_value_event()]}
        )

    def test_passes_when_required_beats_have_independent_preserved_shots(self):
        storyboard = {
            "shots": [
                _shot(10, "create"),
                _shot(11, "draw"),
                _shot(12, "react"),
            ]
        }

        report = supervise_story_event_coverage(self.graph, storyboard)

        self.assertTrue(report["passed"])
        self.assertFalse(report["production_blocking"])
        self.assertEqual(report["declared_high_value_event_count"], 1)
        self.assertEqual(report["events"][0]["covered_beat_ids"], [
            "create", "draw", "react"
        ])
        self.assertEqual(report["issues"], [])

    def test_reports_missing_unknown_order_scene_and_preservation_errors(self):
        wrong_scene = _shot(5, "react", scene_no=9)
        wrong_scene["must_visualize"] = False
        wrong_scene["must_preserve"] = False
        wrong_scene["foldable_into_long_take"] = True
        wrong_scene["folded_into_long_take"] = True
        storyboard = {
            "shots": [
                wrong_scene,
                _shot(6, "create"),
                _shot(7, "unknown"),
            ]
        }

        report = supervise_story_event_coverage(self.graph, storyboard)
        issues = report["events"][0]["issues"]

        self.assertFalse(report["passed"])
        self.assertFalse(report["production_blocking"])
        self.assertTrue(any("错误场次" in issue for issue in issues))
        self.assertTrue(any("未标 must_visualize" in issue for issue in issues))
        self.assertTrue(any("未标 must_preserve" in issue for issue in issues))
        self.assertTrue(any("仍允许折入长镜头" in issue for issue in issues))
        self.assertTrue(any("已被折入长镜头" in issue for issue in issues))
        self.assertTrue(any("缺少必看节拍:draw" in issue for issue in issues))
        self.assertTrue(any("引用了未知节拍:unknown" in issue for issue in issues))
        self.assertTrue(any("镜头顺序错误" in issue for issue in issues))
        self.assertEqual(
            report["events"][0]["repair_scope"]["missing_beat_ids"],
            ["draw"],
        )

    def test_reports_merged_non_mergeable_beats_and_too_few_shots(self):
        merged = _shot(1, "create")
        merged["event_beat_ids"] = ["draw", "react"]
        storyboard = {"shots": [merged]}

        report = supervise_story_event_coverage(self.graph, storyboard)
        issues = report["events"][0]["issues"]

        self.assertTrue(any("禁止把多个节拍合入镜头" in issue for issue in issues))
        self.assertTrue(any("必须使用独立镜头" in issue for issue in issues))
        self.assertTrue(any("至少需要3个独立镜头" in issue for issue in issues))

    def test_allows_explicit_merge_only_when_contract_allows_it(self):
        event = _high_value_event(
            minimum_independent_shots=2,
            routine_montage_allowed=True,
        )
        event["visible_beats"][1]["merge_allowed"] = True
        graph = normalize_story_event_graph({"nodes": [event]})
        first = _shot(1, "create")
        first["event_beat_ids"] = ["draw"]
        storyboard = {"shots": [first, _shot(2, "react")]}

        report = supervise_story_event_coverage(graph, storyboard)

        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
