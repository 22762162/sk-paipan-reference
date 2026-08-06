"""Regression coverage for storyboard/blocking/preflight contract refreshes."""

import copy
import json
import threading
from types import SimpleNamespace

from aifos import workflow
from aifos.app import App
from aifos.spatial_blocking import build_spatial_plan


def _minimal_profile():
    return {
        "rules": {},
        "video_model": "seedance2.0fast_vip",
        "resolution": "720p",
        "voice": "jimeng_builtin",
        "lip_sync": True,
        "burn_subtitles": False,
        "max_segment_seconds": 15,
        "time_precision_seconds": 0.5,
        "standard_fingerprint": "test-standard",
        "standard_version": 1,
    }


def test_build_preflight_uses_the_same_scene_models_as_blocking(monkeypatch):
    original = workflow.build_spatial_plan
    received = []

    def capture(*args, **kwargs):
        received.append(kwargs.get("scene_models"))
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "build_spatial_plan", capture)
    scene_models = {
        "酒店客房": {
            "room": {
                "floor_width_m": 4.2,
                "floor_depth_m": 6.8,
                "wall_height_m": 3.1,
            },
            "objects": [],
        },
    }

    workflow.build_preflight(
        {"scenes": [{"scene_no": 1, "location": "酒店客房"}]},
        {"shots": []},
        {"characters": [], "scenes": []},
        {"passed": True, "assets": []},
        [], _minimal_profile(), scene_models=scene_models)

    assert received == [scene_models]


def test_build_preflight_does_not_waive_blocking_scene_physics_failure():
    """A valid 2D graph cannot overrule a blocking real-3D collision."""
    script = {
        "scenes": [{
            "scene_no": 1, "location": "酒店客房",
            "characters": [], "lines": [],
        }],
    }
    storyboard = {"shots": []}
    continuity = {"characters": [], "scenes": []}
    blocking = build_spatial_plan(script, storyboard, continuity)
    blocking["validation"].update({
        "passed": False,
        "scene_physics_passed": False,
        "scene_physics_issues": [{
            "severity": "block", "field": "camera_furniture_collision",
            "message": "摄影机穿入床头柜",
        }],
    })
    profile = _minimal_profile()
    gate_ids = (
        "script_bible", "character_assets", "continuity", "spatial",
        "spatial_seedance", "five_dimensions", "duration", "dialogue",
        "performance", "camera", "people", "text", "frames", "audio",
        "profile",
    )
    profile["rules"] = {"quality_gates": [
        {"id": gate_id, "enabled": gate_id == "spatial"}
        for gate_id in gate_ids]}

    report = workflow.build_preflight(
        script, storyboard, continuity, {"passed": True}, [], profile,
        blocking=blocking)

    spatial = next(
        gate for gate in report["gates"] if gate["id"] == "spatial")
    assert spatial["passed"] is False
    assert "摄影机穿入床头柜" in spatial["detail"]
    assert report["passed"] is False


