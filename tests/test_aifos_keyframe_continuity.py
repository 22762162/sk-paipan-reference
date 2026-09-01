"""Keyframes use dependency waves instead of blind all-shot parallelism."""

import json
import struct
import zlib

from aifos.app import App
from aifos.continuity_graph import build_keyframe_continuity_plan
from aifos.production.base import ProviderResult


def _png(seed=0, width=9, height=16):
    def chunk(kind, data):
        crc = zlib.crc32(kind)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", crc))

    pixels = (b"\x00" + bytes((seed % 255, 30, 60)) * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                          8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND", b""))


def test_continuity_plan_keeps_reverse_angles_in_one_chain():
    plan = build_keyframe_continuity_plan([
        {"shot_no": 1, "scene_no": 1, "camera": "正打"},
        {"shot_no": 2, "scene_no": 1, "camera": "反打"},
        {"shot_no": 3, "scene_no": 2},
        {"shot_no": 4, "scene_no": 2, "time_jump": True},
    ], {1: "书房", 2: "街道"})

    assert [group["shot_nos"] for group in plan["groups"]] == [
        [1, 2], [3], [4]]
    assert plan["predecessor_by_shot"] == {
        1: None, 2: 1, 3: None, 4: None}


def test_continuity_plan_crosses_scene_numbers_in_same_physical_set():
    plan = build_keyframe_continuity_plan([
        {"shot_no": 5, "scene_no": 1,
         "era_context": "现代卧室，能力印记尚未出现"},
        {"shot_no": 6, "scene_no": 2,
         "era_context": "现代卧室，能力印记已经形成"},
        {"shot_no": 12, "scene_no": 2},
        {"shot_no": 13, "scene_no": 3},
    ], {
        1: "虞家别墅·虞寻欢卧室",
        2: "虞家别墅·虞寻欢卧室",
        3: "虞家别墅·虞寻欢卧室",
    })

    assert [group["shot_nos"] for group in plan["groups"]] == [
        [5, 6, 12, 13]]
    assert plan["predecessor_by_shot"] == {
        5: None, 6: 5, 12: 6, 13: 12}


def test_continuity_plan_explicit_break_still_wins_in_same_set():
    plan = build_keyframe_continuity_plan([
        {"shot_no": 1, "scene_no": 1},
        {"shot_no": 2, "scene_no": 2, "time_jump": True},
    ], {1: "同一卧室", 2: "同一卧室"})

    assert [group["shot_nos"] for group in plan["groups"]] == [[1], [2]]


def _stage_fixture(tmp_path, monkeypatch, *, fail_shot=0):
    app = App(tmp_path / "ws")
    project, _ = app.projects.get_or_create_project("关键帧依赖波次")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    shots = [
        {"shot_no": 1, "scene_no": 1, "characters": []},
        {"shot_no": 2, "scene_no": 1, "characters": []},
        {"shot_no": 3, "scene_no": 2, "characters": []},
        {"shot_no": 4, "scene_no": 2, "characters": []},
    ]
    out_root = tmp_path / "out"
    out_root.mkdir()
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": out_root, "force": True, "fresh_assets": True,
        "run_id": 77,
        "script": {"scenes": [
            {"scene_no": 1, "location": "书房"},
            {"scene_no": 2, "location": "酒店"},
        ]},
        "storyboard": {"shots": shots},
    }
    app.director._plan_write(ctx, {"items": [{
        "id": f"shot:{shot['shot_no']}", "shot_no": shot["shot_no"],
        "category": "shot_image", "label": f"镜头{shot['shot_no']}",
        "status": "pending", "error": "",
    } for shot in shots]})

    monkeypatch.setattr(app.director, "_plan_seed_shots", lambda _ctx: None)
    monkeypatch.setattr(app.director, "_distill_lessons", lambda _ctx: 0)
    monkeypatch.setattr(
        app.director, "reconcile_completed_shot_images",
        lambda _ctx: {"recovered": 0, "awaiting_human_shots": []})
    monkeypatch.setattr("aifos.director.write_relations",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app.director, "_persist_storyboard_reviews",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app.director, "_generation_preflight_issues",
                        lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app.director, "_shot_qc_spec",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app.director, "_generation_rule_lines",
                        lambda *_args, **_kwargs: ())
    monkeypatch.setattr(app.director, "_candidate_group_technical_incomplete",
                        lambda _result: False)
    monkeypatch.setattr(app.director, "_candidate_selection_pending",
                        lambda _result: False)
    monkeypatch.setattr(app.director, "_shot_payload", lambda _ctx, shot: {
        "shot_no": shot["shot_no"],
        "prompt": f"镜头{shot['shot_no']}",
        "prompt_compact": f"镜头{shot['shot_no']}",
        "_reference_prompt_base": f"镜头{shot['shot_no']}",
        "characters": [],
        "quality_decision": {
            "level": "medium", "recommended": "medium",
            "source": "test", "rule": "", "reasons": [],
        },
    })

    def attach(payload):
        payload["reference_manifest"] = ([{
            "index": 1,
            "uri": payload["chain_first_uri"],
            "role": "continuity",
            "mandatory": bool(payload.get(
                "previous_shot_reference_required")),
        }] if payload.get("chain_first_uri") else [])

    monkeypatch.setattr(app.director, "_attach_reference_manifest", attach)
    waves = []

    def run(_ctx, tasks, **kwargs):
        waves.append([int(task["tag"]) for task in tasks])
        results, failures = {}, []
        for task in tasks:
            shot_no = int(task["tag"])
            if shot_no == fail_shot:
                failures.append((task, RuntimeError("provider failed")))
                continue
            output = out_root / f"shot-{shot_no}.png"
            output.write_bytes(_png(shot_no))
            result = ProviderResult(
                provider="stub", model="stub", cost=0.0,
                uri=str(output), data={})
            result.qc = {"passed": True}
            results[shot_no] = result
        return results, failures

    monkeypatch.setattr(app.director, "_run_parallel", run)
    return app, ctx, waves


