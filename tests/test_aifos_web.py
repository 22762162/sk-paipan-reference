"""Web 控制台测试:API、后台制作任务、产物服务与目录穿越防护。"""

import base64
import copy
import http.client
import json
import threading
import time

import pytest

from aifos.app import App
from aifos.db import now
from aifos.director import character_candidate_target
from aifos.story_analysis import build_story_analysis, validate_story_analysis
from aifos.web.server import JobRegistry, _current_render_plan_items, serve


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


def test_legacy_fifth_character_candidate_is_hidden_from_current_plan():
    plan = {"items": [
        {"id": "candidate:林川:4", "category": "character_candidate",
         "candidate_index": 4},
        {"id": "candidate:林川:5", "category": "character_candidate"},
        {"id": "scene:主场景", "category": "scene_art",
         "qc": {"passed": False, "issues": ["旧版母资产质检失败"]}},
        {"id": "shot:1", "category": "shot_image",
         "qc": {"passed": False, "issues": ["镜头质检失败"]}},
    ]}
    current = _current_render_plan_items(plan)
    assert [item["id"] for item in current] == [
        "candidate:林川:4", "scene:主场景", "shot:1"]
    assert "qc" not in current[1], "母资产不得显示旧版视觉质检失败"
    assert current[2]["qc"]["passed"] is False, "镜头质检仍需保留"
    assert len(plan["items"]) == 4, "过滤不能改写磁盘历史计划"
    assert "qc" in plan["items"][2], "API 过滤不能改写磁盘历史计划"


