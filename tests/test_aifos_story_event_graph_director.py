import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aifos.director import Director
from aifos.production.base import ProviderResult
from aifos.story_event_graph import build_story_event_graph
from aifos.workflow import PIPELINE_VERSION, STORYBOARD_ENRICHMENT_VERSION


def _script():
    return {
        "title": "游戏入侵",
        "scenes": [{
            "scene_no": 2,
            "location": "酒店房间",
            "action": "02:22:00到达后，手机游戏正式开启。",
            "active_realm_id": "reality:hotel-room",
            "era_context": "2078年现代现实",
            "characters": ["虞寻歌"],
            "props": ["同一部手机"],
        }],
        "high_value_events": [{
            "event_id": "game-draw",
            "scene_no": 2,
            "dramatic_question": "她能否抽到改变命运的天赋？",
            "minimum_independent_shots": 3,
            "must_visualize": True,
            "routine_montage_allowed": False,
            "required_beats": [
                {
                    "beat_id": "create",
                    "order": 1,
                    "visible_event": "创建亡灵青年男性角色",
                },
                {
                    "beat_id": "draw",
                    "order": 2,
                    "visible_event": "抽取界面揭示SS级盗神",
                },
                {
                    "beat_id": "react",
                    "order": 3,
                    "visible_event": "虞寻歌确认结果并作出反应",
                },
            ],
        }],
    }


def _context(out_root=None):
    return {
        "project": {
            "id": "project-1",
            "title": "游戏入侵",
            "style": "电影感真人漫剧",
            "kind": "drama",
        },
        "episode": {
            "id": "episode-1",
            "number": 1,
            "premise": "女主在游戏降临前夜重生。",
        },
        "force": False,
        "out_root": Path(out_root) if out_root is not None else Path("."),
    }


class _Projects:
    def __init__(self):
        self.documents = {}
        self.versions = {}
        self.save_calls = []

    def seed(self, episode_id, kind, document, version):
        key = (episode_id, kind)
        self.documents[key] = copy.deepcopy(document)
        self.versions[key] = version

    def latest_document(self, episode_id, kind):
        key = (episode_id, kind)
        document = self.documents.get(key)
        return copy.deepcopy(document), self.versions.get(key, 0)

    def save_document(self, episode_id, kind, document):
        key = (episode_id, kind)
        version = self.versions.get(key, 0) + 1
        self.documents[key] = copy.deepcopy(document)
        self.versions[key] = version
        self.save_calls.append((episode_id, kind, copy.deepcopy(document)))
        return version


class _Log:
    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, *args):
        self.infos.append(args)

    def warn(self, *args):
        self.warnings.append(args)


class _Assets:
    def __init__(self):
        self.registered = []

    def register(self, *args, **kwargs):
        self.registered.append((args, kwargs))


def _director(projects=None):
    director = Director.__new__(Director)
    director.projects = projects or _Projects()
    director.log = _Log()
    return director


class DirectorStoryEventGraphScriptTests(unittest.TestCase):
    def test_script_stage_persists_then_reuses_same_event_graph_version(self):
        projects = _Projects()
        projects.seed("episode-1", "script", _script(), 7)
        director = _director(projects)
        director._normalize_script_character_profiles = lambda *_args, **_kwargs: None
        director._persist_script_review = lambda *_args, **_kwargs: 0

        def ensure_story_analysis(ctx, force=False):
            analysis = {"world": {"name": "2078现实世界"}}
            ctx["story_analysis"] = analysis
            ctx["story_analysis_version"] = 3
            return analysis, 3, not force

        director._ensure_story_analysis = ensure_story_analysis

        first_ctx = _context()
        first = director._stage_script(first_ctx)
        second_ctx = _context()
        second = director._stage_script(second_ctx)

        graph_saves = [
            call for call in projects.save_calls if call[1] == "story_event_graph"
        ]
        self.assertEqual(len(graph_saves), 1)
        self.assertEqual(first["story_event_graph_version"], 1)
        self.assertEqual(second["story_event_graph_version"], 1)
        self.assertEqual(first_ctx["story_event_graph_version"], 1)
        self.assertEqual(second_ctx["story_event_graph_version"], 1)
        self.assertEqual(
            first_ctx["story_event_graph"]["fingerprint"],
            second_ctx["story_event_graph"]["fingerprint"],
        )
        self.assertEqual(
            first_ctx["story_event_graph"]["script_version"], 7
        )
        self.assertTrue(
            first_ctx["story_event_graph"]["validation"]["passed"]
        )
        self.assertFalse(
            first_ctx["story_event_graph"]["validation"]["production_blocking"]
        )

    def test_invalid_graph_is_persisted_as_advisory_without_raising(self):
        projects = _Projects()
        director = _director(projects)
        invalid_script = _script()
        invalid_script["high_value_events"][0]["dramatic_question"] = ""
        invalid_script["high_value_events"][0]["required_beats"] = []
        ctx = _context()
        ctx.update({"script": invalid_script, "script_version": 8})

        graph, version = director._persist_story_event_graph(ctx)

        self.assertEqual(version, 1)
        self.assertFalse(graph["validation"]["passed"])
        self.assertFalse(graph["validation"]["production_blocking"])
        self.assertTrue(graph["validation"]["issues"])
        self.assertTrue(director.log.warnings)
        stored, stored_version = projects.latest_document(
            "episode-1", "story_event_graph"
        )
        self.assertEqual(stored_version, 1)
        self.assertEqual(stored, graph)


