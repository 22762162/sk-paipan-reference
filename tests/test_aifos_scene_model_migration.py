"""Local migration contract for saved scene-model scale policies."""

import json
from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.scene_model import (DEFAULT_CAPTURE_HEIGHT_M, SCALE_POLICY,
                               equirect_from_direction, find_object)


ROOM = {
    "floor_width_m": 10.0,
    "floor_depth_m": 7.0,
    "wall_height_m": 4.2,
}


def _legacy_bed_model(*, source=True):
    base_u, base_v = equirect_from_direction(
        0.0, -DEFAULT_CAPTURE_HEIGHT_M, 2.0)
    _top_u, top_v = equirect_from_direction(
        0.0, 0.05 - DEFAULT_CAPTURE_HEIGHT_M, 2.0)
    bed = {
        "name": "双人床",
        "category": "furniture",
        "position_3d": {"x": 0.0, "y": 0.0, "z": 2.0},
        "width_m": 0.02,
        "depth_m": 0.1,
        "height_m": 0.05,
    }
    if source:
        bed["source"] = {
            "base_u": base_u,
            "base_v": base_v,
            "top_v": top_v,
            "width_u": 0.001,
            "depth_m": 0.1,
            "rotation_y_deg": 0.0,
        }
    return {
        "schema": "aifos.scene-model/v1",
        "location": "酒店客房",
        "capture": {"x": 0.0, "y": DEFAULT_CAPTURE_HEIGHT_M, "z": 0.0},
        "room": dict(ROOM),
        "panorama_uri": "legacy-panorama.png",
        "provider": "claude_api",
        "panorama_version": 1,
        "panorama_asset_id": 1,
        "asset_version": 1,
        "objects": [bed],
        "issues": [],
    }


def _register_legacy_scene(app, tmp_path, model):
    project, _ = app.projects.get_or_create_project("旧搭景迁移")
    panorama = tmp_path / "hotel-panorama.png"
    panorama.write_bytes(b"real-panorama-contract-placeholder")
    pano_row = app.assets.register(
        project["id"], "scene_art", "酒店客房::view:panorama",
        str(panorama), meta={
            "provider": "claude_api",
            "real": True,
            "image_quality": "high",
        })
    model["panorama_version"] = pano_row["version"]
    model["panorama_asset_id"] = pano_row["id"]
    legacy_path = tmp_path / "scene_model_hotel_v1.json"
    legacy_path.write_text(
        json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
    legacy_row = app.assets.register(
        project["id"], "scene_model", "酒店客房", str(legacy_path),
        meta={
            "provider": "claude_api",
            "real": True,
            "panorama_version": pano_row["version"],
            "panorama_asset_id": pano_row["id"],
        })
    return project, pano_row, legacy_row, legacy_path


def test_same_panorama_legacy_model_migrates_locally_once(
        tmp_path, monkeypatch):
    app = App(tmp_path / "workspace")
    try:
        project, pano_row, legacy_row, legacy_path = _register_legacy_scene(
            app, tmp_path, _legacy_bed_model())
        calls = []

        def forbidden_router_call(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("same-panorama migration must stay local")

        monkeypatch.setattr(app.director.router, "call", forbidden_router_call)

        migrated = app.director.build_scene_model(
            project["title"], "酒店客房")

        assert calls == []
        assert migrated["scale_policy"] == SCALE_POLICY
        assert migrated["panorama_version"] == pano_row["version"]
        assert migrated["panorama_asset_id"] == pano_row["id"]
        assert migrated["asset_version"] == 2
        assert migrated["migration"] == {
            "mode": "local_saved_object_sources",
            "from_asset_id": legacy_row["id"],
            "from_asset_version": 1,
            "from_scale_policy": "legacy",
            "to_scale_policy": SCALE_POLICY,
        }
        bed = find_object(migrated, "双人床")
        assert bed["width_m"] == 1.8
        assert bed["depth_m"] == 2.0
        assert bed["height_m"] == 0.65
        assert bed["scale_adjusted"] is True

        history = app.assets.history(
            project["id"], "scene_model", "酒店客房")
        assert [row["version"] for row in history] == [1, 2]
        assert Path(history[0]["uri"]) == legacy_path
        assert json.loads(legacy_path.read_text(encoding="utf-8")).get(
            "scale_policy") is None
        migrated_meta = app.assets.meta(history[1])
        assert migrated_meta["scale_policy"] == SCALE_POLICY
        assert migrated_meta["migration"] == "local_saved_object_sources"

        reused = app.director.build_scene_model(
            project["title"], "酒店客房")
        assert reused == migrated
        assert calls == []
        assert len(app.assets.history(
            project["id"], "scene_model", "酒店客房")) == 2
        assert all(
            row["kind"] not in {
                "image", "first_frame", "last_frame", "video"}
            for row in app.assets.list(project["id"]))
    finally:
        app.close()


def test_incomplete_saved_sources_fail_closed_without_model_call(
        tmp_path, monkeypatch):
    app = App(tmp_path / "workspace")
    try:
        project, _pano_row, _legacy_row, _legacy_path = (
            _register_legacy_scene(
                app, tmp_path, _legacy_bed_model(source=False)))
        calls = []

        def forbidden_router_call(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("migration failure must not call a provider")

        monkeypatch.setattr(app.director.router, "call", forbidden_router_call)

        with pytest.raises(AifosError, match="无法无损本地迁移"):
            app.director.build_scene_model(project["title"], "酒店客房")

        assert calls == []
        assert len(app.assets.history(
            project["id"], "scene_model", "酒店客房")) == 1
    finally:
        app.close()


def test_director_collects_scene_model_before_blocking_exists(tmp_path):
    """Initial blocking must already receive the current room dimensions."""
    app = App(tmp_path / "workspace")
    try:
        project, _pano_row, _legacy_row, _legacy_path = (
            _register_legacy_scene(app, tmp_path, _legacy_bed_model()))

        models = app.director._previz_scene_models({
            "project": project,
            "script": {
                "scenes": [{"scene_no": 1, "location": "酒店客房"}],
            },
            "storyboard": {"shots": []},
            # Deliberately no blocking: this is the first-build path.
        })

        assert set(models) == {"酒店客房"}
        assert models["酒店客房"]["room"] == ROOM
    finally:
        app.close()