def test_index_and_static(server):
    status, ctype, raw = _request(server["port"], "GET", "/")
    assert status == 200 and "text/html" in ctype
    html = raw.decode("utf-8")
    assert "AIFOS" in html
    assert "历史记录" in html
    assert "/static/style.css?v=20260726-semantic-qc-1" in html
    assert "/static/app.js?v=20260726-semantic-qc-1" in html
    status, ctype, app_js = _request(server["port"], "GET", "/static/app.js")
    assert status == 200 and "javascript" in ctype
    assert b"showBlockingOverlay" in app_js
    assert "只统计当前有效资产".encode() in app_js
    assert b"stableProductionProgress(data.production_progress)" in app_js
    assert b"logEl.dataset.signature === signature" in app_js
    assert b"function planVisibleQc(item)" in app_js
    assert b"const visibleQc = planVisibleQc(item)" in app_js
    assert b'class="live-elapsed dim"' in app_js
    assert b"pausedProductionState" in app_js
    assert b"pausedProductionAccessHtml" in app_js
    assert b'&& !pausedProduction.active' in app_js
    assert b"episodeWorkspaceNavHtml" in app_js
    assert b"bindEpisodeWorkspaceNav" in app_js
    assert b'data-workspace-stage="${esc(stage.key)}"' in app_js
    assert "可从下方八个制作环节直接进入".encode() in app_js
    assert "制作已暂停，已有内容仍可查看".encode() in app_js
    assert "已生成".encode() in app_js
    assert "可用于下游".encode() in app_js
    assert b"production-audit-records" in app_js
    assert "问题中心".encode() in app_js
    assert b"planNeedsRevision" in app_js
    assert "仅显示需修改".encode() in app_js
    assert "显示全部图片".encode() in app_js
    assert b"refreshOpenPlanOverlay(episodeId, force" in app_js
    assert b"planOverlaySignatures" in app_js
    assert b"planOverlayPollSignatures" in app_js
    assert "空间调度".encode() in app_js
    assert "人物编号图例".encode() in app_js
    assert "人物路线".encode() in app_js
    assert "镜头路线".encode() in app_js
    assert b"mountBlocking3d" in app_js
    assert b"blocking-3d-canvas" in app_js
    assert b"blockingStickFigureGeometry" in app_js
    assert "彩色火柴人=人物".encode() in app_js
    assert "机位方向".encode() in app_js
    assert "拖拽旋转".encode() in app_js
    assert "固定 3D 参考图".encode() in app_js
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
    assert "Codex 多通道".encode() in app_js
    assert "通道 A".encode() in app_js
    assert "通道 B".encode() in app_js
    assert "CODEX_HOME / 配置路径".encode() in app_js
    assert "图片并行生产 · 任务分片".encode() in app_js
    assert b"codex_profiles" in app_js
    assert b"codex_parallel" in app_js
    assert b"/api/image-production/shards" in app_js
    assert "接口待接入".encode() in app_js
    assert b"/api/series/preview" in app_js
    assert b"/api/series/import" in app_js
    assert "选择多集剧本文档".encode() in app_js
    assert "AI 根据剧本自动设计（推荐）".encode() in app_js
    assert "智能解析小说 / 剧本并 AI 分析".encode() in app_js
    assert "小说 / 剧本智能解析完成".encode() in app_js
    assert "AI 根据剧本生成".encode() in app_js
    assert "始终只激活一集".encode() in app_js
    assert b"/api/character/select" in app_js
    assert b"/api/character/regenerate" in app_js
    assert b"/api/story-analysis" in app_js
    assert "世界观、环境与视觉制作圣经".encode() in app_js
    assert "去编辑剧本确认说话人".encode() in app_js
    assert b'character.importance !== "' + "背景路人".encode() + b'"' in app_js
    assert "锁定制作圣经并开始人物图".encode() in app_js
    assert "剧本第一道总闸门".encode() in app_js
    assert "导入小说自动完成影视化改编".encode() in app_js
    assert b"/api/asset/delete" in app_js
    assert b"/api/history/delete" in app_js
    assert b"history-delete-row" in app_js
    assert b"episode-delete-work" in app_js
    assert "删除作品".encode() in app_js
    assert "保留资产中心图片（推荐）".encode() in app_js
    assert "资产分类".encode() in app_js
    assert "查看提示词".encode() in app_js
    assert b"/api/video/references" in app_js
    assert b"/api/redo_video" in app_js
    assert "全部重新生成".encode() in app_js
    assert b"episode-rebuild-all" in app_js
    assert b"btn-rebuild-all-recovery" in app_js
    assert b"force: true" in app_js
    assert "armConfirm(button, \"全部重新生成\"".encode() in app_js
    assert "质检没有通过的原因".encode() in app_js
    assert "批量优化修改".encode() in app_js
    assert "人工通过".encode() in app_js
    assert "打开清单批量优化".encode() in app_js
    assert b"data-image-failure-pass" in app_js
    assert b"planSelectQcFailed" in app_js
    assert b"ensureBatchRevisionCheckpoint" in app_js
    assert b"/api/qc_override" in app_js
    assert "本次质检/重画实际附上的参考图".encode() in app_js
    assert "出图前剧本/逻辑纠正".encode() in app_js
    assert "逻辑修正 ×".encode() in app_js
    assert b"planSemanticCorrections" in app_js
    assert "待人工问题清单".encode() in app_js
    assert b"imageFailurePanelHtml" in app_js
    assert b"focusImageFailureShot" in app_js
    assert b"image_failures" in app_js
    assert b"data-image-failure-shot" in app_js
    assert "跳到镜头".encode() in app_js
    assert "展开修改".encode() in app_js
    assert b"generationDiagnosisHtml" in app_js
    assert b"videoFailurePanelHtml" in app_js
    assert b"data-generation-diagnosis" in app_js
    for heading in ("画面错在哪里", "提示词诊断", "参考图诊断",
                    "本次实际调整", "重试结果"):
        assert heading.encode() in app_js
    assert b"scrollIntoView" in app_js
    assert "先处理问题图".encode() in app_js
    assert "失败稿不会进入正式资产".encode() in app_js
    assert "待生产图片批量 API 加速".encode() in app_js
    assert "API 批量加速".encode() in app_js
    assert "选择 API/模型并加速".encode() in app_js
    assert b'id="btn-plan-live"' in app_js
    assert b'id="btn-image-acceleration"' in app_js
    assert app_js.count(b"imageAccelerationLivebarHtml(data)") >= 4
    assert b'updateViaCache: "none"' in app_js
    assert b"/api/image_acceleration/preflight" in app_js
    assert b"/api/image_acceleration/queue" in app_js
    assert "逐张预检所选图片".encode() in app_js
    assert "中 · 默认生产档".encode() in app_js
    assert "人物定版".encode() in app_js
    assert "分镜头生产表".encode() in app_js
    assert "全流程生产表".encode() in app_js
    assert b"productionLedgerHtml" in app_js
    assert b"productionLedgerRowIsUseful" in app_js
    assert b'row.category === "character_candidate" && !row.selected' in app_js
    assert b'["failed", "retrying", "awaiting_human"].includes(row.status)' in app_js
    assert "未选候选、失败产物、质检废片与占位图已隐藏".encode() in app_js
    assert "全部有效生产项".encode() in app_js
    assert "全生产链画布".encode() in app_js
    assert b"productionCanvasStages" in app_js
    assert b' id="canvas-stage-nav"' in app_js
    assert b"data-canvas-stage" in app_js
    assert b"focusStage(stageKey)" in app_js
    assert "所需参考图".encode() in app_js
    assert "问题 / 干预".encode() in app_js
    assert "故事世界与背景圣经".encode() in app_js
    assert "后续人物、分镜和镜头统一以此为准".encode() in app_js
    assert b"storyBibleHtml(script)" in app_js
    assert "人物介绍".encode() in app_js
    assert "所有正式角色统一4张候选".encode() in app_js
    assert "跑龙套/背景路人不做独立设定".encode() in app_js
    assert "不建立独立人物设定，不生成候选图、立绘或四视图".encode() in app_js
    for heading in ("序号", "时长", "参考分镜", "首尾帧", "运镜",
                    "画面描述", "声音", "生产状态"):
        assert f'<th scope="col">{heading}</th>'.encode() in app_js
    assert b"shotProductionTableHtml(data" in app_js
    assert b"bindShotProductionTable(app, data)" in app_js
    assert b"missing-keyframe" in app_js
    assert b"missing-frames" in app_js
    assert b"missing-video" in app_js
    assert "Seedance 参考图输入表".encode() in app_js
    assert "首帧（必传）".encode() in app_js
    assert "尾帧（必传）".encode() in app_js
    assert "资产参考图（最多 7 张）".encode() in app_js
    assert "锁定人物身份、脸型和发型".encode() in app_js
    assert "人物特征参考".encode() in app_js
    assert b"friendlyVideoReferenceName" in app_js
    assert b"videoReferenceFigureHtml" in app_js
    assert b"video-ref-preview" in app_js
    assert "直接修改此图".encode() in app_js
    assert b"frameInlineRevisionHtml" in app_js
    assert b"revisionGenerationSnapshot" in app_js
    assert "实际生产输入（原提示词）".encode() in app_js
    assert "分镜基础提示词（非实际调用记录）".encode() in app_js
    assert "旧记录未保存实际提交提示词".encode() in app_js
    assert "实际提交参考图对照".encode() in app_js
    assert "质检结论".encode() in app_js
    assert "复制原提示词".encode() in app_js
    assert "同场上一镜的尾帧".encode() in app_js
    assert "同场下一镜的首帧".encode() in app_js
    assert b"first_frame" in app_js
    assert b"last_frame" in app_js
    assert "暂停并修改".encode() in app_js
    assert "修改并同步后续".encode() in app_js
    assert b"bindShotInlineRevisions(root, data)" in app_js
    assert b"storyboardLineNo(data, shot)" in app_js
    status, ctype, style_css = _request(
        server["port"], "GET", "/static/style.css")
    assert status == 200 and "css" in ctype
    assert b".image-cost-guide" in style_css
    assert b".codex-execution-panel" in style_css
    assert b".codex-channel-grid" in style_css
    assert b".codex-shard-board" in style_css
    assert b".codex-progress" in style_css
    assert b".blocking-actor-legend" in style_css
    assert b".blocking-map-scroll" in style_css
    assert b".blocking-3d-stage" in style_css
    assert b".blocking-3d-canvas" in style_css
    assert b".blocking-3d-toolbar" in style_css
    assert b".video-ref-picker-grid" in style_css
    assert b".video-ref-table" in style_css
    assert b".video-ref-card" in style_css
    assert b".video-ref-status" in style_css
    assert b".plan-ref-gallery" in style_css
    assert b".qc-manual-pass" in style_css
    assert b".plan-qc-accept" in style_css
    assert b".image-failure-pass" in style_css
    assert b".generation-diagnosis-grid" in style_css
    assert b".generation-diagnosis-section" in style_css
    assert b".generation-issue-card-main" in style_css
    assert b".acceleration-panel" in style_css
    assert b".image-accel-livebar" in style_css
    assert b".accel-gates" in style_css
    assert b".shot-production-table" in style_css
    assert b".storyboard-table-row" in style_css
    assert b".storyboard-frame-pair" in style_css
    assert b".storyboard-status-stack" in style_css
    assert b".shot-inline-revision" in style_css
    assert b".frame-inline-revision" in style_css
    assert b".storyboard-frame-item" in style_css
    assert b".shot-revision-form" in style_css
    assert b".shot-generation-input" in style_css
    assert b".shot-original-prompt-text" in style_css
    assert b".shot-original-references" in style_css
    assert b".shot-generation-qc" in style_css
    assert b".background-cast-note" in style_css
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
    assert b"aifos-mobile-shell-v7" in raw
    assert b'/static/app.js' in raw and b'fetch(request)' in raw


