"""Read-only episode/UI wiring for non-blocking story intelligence."""

import http.client
import json
import subprocess
import threading

import pytest

from aifos.app import App
from aifos.story_intelligence import (
    ReviewDimension,
    build_script_review_court,
    review_document,
)
from aifos.web.server import serve


@pytest.fixture()
def server(tmp_path):
    workspace = tmp_path / "ws"
    App(workspace).close()
    httpd = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": httpd.server_address[1], "workspace": workspace}
    httpd.shutdown()
    httpd.server_close()


def _json_get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", path)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw)


def _shot(number, *, scene=1):
    return {
        "shot_no": number,
        "scene_no": scene,
        "unit_id": f"U{number:02d}",
        "duration": 4.5,
        "kind": "dialogue",
        "dialogue": {"character": "林昭", "dialogue": f"第{number}镜台词。"},
        "shot_contract": {"景别": "中景", "运镜": "缓慢推进"},
        "shot_function": "推进冲突",
        "script_reference": f"林昭完成第{number}镜对应的剧本行动。",
        "visual_hook": f"第{number}镜结尾钩子。",
        "start_state": {"林昭": {"pose": "站立"}},
        "end_state": {"林昭": {"pose": "转身"}},
        "performance": {"beat": "观察后作出决定"},
        "five_dimensions": {
            "subject_motion": "林昭转身",
            "environment_light": "窗光稳定",
            "camera_design": {"shot_scale": "中景", "movement": "缓慢推进"},
            "time_state": {"start": "站立", "end": "转身"},
            "aesthetics": {"lighting": "冷暖对比"},
        },
    }


def _independent_review(script_version="1", *, same_run=False):
    dimensions = {
        dimension: {
            "score": 4,
            "evidence": [f"{dimension.value} 有具体证据。"],
            "directed_revision": [f"定向完善 {dimension.value}。"],
        }
        for dimension in ReviewDimension
    }
    return review_document(build_script_review_court(
        script_version=script_version,
        generator_run_id="writer-run-1",
        reviewer_run_id=("writer-run-1" if same_run else "review-run-2"),
        reviewer_source="independent-codex-review",
        dimension_reviews=dimensions,
    ))


def _assert_no_reference_chain_fields(value):
    forbidden = {
        "reference_images", "reference_manifest", "reference_assets",
        "composite_uri", "composite_url",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for item in value.values():
            _assert_no_reference_chain_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_reference_chain_fields(item)


def test_episode_payload_exposes_safe_nonblocking_story_views(server):
    app = App(server["workspace"])
    try:
        project, _ = app.projects.get_or_create_project("故事智能接线")
        previous, _ = app.projects.get_or_create_episode(project["id"], 1)
        current, _ = app.projects.get_or_create_episode(project["id"], 2)
        app.projects.save_document(previous["id"], "script", {
            "story_background": {"narrative": {
                "continuity_hooks": ["密诏从何而来"]}},
            "scenes": [{"scene_no": 1, "location": "东宫书房",
                        "director_logic": {"exit_state": "门半开。"}}],
        })
        app.projects.save_document(previous["id"], "storyboard", {
            "shots": [{
                "shot_no": 9, "scene_no": 1,
                "end_state": {"林昭": {
                    "pose": "贴墙站立", "wardrobe": "青衣沾血"}},
                "frame_props": [{"name": "密诏", "phase": "end",
                                 "holder": "林昭左手", "physical_state": "展开"}],
            }],
        })
        app.projects.save_document(current["id"], "script", {
            "logline": "林昭带着密诏逃离东宫。", "characters": [],
            "scenes": [{"scene_no": 1, "location": "东宫书房"}],
        })
        app.projects.save_document(current["id"], "storyboard", {
            "version": 3, "shots": [_shot(number) for number in range(1, 11)],
        })
        app.projects.save_document(
            current["id"], "script_review", _independent_review("1"))
        for shot_no in range(1, 11):
            image = (app.workspace.artifacts_dir
                     / f"p{project['id']:03d}" / "e002" / "images"
                     / f"shot_{shot_no:03d}.png")
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([shot_no]) * 16)
            app.assets.register(
                project["id"], "image", f"e002_shot{shot_no}",
                uri=str(image), meta={"image_quality": "medium"})
        episode_id = current["id"]
        before = {
            "episode_status": app.projects.get_episode(episode_id)["status"],
            "documents": app.db.query_one(
                "SELECT COUNT(*) AS n FROM documents WHERE episode_id=?",
                (episode_id,))["n"],
            "tasks": app.db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE episode_id=?",
                (episode_id,))["n"],
        }
    finally:
        app.close()

    status, payload = _json_get(
        server["port"], f"/api/episode/{episode_id}")
    assert status == 200
    brain = payload["story_intelligence"]
    assert brain["kind"] == "review"
    assert brain["production_blocking"] is False
    assert brain["director_review"]["shot_count"] == 10
    assert brain["director_review"]["production_blocking"] is False
    browser = brain["nine_grid_browser"]
    assert browser["render_mode"] == "independent_shot_cells"
    assert browser["single_image_multi_panel"] is False
    assert browser["generates_reference_asset"] is False
    assert browser["reference_chain_eligible"] is False
    assert browser["view_only"] is True
    assert [len(page["cells"]) for page in browser["pages"]] == [9, 1]
    urls = [cell["keyframe_uri"] for page in browser["pages"]
            for cell in page["cells"]]
    assert all(url.startswith("/artifacts/") for url in urls)
    assert not any(str(server["workspace"]) in url for url in urls)
    _assert_no_reference_chain_fields(browser)

    continuity = brain["cross_episode_continuity"]
    assert continuity["status"] == "ready"
    assert continuity["production_blocking"] is False
    assert continuity["previous_episode_number"] == 1
    assert continuity["review"]["unresolved_hooks"] == ["密诏从何而来"]
    assert any(state["entity_id"] == "密诏"
               for state in continuity["review"]["states"])
    script_review = brain["script_independent_review"]
    assert script_review["status"] == "ready"
    assert script_review["review"]["generator_run_id"] != (
        script_review["review"]["reviewer_run_id"])

    app = App(server["workspace"])
    try:
        after = {
            "episode_status": app.projects.get_episode(episode_id)["status"],
            "documents": app.db.query_one(
                "SELECT COUNT(*) AS n FROM documents WHERE episode_id=?",
                (episode_id,))["n"],
            "tasks": app.db.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE episode_id=?",
                (episode_id,))["n"],
        }
    finally:
        app.close()
    assert after == before, "读取故事智能不得改生产状态、文档或任务"


