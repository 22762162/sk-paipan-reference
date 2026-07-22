"""Web 控制台测试:API、后台制作任务、产物服务与目录穿越防护。"""

import base64
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
    assert "/static/style.css?v=20260722-image-accel" in html
    assert "/static/app.js?v=20260722-image-accel" in html
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
    assert b"/api/series/preview" in app_js
    assert b"/api/series/import" in app_js
    assert "选择多集剧本文档".encode() in app_js
    assert "始终只激活一集".encode() in app_js
    assert b"/api/character/select" in app_js
    assert b"/api/character/regenerate" in app_js
    assert b"/api/asset/delete" in app_js
    assert b"/api/history/delete" in app_js
    assert b"history-delete-row" in app_js
    assert b"episode-delete-work" in app_js
    assert "删除作品".encode() in app_js
    assert "保留资产中心图片（推荐）".encode() in app_js
    assert "资产分类".encode() in app_js
    assert "查看提示词".encode() in app_js
    assert b"/api/video/references" in app_js
    assert "质检没有通过的原因".encode() in app_js
    assert "本次质检/重画实际附上的参考图".encode() in app_js
    assert "待生产图片批量 API 加速".encode() in app_js
    assert "API 批量加速".encode() in app_js
    assert "选择 API/模型并加速".encode() in app_js
    assert b'id="btn-plan-live"' in app_js
    assert b'id="btn-image-acceleration"' in app_js
    assert app_js.count(b"imageAccelerationLivebarHtml(data)") >= 3
    assert b'updateViaCache: "none"' in app_js
    assert b"/api/image_acceleration/preflight" in app_js
    assert b"/api/image_acceleration/queue" in app_js
    assert "逐张预检所选图片".encode() in app_js
    assert "中 · 默认生产档".encode() in app_js
    assert "人物定版".encode() in app_js
    status, ctype, style_css = _request(
        server["port"], "GET", "/static/style.css")
    assert status == 200 and "css" in ctype
    assert b".image-cost-guide" in style_css
    assert b".blocking-actor-legend" in style_css
    assert b".blocking-map-scroll" in style_css
    assert b".video-ref-picker-grid" in style_css
    assert b".plan-ref-gallery" in style_css
    assert b".acceleration-panel" in style_css
    assert b".image-accel-livebar" in style_css
    assert b".accel-gates" in style_css
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
    assert b"aifos-mobile-shell-v2" in raw
    assert b'/static/app.js' in raw and b'fetch(request)' in raw


def test_asset_delete_and_video_reference_api(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("资产接口测试")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        app2.projects.save_document(episode["id"], "storyboard", {
            "shots": [{"shot_no": 1}]})
        path = app2.workspace.artifacts_dir / "p001" / "scene.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        row = app2.assets.register(
            project["id"], "scene_art", "会议室", uri=str(path),
            meta={"image_quality": "medium"})
        asset_id = row["id"]
        episode_id = episode["id"]
    finally:
        app2.close()

    status, saved = _json_request(
        server["port"], "POST", "/api/video/references", {
            "episode_id": episode_id, "shot_no": 1,
            "asset_ids": [asset_id]})
    assert status == 200
    assert saved["shots"]["1"][0]["asset_id"] == asset_id
    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["video_references"]["shots"]["1"][0]["asset_id"] == asset_id
    catalog = detail["artifacts"]["image_assets"]
    assert catalog[0]["asset_id"] == asset_id
    assert catalog[0]["usable_for_video"] is True

    status, deleted = _json_request(
        server["port"], "POST", "/api/asset/delete", {
            "project": "资产接口测试", "asset_id": asset_id})
    assert status == 200 and deleted["history_preserved"] is True
    status, assets = _json_request(
        server["port"], "GET",
        "/api/assets?project=%E8%B5%84%E4%BA%A7%E6%8E%A5%E5%8F%A3%E6%B5%8B%E8%AF%95&kind=scene_art")
    assert status == 200
    assert len(assets) == 2
    assert assets[-1]["meta"]["deleted"] is True
    assert path.exists(), "删除资产中心卡片不应物理删除历史文件"