def test_blocking_svg_url_uses_document_version_to_bust_cache(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("空间图版本")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        svg = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
               / "e001" / "blocking" / "scene_001.svg")
        svg.parent.mkdir(parents=True, exist_ok=True)
        svg.write_text("<svg/>", encoding="utf-8")
        blocking = {
            "scenes": [{"scene_no": 1, "svg_uri": str(svg), "shots": []}],
            "summary": {}, "validation": {"passed": True},
        }
        app2.projects.save_document(episode["id"], "blocking", blocking)
        episode_id = episode["id"]
    finally:
        app2.close()

    status, payload = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")

    assert status == 200
    assert payload["blocking"]["scenes"][0]["svg_url"].endswith("?v=1")


def test_episode_status_is_lightweight_and_changes_with_documents(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("轻量轮询测试")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        episode_id = episode["id"]
    finally:
        app2.close()

    status, first = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}/status")
    assert status == 200
    assert len(first["signature"]) == 64
    assert first["episode"]["id"] == episode_id
    assert first["document_versions"] == {}
    assert first["jobs"] == []
    for heavy_key in (
            "script", "story_analysis", "storyboard", "render_plan",
            "production_progress", "artifacts", "image_failures"):
        assert heavy_key not in first

    app2 = App(server["workspace"])
    try:
        app2.projects.save_document(
            episode_id, "script", {"title": "第一版", "scenes": []})
    finally:
        app2.close()

    status, second = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}/status")
    assert status == 200
    assert second["document_versions"]["script"] == 1
    assert second["signature"] != first["signature"]

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["poll_signature"] == second["signature"]


def test_job_registry_unique_reuses_running_episode(tmp_path):
    """多标签重复确认必须复用同一任务，不新增历史或重复烧额度。"""
    workspace = tmp_path / "ws"
    App(workspace).close()
    jobs = JobRegistry(workspace)
    release = threading.Event()

    def slow_task(app, run_id):
        assert release.wait(5)
        return {"status": "done"}

    first = jobs.start_task(
        "重复确认", 1, slow_task, action="confirm_preflight", unique=True)
    second = jobs.start_task(
        "重复确认", 1, slow_task, action="confirm_preflight", unique=True)
    assert second == first
    assert len(jobs.running_for("重复确认", 1)) == 1
    check = App(workspace)
    try:
        assert check.db.query_one(
            "SELECT COUNT(*) AS n FROM production_runs")["n"] == 1
    finally:
        check.close()
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and jobs.get(first)["status"] == "running":
        time.sleep(0.02)
    assert jobs.get(first)["status"] == "done"


def test_job_registry_accounts_unstaged_adjustment_cost(tmp_path):
    workspace = tmp_path / "ws"
    app = App(workspace)
    try:
        project, _ = app.projects.get_or_create_project("调整成本测试")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        episode_id = episode["id"]
    finally:
        app.close()
    jobs = JobRegistry(workspace)

    def billed_adjustment(app, run_id):
        app.director._task_providers = {"codex"}
        app.projects.add_episode_cost(episode_id, 2.5)
        return {"status": "done", "redone": 1}

    job_id = jobs.start_task(
        "调整成本测试", 1, billed_adjustment, action="redo_items")
    deadline = time.time() + 5
    while time.time() < deadline and jobs.get(job_id)["status"] == "running":
        time.sleep(0.02)
    assert jobs.get(job_id)["status"] == "done"

    check = App(workspace)
    try:
        task = check.db.query_one(
            "SELECT * FROM tasks WHERE run_id=?",
            (jobs.get(job_id)["run_id"],))
        assert task["stage"] == "images"
        assert task["provider"] == "codex"
        assert task["cost"] == pytest.approx(2.5)
        history = check.history.get(jobs.get(job_id)["run_id"])
        assert history["cost"] == pytest.approx(2.5)
        assert history["last_stage"] == "images"
        assert history["stage_count"] == 1
        assert sum(row["total"] for row in
                   check.system.cost_by_stage()) == pytest.approx(2.5)
        assert sum(row["total"] for row in
                   check.system.cost_by_provider()) == pytest.approx(2.5)
    finally:
        check.close()


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
    status, overview = _json_request(
        server["port"], "GET", "/api/overview")
    assert status == 200
    assert "资产接口测试" not in overview["asset_stats"]


