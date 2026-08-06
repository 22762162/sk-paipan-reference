"""真实三维场景在 API、几何模型和两个前端查看器之间的契约。

本文件刻意独立于 ``test_aifos_scene_model.py``：该文件可能正由其他
协作者修改。这里守住跨层接线，避免后端已经反解出家具尺寸，前端却仍然
只显示全景贴图或把所有物件画成同一个默认方块。
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from aifos.app import App
from aifos.scene_model import (DEFAULT_CAPTURE_HEIGHT_M, build_object,
                               camera_placement_issues,
                               equirect_from_direction,
                               scene_layout_clause)
from aifos.web.server import _episode_payload, _scene3d_payload
from aifos.previz_checks import previz_report


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "aifos" / "web" / "static" / "app.js"
SCENE3D_HTML = ROOT / "aifos" / "web" / "static" / "scene3d.html"
ROOM = {
    "floor_width_m": 10.0,
    "floor_depth_m": 7.0,
    "wall_height_m": 4.2,
}


@pytest.fixture()
def scene_app(tmp_path):
    app = App(tmp_path / "ws")
    project, _ = app.projects.get_or_create_project("三维场景契约")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    app.projects.save_document(episode["id"], "blocking", {
        "schema": "aifos.spatial-blocking/v3",
        "summary": {"scenes": 2, "shots": 1, "actors": 1},
        "validation": {"passed": True, "issues": []},
        "scenes": [
            {
                "scene_no": 1,
                "location": "书阁",
                "world": dict(ROOM),
                "svg_uri": "",
                "shots": [],
            },
            {
                "scene_no": 2,
                "location": "坏数据房间",
                "world": dict(ROOM),
                "svg_uri": "",
                "shots": [],
            },
        ],
        "shot_index": {},
    })

    model = {
        "schema": "aifos.scene-model/v1",
        "location": "书阁",
        "capture": {"x": 0.0, "y": 1.55, "z": 0.0},
        "room": dict(ROOM),
        "panorama_version": 1,
        "objects": [{
            "name": "书案",
            "category": "furniture",
            "position_3d": {"x": 0.4, "y": 0.0, "z": 1.2},
            "width_m": 1.6,
            "depth_m": 0.8,
            "height_m": 0.9,
            "rotation_y_deg": 30.0,
            "geometry_sources": {
                "position": "panorama_floor_intersection",
                "depth": "visual_annotation",
                "rotation": "visual_annotation",
            },
        }],
        "issues": [],
    }
    valid_path = tmp_path / "scene_model_book_room.json"
    valid_path.write_text(
        json.dumps(model, ensure_ascii=False), encoding="utf-8")
    bad_path = tmp_path / "scene_model_bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    app.assets.register(
        project["id"], "scene_model", "书阁", str(valid_path))
    app.assets.register(
        project["id"], "scene_model", "坏数据房间", str(bad_path))
    pano_path = app.workspace.artifacts_dir / "scene_contract_panorama.png"
    pano_path.parent.mkdir(parents=True, exist_ok=True)
    pano_path.write_bytes(b"contract-panorama-placeholder")
    app.assets.register(
        project["id"], "scene_art", "书阁::view:panorama", str(pano_path),
        meta={"image_quality": "high", "provider": "contract-test",
              "real": True})

    yield {
        "app": app,
        "project": project,
        "episode": episode,
        "model": model,
    }
    app.close()


def _scene_by_location(blocking, location):
    return next(
        scene for scene in blocking["scenes"]
        if scene["location"] == location)


def test_episode_payload_exposes_scene_model_and_ignores_bad_json(scene_app):
    payload = _episode_payload(
        scene_app["app"], scene_app["episode"]["id"])

    good = _scene_by_location(payload["blocking"], "书阁")
    bad = _scene_by_location(payload["blocking"], "坏数据房间")
    assert good["scene_model"] == scene_app["model"]
    assert "scene_model" not in bad


def test_scene3d_payload_exposes_scene_models_map_and_ignores_bad_json(
        scene_app):
    payload = _scene3d_payload(
        scene_app["app"], scene_app["episode"]["id"])

    assert payload["scene_models"] == {"书阁": scene_app["model"]}


def test_blocking_scene_physics_uses_real_boxes_as_a_hard_gate(scene_app):
    app = scene_app["app"]
    app.config.data.setdefault("defaults", {})["space_first"] = True
    blocking, _version = app.projects.latest_document(
        scene_app["episode"]["id"], "blocking")
    blocking["scenes"][0]["shots"] = [{
        "shot_no": 5,
        "scene_no": 1,
        "actors": [{
            "name": "穿越者",
            "route_3d": [{
                "x": 0.4, "y": 0.0, "z": 1.2, "phase": "start",
            }],
        }],
        "camera": {
            "route_3d": [{
                "x": 0.4, "y": 0.5, "z": 1.2, "phase": "start",
            }],
            "start_3d": {"x": 0.4, "y": 0.5, "z": 1.2},
        },
    }]

    issues = app.director._attach_scene_physics({
        "project": scene_app["project"],
    }, blocking)

    assert blocking["validation"]["scene_physics_passed"] is False
    assert blocking["validation"]["passed"] is False
    assert any(
        issue["severity"] == "block"
        and issue["shot_no"] == 5
        and issue["field"] in {"actor_furniture", "camera_furniture"}
        for issue in issues)


def test_generated_blocking_can_auto_repair_actor_collision(scene_app):
    app = scene_app["app"]
    blocking, _version = app.projects.latest_document(
        scene_app["episode"]["id"], "blocking")
    actor = {
        "name": "穿越者",
        "start": {"x": 500, "y": 350},
        "end": {"x": 500, "y": 350},
        "route": [{"x": 500, "y": 350, "phase": "fixed"}],
        "start_3d": {"x": 0.4, "y": 0.0, "z": 1.2},
        "end_3d": {"x": 0.4, "y": 0.0, "z": 1.2},
        "route_3d": [{
            "x": 0.4, "y": 0.0, "z": 1.2, "phase": "fixed",
        }],
    }
    shot = {
        "shot_no": 5,
        "scene_no": 1,
        "actors": [actor],
        "camera": {},
    }
    blocking["scenes"][0]["shots"] = [shot]
    blocking["shot_index"]["5"] = json.loads(json.dumps(shot))

    issues = app.director._attach_scene_physics(
        {"project": scene_app["project"]}, blocking,
        repair_actor_collisions=True)

    assert not any(
        issue["severity"] == "block"
        and issue["field"] == "actor_furniture"
        for issue in issues)
    assert blocking["validation"]["scene_physics_adjustments"]
    assert actor["start_3d"] == actor["end_3d"]
    assert actor["start"] == actor["end"]
    assert actor["route"][0]["phase"] == "fixed"
    assert {"x": actor["route"][0]["x"], "y": actor["route"][0]["y"]} \
        == actor["start"]
    assert blocking["shot_index"]["5"]["actors"] == shot["actors"]


def test_scene_physics_replans_route_around_furniture(scene_app):
    app = scene_app["app"]
    blocking, _version = app.projects.latest_document(
        scene_app["episode"]["id"], "blocking")
    actor = {
        "name": "穿越者",
        "start": {"x": 250, "y": 350},
        "end": {"x": 750, "y": 350},
        "route": [
            {"x": 250, "y": 350, "phase": "start"},
            {"x": 750, "y": 350, "phase": "end"},
        ],
        "start_3d": {"x": -1.8, "y": 0.0, "z": 1.2},
        "end_3d": {"x": 2.2, "y": 0.0, "z": 1.2},
        "route_3d": [
            {"x": -1.8, "y": 0.0, "z": 1.2, "phase": "start"},
            {"x": 2.2, "y": 0.0, "z": 1.2, "phase": "end"},
        ],
        "moving": True,
    }
    shot = {
        "shot_no": 7, "scene_no": 1, "actors": [actor], "camera": {}}
    blocking["scenes"][0]["shots"] = [shot]
    blocking["shot_index"]["7"] = json.loads(json.dumps(shot))

    app.director._attach_scene_physics(
        {"project": scene_app["project"]}, blocking,
        repair_actor_collisions=True)

    assert len(actor["route_3d"]) >= 3
    report = previz_report(
        blocking,
        {"shots": [{"shot_no": 7}]},
        {"书阁": scene_app["model"]},
    )
    assert not [
        issue for issue in report["issues"]
        if issue["kind"] == "path_collision"]


def test_generated_blocking_can_auto_repair_camera_collision(scene_app):
    app = scene_app["app"]
    blocking, _version = app.projects.latest_document(
        scene_app["episode"]["id"], "blocking")
    camera = {
        "start": {"x": 500, "y": 350},
        "end": {"x": 500, "y": 350},
        "route": [{"x": 500, "y": 350, "phase": "fixed"}],
        "start_3d": {"x": 0.4, "y": 0.5, "z": 1.2},
        "end_3d": {"x": 0.4, "y": 0.5, "z": 1.2},
        "target_start_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
        "target_end_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
        "target_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
        "route_3d": [{
            "x": 0.4, "y": 0.5, "z": 1.2, "phase": "fixed",
        }],
        "director_camera": {},
    }
    shot = {
        "shot_no": 6,
        "scene_no": 1,
        "actors": [],
        "camera": camera,
    }
    blocking["scenes"][0]["shots"] = [shot]
    blocking["shot_index"]["6"] = json.loads(json.dumps(shot))

    issues = app.director._attach_scene_physics(
        {"project": scene_app["project"]}, blocking,
        repair_actor_collisions=True)

    assert not any(
        issue["severity"] == "block"
        and issue["field"] == "camera_furniture"
        for issue in issues)
    camera_adjustments = [
        item for item in
        blocking["validation"]["scene_physics_adjustments"]
        if item.get("type") == "camera"]
    assert camera_adjustments
    assert camera["start_3d"] == camera["end_3d"]
    assert camera["start"] == camera["end"]
    assert blocking["shot_index"]["6"]["camera"] == camera


def _annotation_for_ground_point(x, z, **extra):
    u, v = equirect_from_direction(
        x, -DEFAULT_CAPTURE_HEIGHT_M, z)
    annotation = {
        "name": "书案",
        "category": "furniture",
        "base_u": u,
        "base_v": v,
    }
    annotation.update(extra)
    return annotation


def test_build_object_keeps_real_depth_and_normalizes_y_rotation():
    obj = build_object(
        _annotation_for_ground_point(
            1.5, 2.0, width_u=0.06,
            depth_m="0.80", rotation_y_deg="450"),
        room=ROOM)

    assert obj is not None
    assert obj["depth_m"] == pytest.approx(0.8)
    assert obj["rotation_y_deg"] == pytest.approx(90.0)
    assert set(obj["position_3d"]) == {"x", "y", "z"}
    assert obj["width_m"] > 0


def test_clamped_object_footprint_not_only_center_stays_inside_room():
    obj = build_object(
        _annotation_for_ground_point(
            20.0, 0.0, width_u=0.03,
            depth_m=0.8, rotation_y_deg=0),
        room=ROOM)

    assert obj is not None and obj["inside_room"] is False
    half_room_width = ROOM["floor_width_m"] / 2
    half_room_depth = ROOM["floor_depth_m"] / 2
    assert abs(obj["position_3d"]["x"]) + obj["width_m"] / 2 \
        <= half_room_width + 1e-6
    assert abs(obj["position_3d"]["z"]) + obj["depth_m"] / 2 \
        <= half_room_depth + 1e-6


def test_scene_layout_clause_names_all_three_dimensions_and_rotation():
    model = {
        "objects": [{
            "name": "书案",
            "category": "furniture",
            "position_3d": {"x": 0.4, "y": 0.0, "z": 1.2},
            "distance_m": 1.26,
            "width_m": 1.6,
            "depth_m": 0.8,
            "height_m": 0.9,
            "rotation_y_deg": 30.0,
        }],
    }

    text = scene_layout_clause(model)

    assert "宽1.6米" in text
    assert "深0.8米" in text
    assert "高0.9米" in text
    assert "30" in text
    assert any(word in text for word in ("朝向", "旋转", "Y轴"))


def test_episode_blocking_view_consumes_real_box_depth_and_rotation():
    source = APP_JS.read_text(encoding="utf-8")

    required = (
        "scene.scene_model",
        "objects",
        "position_3d",
        "width_m",
        "depth_m",
        "height_m",
        "rotation_y_deg",
        "drawSceneBoxes();",
    )
    for token in required:
        assert token in source, f"episode 3D 调度未消费 {token}"


def test_standalone_scene3d_view_consumes_scene_model_objects():
    source = SCENE3D_HTML.read_text(encoding="utf-8")

    required = (
        "scene_models",
        "objects",
        "position_3d",
        "width_m",
        "depth_m",
        "height_m",
        "rotation_y_deg",
    )
    for token in required:
        assert token in source, f"独立 3D 查看器未消费 {token}"


def _extract_js_function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"无法提取 JavaScript 函数 {name}")


def test_stick_figure_shoulder_width_scales_with_actor_height():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 Node.js，跳过前端几何行为测试")
    function_source = _extract_js_function(
        APP_JS.read_text(encoding="utf-8"),
        "blockingStickFigureGeometry")
    script = f"""
{function_source}
const short = blockingStickFigureGeometry(
  {{x: 0, y: 0, z: 0}}, 1.2, "standing");