def test_asset_image_catalog_has_category_origin_time_and_prompt(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("资产溯源测试")
        episode, _ = app2.projects.get_or_create_episode(
            project["id"], 3, title="定妆")
        plan_dir = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
                    / "e003")
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "render_plan.json").write_text(json.dumps({
            "items": [{
                "id": "sheet:林昭:costume",
                "prompt": "林昭现代通勤服装设定，正面全身",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        path = plan_dir / "costume.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        app2.assets.register(
            project["id"], "character_sheet", "林昭:costume",
            uri=str(path), meta={"character": "林昭", "sheet": "costume",
                                  "image_quality": "high",
                                  "source_episode_number": 3})
    finally:
        app2.close()

    status, catalog = _json_request(
        server["port"], "GET",
        "/api/asset-images?project=%E8%B5%84%E4%BA%A7%E6%BA%AF%E6%BA%90%E6%B5%8B%E8%AF%95")
    assert status == 200
    item = catalog["items"][0]
    assert item["category"] == "costume"
    assert item["category_label"] == "服装"
    assert item["source_project"] == "资产溯源测试"
    assert item["source_episode"] == 3
    assert item["generated_at"] > 0
    assert item["prompt"] == "林昭现代通勤服装设定，正面全身"
    assert item["prompt_status"] == "recorded"


def test_history_delete_api_can_keep_asset_center_images(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("历史删除接口")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        run_id = app2.history.create_run("历史删除接口", 1)
        path = app2.workspace.artifacts_dir / "history-delete" / "image.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        asset = app2.assets.register(
            project["id"], "image", "e001_shot001", uri=str(path))
    finally:
        app2.close()

    status, deleted = _json_request(
        server["port"], "POST", "/api/history/delete",
        {"run_id": run_id, "delete_assets": False})
    assert status == 200
    assert deleted["episode_deleted"] is True
    assert deleted["assets_soft_deleted"] == 0
    app3 = App(server["workspace"])
    try:
        assert app3.projects.get_episode(episode["id"]) is None
        assert app3.assets.get(asset["id"])["uri"] == str(path)
        assert path.exists()
    finally:
        app3.close()


def test_history_delete_api_accepts_episode_from_overview(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("总览删除接口")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 2)
        app2.projects.save_document(
            episode["id"], "script", {"scenes": [{"location": "办公室"}]})
    finally:
        app2.close()

    status, deleted = _json_request(
        server["port"], "POST", "/api/history/delete",
        {"episode_id": episode["id"], "delete_assets": False})

    assert status == 200
    assert deleted["episode_deleted"] is True
    assert deleted["episode_number"] == 2
    assert deleted["documents_deleted"] == 1
    app3 = App(server["workspace"])
    try:
        assert app3.projects.get_episode(episode["id"]) is None
        assert app3.projects.get_project("总览删除接口") is not None
    finally:
        app3.close()


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

    # 人物定版页允许放弃当前选择并回到候选生成;旧版本仍保留在历史中。
    status, reply = _json_request(port, "POST", "/api/character/regenerate", {
        "episode_id": episode_id})
    assert status == 202 and reply["job_id"]
    job = _wait_job(port, reply["job_id"])
    assert job["summary"]["status"] == "awaiting_cast"
    status, pre = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert status == 200
    selection = pre["cast_selection"]
    assert selection["locked"] == 0 and not selection["passed"]
    assert all(len(c["candidates"]) == 5 for c in selection["characters"])

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
        "episode_id": episode_id, "video_quality": "high"})
    assert status == 202
    job = _wait_job(port, reply["job_id"])
    assert job["summary"]["status"] == "done"

    status, detail = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["qc_report"]["passed"]
    assert detail["quality_policy"]["video_default"] == "high"
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


def test_multi_episode_document_preview_import_and_serial_queue(server):
    port = server["port"]
    text = """第1集 初见
第1场 办公室
林昭:这里就是新公司?
团长:先把第一场直播做好。

第2集 开播
第1场 直播间
林昭:数据怎么突然涨了?
团长:有人在帮我们。
"""
    request = {
        "sentence": "《串行导入测试》第5集",
        "filename": "整季剧本.txt",
        "data_base64": base64.b64encode(text.encode()).decode(),
        "auto_advance": True,
    }
    status, preview = _json_request(
        port, "POST", "/api/series/preview", request)
    assert status == 200
    assert preview["project_title"] == "串行导入测试"
    assert preview["start_number"] == 5
    assert preview["total"] == 2
    assert [item["mode"] for item in preview["episodes"]] == [
        "script", "script"]
    assert preview["can_import"] is True

    status, imported = _json_request(
        port, "POST", "/api/series/import", request)
    assert status == 201 and imported["job_id"] is None
    batch = imported["batch"]
    assert batch["total"] == 2 and batch["auto_advance"] is True
    assert batch["current"]["episode_number"] == 5
    assert batch["next"]["episode_number"] == 6

    _, overview = _json_request(port, "GET", "/api/overview")
    statuses = {item["number"]: item["status"]
                for item in overview["episodes"]}
    assert statuses == {5: "awaiting_script", 6: "queued_script"}
    assert overview["series_batches"][0]["completed"] == 0

    status, blocked = _json_request(
        port, "POST", "/api/series/next", {"batch_id": batch["id"]})
    assert status == 409 and "尚未完成" in blocked["error"]

    app_state = App(server["workspace"])
    try:
        app_state.projects.set_episode_status(
            batch["current"]["episode_id"], "done")
    finally:
        app_state.close()
    status, advanced = _json_request(
        port, "POST", "/api/series/next", {"batch_id": batch["id"]})
    assert status == 202 and advanced["job_id"] is None
    assert advanced["step"]["number"] == 6
    _, detail = _json_request(
        port, "GET", f"/api/episode/{advanced['step']['episode_id']}")
    assert detail["episode"]["status"] == "awaiting_script"
    assert detail["series_source"]["filename"] == "整季剧本.txt"

    # 同集数再次导入只报告冲突，绝不覆盖已经确认的剧本。
    status, conflict = _json_request(
        port, "POST", "/api/series/import", request)
    assert status == 409 and "未覆盖任何内容" in conflict["error"]


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


def test_image_acceleration_options_and_preflight_api(server):
    """加速 API 只读预检会锁定提示词、参考图、provider 和 model。"""
    port = server["port"]
    status, _ = _json_request(port, "POST", "/api/settings", {
        "provider": "image_api",
        "fields": {"enabled": True, "api_key": "test-key",
                   "model": "gpt-image-2"},
    })
    assert status == 200
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("网页加速测试")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        out_root = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
                    / "e001")
        ref = out_root / "identity.png"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"r" * 32)
        ctx = {"project": dict(project), "episode": dict(episode),
               "out_root": out_root}
        app2.director._plan_write(ctx, {"items": [{
            "id": "shot:1", "category": "shot_image",
            "label": "镜头 01", "status": "pending",
            "prompt": "林昭看向镜头",
        }]})
        task = {
            "item_id": "shot:1", "capability": "image",
            "sub_dir": "images", "tag": 1,
            "payload": {
                "shot_no": 1, "characters": ["林昭"],
                "prompt": "林昭看向镜头", "aspect": "9:16",
                "image_quality": "medium", "image_task_class": "batch",
                "identity_references": [{
                    "character": "林昭", "asset_id": 1, "uri": str(ref)}],
                "character_refs": [str(ref)],
                "require_reference_images": True,
            },
        }
        app2.director._prepare_dispatch_contracts(ctx, [task])
        episode_id = episode["id"]
    finally:
        app2.close()

    status, options = _json_request(
        port, "GET", f"/api/image_acceleration/options?episode_id={episode_id}")
    assert status == 200
    item = next(value for value in options["items"]
                if value["item_id"] == "shot:1")
    assert item["status"] == "ready"
    assert item["references"]["items"][0]["url"].startswith("/artifacts/")
    body = {
        "episode_id": episode_id, "item_ids": ["shot:1"],
        "contract_tokens": {"shot:1": item["contract_token"]},
        "provider": "image_api", "model": "gpt-image-2",
        "quality": "medium",
    }
    status, preflight = _json_request(
        port, "POST", "/api/image_acceleration/preflight", body)
    assert status == 200 and preflight["passed"] is True
    assert preflight["items"][0]["references"]["count"] == 1
    status, reply = _json_request(
        port, "POST", "/api/image_acceleration/queue",
        {**body, "fingerprint": "stale"})
    assert status == 409 and "预检结果已过期" in reply["error"]


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