def test_episode_exposes_image_failures_with_artifact_urls(server):
    """二次 QC 失败必须成为可预览、可定位到镜头的待人工问题。"""
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("图片待人工接口")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        out_root = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
                    / "e001")
        failed = out_root / "images" / "shot-7-qc-failed.png"
        failed.parent.mkdir(parents=True, exist_ok=True)
        failed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"f" * 32)
        app2.director._plan_write({
            "out_root": out_root,
        }, {"items": [{
            "id": "shot:7",
            "category": "shot_image",
            "label": "镜头 07",
            "shot_no": 7,
            "status": "awaiting_human",
            "error": "图片质检未通过",
            "output_uri": str(failed),
            "qc": {
                "passed": False,
                "awaiting_human": True,
                "attempts": 2,
                "consecutive_failures": 2,
                "issues": ["人物多出一人", "服装颜色与定版不一致"],
                "generation_input": {
                    "schema": "aifos.image-generation-input/v1",
                    "prompt": "本次真正提交给生图模型的原提示词",
                    "reference_manifest": [{
                        "index": 1,
                        "asset_id": 101,
                        "label": "程沐最终立绘",
                        "role": "identity",
                        "binding": "只锁定程沐的脸型、五官、性别与发型",
                        "uri": str(failed),
                    }],
                    "input_hash": "saved-generation-input",
                },
                "revision_feedback": f"按失败稿 {failed} 定向修正人数",
                "codex_escalation": {
                    "status": "completed",
                    "provider": "codex",
                    "aifos_action": "targeted_redraw",
                    "reason": "人数合同明确，画面多出第三人",
                    "instruction_to_aifos": "只保留两名已登记角色",
                },
                "input_diagnosis": {
                    "image_error": {
                        "summary": "右侧出现多余人物",
                        "evidence": ["右侧边缘可见第三人"],
                    },
                    "prompt_audit": {
                        "status": "conflicting",
                        "issues": ["人数描述互相冲突"],
                    },
                    "reference_audit": {
                        "status": "needs_adjustment",
                        "issues": ["第二张参考图绑定到错误人物"],
                    },
                    "prompt": "这是绝不能通过问题清单接口泄漏的完整提示词",
                    "local_path": str(failed),
                },
                "applied_changes": [{
                    "prompt_patch": "只保留两名角色",
                    "output_uri": str(failed),
                }],
                "attempt_history": [{
                    "attempt": 1,
                    "generation_prompt": "完整提示词副本",
                    "artifact_path": str(failed),
                    "result": "failed",
                }],
                "decision": {"action": "manual_review"},
                "retry_blocked_reason": (
                    f"输入未发生有效变化，失败稿位于 {failed}"),
            },
        }]})
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["render_plan"]["items"][0]["status"] == "awaiting_human"
    saved_input = detail["render_plan"]["items"][0]["qc"]["generation_input"]
    assert saved_input["prompt"] == "本次真正提交给生图模型的原提示词"
    assert saved_input["reference_manifest"][0]["label"] == "程沐最终立绘"
    assert len(detail["image_failures"]) == 1
    failure = detail["image_failures"][0]
    assert failure["item_id"] == "shot:7"
    assert failure["shot_no"] == 7
    assert failure["consecutive_failures"] == 2
    assert failure["codex_escalation"]["status"] == "completed"
    assert failure["codex_escalation"]["aifos_action"] == \
        "targeted_redraw"
    assert failure["codex_escalation"]["instruction_to_aifos"] == \
        "只保留两名已登记角色"
    assert failure["issues"] == ["人物多出一人", "服装颜色与定版不一致"]
    assert failure["input_diagnosis"]["prompt_audit"]["status"] == \
        "conflicting"
    assert failure["input_diagnosis"]["reference_audit"]["status"] == \
        "needs_adjustment"
    assert failure["applied_changes"][0]["prompt_patch"] == \
        "只保留两名角色"
    assert failure["attempt_history"][0]["attempt"] == 1
    assert failure["retry_decision"]["action"] == "manual_review"
    assert "[本地路径已隐藏]" in failure["retry_blocked_reason"]
    assert "[本地路径已隐藏]" in failure["revision_feedback"]
    safe_failure = json.dumps(failure, ensure_ascii=False)
    assert str(failed) not in safe_failure
    assert "绝不能通过问题清单接口泄漏" not in safe_failure
    assert "完整提示词副本" not in safe_failure
    failed_url = failure["failed_output_url"]
    assert failed_url.startswith("/artifacts/")
    status, ctype, raw = _request(server["port"], "GET", failed_url)
    assert status == 200 and ctype.startswith("image/png") and raw


def test_episode_exposes_safe_video_input_diagnosis(server):
    """视频问题卡取得同一诊断契约，但不取得提示词全文或源帧绝对路径。"""
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("视频输入诊断接口")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        local_frame = (
            app2.workspace.artifacts_dir / f"p{project['id']:03d}"
            / "e001" / "frames" / "shot-2-first.png")
        app2.projects.save_document(episode["id"], "video_qc_report", {
            "schema": "aifos.video-qc/v2",
            "awaiting_human": True,
            "shots": [{
                "shot_no": 2,
                "passed": False,
                "awaiting_human": True,
                "issues": ["人物动作漂移"],
                "input_diagnosis": {
                    "prompt_diagnosis": {
                        "valid": False,
                        "problems": ["包含第二个主动作"],
                    },
                    "reference_audit": {
                        "valid": False,
                        "problems": ["尾帧不属于当前镜头"],
                    },
                    "frame_audit": {
                        "first_valid": True,
                        "last_valid": False,
                    },
                    "prompt_sent": "完整 Seedance 提示词不得由问题卡接口返回",
                },
                "decision": {
                    "action": "repair_frames_first",
                    "safe_to_auto_retry": False,
                    "reason": "先修复尾帧",
                },
                "attempt_history": [{
                    "attempt": 1,
                    "first_frame": str(local_frame),
                    "prompt_sent_hash": "hash-only",
                }],
            }],
        })
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    shot = detail["video_qc_report"]["shots"][0]
    assert shot["input_diagnosis"]["prompt_diagnosis"]["valid"] is False
    assert shot["input_diagnosis"]["reference_audit"]["valid"] is False
    assert shot["input_diagnosis"]["frame_audit"]["last_valid"] is False
    assert shot["retry_decision"]["action"] == "repair_frames_first"
    assert shot["attempt_history"][0]["first_frame"] == "[本地路径已隐藏]"
    safe_diagnosis = json.dumps({
        "input_diagnosis": shot["input_diagnosis"],
        "attempt_history": shot["attempt_history"],
        "retry_decision": shot["retry_decision"],
    }, ensure_ascii=False)
    assert str(local_frame) not in safe_diagnosis
    assert "完整 Seedance 提示词" not in safe_diagnosis


