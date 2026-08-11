"""Backend truth and optimistic-save coverage for the editable 3D set."""

import http.client
import json
import threading
from pathlib import Path

import pytest

from aifos.app import App
from aifos.project_center import DocumentConflictError
from aifos.scene_model import (build_scene_model, equirect_from_direction,
                               normalize_scene_model_contract,
                               validate_scene_model)
from aifos.scene_render import build_scene_render_contract
from aifos.web.server import (_scene3d_payload, _scene3d_save_edits,
                              serve)


ROOM = {
    "floor_width_m": 6.0,
    "floor_depth_m": 5.0,
    "wall_height_m": 3.2,
}


def _annotation(name, x, z, *, category="furniture", **extra):
    base_u, base_v = equirect_from_direction(x, -1.55, z)
    _top_u, top_v = equirect_from_direction(x, -0.65, z)
    return {
        "name": name,
        "category": category,
        "base_u": base_u,
        "base_v": base_v,
        "top_v": top_v,
        "width_u": 0.035,
        "depth_m": 0.55,
        "rotation_y_deg": 0.0,
        **extra,
    }


def _object(name, x, z, **extra):
    return {
        "name": name,
        "category": "furniture",
        "position_3d": {"x": x, "y": 0.0, "z": z},
        "width_m": 0.55,
        "height_m": 0.9,
        "depth_m": 0.55,
        "rotation_y_deg": 0.0,
        "geometry_sources": {
            "position": "panorama_floor_intersection",
            "width": "panorama_angular_span",
            "height": "panorama_vertical_ray",
            "depth": "visual_annotation",
            "rotation": "visual_annotation",
        },
        **extra,
    }


def test_duplicate_named_objects_get_stable_distinct_ids_in_render_contract():
    annotations = [
        _annotation("客椅", -1.0, 1.4),
        _annotation("客椅", 1.0, 1.4),
    ]

    first = build_scene_model(annotations, location="会客厅", room=ROOM)
    second = build_scene_model(annotations, location="会客厅", room=ROOM)
    reordered = build_scene_model(
        list(reversed(annotations)), location="会客厅", room=ROOM)
    first_ids = [obj["object_id"] for obj in first["objects"]]
    second_ids = [obj["object_id"] for obj in second["objects"]]

    assert len(set(first_ids)) == 2
    assert first_ids == second_ids
    assert {
        obj["position_3d"]["x"]: obj["object_id"]
        for obj in first["objects"]
    } == {
        obj["position_3d"]["x"]: obj["object_id"]
        for obj in reordered["objects"]
    }
    assert all(obj["semantic_type"] == "chair" for obj in first["objects"])
    assert all(obj["mount_type"] == "floor_contact"
               for obj in first["objects"])
    contract = build_scene_render_contract(first, location="会客厅")
    assert [obj["object_id"] for obj in contract["objects"]] == first_ids


def test_tabletop_prop_has_structured_support_without_false_overlap():
    model = normalize_scene_model_contract({
        "location": "书房",
        "room": dict(ROOM),
        "objects": [
            _object("书案", 0.0, 0.8, object_id="desk-1",
                    width_m=1.8, depth_m=0.9, height_m=0.82),
            {
                **_object("案上宣纸", 0.0, 0.8, object_id="paper-1",
                          category="prop", width_m=0.35, depth_m=0.25,
                          height_m=0.01),
                "position_3d": {"x": 0.0, "y": 0.82, "z": 0.8},
                "mount_type": "support_surface",
                "support_id": "desk-1",
            },
        ],
        "issues": [],
    })

    paper = next(obj for obj in model["objects"]
                 if obj["object_id"] == "paper-1")
    assert paper["semantic_type"] == "paper"
    assert paper["mount_type"] == "support_surface"
    assert paper["support_id"] == "desk-1"
    validation = validate_scene_model(model)
    assert validation["passed"] is True
    assert not any(issue.get("code") == "overlap"
                   for issue in validation["issues"])
    rendered = build_scene_render_contract(model)["objects"][1]
    assert rendered["render_transform"]["support"]["object_id"] == "desk-1"
    assert rendered["render_transform"]["source"] == "explicit_support_id"


def _editing_app(tmp_path):
    app = App(tmp_path / "workspace")
    project, _ = app.projects.get_or_create_project("物理场景编辑")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    app.projects.save_document(episode["id"], "script", {
        "scenes": [
            {"scene_no": 1, "location": "卧室",
             "physical_scene_id": "卧室"},
            {"scene_no": 2, "location": "卧室床侧",
             "physical_scene_id": "卧室"},
        ],
    })
    app.projects.save_document(episode["id"], "blocking", {
        "schema": "aifos.spatial-blocking/v3",
        "scenes": [{
            "scene_no": 2,
            "location": "卧室床侧",
            "world": dict(ROOM),
            "shots": [],
        }],
        "shot_index": {},
    })
    legacy_model = {
        "schema": "aifos.scene-model/v1",
        "location": "卧室床侧",
        "room": dict(ROOM),
        "objects": [
            _object("椅子", -0.8, 1.0),
            _object("椅子", 0.8, 1.0),
        ],
        "issues": [],
    }
    legacy_path = tmp_path / "legacy-zone-model.json"
    legacy_path.write_text(json.dumps(
        legacy_model, ensure_ascii=False), encoding="utf-8")
    legacy_row = app.assets.register(
        project["id"], "scene_model", "卧室床侧", str(legacy_path))
    return app, project, episode, legacy_path, legacy_row


