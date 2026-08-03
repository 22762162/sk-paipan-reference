"""Director uses one isolated rule stack across project, episode and shot."""

from aifos.app import App
from aifos.workflow import production_profile


def _ctx(app, project, episode, script=None):
    standard = app.standards.active()
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "production_standard": standard,
        "production_profile": production_profile(app.config, standard),
        "script": script or {},
    }
    ctx["rule_layers"] = app.director._load_rule_layers(
        ctx["project"], ctx["episode"], standard)
    return ctx


def _pack(scope, rules, suppressions=None):
    return {
        "schema": "aifos.creative-rule-pack/v1",
        "scope": scope,
        "rules": rules,
        "suppressions": suppressions or [],
    }


def test_episode_overrides_series_and_shot_overrides_episode(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("双时空")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.save_project_document(
            project["id"], "project_rules", _pack("project_series", [
                {"key": "world.era", "text": "本剧包含现代与明代"},
                {"key": "visual.palette", "text": "冷暖双世界统一人物色"},
            ]))
        app.projects.save_document(
            episode["id"], "episode_rules", _pack("episode_temporary", [
                {"key": "world.era", "text": "本集开场位于现代酒店"},
            ]))
        ctx = _ctx(app, project, episode)
        episode_rules = app.director._resolve_effective_rules(
            ctx, shot={"shot_no": 1, "scene_no": 1},
            stage="images", modality="image")
        assert episode_rules["world.era"] == "本集开场位于现代酒店"
        assert episode_rules["visual.palette"] == "冷暖双世界统一人物色"

        shot_rules = app.director._resolve_effective_rules(
            ctx, shot={
                "shot_no": 3, "scene_no": 2,
                "story_phase": "time_travel",
                "active_realm_id": "ming_history",
                "era_context": "明代京城",
                "sanctioned_anachronisms": ["女主随身的手机"],
            }, stage="images", modality="image")
        assert shot_rules["world.era"] == "明代京城"
        assert shot_rules["world.sanctioned_anachronisms"] == [
            "女主随身的手机"]
        assert shot_rules.sources["world.era"]["layer"] == "current_shot"
    finally:
        app.close()


def test_episode_rules_do_not_leak_to_next_episode_or_project(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project_a, _ = app.projects.get_or_create_project("A剧")
        ep1, _ = app.projects.get_or_create_episode(project_a["id"], 1)
        ep2, _ = app.projects.get_or_create_episode(project_a["id"], 2)
        project_b, _ = app.projects.get_or_create_project("B剧")
        ep_b, _ = app.projects.get_or_create_episode(project_b["id"], 1)
        app.projects.save_project_document(
            project_a["id"], "project_rules", _pack("project_series", [
                {"key": "world.travel_mechanism", "value": "铜镜穿越"},
            ]))
        app.projects.save_document(
            ep1["id"], "episode_rules", _pack("episode_temporary", [
                {"key": "world.era", "value": "明代"},
            ]))

        resolved_ep2 = app.director._resolve_effective_rules(
            _ctx(app, project_a, ep2), shot={"shot_no": 1},
            stage="storyboard", modality="image")
        assert resolved_ep2["world.travel_mechanism"] == "铜镜穿越"
        assert "world.era" not in resolved_ep2

        resolved_b = app.director._resolve_effective_rules(
            _ctx(app, project_b, ep_b), shot={"shot_no": 1},
            stage="storyboard", modality="image")
        assert "world.travel_mechanism" not in resolved_b
        assert "world.era" not in resolved_b
    finally:
        app.close()


def test_episode_suppression_removes_series_creative_default(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("克制配饰")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.save_project_document(
            project["id"], "project_rules", _pack("project_series", [
                {"key": "wardrobe.default_accessory", "value": "佩戴项链"},
            ]))
        app.projects.save_document(
            episode["id"], "episode_rules", _pack(
                "episode_temporary", [], ["wardrobe.default_accessory"]))
        resolved = app.director._resolve_effective_rules(
            _ctx(app, project, episode), shot={"shot_no": 1},
            stage="images", modality="image")
        assert "wardrobe.default_accessory" not in resolved
        assert any(item["reason"] == "higher_creative_suppression"
                   for item in resolved.overridden)
        assert resolved["technical.resolution"] == "720p"
    finally:
        app.close()