def test_manual_qc_override_api_promotes_problem_image(server, monkeypatch):
    """人工通过接口一次处理问题图，并保留质检审计字段。"""
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("人工通过接口")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        app2.projects.save_document(
            episode["id"], "script",
            {"scenes": [{"scene_no": 1, "location": "室内"}],
             "characters": []})
        app2.projects.save_document(
            episode["id"], "storyboard",
            {"shots": [{"shot_no": 1, "scene_no": 1,
                         "characters": [], "description": "停顿"}]})
        out_root = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
                    / "e001")
        failed = out_root / "images" / "shot_001.failed.png"
        failed.parent.mkdir(parents=True, exist_ok=True)
        failed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
        app2.director._plan_write({"out_root": out_root}, {"items": [{
            "id": "shot:1", "category": "shot_image", "shot_no": 1,
            "label": "镜头 01", "status": "awaiting_human",
            "output_uri": str(failed),
            "qc": {"passed": False, "issues": ["轻微构图偏差"]},
        }]})
        episode_id = episode["id"]
    finally:
        app2.close()
    monkeypatch.setattr(
        JobRegistry, "running_for",
        lambda self, title, number: [{"status": "running"}])
    status, result = _json_request(server["port"], "POST", "/api/qc_override", {
        "episode_id": episode_id, "item_ids": ["shot:1"],
        "note": "人工确认可接受",
    })
    assert status == 200 and result["passed"] == 1
    app3 = App(server["workspace"])
    try:
        plan = json.loads((out_root / "render_plan.json").read_text(
            encoding="utf-8"))
    finally:
        app3.close()
    item = plan["items"][0]
    assert item["status"] == "done"
    assert item["qc"]["manual_override"] is True


def test_episode_production_progress_uses_verified_formal_assets(server):
    """完成进度只认正式资产，并准确列出正在生产和待处理问题。"""
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("真实生产进度")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        out_root = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
                    / "e001")
        completed = out_root / "images" / "shot-1.png"
        completed.parent.mkdir(parents=True, exist_ok=True)
        completed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"d" * 32)
        app2.assets.register(
            project["id"], "image", "e001_shot001",
            uri=str(completed))
        started_at = time.time() - 12
        app2.director._plan_write({
            "out_root": out_root,
        }, {"items": [
            {
                "id": "shot:1", "category": "shot_image",
                "label": "镜头 01", "shot_no": 1, "status": "done",
            },
            {
                "id": "shot:2", "category": "shot_image",
                "label": "镜头 02", "shot_no": 2, "status": "done",
            },
            {
                "id": "shot:3", "category": "shot_image",
                "label": "镜头 03", "shot_no": 3,
                "status": "generating", "started_at": started_at,
            },
            {
                "id": "shot:4", "category": "shot_image",
                "label": "镜头 04", "shot_no": 4,
                "status": "awaiting_human",
                "qc": {"issues": ["人物身份不一致"]},
            },
            {
                "id": "frames:1", "category": "frames",
                "label": "镜头 01 首尾帧", "shot_no": 1,
                "status": "reused",
            },
        ]})
        stamp = now()
        app2.db.execute(
            "INSERT INTO tasks(episode_id, stage, name, status, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?)",
            (episode["id"], "images", "批量关键帧", "running",
             stamp, stamp))
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    progress = detail["production_progress"]
    shots = next(item for item in progress["categories"]
                 if item["category"] == "shot_image")
    assert shots["total"] == 4
    assert shots["done"] == 1
    assert shots["generated"] == 1
    assert shots["usable"] == 1
    assert shots["awaiting_review"] == 0
    assert shots["needs_repair"] == 1
    assert shots["queued"] == 0
    assert shots["not_generated"] == 1
    assert shots["unverified_done"] == 1
    assert shots["generating"] == 1
    assert shots["awaiting_human"] == 1
    assert shots["percent"] == 25.0
    assert progress["overall"]["running"] is True
    assert progress["overall"]["current_stage"] == "images"
    assert progress["overall"]["current_stage_label"] == "批量关键帧"
    assert progress["overall"]["parallelism"]["image"] == {
        "active": 1, "limit": 3}
    assert progress["overall"]["parallelism"]["video"] == {
        "active": 0, "limit": 4}
    frames = next(item for item in progress["categories"]
                  if item["category"] == "frames")
    assert frames["reused"] == 0
    assert frames["unverified_done"] == 1
    active = progress["active_items"]
    assert len(active) == 1
    assert active[0]["item_id"] == "shot:3"
    assert active[0]["shot_no"] == 3
    assert active[0]["elapsed"] >= 11
    issues = {item["item_id"]: item for item in progress["issues"]}
    assert issues["shot:2"]["status"] == "unverified_done"
    assert issues["shot:4"]["issues"] == ["人物身份不一致"]
    assert issues["frames:1"]["status"] == "unverified_done"

    app3 = App(server["workspace"])
    try:
        app3.db.execute(
            "UPDATE tasks SET status='done', updated_at=? "
            "WHERE episode_id=? AND status='running'",
            (now(), episode_id))
    finally:
        app3.close()
    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    progress = detail["production_progress"]
    assert progress["overall"]["running"] is False
    assert progress["active_items"] == []
    shots = next(item for item in progress["categories"]
                 if item["category"] == "shot_image")
    assert shots["generating"] == 0
    assert shots["pending"] == 1
    issues = {item["item_id"]: item for item in progress["issues"]}
    assert issues["shot:3"]["status"] == "stale_active"


