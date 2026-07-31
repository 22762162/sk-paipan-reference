"""Photoreal 3D stage contract and viewer integration.

The renderer has two independent authorities: ``scene_model`` supplies only
reconstructed geometry and the panorama supplies only visible appearance.
These tests intentionally reject silent completion of hidden geometry.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from aifos.app import App
from aifos.scene_render import SCHEMA, build_scene_render_contract
from aifos.web.server import _scene3d_payload


ROOT = Path(__file__).resolve().parents[1]
SCENE3D_HTML = ROOT / "aifos" / "web" / "static" / "scene3d.html"
ROOM = {
    "floor_width_m": 10.0,
    "floor_depth_m": 7.0,
    "wall_height_m": 4.2,
}


def _scene_model(*, objects=None, room=None):
    return {
        "schema": "aifos.scene-model/v2",
        "location": "旧书房",
        "capture": {"x": 0.0, "y": 1.55, "z": 0.0},
        "room": dict(ROOM if room is None else room),
        "objects": list(objects or []),
        "issues": [],
    }


def _measured_desk():
    return {
        "name": "书案",
        "category": "furniture",
        "position_3d": {"x": 0.4, "y": 0.0, "z": 1.2},
        "width_m": 1.6,
        "height_m": 0.9,
        "depth_m": 0.8,
        "rotation_y_deg": 30.0,
        "geometry_sources": {
            "position": "panorama_floor_intersection",
            "width": "panorama_angular_span",
            "height": "panorama_vertical_ray",
            "depth": "visual_annotation",
            "rotation": "visual_annotation",
        },
        "material": {
            "base_color": "#765438",
            "roughness": 0.72,
            "metalness": 0.02,
        },
    }


def _warning_codes(contract):
    return {
        warning.get("code")
        for warning in contract.get("warnings", [])
        if isinstance(warning, dict)
    }


def test_contract_separates_geometry_and_visible_appearance_authorities():
    contract = build_scene_render_contract(
        _scene_model(objects=[_measured_desk()]),
        {"url": "/artifacts/old-study-pano.png", "version": 7},
    )

    assert contract["schema"] == SCHEMA == "aifos.scene-render-contract/v1"
    assert contract["location"] == "旧书房"
    layers = contract["source_layers"]
    assert layers["scene_model"]["authority"] == \
        "reconstructed_geometry_only"
    assert layers["panorama"]["authority"] == "visible_appearance_only"
    assert layers["scene_model"]["status"] == "available"
    assert layers["panorama"]["status"] == "available"

    desk = contract["objects"][0]
    assert desk["position"] == {"x": 0.4, "y": 0.0, "z": 1.2}
    assert (desk["width"], desk["height"], desk["depth"]) == \
        pytest.approx((1.6, 0.9, 0.8))
    assert desk["rotation"]["y_deg"] == pytest.approx(30.0)
    assert desk["material"]["base_color"] == "#765438"


def test_category_defaults_are_visible_but_never_claimed_as_measurements():
    defaulted = _measured_desk()
    defaulted.pop("material")
    defaulted.update({
        "width_m": 0.8,
        "height_m": 0.9,
        "depth_m": 0.46,
        "geometry_sources": {
            "position": "panorama_floor_intersection",
            "width": "category_default",
            "height": "category_default",
            "depth": "category_default",
            "rotation": "radial_fallback",
        },
    })

    contract = build_scene_render_contract(
        _scene_model(objects=[defaulted]),
        {"url": "/artifacts/old-study-pano.png", "version": 1},
    )
    desk = contract["objects"][0]

    # Upstream defaults are usable render dimensions, but their provenance is
    # materially weaker than panorama/annotation measurements.
    assert (desk["width"], desk["height"], desk["depth"]) == \
        pytest.approx((0.8, 0.9, 0.46))
    assert desk["geometry_confidence"]["level"] != "high"
    assert desk["material"]["source"] == "category_render_default"
    assert desk["material_confidence"]["level"] == "low"
    warning_dump = json.dumps(
        contract["warnings"], ensure_ascii=False).lower()
    assert "category_default" in warning_dump or "默认" in warning_dump
    assert "unverified_material" in _warning_codes(contract)

    surfaces = {item["name"]: item for item in contract["surfaces"]}
    assert {"floor", "walls", "ceiling"} <= set(surfaces)
    assert all(
        item["material"]["source"] == "category_render_default"
        and item["material_confidence"]["level"] == "low"
        for item in surfaces.values()
    )


def test_missing_room_dimensions_use_labelled_viewer_defaults_with_warnings():
    contract = build_scene_render_contract(
        _scene_model(room={}),
        {"url": "/artifacts/old-study-pano.png"},
    )

    room = contract["room"]
    assert (room["width"], room["depth"], room["height"]) == \
        pytest.approx((10.0, 7.0, 4.2))
    assert room["geometry_confidence"]["level"] == "low"
    assert set(room["geometry_sources"].values()) == {"render_default"}
    warnings = [
        warning for warning in contract["warnings"]
        if warning.get("code") == "missing_room_geometry"
    ]
    assert {warning["field"] for warning in warnings} == {
        "room.width", "room.depth", "room.height"}


def test_unknown_geometry_and_occlusion_remain_explicitly_unverified():
    contract = build_scene_render_contract(
        _scene_model(objects=[{
            "name": "屏风背后的箱子",
            "category": "prop",
            "position_3d": {"x": None, "y": None, "z": None},
        }]),
        {"url": "/artifacts/old-study-pano.png"},
    )
    hidden = contract["objects"][0]

    assert hidden["position"] == {"x": None, "y": None, "z": None}
    assert hidden["width"] is None
    assert hidden["height"] is None
    assert hidden["depth"] is None
    assert hidden["occlusion_completion"] == "unverified"
    assert contract["occlusion_completion"] == "unverified"
    assert contract["source_layers"]["panorama"]["unseen_regions"] == \
        "unverified"
    assert hidden["geometry_confidence"]["level"] == "low"
    assert "missing_geometry" in _warning_codes(contract)


def test_scene3d_payload_exposes_render_contracts_by_location(tmp_path):
    app = App(tmp_path / "workspace")
    try:
        project, _ = app.projects.get_or_create_project("写实三维舞台")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.save_document(episode["id"], "blocking", {
            "schema": "aifos.spatial-blocking/v3",
            "scenes": [{
                "scene_no": 1,
                "location": "旧书房",
                "world": dict(ROOM),
                "shots": [],
            }],
            "shot_index": {},
        })

        model_path = tmp_path / "scene-model.json"
        model_path.write_text(json.dumps(
            _scene_model(objects=[_measured_desk()]),
            ensure_ascii=False), encoding="utf-8")
        app.assets.register(
            project["id"], "scene_model", "旧书房", str(model_path))

        pano_path = app.workspace.artifacts_dir / "old-study-pano.png"
        pano_path.parent.mkdir(parents=True, exist_ok=True)
        pano_path.write_bytes(b"not-a-real-png-but-a-valid-asset-marker")
        app.assets.register(
            project["id"], "scene_art", "旧书房::view:panorama",
            str(pano_path))

        payload = _scene3d_payload(app, episode["id"])

        assert set(payload["render_contracts"]) == {"旧书房"}
        render = payload["render_contracts"]["旧书房"]
        assert render["schema"] == SCHEMA
        assert render["objects"][0]["name"] == "书案"
        assert render["source_layers"]["panorama"]["status"] == "available"
    finally:
        app.close()


class _DocumentInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}
        self._button_id = None
        self._button_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.elements[element_id] = {
                "tag": tag, "attrs": attributes, "text": ""}
        if tag == "button":
            self._button_id = element_id
            self._button_text = []

    def handle_data(self, data):
        if self._button_id:
            self._button_text.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self._button_id:
            self.elements[self._button_id]["text"] = \
                "".join(self._button_text).strip()
            self._button_id = None
            self._button_text = []


def _page_source_and_inventory():
    source = SCENE3D_HTML.read_text(encoding="utf-8")
    inventory = _DocumentInventory()
    inventory.feed(source)
    return source, inventory


def _extract_js_function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"cannot extract JavaScript function {name}")


def test_scene3d_page_has_realistic_structure_and_control_modes():
    source, page = _page_source_and_inventory()

    assert page.elements["modeReal"]["text"] == "写实"
    assert page.elements["modeStructure"]["text"] == "结构"
    assert page.elements["tControl"]["text"] == "控制标注"
    assert page.elements["tControl"]["attrs"]["aria-pressed"] == "false"
    for control_id in ("tPath", "tFrus", "tAxis"):
        assert "disabled" in page.elements[control_id]["attrs"]

    for behavior in (
            "activeSceneModel", "activeRenderContract", "materialFor",
            "lightingFor", "setDisplayMode", "updateSceneStatus"):
        assert f"function {behavior}(" in source
    assert "render_contracts" in source
    assert "show.control" in source


def test_scene3d_page_consumes_real_boxes_lighting_and_shadows():
    source, _page = _page_source_and_inventory()

    # These are the stable render-contract fields, rather than a single long
    # prompt or UI sentence whose wording can change harmlessly.
    for field in ("position", "width", "height", "depth", "rotation"):
        assert field in source
    for render_primitive in (
            "pLit", "pAlpha", "geo.lit", "geo.shadow", "lightingFor("):
        assert render_primitive in source


def test_scene3d_error_surfaces_do_not_interpret_remote_error_as_html():
    source, page = _page_source_and_inventory()

    assert page.elements["notice"]["attrs"]["role"] == "status"
    for function_name in ("fail", "warn"):
        function = _extract_js_function(source, function_name)
        assert ".textContent" in function
        assert ".innerHTML" not in function


def test_effect_export_is_clean_recoverable_and_reads_a_live_webgl_frame():
    source, page = _page_source_and_inventory()

    assert page.elements["exportPng"]["tag"] == "button"
    assert "导出" in page.elements["exportPng"]["text"]
    export = _extract_js_function(source, "exportCleanPng")
    assert ".toBlob(" in export or ".toDataURL(" in export
    assert "fail(" in export or "warn(" in export

    # A clean effect image must not contain control annotations. Preserve and
    # restore the user's prior switch state even if encoding/download fails.
    saved = re.search(
        r"(?:const|let)\s+(\w+)\s*=\s*show\.control", export)
    assert saved, "export must save the current control-overlay state"
    assert re.search(r"show\.control\s*=\s*false", export)
    assert re.search(
        rf"show\.control\s*=\s*{re.escape(saved.group(1))}\b", export)
    assert "finally" in export

    # WebGL buffers may be discarded after compositing. Export must either
    # retain the drawing buffer or draw and read it in the same animation
    # frame, rather than downloading a sometimes-empty canvas.
    preserved = bool(re.search(
        r"getContext\(\s*[\"']webgl[\"']\s*,\s*\{[^}]*"
        r"preserveDrawingBuffer\s*:\s*true",
        source, re.DOTALL))
    same_frame_readback = (
        "requestAnimationFrame" in export
        and (".toBlob(" in export or ".toDataURL(" in export))
    assert preserved or same_frame_readback
