"""场景视角扩展:720° 全景母版 + 四向视角,以及跨镜头一致性接线。

一张全景当空间几何基准 → 四个朝向都以它为参考链式生成 → 后续镜头按
机位自动取对应视角。覆盖:生成与复用、缺图回退链、机位路由、
工坊自建场景接入、Web API 与前端契约。
"""

import http.client
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aifos.app import App
from aifos.camera_language import scene_view_for_camera
from aifos.director import ASPECT_DIMS, PANORAMA_ASPECT
from aifos.errors import AifosError
from aifos.web.server import serve

STATIC = Path(__file__).parents[1] / "aifos" / "web" / "static"


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    instance.projects.get_or_create_project("雨夜凶杀", style="写实电影感")
    yield instance
    instance.close()


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw)


# ---- 生成 ----

def test_expansion_produces_panorama_and_four_directions(app):
    result = app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    assert [item["view"] for item in result["created"]] == [
        "panorama", "main", "side", "reverse", "side_left"]
    assert all(Path(item["uri"]).exists() for item in result["created"])
    state = result["state"]
    assert state["missing"] == []
    assert all(view["ready"] for view in state["views"])


def test_panorama_uses_two_to_one_aspect_and_directions_stay_16_9(app,
                                                                 monkeypatch):
    seen = []
    original = app.router.call

    def spy(capability, payload, out_dir, cancel=None):
        if capability == "image":
            seen.append((payload.get("art_name", ""), payload.get("aspect"),
                         payload.get("width"), payload.get("height")))
        return original(capability, payload, out_dir, cancel=cancel)

    monkeypatch.setattr(app.router, "call", spy)
    app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    panorama = [row for row in seen if "全景" in row[0]]
    assert panorama and panorama[0][1] == PANORAMA_ASPECT
    # 等距圆柱投影必须真的是 2:1,否则四向切出来对不上同一个空间
    assert panorama[0][2] == ASPECT_DIMS[PANORAMA_ASPECT]["width"]
    assert panorama[0][2] == panorama[0][3] * 2
    directions = [row for row in seen if "全景" not in row[0]]
    assert directions and all(row[1] == "16:9" for row in directions)


def test_four_directions_are_chained_from_the_panorama(app, monkeypatch):
    refs = {}
    original = app.router.call
    panorama_uri = {}

    def spy(capability, payload, out_dir, cancel=None):
        result = original(capability, payload, out_dir, cancel=cancel)
        if capability == "image":
            name = payload.get("art_name", "")
            if "全景" in name:
                panorama_uri["dest"] = result.uri
            else:
                refs[name] = payload.get("scene_ref", "")
        return result

    monkeypatch.setattr(app.router, "call", spy)
    app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    assert len(refs) == 4
    # 四向全部以同一张全景为参考——这正是空间一致性的来源
    assert len(set(refs.values())) == 1
    assert all(ref for ref in refs.values())


def test_real_panorama_uses_deterministic_four_direction_projection(
        app, monkeypatch, tmp_path):
    router_calls = []
    projected_yaws = []

    def fake_router(capability, payload, out_dir, cancel=None):
        router_calls.append(payload.get("art_name", ""))
        source = tmp_path / "real-panorama.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nreal")
        return SimpleNamespace(
            provider="seedream5_lite", model="seedream", cost=1.0,
            data={}, uri=str(source))

    def fake_slice(pano_path, out_dir, yaw, pitch, h_fov, size):
        projected_yaws.append((yaw, pitch, h_fov, size, pano_path))
        dest = Path(out_dir) / f"yaw-{yaw}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\nslice")
        return str(dest)

    monkeypatch.setattr(app.router, "call", fake_router)
    monkeypatch.setattr("aifos.director.slice_panorama", fake_slice)
    result = app.director.expand_scene_views("雨夜凶杀", "雨夜天台")

    assert router_calls == ["雨夜天台·720°全景母版"]
    assert [row[0] for row in projected_yaws] == [0, 90, 180, -90]
    assert all(row[1:4] == (0, 90, (1920, 1080))
               for row in projected_yaws)
    assert all(row["view"] in {
        "panorama", "main", "side", "reverse", "side_left"}
        for row in result["created"])
    assert "panorama_slice" in result["providers"]