def test_stage_preflight_rebuilds_stale_blocking_from_final_storyboard(
        tmp_path):
    app = App(tmp_path / "workspace")
    try:
        project, _ = app.projects.get_or_create_project("最终调度刷新")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        profile = _minimal_profile()
        gate_ids = (
            "script_bible", "character_assets", "continuity", "spatial",
            "spatial_seedance", "five_dimensions", "duration", "dialogue",
            "performance", "camera", "people", "text", "frames", "audio",
            "profile",
        )
        profile["rules"] = {
            "quality_gates": [
                {"id": gate_id, "enabled": gate_id == "spatial"}
                for gate_id in gate_ids
            ],
        }
        script = {
            "scenes": [{
                "scene_no": 1, "location": "普通房间",
                "characters": [], "lines": [],
            }],
        }
        continuity = {"characters": [], "scenes": []}
        old_storyboard = {"shots": [{
            "shot_no": 1, "scene_no": 1, "unit_id": "U01",
            "characters": [], "character_count": 0,
            "description": "固定中景", "prompt": "固定中景",
            "camera": "中景·平视·固定机位",
            "start_state": {}, "end_state": {},
            "five_dimensions": {"camera_design": {"shot_scale": "中景"}},
        }]}
        final_storyboard = copy.deepcopy(old_storyboard)
        final_storyboard["shots"][0].update({
            "description": "摄影机改为左侧近景",
            "prompt": "摄影机改为左侧近景",
            "camera": "近景·平视·左侧机位·固定",
            "five_dimensions": {
                "camera_design": {"shot_scale": "近景", "angle": "平视"},
            },
        })
        stale = build_spatial_plan(script, old_storyboard, continuity)
        app.projects.save_document(episode["id"], "blocking", stale)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root, "script": script,
            "storyboard": final_storyboard, "continuity": continuity,
            "text_assets": {"passed": True, "assets": []}, "frames": [],
            "production_profile": profile, "blocking": stale,
            "quality_policy": {}, "character_asset_policy": {},
            "cast_selection": {},
        }
        expected_fingerprint = build_spatial_plan(
            script, final_storyboard, continuity,
            scene_models=app.director._previz_scene_models(ctx)
        )["source_fingerprint"]

        result = app.director._stage_preflight(ctx)

        assert result["passed"] is True
        assert ctx["preflight"]["blocking_refresh"]["refreshed"] is True
        assert ctx["blocking"]["source_fingerprint"] != \
            stale["source_fingerprint"]
        assert ctx["blocking"]["source_fingerprint"] == \
            expected_fingerprint
        saved, _version = app.projects.latest_document(
            episode["id"], "blocking")
        assert saved["source_fingerprint"] == expected_fingerprint
        spatial = next(
            gate for gate in ctx["preflight"]["gates"]
            if gate["id"] == "spatial")
        assert spatial["passed"] is True
    finally:
        app.close()


def test_blocking_refresh_detects_furniture_change_inside_same_room(tmp_path):
    app = App(tmp_path / "workspace")
    try:
        project, _ = app.projects.get_or_create_project("家具指纹刷新")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        panorama = tmp_path / "room-panorama.png"
        panorama.write_bytes(b"panorama-placeholder")
        pano = app.assets.register(
            project["id"], "scene_art", "客房::view:panorama",
            str(panorama), meta={"real": True, "image_quality": "high"})
        room = {
            "floor_width_m": 10.0, "floor_depth_m": 7.0,
            "wall_height_m": 3.2,
        }

        def register_model(version_name, objects):
            path = tmp_path / f"{version_name}.json"
            path.write_text(json.dumps({
                "schema": "aifos.scene-model/v1", "location": "客房",
                "room": room, "panorama_version": pano["version"],
                "objects": objects, "issues": [],
            }, ensure_ascii=False), encoding="utf-8")
            return app.assets.register(
                project["id"], "scene_model", "客房", str(path),
                meta={"real": True, "panorama_version": pano["version"]})

        register_model("scene-v1", [])
        script = {"scenes": [{
            "scene_no": 1, "location": "客房",
            "characters": [], "lines": [],
        }]}
        storyboard = {"shots": [{
            "shot_no": 1, "scene_no": 1, "unit_id": "U01",
            "characters": [], "character_count": 0,
            "description": "客房空镜", "prompt": "客房空镜",
            "camera": "中景·平视·固定机位",
            "start_state": {}, "end_state": {},
            "five_dimensions": {"camera_design": {"shot_scale": "中景"}},
        }]}
        continuity = {"characters": [], "scenes": []}
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root, "script": script,
            "storyboard": storyboard, "continuity": continuity,
            "production_profile": _minimal_profile(),
        }
        first = app.director._refresh_final_blocking_contract(
            ctx, storyboard)
        old_source = first["blocking"]["source_fingerprint"]
        old_scene = first["blocking"]["scene_model_fingerprint"]
        ctx["blocking"]["validation"]["passed"] = False
        invalid_refresh = app.director._refresh_final_blocking_contract(
            ctx, storyboard)
        assert invalid_refresh["refreshed"] is True
        assert invalid_refresh["blocking"]["validation"]["passed"] is True
        old_scene = invalid_refresh["blocking"]["scene_model_fingerprint"]

        register_model("scene-v2", [{
            "name": "新边柜", "category": "furniture",
            "position_3d": {"x": 4.3, "y": 0.0, "z": 3.0},
            "width_m": 0.2, "height_m": 0.2, "depth_m": 0.2,
            "rotation_y_deg": 0.0,
        }])
        refreshed = app.director._refresh_final_blocking_contract(
            ctx, storyboard)

        assert refreshed["refreshed"] is True
        # Furniture geometry participates in the blocking source contract: a
        # moved/added obstacle must invalidate routes even when the script and
        # storyboard text are unchanged.
        assert refreshed["blocking"]["source_fingerprint"] != old_source
        assert refreshed["blocking"]["scene_model_fingerprint"] != old_scene
    finally:
        app.close()