def test_edit_forks_legacy_zone_into_canonical_scene_and_refreshes(tmp_path):
    app, project, episode, legacy_path, legacy_row = _editing_app(tmp_path)
    try:
        before = _scene3d_payload(app, episode["id"])
        meta = before["scene_model_revisions"]["卧室床侧"]
        assert meta == {
            "revision": 0,
            "asset_id": None,
            "physical_scene_id": "卧室",
            "zones": ["卧室", "卧室床侧"],
            "source_asset_id": legacy_row["id"],
            "source_asset_name": "卧室床侧",
            "source_revision": 1,
            "canonical_fork_required": True,
        }
        contract_objects = before["scene_model_contracts"]["卧室床侧"][
            "objects"]
        assert len({obj["object_id"] for obj in contract_objects}) == 2
        target_id = contract_objects[1]["object_id"]

        saved = _scene3d_save_edits(app, episode["id"], {
            "physical_scene_id": "卧室",
            "expected_revision": 0,
            "updates": [{
                "object_id": target_id,
                "position_3d": {"x": 1.25},
                "rotation_y_deg": 18.0,
            }],
        })

        assert saved["revision"] == 1
        assert saved["physical_scene_id"] == "卧室"
        assert saved["zones"] == ["卧室", "卧室床侧"]
        assert Path(legacy_row["uri"]) == legacy_path
        assert json.loads(legacy_path.read_text(encoding="utf-8")) \
            == before["scene_models"]["卧室床侧"]
        canonical_history = app.assets.history(
            project["id"], "scene_model", "卧室")
        assert [row["version"] for row in canonical_history] == [1]
        assert len(app.assets.history(
            project["id"], "scene_model", "卧室床侧")) == 1

        refreshed = _scene3d_payload(app, episode["id"])
        refreshed_meta = refreshed["scene_model_revisions"]["卧室床侧"]
        assert refreshed_meta["revision"] == 1
        assert refreshed_meta["asset_id"] == saved["asset_id"]
        assert refreshed_meta["canonical_fork_required"] is False
        refreshed_object = next(
            obj for obj in refreshed["scene_model_contracts"]["卧室床侧"][
                "objects"] if obj["object_id"] == target_id)
        assert refreshed_object["position_3d"]["x"] == 1.25
        assert refreshed_object["rotation_y_deg"] == 18.0
        persisted_object = next(
            obj for obj in refreshed["scene_models"]["卧室床侧"][
                "objects"] if obj["object_id"] == target_id)
        assert persisted_object["position_3d"]["x"] == 1.25
        assert {
            obj["object_id"]
            for obj in refreshed["render_contracts"]["卧室床侧"]["objects"]
        } == {
            obj["object_id"]
            for obj in refreshed["scene_model_contracts"]["卧室床侧"][
                "objects"]
        }
    finally:
        app.close()


def test_stale_revision_conflict_preserves_current_asset(tmp_path):
    app, project, episode, _legacy_path, _legacy_row = _editing_app(tmp_path)
    try:
        before = _scene3d_payload(app, episode["id"])
        target_id = before["scene_model_contracts"]["卧室床侧"][
            "objects"][0]["object_id"]
        request = {
            "physical_scene_id": "卧室",
            "expected_revision": 0,
            "updates": [{"object_id": target_id, "position_3d": {"x": -1.1}}],
        }
        first = _scene3d_save_edits(app, episode["id"], request)
        with pytest.raises(DocumentConflictError) as conflict:
            _scene3d_save_edits(app, episode["id"], request)

        assert conflict.value.expected_version == 0
        assert conflict.value.actual_version == 1
        history = app.assets.history(project["id"], "scene_model", "卧室")
        assert [row["version"] for row in history] == [1]
        assert history[0]["id"] == first["asset_id"]
    finally:
        app.close()


def test_patch_endpoint_accepts_query_episode_and_returns_visible_conflict(
        tmp_path):
    app, _project, episode, _legacy_path, _legacy_row = _editing_app(tmp_path)
    workspace = app.workspace.root
    before = _scene3d_payload(app, episode["id"])
    target_id = before["scene_model_contracts"]["卧室床侧"][
        "objects"][0]["object_id"]
    episode_id = episode["id"]
    app.close()
    httpd = serve(workspace, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def patch(body):
            conn = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=30)
            conn.request(
                "PATCH", f"/api/scene3d?episode={episode_id}",
                body=json.dumps(body),
                headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            conn.close()
            return response.status, data

        legacy_request = {
            "scene_location": "卧室床侧",
            "revision": 0,
            "edits": [{
                "object_id": target_id,
                "position_3d": {"x": -1.15},
            }],
        }
        status, saved = patch(legacy_request)
        assert status == 201
        assert saved["revision"] == 1
        assert saved["physical_scene_id"] == "卧室"

        status, conflict = patch(legacy_request)
        assert status == 409
        assert conflict["expected"] == conflict["expected_revision"] == 0
        assert conflict["actual"] == conflict["actual_revision"] == 1
        assert "刷新" in conflict["message"]
    finally:
        httpd.shutdown()
        httpd.server_close()