const tall = blockingStickFigureGeometry(
  {{x: 0, y: 0, z: 0}}, 1.9, "standing");
const width = (figure) => Math.abs(
  figure.joints.shoulderR.x - figure.joints.shoulderL.x);
process.stdout.write(JSON.stringify({{
  shortWidth: width(short),
  tallWidth: width(tall),
  shortHeadY: short.joints.head.y,
  tallHeadY: tall.joints.head.y
}}));
"""
    result = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True)
    measured = json.loads(result.stdout)

    assert measured["tallWidth"] > measured["shortWidth"]
    assert measured["tallHeadY"] > measured["shortHeadY"]
    assert measured["shortWidth"] / 1.2 == pytest.approx(
        measured["tallWidth"] / 1.9, rel=1e-6)
    assert measured["shortWidth"] / 1.2 == pytest.approx(0.23, rel=1e-6)


def test_scene_model_dimensions_are_finite_positive_numbers():
    obj = build_object(
        _annotation_for_ground_point(
            1.5, 2.0, width_u=0.06,
            depth_m=0.8, rotation_y_deg=-30),
        room=ROOM)

    for field in ("width_m", "depth_m"):
        assert math.isfinite(obj[field]) and obj[field] > 0
    assert math.isfinite(obj["rotation_y_deg"])


def test_camera_route_through_measured_real_box_is_blocked():
    model = {
        "room": dict(ROOM),
        "objects": [{
            "name": "书案",
            "category": "furniture",
            "position_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
            "width_m": 1.6,
            "depth_m": 0.8,
            "height_m": 0.9,
            "rotation_y_deg": 30.0,
            "geometry_sources": {
                "position": "panorama_floor_intersection",
                "depth": "visual_annotation",
                "rotation": "visual_annotation",
            },
        }],
    }
    issues = camera_placement_issues(model, {
        "route_3d": [{
            "x": 0.0, "y": 0.55, "z": 0.0, "phase": "start",
        }],
    })

    assert any(
        issue["severity"] == "block"
        and issue["field"] == "camera_furniture"
        for issue in issues)


def test_legacy_overheight_desk_does_not_block_camera_above_real_desk():
    model = {
        "room": dict(ROOM),
        "objects": [{
            "name": "验牒书案(大画案)",
            "category": "furniture",
            "position_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
            "width_m": 1.6,
            "depth_m": 0.8,
            "height_m": 1.51,
            "rotation_y_deg": 0.0,
            "geometry_sources": {
                "depth": "visual_annotation",
                "rotation": "visual_annotation",
            },
        }],
    }

    issues = camera_placement_issues(model, {
        "route_3d": [{
            "x": 0.0, "y": 1.43, "z": 0.0, "phase": "fixed",
        }],
    })

    assert not any(issue["severity"] == "block" for issue in issues)


def test_seated_actor_at_declared_desk_support_is_not_hard_blocked():
    from aifos.scene_model import actor_placement_issues

    model = {
        "room": dict(ROOM),
        "objects": [{
            "name": "帘后木案",
            "category": "furniture",
            "position_3d": {"x": -2.28, "y": 0.0, "z": -0.07},
            "width_m": 0.87,
            "depth_m": 0.6,
            "height_m": 1.18,
            "rotation_y_deg": 0.0,
            "geometry_sources": {
                "depth": "visual_annotation",
                "rotation": "visual_annotation",
            },
        }],
    }
    actor = {
        "name": "顾明昭",
        "pose_start": "sitting",
        "pose_end": "sitting",
        "support_start": "座椅与桌面",
        "support_end": "座椅与桌面",
        "route_3d": [{
            "x": -2.44, "y": 0.0, "z": -0.3, "phase": "fixed",
        }],
    }

    issues = actor_placement_issues(model, [actor])

    assert any(issue["severity"] == "warning" for issue in issues)
    assert not any(issue["severity"] == "block" for issue in issues)


def test_missing_camera_contract_does_not_invent_origin_collision():
    model = {
        "room": dict(ROOM),
        "objects": [{
            "name": "书案",
            "category": "furniture",
            "position_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
            "width_m": 1.6,
            "depth_m": 0.8,
            "height_m": 0.9,
            "rotation_y_deg": 0.0,
            "geometry_sources": {
                "depth": "visual_annotation",
                "rotation": "visual_annotation",
            },
        }],
    }

    assert camera_placement_issues(model, {}) == []


def test_storyboard_scene_physics_defers_placement_until_blocking(scene_app):
    app = scene_app["app"]
    app.config.data.setdefault("defaults", {})["space_first"] = True

    result = app.director._shot_scene_physics(
        {"project": scene_app["project"]}, "书阁", None)

    assert result["passed"] is True
    assert result["deferred_until_blocking"] is True
    assert result["issues"] == []
    assert result["asset_version"] > 0


def test_frontend_box_rotation_uses_same_right_handed_signs_as_backend():
    app_source = APP_JS.read_text(encoding="utf-8")
    scene_source = SCENE3D_HTML.read_text(encoding="utf-8")

    assert "px + lx * cos + lz * sin" in app_source
    assert "pz - lx * sin + lz * cos" in app_source
    assert "x+lx*co+lz*si" in scene_source
    assert "z-lx*si+lz*co" in scene_source