def test_direction_prompts_declare_degrees_and_forbid_panorama_distortion(app):
    prompt = app.director._scene_direction_prompt(
        "雨夜天台", "写实电影感", {}, "雨夜天台·反打视角", "反打视角", 180,
        "自正向转180°的正对面朝向")
    assert "180°" in prompt
    assert "正常透视画面" in prompt and "桶形畸变" in prompt
    context = app.director._scene_direction_review_context(
        "雨夜天台", "写实电影感", {}, "反打视角", 180)
    assert "view_consistency_precedence" in context
    assert "不构成需要裁决的冲突" in context["view_consistency_precedence"]


def test_panorama_prompt_demands_seamless_360_wrap(app):
    prompt = app.director._scene_panorama_prompt(
        "雨夜天台", "写实电影感", {}, "雨夜天台·720°全景母版")
    assert "360°×180°" in prompt and "无缝首尾相接" in prompt
    context = app.director._scene_panorama_review_context(
        "雨夜天台", "写实电影感", {})
    assert "panorama_contract" in context


# ---- 复用与定向重画 ----

def test_second_run_reuses_everything_and_burns_no_quota(app):
    app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    again = app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    assert again["created"] == []
    assert sorted(again["reused"]) == sorted(
        ["panorama", "main", "side", "reverse", "side_left"])


def test_targeted_regeneration_only_touches_requested_direction(app):
    first = app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    before = {item["view"]: item["asset_id"] for item in first["created"]}
    redo = app.director.expand_scene_views(
        "雨夜凶杀", "雨夜天台", directions=["side_left"], regenerate=True,
        include_panorama=False)
    assert [item["view"] for item in redo["created"]] == ["side_left"]
    assert redo["created"][0]["asset_id"] != before["side_left"]
    project = app.projects.get_project("雨夜凶杀")
    # 旧版本仍在历史里,可回溯
    history = app.assets.history(
        project["id"], "scene_art", "雨夜天台::view:side_left")
    assert [row["version"] for row in history] == [1, 2]


def test_unknown_direction_is_rejected(app):
    with pytest.raises(AifosError) as excinfo:
        app.director.expand_scene_views(
            "雨夜凶杀", "雨夜天台", directions=["上帝视角"])
    assert "main/side/reverse/side_left" in str(excinfo.value)


def test_expansion_without_panorama_and_without_base_art_is_refused(app):
    with pytest.raises(AifosError) as excinfo:
        app.director.expand_scene_views(
            "雨夜凶杀", "雨夜天台", include_panorama=False)
    assert "无法保证四向一致" in str(excinfo.value)


# ---- 后续镜头一致性:机位 → 视角路由 ----

@pytest.mark.parametrize("camera,expected", [
    ("正面平视", "main"),
    ("过肩反打", "reverse"),
    ("背面全景", "reverse"),
    ("右侧面中景", "side"),
    ("左侧面近景", "side_left"),
    ("俯拍全景", "main"),
])
def test_camera_maps_to_the_matching_view_master(app, camera, expected):
    app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    project = app.projects.get_project("雨夜凶杀")
    row, label = app.director._scene_view_reference(
        project["id"], "雨夜天台", camera)
    assert row is not None
    suffix = "" if expected == "main" else f"::view:{expected}"
    assert row["name"].endswith(suffix or "::view:main")
    assert app.director.SCENE_VIEW_LABELS[expected] in label


def test_missing_left_view_falls_back_to_side_then_main(app):
    app.director.expand_scene_views(
        "雨夜凶杀", "雨夜天台", directions=["main", "side"])
    project = app.projects.get_project("雨夜凶杀")
    row, label = app.director._scene_view_reference(
        project["id"], "雨夜天台", "左侧面近景")
    assert row["name"].endswith("::view:side")     # 左缺 → 退通用侧向
    assert "右侧向视角" in label
    app.assets.soft_delete(project["id"], "scene_art", "雨夜天台::view:side")
    row, _label = app.director._scene_view_reference(
        project["id"], "雨夜天台", "左侧面近景")
    assert row["name"].endswith("::view:main")     # 再缺 → 退正向


def test_shot_zone_inherits_the_root_scene_master(app):
    app.director.expand_scene_views("雨夜凶杀", "虞家别墅·虞寻欢卧室")
    project = app.projects.get_project("雨夜凶杀")
    script = {"scenes": [
        {"location": "虞家别墅·虞寻欢卧室"},
        {"location": "虞家别墅·虞寻欢卧室床侧"},
    ]}
    row, label = app.director._scene_view_reference(
        project["id"], "虞家别墅·虞寻欢卧室床侧", "正面平视",
        script=script)
    assert row["name"].startswith("虞家别墅·虞寻欢卧室::view:")
    assert "物理母场景:虞家别墅·虞寻欢卧室" in label


