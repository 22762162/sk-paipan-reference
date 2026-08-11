"""Clean scene geometry must reach both image and Seedance reference chains."""

import hashlib
import json
from pathlib import Path

import pytest

from aifos.app import App


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
       b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89")


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


@pytest.fixture()
def deterministic_svg_png(monkeypatch):
    """Make tests independent of the host SVG converter.

    The fake PNG remains content-addressed: a geometry pixel-contract change
    changes its bytes and therefore exercises the production cache correctly.
    """
    def render(svg_path, png_path):
        digest = hashlib.sha256(Path(svg_path).read_bytes()).digest()
        Path(png_path).write_bytes(PNG + digest)
        return ""

    monkeypatch.setattr(
        "aifos.spatial_blocking._render_svg_png", render)
    monkeypatch.setattr(
        "aifos.director.slice_for_block", lambda *_args, **_kwargs: "")


def _fixture(app, tmp_path):
    project, _ = app.projects.get_or_create_project("干净空间参考")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    location = "同一客厅"
    pano = tmp_path / "pano.png"
    pano.write_bytes(PNG)
    pano_row = app.assets.register(
        project["id"], "scene_art",
        app.director._scene_view_asset_name(location, "panorama"),
        uri=str(pano), meta={
            "image_quality": "high",
            "physical_scene_id": location,
            "projection_type": "equirectangular_360x180",
            "equirectangular_validated": True,
        })
    model = {
        "schema": "aifos.scene-model/v1",
        "panorama_version": int(pano_row["version"]),
        "room": {"width_m": 6.0, "depth_m": 5.0, "height_m": 3.0},
        "objects": [{
            "name": "长沙发", "category": "furniture",
            "position_3d": {"x": -1.2, "y": 0.0, "z": 0.4},
            "width_m": 2.1, "depth_m": .8, "height_m": .9,
            "rotation_y_deg": 0,
        }, {
            "name": "茶几", "category": "furniture",
            "position_3d": {"x": .2, "y": 0.0, "z": .2},
            "width_m": 1.0, "depth_m": .6, "height_m": .45,
            "rotation_y_deg": 0,
        }],
    }
    model_path = tmp_path / "scene-model-v1.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    model_row = app.assets.register(
        project["id"], "scene_model", location, uri=str(model_path),
        meta={"panorama_version": pano_row["version"]})
    camera = {
        "start": {"x": 260, "y": 600},
        "end": {"x": 420, "y": 480},
        "moving": True,
        "start_3d": {"x": -2.0, "y": 1.55, "z": 2.0},
        "end_3d": {"x": -1.2, "y": 1.55, "z": 1.2},
        "target_3d": {"x": 0.0, "y": 1.0, "z": 0.0},
    }
    block = {
        "shot_no": 1, "scene_no": 1, "character_count": 0,
        "camera": camera, "actors": [],
    }
    blocking = {
        "schema": "aifos.spatial-blocking/v3",
        "source_fingerprint": "blocking-v1",
        "scene_model_fingerprint": "model-v1",
        "shot_index": {"1": block},
        "scenes": [{
            "scene_no": 1, "location": location,
            "world": {"width_m": 6.0, "depth_m": 5.0},
            "shots": [block],
        }],
    }
    script = {
        "characters": [],
        "scenes": [{"scene_no": 1, "location": location}],
    }
    storyboard = {"shots": [{
        "shot_no": 1, "scene_no": 1, "characters": [],
        "location": location, "camera": camera, "duration": 5.0,
    }]}
    app.projects.save_document(episode["id"], "script", script)
    app.projects.save_document(episode["id"], "storyboard", storyboard)
    app.projects.save_document(episode["id"], "blocking", blocking)
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": app.workspace.artifacts_dir
        / f"p{project['id']:03d}" / "e001",
        "script": script, "storyboard": storyboard, "blocking": blocking,
    }
    return ctx, model, model_row, location


def test_furniture_move_changes_clean_geometry_file_hash(
        app, tmp_path, deterministic_svg_png):
    ctx, model, first_model_row, location = _fixture(app, tmp_path)
    first = app.director._spatial_scene_clean_row(ctx, location, 1)
    first_meta = app.director._asset_meta(first)
    clean_svg = Path(first["uri"]).with_suffix(".svg").read_text(
        encoding="utf-8")
    assert "<text" not in clean_svg
    assert "<line" not in clean_svg
    assert "marker" not in clean_svg

    changed = json.loads(json.dumps(model))
    changed["objects"][1]["position_3d"]["x"] = 1.7
    changed_path = tmp_path / "scene-model-v2.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    second_model_row = app.assets.register(
        ctx["project"]["id"], "scene_model", location,
        uri=str(changed_path), meta={"panorama_version": 1},
        new_version=True)
    ctx["blocking"]["scene_model_fingerprint"] = "model-v2"
    second = app.director._spatial_scene_clean_row(ctx, location, 1)
    second_meta = app.director._asset_meta(second)

    assert second_model_row["version"] > first_model_row["version"]
    assert second["version"] > first["version"]
    assert first_meta["render_signature"] != second_meta["render_signature"]
    assert first_meta["file_sha256"] != second_meta["file_sha256"]
    assert Path(first["uri"]).read_bytes() != Path(second["uri"]).read_bytes()


def test_image_and_video_chains_receive_three_separate_space_roles(
        app, tmp_path, deterministic_svg_png):
    ctx, _model, _model_row, location = _fixture(app, tmp_path)
    clean = app.director._spatial_scene_clean_row(ctx, location, 1)
    movement = tmp_path / "movement-control.png"
    movement.write_bytes(PNG)
    block = ctx["blocking"]["shot_index"]["1"]
    block["spatial_reference_uri"] = str(movement)
    ctx["blocking"]["scenes"][0]["shots"][0][
        "spatial_reference_uri"] = str(movement)

    # Image-generation manifest: appearance, measured geometry and movement
    # control are three distinct uploaded responsibilities.
    refs = app.director._art_refs(
        ctx, [], location, shot_no=1, spatial_ref=str(movement))
    assert refs["spatial_scene_clean_ref"] == clean["uri"]
    payload = {
        "prompt": "同一客厅内的静态关键帧", "location": location,
        **refs,
    }
    app.director._attach_reference_manifest(payload)
    roles = {item["role"] for item in payload["reference_manifest"]}
    assert {"scene", "spatial_scene_clean", "spatial"} <= roles

    # Seedance auto-selection must carry the same three facts.  The annotated
    # movement asset is registered by the production helper, not by the test.
    rows = app.director._auto_video_reference_rows(ctx, 1)
    kinds = {row["kind"] for row in rows}
    assert {"scene_art", "spatial_scene_clean", "spatial_blocking"} <= kinds
    bindings = {
        row["kind"]: app.director._video_reference_binding(
            row, shot=ctx["storyboard"]["shots"][0], script=ctx["script"])
        for row in rows
    }
    assert "运动控制" in bindings["spatial_blocking"]
    assert "固定家具" in bindings["spatial_scene_clean"]
    assert "唯一空间真相" in bindings["scene_art"]