def test_episode_progress_multiplies_per_channel_codex_capacity(server):
    """每通道 8 路；双 Codex 就绪时生产面板显示总容量 16 路。"""
    port = server["port"]
    status, _ = _json_request(port, "POST", "/api/settings", {
        "defaults": {"parallel_images": 8},
    })
    assert status == 200
    status, _ = _json_request(port, "POST", "/api/settings", {
        "codex_profiles": [
            {"id": "codex_a", "name": "A", "enabled": True,
             "codex_home": str(server["workspace"]), "command": "codex"},
            {"id": "codex_b", "name": "B", "enabled": True,
             "codex_home": str(server["workspace"]), "command": "codex"},
        ],
    })
    assert status == 200
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("双通道生产面板")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        out_root = (app2.workspace.artifacts_dir / f"p{project['id']:03d}"
                    / "e001")
        app2.director._plan_write({"out_root": out_root}, {"items": [
            {"id": "shot:1", "category": "shot_image", "shot_no": 1,
             "status": "generating", "codex_profile": "codex_a"},
        ]})
        stamp = now()
        app2.db.execute(
            "INSERT INTO tasks(episode_id, stage, name, status, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?)",
            (episode["id"], "images", "关键帧", "running", stamp, stamp))
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        port, "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["production_progress"]["overall"]["parallelism"]["image"] == {
        "active": 1, "limit": 16}

    app3 = App(server["workspace"])
    try:
        app3.db.execute(
            "UPDATE tasks SET status='done', updated_at=? "
            "WHERE episode_id=? AND status='running'",
            (now(), episode_id))
    finally:
        app3.close()


def test_episode_guidance_pauses_at_incomplete_keyframe_gate(server):
    """错误的 awaiting_confirm 不能越过正式关键帧和首尾帧门禁。"""
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("智能生产引导")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        app2.projects.set_episode_status(episode["id"], "awaiting_confirm")
        app2.projects.save_document(
            episode["id"], "storyboard", {
                "shots": [
                    {
                        "shot_no": number,
                        "scene_no": 1,
                        "description": f"镜头 {number}",
                    }
                    for number in range(1, 67)
                ],
            })
        out_root = (
            app2.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001")
        artifact = out_root / "images" / "verified.png"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"\x89PNG\r\n\x1a\n" + b"v" * 32)
        plan_items = []
        for number in range(1, 67):
            keyframe_status = (
                "done" if number <= 54
                else "pending" if number <= 63
                else "awaiting_human")
            item = {
                "id": f"shot:{number}",
                "category": "shot_image",
                "shot_no": number,
                "label": f"镜头 {number:02d}",
                "status": keyframe_status,
            }
            if keyframe_status == "awaiting_human":
                item["qc"] = {
                    "passed": False,
                    "issues": ["人物身份不一致"],
                    "hard_failure": number == 64,
                    "identity_match": number != 64,
                    "gender_match": True,
                    "count_match": True,
                }
            plan_items.append(item)
            if number <= 54:
                app2.assets.register(
                    project["id"], "image", f"e001_shot{number:03d}",
                    uri=str(artifact))

            frame_status = "done" if number <= 3 else "pending"
            plan_items.append({
                "id": f"frames:{number}",
                "category": "frames",
                "shot_no": number,
                "label": f"镜头 {number:02d} 首尾帧",
                "status": frame_status,
            })
            if number <= 3:
                for kind in ("first_frame", "last_frame"):
                    app2.assets.register(
                        project["id"], kind, f"e001_shot{number:03d}",
                        uri=str(artifact))
        app2.director._plan_write(
            {"out_root": out_root}, {"items": plan_items})
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["episode"]["status"] == "awaiting_confirm"
    guidance = detail["production_guidance"]
    assert guidance["state"] == "paused"
    assert guidance["phase"] == "keyframes"
    assert guidance["current_step"] == "keyframes"
    assert guidance["current_step_label"] == "关键帧生产"
    assert guidance["next_action"] == {
        "action": "resume_keyframes",
        "label": "继续生产 9 个非问题关键帧",
        "count": 9,
    }
    assert guidance["actions"]["pending_images"] == {
        "action": "resume_pending_images",
        "count": 9,
        "shot_nos": list(range(55, 64)),
        "item_ids": [f"shot:{number}" for number in range(55, 64)],
        "enabled": True,
        "label": "继续生产 9 个非问题关键帧",
    }
    assert guidance["actions"]["resolve_image_issues"] == {
        "action": "resolve_image_issues",
        "count": 3,
        "shot_nos": [64, 65, 66],
        "item_ids": ["shot:64", "shot:65", "shot:66"],
        "hard_failure": True,
        "severity": "mixed",
        "must_fix_count": 1,
        "review_or_accept_count": 2,
        "items": [
            {
                "item_id": "shot:64",
                "shot_no": 64,
                "hard_failure": True,
                "severity": "must_fix",
                "issues": ["人物身份不一致"],
                "identity_match": False,
                "gender_match": True,
                "count_match": True,
            },
            *[
                {
                    "item_id": f"shot:{number}",
                    "shot_no": number,
                    "hard_failure": False,
                    "severity": "review_or_accept",
                    "issues": ["人物身份不一致"],
                    "identity_match": True,
                    "gender_match": True,
                    "count_match": True,
                }
                for number in (65, 66)
            ],
        ],
        "enabled": True,
        "label": "修正 1 个必须修复、复核 2 个可放行关键帧",
    }
    assert guidance["next_actions"]["resume_pending_images"] == \
        guidance["actions"]["pending_images"]
    assert guidance["next_actions"]["resolve_image_issues"] == \
        guidance["actions"]["resolve_image_issues"]
    assert guidance["can_start_frames"] is False
    assert guidance["can_start_videos"] is False
    assert guidance["can_confirm_seedance"] is False

    keyframes = guidance["stages"]["keyframes"]
    assert (keyframes["status"], keyframes["usable"], keyframes["total"]) == (
        "paused", 54, 66)
    assert keyframes["pending"] == 9
    assert keyframes["awaiting_human"] == 3
    frames = guidance["stages"]["frames"]
    assert (frames["status"], frames["usable"], frames["total"]) == (
        "blocked", 3, 66)
    assert frames["blocked_by"] == [
        "keyframes_pending", "keyframes_awaiting_human"]
    assert frames["continuity_chains"] == 1
    assert frames["parallel_capacity"] == 1
    assert frames["note"] == "当前仅1条连续链，首尾帧将按链串行"
    videos = guidance["stages"]["videos"]
    assert (videos["status"], videos["usable"], videos["total"]) == (
        "blocked", 0, 66)
    assert videos["blocked_by"] == ["frames_incomplete"]
    assert [item["code"] for item in guidance["blockers"]] == [
        "keyframes_pending", "keyframes_awaiting_human"]
    issue_blocker = guidance["blockers"][1]
    assert issue_blocker["hard_failure"] is True
    assert issue_blocker["severity"] == "mixed"
    assert issue_blocker["must_fix_count"] == 1
    assert issue_blocker["review_or_accept_count"] == 2
    assert [item["severity"] for item in guidance["issues"]] == [
        "must_fix", "review_or_accept", "review_or_accept"]


