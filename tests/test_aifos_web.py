"""Web 控制台测试:API、后台制作任务、产物服务与目录穿越防护。"""

import copy
import http.client
import json
import threading
import time

import pytest

from aifos.app import App
from aifos.web.server import serve


@pytest.fixture()
def server(tmp_path):
    ws = tmp_path / "ws"
    App(ws).close()  # 初始化工作区
    httpd = serve(ws, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": httpd.server_address[1], "workspace": ws}
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type", ""), raw


def _json_request(port, method, path, body=None):
    status, _, raw = _request(port, method, path, body)
    return status, json.loads(raw)


def _wait_job(port, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, job = _json_request(port, "GET", f"/api/jobs/{job_id}")
        if job["status"] != "running":
            return job
        time.sleep(0.2)
    raise TimeoutError("制作任务超时")


def test_index_and_static(server):
    status, ctype, raw = _request(server["port"], "GET", "/")
    assert status == 200 and "text/html" in ctype
    html = raw.decode("utf-8")
    assert "AIFOS" in html
    assert "历史记录" in html
    status, ctype, app_js = _request(server["port"], "GET", "/static/app.js")
    assert status == 200 and "javascript" in ctype
    assert b"showBlockingOverlay" in app_js
    assert "空间调度".encode() in app_js
    assert "人物编号图例".encode() in app_js
    assert "人物路线".encode() in app_js
    assert "镜头路线".encode() in app_js
    assert "Codex 订阅".encode() in app_js
    assert "Seedream 5.0 Lite".encode() in app_js
    assert "¥0.22/张".encode() in app_js
    assert "GPT Image 2 medium".encode() in app_js
    assert "约 ¥0.28/张".encode() in app_js
    assert "GPT Image 2 high".encode() in app_js
    assert "仅终稿/复杂文字".encode() in app_js
    assert "全部 Codex 优先".encode() in app_js
    assert "全部 Seedream 5.0 Lite 优先".encode() in app_js
    assert "全部 OpenAI 图片 API 优先".encode() in app_js
    assert b"image_strategy" in app_js
    assert b"/api/character/select" in app_js
    assert "人物定版".encode() in app_js
    status, ctype, style_css = _request(
        server["port"], "GET", "/static/style.css")
    assert status == 200 and "css" in ctype
    assert b".image-cost-guide" in style_css
    assert b".blocking-actor-legend" in style_css
    assert b".blocking-map-scroll" in style_css
    status, ctype, raw = _request(
        server["port"], "GET", "/manifest.webmanifest")
    assert status == 200 and "application/manifest+json" in ctype
    manifest = json.loads(raw)
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/#/"
    assert {icon["sizes"] for icon in manifest["icons"]} == {
        "192x192", "512x512"}
    status, ctype, raw = _request(server["port"], "GET", "/sw.js")
    assert status == 200 and "javascript" in ctype
    assert b"aifos-mobile-shell" in raw


def test_mobile_access_api_is_safe_by_default(server):
    status, data = _json_request(server["port"], "GET", "/api/access")
    assert status == 200
    assert data["lan_enabled"] is False
    assert data["lan_urls"] == []
    assert data["local_url"].startswith("http://127.0.0.1:")
    assert "Safari" in data["install"]["ios"]
    assert "Chrome" in data["install"]["android"]


def test_overview_empty(server):
    status, data = _json_request(server["port"], "GET", "/api/overview")
    assert status == 200
    assert data["stats"]["episodes"] == 0
    assert data["episodes"] == []


def test_standard_center_api_lifecycle(server):
    """标准中心 API 覆盖读取、校验、版本、激活、重置与交换包。"""
    port = server["port"]
    status, initial = _json_request(port, "GET", "/api/standards")
    assert status == 200
    assert initial["active"]["version"] == 1
    assert len(initial["active"]["content"]["rules"]["quality_gates"]) == 12
    assert initial["capabilities"] == {
        "versioned": True,
        "episode_snapshot": True,
        "import_export": True,
        "locked_paths": [
            "rules.production.video_model",
            "rules.production.resolution",
            "rules.production.voice",
                "rules.production.lip_sync",
                "rules.production.burn_subtitles",
                "rules.production.fast_vip_real_face_conflict",
            ],
    }

    content = copy.deepcopy(initial["active"]["content"])
    content["name"] = "API 高密度测试标准"
    content["rules"]["dialogue"]["max_chars_per_shot"] = 20
    status, saved = _json_request(port, "POST", "/api/standards/save", {
        "content": content,
        "change_note": "缩短单镜台词",
        "activate": False,
        "expected_active_id": initial["active"]["version_id"],
    })
    assert status == 201
    draft = saved["standard"]
    assert draft["version"] == 2
    assert draft["active"] is False

    invalid = copy.deepcopy(content)
    invalid["rules"]["production"]["video_model"] = "seedance2.0_vip"
    status, rejected = _json_request(
        port, "POST", "/api/standards/save", {"content": invalid})
    assert status == 400
    assert rejected["error"] == "制作标准校验失败"
    assert any(item["path"] == "rules.production.video_model"
               for item in rejected["issues"])

    status, activated = _json_request(
        port, "POST", "/api/standards/activate",
        {"version_id": draft["version_id"]})
    assert status == 200
    assert activated["standard"]["active"] is True

    status, bundle = _json_request(
        port, "GET", f"/api/standards/export?version_id={draft['version_id']}")
    assert status == 200
    assert bundle["schema"] == "aifos.production-standard/v1"
    assert bundle["standard"]["fingerprint"] == draft["fingerprint"]

    status, reset = _json_request(
        port, "POST", "/api/standards/reset",
        {"change_note": "API 恢复厂标"})
    assert status == 201
    assert reset["standard"]["version"] == 3
    assert reset["standard"]["content"]["name"] == "SK AI 漫剧工业制作标准 V5"

    status, imported = _json_request(
        port, "POST", "/api/standards/import", {
            "bundle": bundle,
            "change_note": "API 重新导入高密度标准",
            "activate": True,
        })
    assert status == 201
    assert imported["standard"]["version"] == 4
    assert imported["standard"]["active"] is True
    assert imported["standard"]["content"]["rules"]["dialogue"][
        "max_chars_per_shot"] == 20

    status, final = _json_request(port, "GET", "/api/standards")
    assert status == 200
    assert final["active"]["version_id"] == imported["standard"]["version_id"]
    assert len(final["history"]) == 4


def test_produce_flow_and_episode_api(server):
    port = server["port"]
    # Web 默认流程:剧本 → 每人五选一 → 预生产 → 开拍确认
    status, reply = _json_request(port, "POST", "/api/produce", {
        "sentence": "开始制作《万妖图录》第15集"})
    assert status == 202
    job = _wait_job(port, reply["job_id"])
    assert job["status"] == "done"
    assert job["summary"]["status"] == "awaiting_script"

    # 每次生产运行都立即沉淀到 SQLite，刷新或重启后仍可查看。
    status, history = _json_request(port, "GET", "/api/history")
    assert status == 200
    assert history["stats"]["total"] == 1
    run = history["items"][0]
    assert run["status"] == "paused"
    assert run["action"] == "produce"
    status, run_detail = _json_request(
        port, "GET", f"/api/history/{run['id']}")
    assert status == 200
    assert run_detail["summary"]["status"] == "awaiting_script"
    assert run_detail["tasks"][0]["stage"] == "script"

    _, overview = _json_request(port, "GET", "/api/overview")
    assert overview["stats"]["episodes"] == 1
    episode_id = overview["episodes"][0]["id"]
    assert overview["episodes"][0]["status"] == "awaiting_script"

    # 剧本审阅页:有剧本、但一张图都还没画
    status, pre = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert pre["script"]["scenes"]
    assert pre["storyboard"] is None
    assert pre["artifacts"]["cast_art"] == []

    # 第一道确认(剧本 OK)→ 只生成每名人物5张候选后停下。
    status, reply = _json_request(port, "POST", "/api/confirm", {
        "episode_id": episode_id})
    assert status == 202 and reply["phase"] == "awaiting_script"
    job = _wait_job(port, reply["job_id"])
    assert job["summary"]["status"] == "awaiting_cast"

    status, pre = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert pre["storyboard"] is None
    selection = pre["cast_selection"]
    assert not selection["passed"]
    assert all(len(c["candidates"]) == 5 for c in selection["characters"])
    assert all(candidate["url"].startswith("/artifacts/")
               for c in selection["characters"]
               for candidate in c["candidates"])
    assert pre["artifacts"]["cast_art"] == []
    assert pre["artifacts"]["scene_art"] == []
    # 未完成五选一，后端也必须拒绝绕过门禁。
    status, blocked = _json_request(port, "POST", "/api/confirm", {
        "episode_id": episode_id})
    assert status == 409 and "最终立绘" in blocked["error"]
    for character in selection["characters"]:
        status, selected = _json_request(
            port, "POST", "/api/character/select", {
                "episode_id": episode_id,
                "character": character["character"],
                "candidate_index": 1,
            })
        assert status == 200
    assert selected["passed"] is True

    # 人物已锁定 → 才生成场景/分镜/首尾帧，再停等开拍。
    status, reply = _json_request(port, "POST", "/api/confirm", {
        "episode_id": episode_id})
    assert status == 202 and reply["phase"] == "awaiting_cast"
    job = _wait_job(port, reply["job_id"])
    assert job["summary"]["status"] == "awaiting_confirm"
    status, pre = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert pre["storyboard"]["shots"]
    assert pre["artifacts"]["cast_art"], "确认页需要最终人物立绘"
    assert pre["artifacts"]["scene_art"], "确认页需要场景概念图"
    assert pre["artifacts"]["videos"] == {}, "确认前不应生产视频"

    # 开拍确认 → 自动完成剩余全部阶段
    status, reply = _json_request(port, "POST", "/api/confirm", {
        "episode_id": episode_id})
    assert status == 202
    job = _wait_job(port, reply["job_id"])
    assert job["summary"]["status"] == "done"

    status, detail = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["qc_report"]["passed"]
    assert len(detail["tasks"]) >= 11

    art = detail["artifacts"]
    shot_keys = list(art["images"].keys())
    assert shot_keys, "关键图索引不能为空"
    assert art["video_audio"] and all(art["video_audio"].values())
    assert set(art["video_providers"].values()) == {"mock"}
    assert art["voices"] == {}, "随视频配音模式不应产生外挂对白音轨"
    assert art["cover"] and art["final"]
    assert len(art["titles"]) == 3
    # 产物 URL 可访问且类型正确
    status, ctype, _ = _request(port, "GET", art["images"][shot_keys[0]])
    assert status == 200 and ctype == "image/svg+xml"
    status, _, _ = _request(port, "GET", art["cover"])
    assert status == 200

    # 资产 API
    status, assets = _json_request(
        port, "GET", "/api/assets?project=%E4%B8%87%E5%A6%96%E5%9B%BE%E5%BD%95")
    assert status == 200
    assert any(a["kind"] == "character" for a in assets)

    # 日志 API
    status, logs = _json_request(port, "GET", "/api/logs?limit=10")
    assert status == 200 and logs


def test_produce_rejects_bad_input(server):
    # 自由识别后随便一句话都能开工;只有空输入才拒绝
    status, reply = _json_request(
        server["port"], "POST", "/api/produce", {"sentence": ""})
    assert status == 400
    assert "作品名" in reply["error"]
    status, _ = _json_request(server["port"], "POST", "/api/produce", {})
    assert status == 400


def test_artifact_traversal_blocked(server):
    status, _, _ = _request(
        server["port"], "GET", "/artifacts/../config.json")
    assert status == 404
    status, _, _ = _request(
        server["port"], "GET", "/artifacts/..%2F..%2Fetc%2Fpasswd")
    assert status == 404
    status, _, _ = _request(
        server["port"], "GET", "/static/../server.py")
    assert status == 404


def test_unknown_routes(server):
    status, _ = _json_request(server["port"], "GET", "/api/nothing")
    assert status == 404
    status, _ = _json_request(server["port"], "GET", "/api/episode/999")
    assert status == 404
    status, _ = _json_request(server["port"], "GET", "/api/jobs/nope")
    assert status == 404
    from urllib.parse import quote
    status, _ = _json_request(
        server["port"], "GET", "/api/assets?project=" + quote("不存在"))
    assert status == 404


def test_project_style_api(server):
    """剧本确认页的画风确认:保存后所有出图提示按新画风执行。"""
    port = server["port"]
    status, reply = _json_request(port, "POST", "/api/produce", {
        "sentence": "开始制作《风格测试》第1集"})
    assert status == 202
    _wait_job(port, reply["job_id"])
    status, project = _json_request(port, "POST", "/api/project/style", {
        "project": "风格测试", "style": "水墨国风,淡彩留白"})
    assert status == 200
    assert project["style"] == "水墨国风,淡彩留白"
    # 空画风/不存在的项目要报错
    status, _ = _json_request(port, "POST", "/api/project/style", {
        "project": "风格测试", "style": ""})
    assert status == 400
    status, _ = _json_request(port, "POST", "/api/project/style", {
        "project": "不存在", "style": "x"})
    assert status == 400


def test_image_line_switch_and_parallel(server):
    """设置 API 暴露按用途出图策略，并保留高级路由与并行数写入。"""
    port = server["port"]
    status, view = _json_request(port, "GET", "/api/settings")
    assert status == 200
    seedream = next(
        provider for provider in view["providers"]
        if provider["name"] == "seedream5_lite")
    assert seedream["type"] == "seedream_image"
    assert seedream["model"] == "doubao-seedream-5-0-lite-260128"
    assert seedream["cost_per_call"] == pytest.approx(0.22)
    assert {"image", "frames", "cover"}.issubset(seedream["capabilities"])
    assert view["image_routing"]["batch"][0] == "seedream5_lite"
    assert view["image_routing"]["important"][0] == "codex"
    assert view["image_routing"]["final"][0] == "codex"
    assert view["image_routing"]["complex_text"][0] == "codex"
    assert view["image_strategy"] == "smart"
    # 快捷策略必须同时改分类图片和未分类旧调用，不能只切界面显示。
    for strategy, provider in (
            ("codex", "codex"),
            ("seedream5_lite", "seedream5_lite"),
            ("image_api", "image_api")):
        status, view = _json_request(port, "POST", "/api/settings", {
            "image_strategy": strategy})
        assert status == 200
        assert view["image_strategy"] == strategy
        assert all(view["image_routing"][kind][0] == provider
                   for kind in ("batch", "important", "final",
                                "complex_text"))
        assert all(view["routing"][capability][0] == provider
                   for capability in ("image", "frames", "cover"))
    status, view = _json_request(port, "POST", "/api/settings", {
        "image_strategy": "smart"})
    assert status == 200 and view["image_strategy"] == "smart"
    assert view["image_routing"]["batch"][0] == "seedream5_lite"
    assert view["image_routing"]["important"][0] == "codex"
    status, reply = _json_request(port, "POST", "/api/settings", {
        "image_strategy": "not-a-strategy"})
    assert status == 400 and "未知出图策略" in reply["error"]
    # 切到 OpenAI 图片 API 优先
    status, _ = _json_request(port, "POST", "/api/settings", {
        "capability": "image",
        "chain": ["image_api", "codex", "api", "mock"]})
    assert status == 200
    status, view = _json_request(port, "GET", "/api/settings")
    assert view["routing"]["image"][0] == "image_api"
    # 并行路数写入 defaults
    status, _ = _json_request(port, "POST", "/api/settings", {
        "defaults": {"parallel_images": 6}})
    assert status == 200
    status, view = _json_request(port, "GET", "/api/settings")
    assert view["defaults"]["parallel_images"] == 6
    # 非法值被拒
    status, _ = _json_request(port, "POST", "/api/settings", {
        "defaults": {"parallel_images": "abc"}})
    assert status == 400


def test_overview_and_episode_expose_build(server):
    """前端凭 build 哈希发现服务已自动更新并自动刷新页面。"""
    port = server["port"]
    status, overview = _json_request(port, "GET", "/api/overview")
    assert status == 200 and "build" in overview


def test_artifact_thumbnail_and_cache(server):
    """列表缩略图:?w= 请求 200 且带长缓存头;无缩放工具时回退原图。"""
    port = server["port"]
    status, reply = _json_request(port, "POST", "/api/produce", {
        "sentence": "开始制作《缩略图测试》第1集", "review": False})
    assert status == 202
    _wait_job(port, reply["job_id"], timeout=120)
    status, episode = _json_request(port, "GET", "/api/episode/1")
    from urllib.parse import quote
    art = episode["cast_selection"]["characters"][0]["candidates"][0]
    url = art["url"]
    assert url.startswith("/artifacts/")
    path, _, ver = url.partition("?")
    url = quote(path) + (f"?{ver}" if ver else "")
    sep = "&" if "?" in url else "?"
    conn_status, headers_ct, raw = _request(
        port, "GET", f"{url}{sep}w=240")
    assert conn_status == 200 and raw
    import http.client as _hc
    conn = _hc.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", f"{url}{sep}w=240")
    resp = conn.getresponse()
    cache = resp.getheader("Cache-Control", "")
    resp.read()
    conn.close()
    assert "max-age" in cache
