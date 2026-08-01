"""Director wiring for the non-blocking story intelligence documents."""

from pathlib import Path
from types import SimpleNamespace

import aifos.director as director_module
from aifos.app import App
from aifos.workflow import PIPELINE_VERSION


def _script_result(*, independent_review=None):
    script = {
        "project_title": "连续性接线",
        "episode_number": 2,
        "logline": "林昭承接密诏危机。",
        "characters": [],
        "scenes": [],
    }
    if independent_review is not None:
        script["independent_review"] = independent_review
    return SimpleNamespace(
        provider="writer-provider", cost=0.0, data=script, uri="")


def _dimension_reviews():
    return {
        name: {
            "score": 4,
            "evidence": [f"{name} 有独立逐场证据。"],
            "directed_revision": [f"定向复核 {name}。"],
        }
        for name in (
            "causal_chain",
            "conflict_density",
            "character_arc",
            "dialogue_quality",
            "hook_strength",
        )
    }


def _prepare_script_stage(app, monkeypatch, tmp_path, result):
    project, _ = app.projects.get_or_create_project(
        "连续性接线", style="电影半写实", kind="drama")
    previous, _ = app.projects.get_or_create_episode(
        project["id"], 1, premise="密诏危机")
    current, _ = app.projects.get_or_create_episode(
        project["id"], 2, premise="承接前集")
    app.projects.save_document(previous["id"], "script", {
        "unresolved_hooks": ["门外人影是谁"],
        "scenes": [{
            "scene_no": 1,
            "location": "东宫书房",
            "exit_state": "门半开，烛台仍在燃烧。",
        }],
    })
    app.projects.save_document(previous["id"], "storyboard", {
        "shots": [{
            "shot_no": 9,
            "scene_no": 1,
            "end_state": {
                "林昭": {"pose": "贴墙站立", "injury": "右臂受伤"},
            },
        }],
    })
    app.projects.save_document(previous["id"], "continuity", {
        "unresolved_hooks": ["密诏落款来源"],
    })

    captured = {}

    def fake_call(_ctx, capability, payload, sub_dir):
        assert (capability, sub_dir) == ("script", "script")
        captured["payload"] = payload
        return result

    def fake_analysis(ctx, force=False):
        version = app.projects.save_document(
            current["id"], "script", ctx["script"])
        ctx["script_version"] = version
        ctx["story_analysis_version"] = 1
        return {"world": {"name": "大明"}}, 1, False

    monkeypatch.setattr(app.director, "_call", fake_call)
    monkeypatch.setattr(
        app.director, "_record_script_lessons", lambda *_args: 0)
    monkeypatch.setattr(
        app.director, "_normalize_script_character_profiles",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app.director, "_ensure_story_analysis", fake_analysis)
    ctx = {
        "project": dict(project),
        "episode": dict(current),
        "out_root": Path(tmp_path) / "artifacts",
        "run_id": "writer-run-22",
    }
    return ctx, captured, current