def test_episode_payload_upgrades_legacy_story_analysis(server):
    script = {
        "project_title": "旧制作圣经兼容测试",
        "episode_title": "第一集",
        "logline": "都市女团成员在直播间解决一次意外。",
        "characters": [{
            "name": "乔安",
            "role": "主角",
            "identity": "女团成员",
            "age_range": "22岁",
            "personality": "果断",
        }],
        "scenes": [{
            "scene_no": 1,
            "location": "现代直播间",
            "characters": ["乔安"],
            "action": "乔安处理直播事故。",
        }],
    }
    legacy = build_story_analysis(script, "现代都市乙女游戏CG")
    for character in legacy["characters"]:
        character.pop("character_analysis", None)
        character.pop("visual_dna", None)
        character.pop("cast_dedup", None)

    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project(
            "旧制作圣经兼容测试", style="现代都市乙女游戏CG")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        app2.projects.save_document(episode["id"], "script", script)
        app2.projects.save_document(
            episode["id"], "story_analysis", legacy)
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    character = detail["story_analysis"]["characters"][0]
    assert character["character_analysis"]["identity_and_class"]
    assert character["visual_dna"]["temperament_keywords"]
    assert isinstance(character["cast_dedup"], dict)
    assert detail["script"]["production_analysis"]["characters"]


def test_episode_payload_compacts_valid_repeated_character_prompt(server):
    script = {
        "project_title": "人物提示词兼容测试",
        "episode_title": "第一集",
        "logline": "明代皇太子伤后醒来。",
        "characters": [{
            "name": "朱慈烺", "role": "主角",
            "gender": "男", "age_range": "约十五岁",
        }],
        "scenes": [{
            "scene_no": 1, "location": "东宫寝殿",
            "characters": ["朱慈烺"], "action": "太子起身。",
        }],
    }
    analysis = build_story_analysis(script, "电影级半写实精品漫剧")
    compact = (
        "电影级半写实精品漫剧，明代皇太子朱慈烺，约十五岁少年，"
        "乌黑长发束发无辫；全身正面，纯净棚拍背景，无文字")
    repeated = (
        "单人角色定妆母图：朱慈烺；父崇祯帝、母周皇后；"
        "距亡国不足108天，谋划说服父皇")
    analysis["characters"][0]["image_prompt"] = (
        f"{compact}；{repeated}；{repeated}")
    assert validate_story_analysis(analysis) is None

    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project(
            "人物提示词兼容测试", style="电影级半写实精品漫剧")
        episode, _ = app2.projects.get_or_create_episode(project["id"], 1)
        app2.projects.save_document(episode["id"], "script", script)
        app2.projects.save_document(
            episode["id"], "story_analysis", analysis)
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    prompt = detail["story_analysis"]["characters"][0]["image_prompt"]
    assert prompt == compact
    assert "108天" not in prompt
    assert "说服父皇" not in prompt


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
    assert item["board_group"] == "character_support"
    assert item["board_group_label"] == "人物辅助设定"
    assert item["usage_label"] == "辅助参考·按镜头调用"
    assert item["selected"] is False