def test_camera_language_distinguishes_left_and_right_side():
    assert scene_view_for_camera("左侧面") == "side_left"
    assert scene_view_for_camera("侧面") == "side"
    assert scene_view_for_camera({"机位": "左侧向"}) == "side_left"
    assert scene_view_for_camera({"机位": "过肩"}) == "reverse"
    assert scene_view_for_camera("") == "main"


def test_main_view_backfills_the_plain_scene_concept_art(app):
    app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    project = app.projects.get_project("雨夜凶杀")
    main = app.assets.latest(project["id"], "scene_art", "雨夜天台")
    assert main is not None and Path(main["uri"]).exists()
    assert app.assets.meta(main)["scene_expansion"] is True


# ---- 与资产工坊的接线 ----

def test_studio_scene_asset_is_listed_and_used_as_the_panorama_base(
        app, monkeypatch):
    created = app.director.generate_studio_asset(
        "雨夜凶杀", "scene", "雨夜天台", "高层写字楼天台,雨夜,霓虹反光,湿滑地面")
    overview = app.director.scene_expansion_overview("雨夜凶杀")
    assert [scene["location"] for scene in overview["scenes"]] == ["雨夜天台"]
    assert [item["view"] for item in overview["view_order"]] == [
        "panorama", "main", "side", "reverse", "side_left"]
    seen = {}
    original = app.router.call

    def spy(capability, payload, out_dir, cancel=None):
        if capability == "image" and payload.get("aspect") == PANORAMA_ASPECT:
            seen["ref"] = payload.get("scene_ref", "")
        return original(capability, payload, out_dir, cancel=cancel)

    monkeypatch.setattr(app.router, "call", spy)
    app.director.expand_scene_views("雨夜凶杀", "雨夜天台")
    assert seen["ref"] == created["items"][0]["uri"]


def test_overview_lists_script_scenes_that_have_no_art_yet(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    overview = app.director.scene_expansion_overview("万妖图录")
    assert overview["scenes"]
    assert all(scene["missing"] for scene in overview["scenes"])


# ---- Web API ----

def test_scene_expansion_web_endpoints(tmp_path):
    workspace = tmp_path / "workspace"
    instance = App(workspace)
    instance.projects.get_or_create_project("雨夜凶杀", style="写实电影感")
    instance.director.generate_studio_asset(
        "雨夜凶杀", "scene", "雨夜天台", "高层写字楼天台,雨夜,霓虹反光")
    instance.close()
    server = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    project = "%E9%9B%A8%E5%A4%9C%E5%87%B6%E6%9D%80"
    location = "%E9%9B%A8%E5%A4%9C%E5%A4%A9%E5%8F%B0"
    try:
        status, overview = _request(
            port, "GET", f"/api/scene/expansion?project={project}")
        assert status == 200
        assert overview["scenes"][0]["location"] == "雨夜天台"

        status, missing = _request(port, "GET", "/api/scene/expansion")
        assert status == 400 and "project" in missing["error"]

        status, expanded = _request(port, "POST", "/api/scene/expand", {
            "project": "雨夜凶杀", "location": "雨夜天台"})
        assert status == 201
        assert len(expanded["created"]) == 5
        assert expanded["state"]["missing"] == []

        status, single = _request(
            port, "GET",
            f"/api/scene/expansion?project={project}&location={location}")
        assert status == 200 and single["missing"] == []

        status, bad = _request(port, "POST", "/api/scene/expand", {
            "project": "雨夜凶杀"})
        assert status == 400 and "location" in bad["error"]

        status, bad_dir = _request(port, "POST", "/api/scene/expand", {
            "project": "雨夜凶杀", "location": "雨夜天台",
            "directions": "main"})
        assert status == 400 and "数组" in bad_dir["error"]
    finally:
        server.shutdown()
        server.server_close()


# ---- 前端契约 ----

def test_scene_expansion_ui_contract():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "sceneExpansionHtml(sceneExpansion)" in js
    assert "/api/scene/expansion?project=" in js
    assert "/api/scene/expand" in js
    assert "bindSceneExpansion(title, reload)" in js
    assert "scene-expand-redo" in js
    assert "720°" in js                      # 面板要说清这是干什么的
    assert ".scene-view-chip.ready" in css
    assert ".scene-expansion-row" in css