def test_script_stage_passes_only_real_previous_episode_facts_and_saves_them(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx, captured, current = _prepare_script_stage(
            app, monkeypatch, tmp_path, _script_result())

        app.director._stage_script(ctx)

        compact = captured["payload"]["previous_episode_continuity"]
        assert compact["previous_episode_number"] == 1
        assert compact["previous_exit_state"].startswith(
            "前集分镜第9镜结尾")
        assert compact["unresolved_hooks"] == [
            "门外人影是谁", "密诏落款来源"]
        assert compact["source_versions"] == {
            "script": 1, "storyboard": 1, "continuity": 1}
        saved, _ = app.projects.latest_document(
            current["id"], "previous_episode_continuity")
        assert saved["production_blocking"] is False
        assert saved["source_versions"] == compact["source_versions"]
    finally:
        app.close()


def test_script_stage_records_pending_instead_of_fake_self_scores(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx, _captured, current = _prepare_script_stage(
            app, monkeypatch, tmp_path, _script_result())

        app.director._stage_script(ctx)

        review, _ = app.projects.latest_document(
            current["id"], "script_review")
        assert review["status"] == "pending"
        assert review["production_blocking"] is False
        assert review["scores_available"] is False
        assert "dimensions" not in review
        assert "未返回独立评审运行" in review["reason"]
    finally:
        app.close()


def test_script_stage_accepts_complete_independent_review_only(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        independent = {
            "generator_run_id": "writer-run-22",
            "reviewer_run_id": "reviewer-run-91",
            "reviewer_source": "independent-codex-review",
            "dimension_reviews": _dimension_reviews(),
        }
        ctx, _captured, current = _prepare_script_stage(
            app, monkeypatch, tmp_path,
            _script_result(independent_review=independent))

        app.director._stage_script(ctx)

        review, _ = app.projects.latest_document(
            current["id"], "script_review")
        assert review["status"] == "ready"
        assert review["generator_run_id"] == "writer-run-22"
        assert review["reviewer_run_id"] == "reviewer-run-91"
        assert len(review["dimensions"]) == 5
        assert review["production_blocking"] is False
    finally:
        app.close()


def test_reusing_same_script_version_never_downgrades_ready_review(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("评审复用")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "script_version": 4,
            "run_id": "writer-run-4",
        }
        result = _script_result(independent_review={
            "generator_run_id": "writer-run-4",
            "reviewer_run_id": "reviewer-run-8",
            "reviewer_source": "independent-codex-review",
            "dimension_reviews": _dimension_reviews(),
        })
        ready_version = app.director._persist_script_review(ctx, result)

        reused_version = app.director._persist_script_review(ctx)

        review, saved_version = app.projects.latest_document(
            episode["id"], "script_review")
        assert reused_version == ready_version == saved_version == 1
        assert review["status"] == "ready"
        assert review["reviewer_run_id"] == "reviewer-run-8"
    finally:
        app.close()


def test_new_script_version_marks_old_ready_review_stale_without_scores(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("评审失效")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "script_version": 4,
            "run_id": "writer-run-4",
        }
        app.director._persist_script_review(
            ctx, _script_result(independent_review={
                "generator_run_id": "writer-run-4",
                "reviewer_run_id": "reviewer-run-8",
                "reviewer_source": "independent-codex-review",
                "dimension_reviews": _dimension_reviews(),
            }))

        ctx["script_version"] = 5
        app.director._persist_script_review(ctx)

        review, version = app.projects.latest_document(
            episode["id"], "script_review")
        assert version == 2
        assert review["status"] == "pending"
        assert review["script_version"] == "5"
        assert review["stale_from_script_version"] == "4"
        assert review["scores_available"] is False
        assert "dimensions" not in review
    finally:
        app.close()


def _shot(number=1):
    return {
        "shot_no": number,
        "scene_no": 1,
        "unit_id": f"U{number:02d}",
        "duration": 5,
        "dialogue": {"dialogue": "密诏是真的。"},
        "shot_contract": {"景别": "中景", "运镜": "固定"},
        "shot_function": "揭示信息",
        "script_reference": "林昭展开密诏。",
        "start_state": {"林昭": {"pose": "坐"}},
        "end_state": {"林昭": {"pose": "起身"}},
        "performance": {"beat": "由确认转为警觉"},
        "five_dimensions": {
            "subject_motion": "林昭展开密诏",
            "environment_light": "暖烛光",
            "camera_design": {"shot_scale": "中景", "movement": "固定"},
            "time_state": {"start": "坐", "end": "起身"},
            "aesthetics": {"lighting": "暖烛光"},
        },
    }


def test_storyboard_reviews_are_documents_only_and_refresh_selected_keyframe(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("九宫格接线")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        ctx = {
            "project": project,
            "episode": episode,
            "storyboard": {"shots": [_shot()]},
            "storyboard_version": 3,
        }
        before = len(app.assets.list(project["id"]))

        versions = app.director._persist_storyboard_reviews(
            ctx, keyframes=[{
                "shot_no": 1,
                "uri": "/formal/selected-shot-001.png",
            }])

        assert set(versions) == {
            "episode_director_review", "nine_grid_browser"}
        browser, _ = app.projects.latest_document(
            episode["id"], "nine_grid_browser")
        cell = browser["pages"][0]["cells"][0]
        assert cell["keyframe_uri"] == "/formal/selected-shot-001.png"
        assert browser["view_only"] is True
        assert browser["reference_chain_eligible"] is False
        assert browser["generates_reference_asset"] is False
        assert len(app.assets.list(project["id"])) == before
    finally:
        app.close()


def test_storyboard_stage_persists_director_and_grid_documents_on_reuse(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("分镜阶段接线")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        storyboard = {
            "pipeline_version": PIPELINE_VERSION,
            "script_version": 1,
            "story_analysis_version": 2,
            "profile": {"standard_fingerprint": "standard-fp"},
            "shots": [_shot()],
        }
        app.projects.save_document(episode["id"], "storyboard", storyboard)
        monkeypatch.setattr(
            app.director, "_ensure_space_first_scenes", lambda _ctx: None)
        monkeypatch.setattr(
            app.director, "_plan_seed_shots", lambda _ctx: None)
        monkeypatch.setattr(
            director_module, "repair_storyboard_appearance_continuity",
            lambda value, *_args: value)
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "script": {"scenes": []},
            "continuity": {},
            "script_version": 1,
            "story_analysis_version": 2,
            "production_profile": {"standard_fingerprint": "standard-fp"},
        }

        result = app.director._stage_storyboard(ctx)

        assert result["reused"] is True
        director_review, _ = app.projects.latest_document(
            episode["id"], "episode_director_review")
        browser, _ = app.projects.latest_document(
            episode["id"], "nine_grid_browser")
        assert director_review["shot_count"] == 1
        assert browser["pages"][0]["cells"][0]["keyframe_status"] == "pending"
        assert browser["reference_chain_eligible"] is False
    finally:
        app.close()


def test_stage_images_refreshes_grid_without_blocking_empty_batch(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("图片后刷新")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        ctx = {
            "project": project,
            "episode": episode,
            "out_root": tmp_path / "artifacts",
            "storyboard": {"shots": []},
        }
        calls = []
        monkeypatch.setattr(
            app.director, "_plan_read", lambda _ctx: {"items": []})
        monkeypatch.setattr(
            app.director, "_plan_seed_shots", lambda _ctx: None)
        monkeypatch.setattr(
            app.director, "_distill_lessons", lambda _ctx: 0)
        monkeypatch.setattr(
            app.director, "reconcile_completed_shot_images",
            lambda _ctx: {"recovered": 0, "awaiting_human_shots": []})
        monkeypatch.setattr(
            app.director, "_persist_storyboard_reviews",
            lambda _ctx, keyframes=(): calls.append(list(keyframes)) or {})

        report = app.director._stage_images(ctx)

        assert report["count"] == 0
        assert calls == [[]]
    finally:
        app.close()


def test_review_document_failures_never_block_production(tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("非阻断")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        monkeypatch.setattr(
            app.director, "_save_review_document",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("review store unavailable")))

        result = app.director._persist_storyboard_reviews({
            "project": project,
            "episode": episode,
            "storyboard": {"shots": [_shot()]},
            "storyboard_version": 1,
        })

        assert result == {}
    finally:
        app.close()
