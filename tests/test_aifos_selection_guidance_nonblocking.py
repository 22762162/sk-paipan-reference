"""选优模式的 Web 进度不得把内容观察重新翻译成人工阻断。"""

import http.client
import json
import threading

import pytest

from aifos.app import App
from aifos.db import now
from aifos.settings import set_defaults
from aifos.web.server import serve


@pytest.fixture()
def server_factory(tmp_path):
    servers = []

    def start(*, selection_mode, legacy_only=False,
              legacy_status="awaiting_human", failed_stage="",
              failed_action="produce"):
        workspace = tmp_path / f"ws-{len(servers)}"
        app = App(workspace)
        try:
            project, _ = app.projects.get_or_create_project("选优状态测试")
            episode, _ = app.projects.get_or_create_episode(project["id"], 1)
            shots = [
                {"shot_no": 1, "scene_no": 1, "description": "镜头1"},
            ]
            if not legacy_only:
                shots.append({
                    "shot_no": 2, "scene_no": 1,
                    "description": "镜头2",
                })
            app.projects.save_document(
                episode["id"], "storyboard", {"shots": shots})
            out_root = (
                app.workspace.artifacts_dir / f"p{project['id']:03d}"
                / "e001")
            image_dir = out_root / "images"
            image_dir.mkdir(parents=True, exist_ok=True)

            legacy_uri = image_dir / "legacy.png"
            legacy_uri.write_bytes(b"legacy-image")
            legacy_shot = 1 if legacy_only else 2
            app.assets.register(
                project["id"], "image",
                f"e001_shot{legacy_shot:03d}", uri=str(legacy_uri))
            legacy = {
                "id": f"shot:{legacy_shot}",
                "category": "shot_image",
                "shot_no": legacy_shot,
                "label": f"镜头 {legacy_shot:02d}",
                "status": legacy_status,
                # 兼容 API 已净化/旧计划只留下浏览器 URL 的状态；它同样
                # 代表已有技术产物，不能被误标成 technical_incomplete。
                ("output_url" if selection_mode else "output_uri"): (
                    "/artifacts/legacy.png"
                    if selection_mode else str(legacy_uri)),
                "qc": {
                    "passed": False,
                    "hard_failure": True,
                    "awaiting_human": True,
                    "issues": ["旧内容判词未过"],
                },
            }
            items = [legacy]

            if not legacy_only:
                selected_uri = image_dir / "best-effort.png"
                selected_uri.write_bytes(b"best-effort-image")
                candidate_uri = image_dir / "candidate-1.png"
                candidate_uri.write_bytes(b"candidate-image")
                token = "candidate-set-token"
                app.assets.register(
                    project["id"], "image", "e001_shot001",
                    uri=str(selected_uri))
                items.insert(0, {
                    "id": "shot:1",
                    "category": "shot_image",
                    "shot_no": 1,
                    "label": "镜头 01",
                    "status": "done",
                    # 故意不放 output_uri：正式资产中心存在时，旧清单漏字段
                    # 也不能把 AI 已晋升稿误判为技术未完成。
                    "candidate_group": {
                        "complete": True,
                        "technical_incomplete": False,
                        "candidate_set_id": "set-1",
                        "candidate_set_token": token,
                        "candidate_revision": 1,
                        "candidates": [{
                            "candidate_id": f"{token}#1",
                            "candidate_index": 1,
                            "candidate_set_token": token,
                            "url": "/artifacts/candidate-1.png",
                        }],
                        "selection": {
                            "candidate_set_id": "set-1",
                            "candidate_set_token": token,
                            "candidate_revision": 1,
                            "candidate_id": f"{token}#1",
                            "candidate_index": 1,
                            "selected_url": "/artifacts/candidate-1.png",
                            "source": "ai",
                            "best_effort_risk": True,
                        },
                    },
                    "qc": {
                        "passed": False,
                        "hard_failure": True,
                        "best_effort_promoted": True,
                        "issues": ["内容风险仅记录"],
                    },
                })
            app.director._plan_write(
                {"out_root": out_root}, {"items": items})
            if failed_stage:
                run_id = app.history.create_run(
                    "选优状态测试", 1, action=failed_action)
                stamp = now()
                reason = "生产门禁未通过：空间调度指纹与当前分镜不一致"
                app.db.execute(
                    "INSERT INTO tasks(episode_id, run_id, stage, name, "
                    "status, error, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (episode["id"], run_id, failed_stage, "生产门禁",
                     "failed", reason, stamp, stamp))
                app.history.finish_run(run_id, summary={
                    "project": "选优状态测试", "episode": 1,
                    "status": "failed",
                    "stages": [{
                        "stage": failed_stage, "status": "failed",
                        "error": reason,
                    }],
                })
                app.projects.set_episode_status(episode["id"], "failed")
            episode_id = int(episode["id"])
        finally:
            app.close()

        set_defaults(workspace / "config.json", {
            "selection_mode": selection_mode,
        })
        httpd = serve(workspace, host="127.0.0.1", port=0)
        thread = threading.Thread(
            target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        return httpd.server_address[1], episode_id

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _episode(port, episode_id):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", f"/api/episode/{episode_id}")
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    assert response.status == 200
    return payload


def test_selection_mode_promotes_best_effort_and_auto_takes_legacy_failure(
        server_factory):
    port, episode_id = server_factory(
        selection_mode=True, legacy_only=False)

    detail = _episode(port, episode_id)
    items = {
        int(item["shot_no"]): item
        for item in detail["render_plan"]["items"]
    }
    assert items[1]["status"] == "done"
    assert items[1]["nonblocking_risk"] is True
    assert items[2]["status"] == "pending"
    assert items[2]["qc"]["awaiting_human"] is False
    assert items[2]["qc"]["blocking"] is False
    assert items[2]["automatic_repair"] == {
        "owner": "system",
        "strategy": "codex_optimize_prompt_refs_then_generate_4",
        "candidate_count": 4,
        "max_candidate_rounds": 10,
        "first_round_included": True,
        "requires_human": False,
        "label": (
            "Codex自动归因并优化提示词与参考图；每轮生成4张、"
            "AI选优复检，最多10轮"),
    }
    assert detail["image_failures"] == []

    shots = next(
        stage for stage in detail["production_progress"]["categories"]
        if stage["category"] == "shot_image")
    assert shots["usable"] == 1
    assert shots["pending"] == 1
    assert shots["awaiting_human"] == 0
    assert shots["failed"] == 0

    guidance = detail["production_guidance"]
    assert guidance["state"] == "paused"
    assert guidance["phase"] == "keyframes"
    assert guidance["issues"] == []
    assert [row["code"] for row in guidance["blockers"]] == [
        "keyframes_pending"]
    assert "Codex 自动归因并优化提示词与参考图" in (
        guidance["blockers"][0]["message"])
    assert "每轮并行生成4张" in guidance["blockers"][0]["message"]
    assert "最多10轮" in guidance["blockers"][0]["message"]
    assert guidance["next_action"] == {
        "action": "resume_keyframes",
        "label": "自动四抽选优并复检（最多10轮 · 1镜）",
        "count": 1,
    }
    resolve = guidance["actions"]["resolve_image_issues"]
    assert resolve["enabled"] is False
    assert resolve["count"] == 0
    assert resolve["severity"] == "none"
    assert resolve["items"] == []
    visible_text = json.dumps(guidance, ensure_ascii=False)
    assert "二次质检失败" not in visible_text
    assert "需人工处理" not in visible_text
    assert '"severity": "must_fix"' not in visible_text


def test_selection_mode_api_reports_effective_nonblocking_qc_policy(
        server_factory):
    port, _ = server_factory(selection_mode=True, legacy_only=True)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/api/selection-mode")
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()

    assert response.status == 200
    assert payload["image_content_qc"] is True
    assert payload["effective_image_content_qc"] is True
    assert payload["effective_video_content_qc"] is True
    assert payload["content_qc_blocking"] is False
    assert payload["content_qc_auto_retry"] is True
    assert payload["codex_repair_enabled"] is True
    assert payload["reference_reselection_enabled"] is True
    assert payload["shot_candidate_count"] == 4
    assert payload["shot_repair_candidate_count"] == 4
    assert payload["max_candidate_rounds"] == 10
    assert payload["first_round_included"] is True
    assert payload["failure_blocks_other_shots"] is False
    assert payload["failure_blocks_downstream_stage"] is False
    assert payload["limit_behavior"] == (
        "promote_best_with_nonblocking_risk")


def test_old_reused_formal_keyframe_without_candidate_selection_stays_usable(
        server_factory):
    """四抽上线前的正式复用图没有 selection，也不能整集倒退待重抽。"""
    port, episode_id = server_factory(
        selection_mode=True, legacy_only=True, legacy_status="reused")

    detail = _episode(port, episode_id)
    item = detail["render_plan"]["items"][0]
    assert item["status"] == "reused"
    assert item["legacy_formal_reuse"] is True
    shots = next(
        stage for stage in detail["production_progress"]["categories"]
        if stage["category"] == "shot_image")
    assert shots["usable"] == 1
    assert shots["pending"] == 0
    guidance = detail["production_guidance"]
    assert guidance["stages"]["keyframes"]["usable"] == 1
    assert guidance["stages"]["keyframes"]["remaining"] == 0
    assert all(row["code"] != "keyframes_pending"
               for row in guidance["blockers"])


def test_latest_failed_run_overrides_stale_candidate_guidance(server_factory):
    port, episode_id = server_factory(
        selection_mode=True, legacy_only=True,
        legacy_status="awaiting_human", failed_stage="preflight")

    detail = _episode(port, episode_id)
    guidance = detail["production_guidance"]
    assert guidance["state"] == "failed"
    assert guidance["phase"] == "preflight"
    assert guidance["headline"] == "上次生产在「生产门禁」失败"
    assert guidance["reason"] == (
        "生产门禁未通过：空间调度指纹与当前分镜不一致")
    assert guidance["failure"]["stage"] == "preflight"
    assert guidance["next_action"] == {
        "action": "resume_from_checkpoint",
        "label": "从断点自动修复并继续",
        "count": 1,
    }
    assert guidance["actions"]["recovery"]["enabled"] is True
    assert guidance["actions"]["pending_images"]["enabled"] is False
    assert guidance["actions"]["resolve_image_issues"]["enabled"] is False
    assert guidance["blockers"][0]["code"] == "latest_run_failed"
    assert guidance["can_start_frames"] is False
    assert guidance["can_confirm_seedance"] is False


def test_failed_adjustment_does_not_masquerade_as_pipeline_failure(
        server_factory):
    port, episode_id = server_factory(
        selection_mode=True, legacy_only=True,
        legacy_status="awaiting_human", failed_stage="qc",
        failed_action="qc_all")

    guidance = _episode(port, episode_id)["production_guidance"]

    assert guidance["failure"] is None
    assert guidance["state"] != "failed"
    assert guidance["actions"]["recovery"]["enabled"] is False


def test_strict_mode_keeps_legacy_manual_qc_gate(server_factory):
    port, episode_id = server_factory(
        selection_mode=False, legacy_only=True)

    detail = _episode(port, episode_id)
    item = detail["render_plan"]["items"][0]
    assert item["status"] == "awaiting_human"
    assert len(detail["image_failures"]) == 1
    shots = detail["production_guidance"]["stages"]["keyframes"]
    assert shots["awaiting_human"] == 1
    issue = detail["production_guidance"]["issues"][0]
    assert issue["severity"] == "must_fix"
    blocker = detail["production_guidance"]["blockers"][0]
    assert blocker["code"] == "keyframes_awaiting_human"
    assert "二次质检未通过，需人工处理" in blocker["message"]