def test_script_review_ready_pending_stale_invalid_and_missing(server):
    app = App(server["workspace"])
    try:
        project, _ = app.projects.get_or_create_project("独立评审状态")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.save_document(episode["id"], "script", {
            "logline": "状态测试", "characters": [], "scenes": []})
        episode_id = episode["id"]
    finally:
        app.close()

    _, payload = _json_get(server["port"], f"/api/episode/{episode_id}")
    assert payload["story_intelligence"]["script_independent_review"]["status"] == "pending"

    app = App(server["workspace"])
    try:
        app.projects.save_document(episode_id, "script_review", {
            "schema": "aifos.story-review-pending/v1",
            "kind": "review", "production_blocking": False,
            "status": "pending", "script_version": "1",
            "reason": "当前编剧 Provider 未返回独立评审运行",
        })
    finally:
        app.close()
    _, payload = _json_get(server["port"], f"/api/episode/{episode_id}")
    entry = payload["story_intelligence"]["script_independent_review"]
    assert entry["status"] == "pending"
    assert entry["production_blocking"] is False

    app = App(server["workspace"])
    try:
        app.projects.save_document(
            episode_id, "script_review", _independent_review("0"))
    finally:
        app.close()
    _, payload = _json_get(server["port"], f"/api/episode/{episode_id}")
    assert payload["story_intelligence"]["script_independent_review"]["status"] == "stale"

    invalid = _independent_review("1")
    invalid["status"] = "ready"
    invalid["reviewer_run_id"] = invalid["generator_run_id"]
    app = App(server["workspace"])
    try:
        app.projects.save_document(episode_id, "script_review", invalid)
    finally:
        app.close()
    _, payload = _json_get(server["port"], f"/api/episode/{episode_id}")
    entry = payload["story_intelligence"]["script_independent_review"]
    assert entry["status"] == "invalid"
    assert entry["production_blocking"] is False


def test_cross_episode_requires_exact_predecessor_and_never_blocks(server):
    app = App(server["workspace"])
    try:
        project, _ = app.projects.get_or_create_project("连续性不跨集跳读")
        app.projects.get_or_create_episode(project["id"], 1)
        third, _ = app.projects.get_or_create_episode(project["id"], 3)
        episode_id = third["id"]
    finally:
        app.close()
    status, payload = _json_get(
        server["port"], f"/api/episode/{episode_id}")
    assert status == 200
    continuity = payload["story_intelligence"]["cross_episode_continuity"]
    assert continuity["status"] == "unavailable"
    assert continuity["previous_episode_number"] == 2
    assert continuity["production_blocking"] is False


def test_mobile_nine_grid_is_three_independent_columns_and_nonblocking():
    app_js_path = "aifos/web/static/app.js"
    css_path = "aifos/web/static/style.css"
    javascript = open(app_js_path, encoding="utf-8").read()
    css = open(css_path, encoding="utf-8").read()
    subprocess.run(["node", "--check", app_js_path], check=True)

    assert "function storyIntelligenceHtml" in javascript
    assert "function bindStoryIntelligence" in javascript
    assert "function nineGridBrowserHtml" in javascript
    assert 'class="nine-grid-cell" data-shot-no=' in javascript
    assert "data-nine-grid-preview" in javascript
    assert "data-nine-grid-locate" in javascript
    assert "showImageLightbox(button.dataset.nineGridPreview" in javascript
    assert "只读审片 · 每格一镜 · 不进入参考链" in javascript
    assert "整集节奏、九宫格、跨集连续性与独立剧本评审 · 非生产门禁" in javascript
    assert "Object.prototype.hasOwnProperty.call(object, key)" in javascript
    assert 'grid-template-columns: repeat(3, minmax(0, 1fr))' in css
    assert "@media (max-width: 380px)" in css
    narrow = css.split("@media (max-width: 380px)", 1)[1]
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in narrow
    assert "grid-template-columns: 1fr" not in narrow.split("}", 2)[0]