class DirectorStoryEventGraphStoryboardTests(unittest.TestCase):
    def test_storyboard_receives_graph_and_saves_nonblocking_supervision(self):
        projects = _Projects()
        director = _director(projects)
        director.assets = _Assets()
        director._ensure_space_first_scenes = lambda *_args, **_kwargs: None
        director._persist_storyboard_reviews = lambda *_args, **_kwargs: {}
        director._storyboard_rule_payload = lambda *_args, **_kwargs: {
            "episode_rule_marker": "本集临时规则优先",
        }
        captured = {}

        def call(_ctx, capability, payload, task):
            captured.update({
                "capability": capability,
                "task": task,
                "payload": copy.deepcopy(payload),
            })
            return ProviderResult(
                provider="fake-storyboard",
                cost=0,
                data={"shots": [{"shot_no": 1}]},
            )

        director._call = call
        script = _script()
        graph = build_story_event_graph(
            script,
            project_id="project-1",
            episode_id="episode-1",
            source_document_ref="script",
            source_version=7,
        )
        enriched = {
            "pipeline_version": PIPELINE_VERSION,
            "storyboard_enrichment_version": STORYBOARD_ENRICHMENT_VERSION,
            "shots": [{
                "shot_no": 1,
                "scene_no": 2,
                "prompt": "女主看向手机",
                "seedance_prompt": "女主看向手机",
                "unit_id": "u001",
            }],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _context(temp_dir)
            ctx.update({
                "script": script,
                "script_version": 7,
                "story_analysis_version": 3,
                "continuity": {"characters": [], "scenes": []},
                "production_profile": {"standard_fingerprint": "standard-1"},
                "story_event_graph": graph,
                "story_event_graph_version": 4,
            })
            with patch(
                "aifos.director.enrich_storyboard",
                return_value=copy.deepcopy(enriched),
            ):
                result = director._stage_storyboard(ctx)

            raw_path = Path(temp_dir) / "storyboard" / "raw_provider.json"
            self.assertTrue(raw_path.exists())

        self.assertEqual(captured["capability"], "storyboard")
        self.assertEqual(captured["task"], "storyboard")
        self.assertEqual(captured["payload"]["story_event_graph"], graph)
        self.assertIsNot(captured["payload"]["story_event_graph"], graph)
        self.assertEqual(
            captured["payload"]["episode_rule_marker"],
            "本集临时规则优先",
        )
        self.assertFalse(result["story_event_supervision_passed"])
        self.assertEqual(result["story_event_supervision_version"], 1)
        self.assertTrue(ctx["force"])
        self.assertFalse(ctx["story_event_supervision"]["passed"])
        self.assertFalse(
            ctx["story_event_supervision"]["production_blocking"]
        )
        self.assertEqual(
            ctx["story_event_supervision"]["story_event_graph_version"], 4
        )
        self.assertEqual(
            ctx["story_event_supervision"]["storyboard_version"], 1
        )
        self.assertTrue(ctx["story_event_supervision"]["issues"])
        self.assertTrue(director.log.warnings)
        stored, version = projects.latest_document(
            "episode-1", "story_event_supervision"
        )
        self.assertEqual(version, 1)
        self.assertEqual(stored, ctx["story_event_supervision"])


if __name__ == "__main__":
    unittest.main()