def test_episode_catalog_accepts_inner_persona_asset(server):
    app2 = App(server["workspace"])
    try:
        project, _ = app2.projects.get_or_create_project("内心Q版资产测试")
        episode, _ = app2.projects.get_or_create_episode(
            project["id"], 1, title="内心戏")
        path = (app2.workspace.artifacts_dir
                / f"p{project['id']:03d}" / "inner_persona.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        row = app2.assets.register(
            project["id"], "inner_persona", "主角:chibi-a",
            uri=str(path), meta={
                "image_quality": "high",
                "prompt": "当前服装的夸张Q版内心人格，透明背景",
            })
        app2.assets.register(
            project["id"], "character_identity", "主角",
            uri=str(path), meta={"image_quality": "high", "locked": True})
        app2.projects.save_document(episode["id"], "storyboard", {
            "shots": [{
                "shot_no": 1, "scene_no": 1, "characters": ["主角"],
                "character_count": 1,
                "narrative_overlays": [{
                    "kind": "inner_persona_chibi",
                    "host_character": "主角",
                    "asset_name": row["name"],
                }],
            }],
        })
        episode_id = episode["id"]
    finally:
        app2.close()

    status, detail = _json_request(
        server["port"], "GET", f"/api/episode/{episode_id}")
    assert status == 200
    assert detail["artifacts"]["inner_personas"][0]["asset_id"] == row["id"]
    catalog_item = next(
        item for item in detail["artifacts"]["image_assets"]
        if item["asset_id"] == row["id"])
    assert catalog_item["label"].startswith("内心Q版母资产")
    assert catalog_item["category"] == "character"
    assert catalog_item["usage_label"] == "必要内心戏自动使用·不计现场真人"


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
    assert len(initial["active"]["content"]["rules"]["quality_gates"]) == 15
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
    # Web 默认流程:剧本 → 正式人物/核心道具四选一 → 预生产 → 开拍确认
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
    assert pre["story_analysis"]["schema"] == "aifos.story-analysis/v1"
    assert pre["story_analysis"]["prompt_bible"]["global_image_prefix"]
    assert pre["script"]["production_analysis"]["scenes"]
    assert pre["storyboard"] is None
    assert pre["artifacts"]["cast_art"] == []

    # 第一道确认(剧本 OK)→ 按角色重要度生成候选后停下。
    status, reply = _json_request(port, "POST", "/api/confirm", {
        "episode_id": episode_id})
    assert status == 202 and reply["phase"] == "awaiting_script"
    job = _wait_job(port, reply["job_id"])
    assert job["summary"]["status"] == "awaiting_cast"

    status, pre = _json_request(port, "GET", f"/api/episode/{episode_id}")
    assert pre["storyboard"] is None
    selection = pre["cast_selection"]
    assert not selection["passed"]
    assert all(len(c["candidates"]) == character_candidate_target(c)
               for c in selection["characters"])
    assert all(candidate["url"].startswith("/artifacts/")
               for c in selection["characters"]
               for candidate in c["candidates"])
    assert all(candidate["variant_source"] == "generated"
               for c in selection["characters"]
               for candidate in c["candidates"])
    assert all(candidate["variant_label"] and candidate["look_variant"]
               for c in selection["characters"]
               for candidate in c["candidates"])
    assert pre["artifacts"]["cast_art"] == []
    assert pre["artifacts"]["scene_art"] == []
    assert pre["character_asset_policy"]["mode"] == "auto"
    policy_version = pre["character_asset_policy_version"]
    status, missing_version = _json_request(
        port, "POST", "/api/character/assets-policy", {
            "episode_id": episode_id, "mode": "simple",
        })
    assert status == 400 and "expected_version" in missing_version["error"]
    status, null_version = _json_request(
        port, "POST", "/api/character/assets-policy", {
            "episode_id": episode_id, "mode": "simple",
            "expected_version": None,
        })
    assert status == 400 and "非负整数" in null_version["error"]
    status, saved_policy = _json_request(
        port, "POST", "/api/character/assets-policy", {
            "episode_id": episode_id, "mode": "simple",
            "expected_version": policy_version,
        })
    assert status == 200
    assert saved_policy["policy"]["resolved_mode"] == "simple"
    assert saved_policy["policy"]["generate_sheets"] is False
    status, stale = _json_request(
        port, "POST", "/api/character/assets-policy", {
            "episode_id": episode_id, "mode": "full",
            "expected_version": policy_version,
        })
    assert status == 409 and "刷新" in stale["error"]
    # 未完成四选一，后端也必须拒绝绕过门禁。
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
    assert all(len(c["candidates"]) == character_candidate_target(c)
               for c in selection["characters"])

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
    assert pre["character_asset_policy"]["resolved_mode"] == "simple"
    assert not [item for item in pre["render_plan"]["items"]
                if item["category"] == "character_sheet"]
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
    assert detail["video_qc_report"]["passed"]
    assert detail["video_qc_report"]["auto_retry_limit"] == 1
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
    assert status == 201 and imported["job_id"]
    first_job = _wait_job(port, imported["job_id"])
    assert first_job["status"] == "done"
    assert first_job["summary"]["status"] == "awaiting_script"
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
    assert status == 202 and advanced["job_id"]
    second_job = _wait_job(port, advanced["job_id"])
    assert second_job["status"] == "done"
    assert second_job["summary"]["status"] == "awaiting_script"
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
    assert view["defaults"]["parallel_videos"] == 4
    # 视频生产独立配置，默认 4 路，也可按账号限流自主调整。
    status, _ = _json_request(port, "POST", "/api/settings", {
        "defaults": {"parallel_videos": 6}})
    assert status == 200
    status, view = _json_request(port, "GET", "/api/settings")
    assert view["defaults"]["parallel_videos"] == 6
    # 非法值被拒
    status, _ = _json_request(port, "POST", "/api/settings", {
        "defaults": {"parallel_videos": "abc"}})
    assert status == 400


def test_codex_parallel_settings_and_shards_api(server):
    """三 Codex 配置可保存，分片快照始终返回可观测状态。"""
    port = server["port"]
    status, view = _json_request(port, "GET", "/api/settings")
    assert status == 200
    assert len(view["codex_profiles"]) == 3
    assert {item["id"] for item in view["codex_profiles"]} == {
        "codex_a", "codex_b", "codex_c"}
    assert view["codex_parallel"]["max_parallel"] == 3
    status, saved = _json_request(port, "POST", "/api/settings", {
        "codex_profiles": [
            {"id": "codex_a", "name": "A", "enabled": True,
             "codex_home": "/tmp/not-a-real-codex-home", "command": "codex"},
            {"id": "codex_b", "name": "B", "enabled": False,
             "codex_home": "~/.codex-account-b", "command": "codex"},
            {"id": "codex_c", "name": "C", "enabled": False,
             "codex_home": "~/.codex-account-c", "command": "codex"},
        ],
    })
    assert status == 200
    assert saved["codex_profiles"][0]["name"] == "A"
    status, shards = _json_request(
        port, "GET", "/api/image-production/shards")
    assert status == 200
    assert len(shards["shards"]) == 3
    assert {item["id"] for item in shards["shards"]} == {
        "codex_a", "codex_b", "codex_c"}


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


def test_lesson_approval_endpoint_toggles_prompt_injection(server):
    """审批接口:批准后教训才进提示词,撤销后立刻停止注入。"""
    from aifos.lessons import (DOMAIN_SCRIPT, project_lessons, record_lessons,
                               script_lessons_block)

    app = App(server["workspace"])
    try:
        project, _ = app.projects.get_or_create_project("审批接口")
        pid = project["id"]
        record_lessons(app.assets, pid, ["把旁白当成了人物写进人物表"],
                       category="non_person_speaker", domain=DOMAIN_SCRIPT)
        [row] = project_lessons(app.assets, pid)
    finally:
        app.close()

    status, reply = _json_request(
        server["port"], "POST", "/api/lessons/approve",
        {"project_id": pid, "lesson_id": row["id"], "approved": True})
    assert status == 200
    assert reply["approved_for_prompt"] is True
    assert reply["status"] == "approved"
    assert reply["domain"] == DOMAIN_SCRIPT

    app = App(server["workspace"])
    try:
        assert "旁白" in script_lessons_block(app.assets, pid)
    finally:
        app.close()

    status, reply = _json_request(
        server["port"], "POST", "/api/lessons/approve",
        {"project_id": pid, "lesson_id": row["id"], "approved": False})
    assert status == 200
    assert reply["approved_for_prompt"] is False

    app = App(server["workspace"])
    try:
        assert script_lessons_block(app.assets, pid) == ""
    finally:
        app.close()

    status, _ = _json_request(
        server["port"], "POST", "/api/lessons/approve",
        {"project_id": pid, "lesson_id": "missing", "approved": True})
    assert status == 404
    status, _ = _json_request(
        server["port"], "POST", "/api/lessons/approve", {"approved": True})
    assert status == 400