def test_stage_images_runs_scene_groups_in_parallel_and_chains_exact_prior(
        tmp_path, monkeypatch):
    app, ctx, waves = _stage_fixture(tmp_path, monkeypatch)
    try:
        report = app.director._stage_images(ctx)

        assert waves == [[1, 3], [2, 4]]
        assert report["count"] == 4
        image_by_shot = {row["shot_no"]: row for row in ctx["images"]}
        row2 = app.assets.latest(
            ctx["project"]["id"], "image", "e001_shot002")
        row4 = app.assets.latest(
            ctx["project"]["id"], "image", "e001_shot004")
        meta2 = json.loads(row2["meta"])
        meta4 = json.loads(row4["meta"])
        assert meta2["continuity_dependency"]["previous_shot_no"] == 1
        assert meta4["continuity_dependency"]["previous_shot_no"] == 3
        assert meta2["fresh_run_id"] == 77
        assert image_by_shot[2]["continuity_group_id"]
    finally:
        app.close()


def test_failed_group_waits_but_other_group_continues(
        tmp_path, monkeypatch):
    app, ctx, waves = _stage_fixture(
        tmp_path, monkeypatch, fail_shot=1)
    try:
        report = app.director._stage_images(ctx)

        assert waves == [[1, 3], [4]]
        assert [row["shot_no"] for row in ctx["images"]] == [3, 4]
        assert report["technical_incomplete"] is True
        assert set(report["technical_incomplete_shots"]) >= {1, 2}
        plan = app.director._plan_read(ctx)
        shot2 = next(item for item in plan["items"]
                     if item["id"] == "shot:2")
        assert shot2["status"] == "pending"
        assert shot2["continuity_predecessor_shot"] == 1
    finally:
        app.close()


def test_exact_previous_reference_is_mandatory_but_not_camera_override(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        anchor = tmp_path / "anchor.png"
        anchor.write_bytes(_png(9))
        payload = {
            "chain_first_uri": str(anchor),
            "previous_shot_reference_required": True,
            "characters": [],
        }

        manifest = app.director._reference_manifest(payload)

        assert len(manifest) == 1
        assert manifest[0]["role"] == "continuity"
        assert manifest[0]["mandatory"] is True
        assert "camera_override" in manifest[0]["excludes"]
        assert "relative_positions" in manifest[0]["inherits"]
    finally:
        app.close()


def test_upstream_pixel_change_invalidates_downstream_dependency(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("依赖失效")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        previous = tmp_path / "previous.png"
        current = tmp_path / "current.png"
        previous.write_bytes(_png(1))
        current.write_bytes(_png(2))
        app.assets.register(
            project["id"], "image", "e001_shot001",
            uri=str(previous), meta={"fresh_run_id": 8})
        ctx = {
            "project": dict(project), "episode": dict(episode),
        }
        dependency = app.director._keyframe_continuity_dependency(
            ctx, 1, str(previous))
        current_row = app.assets.register(
            project["id"], "image", "e001_shot002", uri=str(current),
            meta={"continuity_dependency": dependency})
        assert app.director._continuity_dependency_matches(
            current_row, dependency)

        previous.write_bytes(_png(9))
        changed = app.director._keyframe_continuity_dependency(
            ctx, 1, str(previous))
        assert changed["token"] != dependency["token"]
        assert not app.director._continuity_dependency_matches(
            current_row, changed)
    finally:
        app.close()


def test_derived_continuity_upload_position_does_not_change_semantic_hash(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        shot = {"shot_no": 2, "scene_no": 1, "characters": []}
        scene_ref = {
            "index": 1, "image_index": 1, "role": "scene",
            "uri": "/scene.png", "label": "场景",
        }
        before = {
            "reference_manifest": [dict(scene_ref)],
            "prompt_contract": {"references": [dict(scene_ref)]},
        }
        shifted = dict(scene_ref, index=2, image_index=2)
        continuity = {
            "index": 1, "image_index": 1, "role": "continuity",
            "uri": "/previous.png", "label": "上一镜",
        }
        after = {
            "reference_manifest": [continuity, shifted],
            "prompt_contract": {"references": [continuity, shifted]},
        }

        assert app.director._shot_content_hash(
            shot, before) == app.director._shot_content_hash(shot, after)
    finally:
        app.close()