def test_stage_preflight_refresh_uses_active_storyboard_when_scene_skipped(
        tmp_path):
    app = App(tmp_path / "workspace")
    try:
        project, _ = app.projects.get_or_create_project("跳场调度刷新")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        profile = _minimal_profile()
        gate_ids = (
            "script_bible", "character_assets", "continuity", "spatial",
            "spatial_seedance", "five_dimensions", "duration", "dialogue",
            "performance", "camera", "people", "text", "frames", "audio",
            "profile",
        )
        profile["rules"] = {"quality_gates": [
            {"id": gate_id, "enabled": gate_id == "spatial"}
            for gate_id in gate_ids]}
        script = {"scenes": [
            {"scene_no": 1, "location": "保留场", "characters": [],
             "lines": []},
            {"scene_no": 2, "location": "跳过场", "characters": [],
             "lines": []},
        ]}

        def make_shot(no, scene):
            return {
                "shot_no": no, "scene_no": scene, "unit_id": f"U{no:02d}",
                "characters": [], "character_count": 0,
                "description": f"镜头{no}", "prompt": f"镜头{no}",
                "camera": "中景·平视·固定机位",
                "start_state": {}, "end_state": {},
                "five_dimensions": {
                    "camera_design": {"shot_scale": "中景"}},
            }

        storyboard = {"shots": [make_shot(1, 1), make_shot(2, 2)]}
        continuity = {"characters": [], "scenes": []}
        full = build_spatial_plan(script, storyboard, continuity)
        app.projects.save_document(episode["id"], "blocking", full)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root, "script": script,
            "storyboard": storyboard, "continuity": continuity,
            "scene_plan": {"skipped_scenes": [2]},
            "text_assets": {"passed": True, "assets": []}, "frames": [],
            "production_profile": profile, "blocking": full,
            "quality_policy": {}, "character_asset_policy": {},
            "cast_selection": {},
        }

        result = app.director._stage_preflight(ctx)

        assert result["passed"] is True
        assert set(ctx["blocking"]["shot_index"]) == {"1"}
        assert ctx["preflight"]["units"] == 1
        assert ctx["preflight"]["blocking_refresh"]["refreshed"] is True
    finally:
        app.close()


def test_prompt_repairs_batch_refresh_task_spatial_payload_and_hash(
        tmp_path, monkeypatch):
    app = App(tmp_path / "workspace")
    try:
        project, _ = app.projects.get_or_create_project("出图前空间收敛")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        script = {"scenes": [
            {"scene_no": 1, "location": "甲房", "characters": [],
             "lines": []},
            {"scene_no": 2, "location": "乙房", "characters": [],
             "lines": []},
        ]}

        def shot(no, scene, camera="中景·平视·固定机位"):
            return {
                "shot_no": no, "scene_no": scene, "unit_id": f"U{no:02d}",
                "characters": [], "character_count": 0,
                "description": f"镜头{no}", "prompt": f"镜头{no}",
                "camera": camera, "start_state": {}, "end_state": {},
                "five_dimensions": {
                    "camera_design": {"shot_scale": "中景"}},
            }

        old_storyboard = {"shots": [shot(1, 1), shot(2, 1), shot(3, 2)]}
        final_storyboard = copy.deepcopy(old_storyboard)
        final_storyboard["shots"][0].update({
            "description": "镜头1改为左侧近景",
            "prompt": "镜头1改为左侧近景",
            "camera": "近景·平视·左侧机位·固定",
        })
        continuity = {"characters": [], "scenes": []}
        stale = build_spatial_plan(script, old_storyboard, continuity)
        app.projects.save_document(episode["id"], "blocking", stale)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root, "script": script,
            "storyboard": final_storyboard, "continuity": continuity,
            "production_profile": _minimal_profile(), "blocking": stale,
            "quality_policy": {}, "images": [
                {"shot_no": 2, "uri": "old-shot-2.png"},
                {"shot_no": 3, "uri": "old-shot-3.png"},
            ],
            "_blocked_prompt_spatial_repairs": {1},
        }

        def payload_for(current_ctx, current_shot, **_kwargs):
            block = (current_ctx["blocking"].get("shot_index") or {}).get(
                str(current_shot["shot_no"])) or {}
            spatial_ref = "spatial:" + str(
                current_ctx["blocking"].get("source_fingerprint") or "")
            prompt = str(block.get("constraint") or "")
            return {
                "_episode_id": episode["id"], "_contract_revision": 1,
                "shot_no": current_shot["shot_no"],
                "prompt": prompt, "prompt_compact": prompt,
                "characters": [], "character_count": 0,
                "reference_manifest": [{
                    "index": 1, "uri": spatial_ref, "label": "空间调度",
                    "role": "spatial", "binding": "锁定机位与遮挡",
                }],
                "spatial_ref": spatial_ref,
                "spatial_blocking": copy.deepcopy(block),
                "prompt_contract": {
                    "schema": "test", "spatial": prompt,
                    "references": [{
                        "index": 1, "uri": spatial_ref,
                        "role": "spatial"}],
                },
                "quality_decision": {"level": "medium"},
                "image_quality": "medium", "frame_kind": "keyframe",
            }

        monkeypatch.setattr(app.director, "_shot_payload", payload_for)
        monkeypatch.setattr(
            app.director, "_shot_qc_spec",
            lambda _ctx, payload: {
                "spatial": copy.deepcopy(payload["spatial_blocking"])})
        monkeypatch.setattr(
            app.director, "_attach_reference_manifest", lambda payload: payload)
        refresh_calls = []
        original_refresh = app.director._refresh_final_blocking_contract

        def count_refresh(*args, **kwargs):
            refresh_calls.append(1)
            return original_refresh(*args, **kwargs)

        monkeypatch.setattr(
            app.director, "_refresh_final_blocking_contract", count_refresh)
        app.director._plan_seed_shots(ctx)
        task_payload = payload_for(ctx, final_storyboard["shots"][0])
        task_payload["prompt_review"] = {
            "approved": True, "status": "approved",
            "optimized_prompt": "旧空间参考下的优化稿",
        }
        task_payload["_prompt_review_frozen_input_hash"] = "old-input"
        tasks = [{
            "item_id": "shot:1", "capability": "image",
            "payload": task_payload, "sub_dir": "images", "tag": 1,
            "priority": 1, "qc_spec": {},
        }]
        old_ref = task_payload["spatial_ref"]

        refreshed = app.director._refresh_repaired_image_tasks_before_dispatch(
            ctx, tasks)

        by_shot = {int(task["tag"]): task for task in refreshed}
        assert refresh_calls == [1]
        assert set(by_shot) == {1, 2}
        assert by_shot[1]["payload"]["spatial_ref"] != old_ref
        assert by_shot[1]["payload"]["previous_prompt_review"][
            "optimized_prompt"] == "旧空间参考下的优化稿"
        assert "prompt_review" not in by_shot[1]["payload"]
        assert "_prompt_review_frozen_input_hash" not in \
            by_shot[1]["payload"]
        assert by_shot[1]["payload"]["spatial_blocking"] == \
            ctx["blocking"]["shot_index"]["1"]
        assert by_shot[2]["payload"]["spatial_blocking"] == \
            ctx["blocking"]["shot_index"]["2"]
        assert [image["shot_no"] for image in ctx["images"]] == [3]
        plan = app.director._plan_read(ctx)
        plan_by_id = {item["id"]: item for item in plan["items"]}
        for shot_no in (1, 2):
            expected_hash = app.director._shot_content_hash(
                final_storyboard["shots"][shot_no - 1],
                by_shot[shot_no]["payload"])
            assert plan_by_id[f"shot:{shot_no}"]["content_hash"] == \
                expected_hash
            assert plan_by_id[f"shot:{shot_no}"][
                "spatial_contract_refreshed"] is True
        assert plan_by_id["shot:3"].get("spatial_contract_refreshed") is not True
    finally:
        app.close()


def test_parallel_runtime_repair_discards_late_old_spatial_result(
        tmp_path, monkeypatch):
    """An in-flight old-revision image is never promoted after a repair."""
    app = App(tmp_path / "workspace")
    try:
        project, _ = app.projects.get_or_create_project("运行中空间版本")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root, "images": [],
        }
        repair_applied = threading.Event()
        calls = {1: [], 2: []}
        accepted = []
        plan_events = []
        app.director._task_cost = 0.0
        app.director._task_providers = set()

        def result(shot_no, revision, *, repair=False, cost=1.0):
            return SimpleNamespace(
                provider="fake", model="fake", cost=cost, uri=(
                    f"shot-{shot_no}-r{revision}.png"), qc={},
                fallbacks=[], data={
                    "shot_no": shot_no, "revision": revision,
                    "needs_repair": repair,
                })

        def generate(_capability, payload, _out_dir, _cancel, _qc):
            shot_no = int(payload["shot_no"])
            revision = int(payload["_contract_revision"])
            calls[shot_no].append(revision)
            if shot_no == 2 and revision == 1:
                assert repair_applied.wait(3)
            return result(
                shot_no, revision,
                repair=(shot_no == 1 and revision == 1))

        def repair_generate(
                _capability, payload, _out_dir, _cancel, _qc):
            shot_no = int(payload["shot_no"])
            revision = int(payload["_contract_revision"])
            calls[shot_no].append(revision)
            return result(shot_no, revision)

        def refresh(_ctx, current_tasks, **_kwargs):
            for current in current_tasks:
                current["payload"]["_contract_revision"] = 2
            repair_applied.set()
            return current_tasks, list(current_tasks)

        monkeypatch.setattr(app.director, "_total_image_workers", lambda: 8)
        monkeypatch.setattr(app.director, "_shot_candidate_count", lambda: 4)
        monkeypatch.setattr(
            app.director, "_ensure_shot_contract_nonblocking",
            lambda _ctx, _task: None)
        monkeypatch.setattr(
            app.director, "_review_image_tasks",
            lambda _ctx, _tasks, **_kwargs: [])
        monkeypatch.setattr(
            app.director, "_prepare_dispatch_contracts",
            lambda _ctx, _tasks: None)
        monkeypatch.setattr(
            app.director, "_attach_candidate_progress_reporter",
            lambda _ctx, _task: None)
        monkeypatch.setattr(
            app.director, "_generate_shot_candidate_group", generate)
        monkeypatch.setattr(
            app.director, "_generate_repair_candidate_group",
            repair_generate)
        monkeypatch.setattr(
            app.director, "_candidate_group_technical_incomplete",
            lambda _result: False)
        monkeypatch.setattr(
            app.director, "_candidate_selection_pending",
            lambda _result: False)
        monkeypatch.setattr(
            app.director, "_critical_qc_error",
            lambda current: (
                "repair" if current.data.get("needs_repair") else ""))
        monkeypatch.setattr(
            app.director, "_auto_apply_codex_escalation",
            lambda _ctx, _task, _result: "camera repaired")
        monkeypatch.setattr(
            app.director, "_refresh_runtime_spatial_repairs", refresh)
        monkeypatch.setattr(
            app.director, "_plan_done_extra", lambda _result: {})
        monkeypatch.setattr(
            app.director, "_finish_dispatch_task",
            lambda _ctx, _task, **_kwargs: None)
        monkeypatch.setattr(
            app.director, "_plan_mark",
            lambda _ctx, item_id, status, **kwargs: plan_events.append(
                (item_id, status, kwargs.get("extra") or {})))

        tasks = []
        for shot_no in (1, 2):
            payload = {
                "_episode_id": episode["id"], "shot_no": shot_no,
                "_contract_revision": 1, "prompt": f"shot {shot_no}",
                "quality_decision": {"level": "medium"},
            }
            tasks.append({
                "item_id": f"shot:{shot_no}", "capability": "image",
                "payload": payload, "sub_dir": "images", "tag": shot_no,
                "priority": 1, "qc_spec": {},
                "on_success": (
                    lambda current, no=shot_no: accepted.append(
                        (no, current.data["revision"]))),
            })

        results, failures = app.director._run_parallel(
            ctx, tasks, continue_on_qc_failure=True)

        assert failures == []
        assert set(results) == {1, 2}
        assert sorted(accepted) == [(1, 2), (2, 2)]
        assert calls[1] == [1, 2]
        assert calls[2] == [1, 2]
        stale = [extra for item, _status, extra in plan_events
                 if item == "shot:2"
                 and extra.get("stale_spatial_result_discarded")]
        assert stale == [{
            "stale_spatial_result_discarded": True,
            "submitted_contract_revision": 1,
            "current_contract_revision": 2,
            "candidate_count": 4,
        }]
    finally:
        app.close()
