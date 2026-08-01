"""选优模式的 Web 进度不得把内容观察重新翻译成人工阻断。"""

import http.client
import json
import threading

import pytest

from aifos.app import App
from aifos.settings import set_defaults
from aifos.web.server import serve


@pytest.fixture()
def server_factory(tmp_path):
    servers = []

    def start(*, selection_mode, legacy_only=False):
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
                "status": "awaiting_human",
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
        "strategy": "optimize_prompt_then_generate_3",
        "candidate_count": 3,
        "requires_human": False,
        "label": "系统自动优化提示词并补抽3张，由AI选优",
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
    assert "系统自动优化提示词" in guidance["blockers"][0]["message"]
    assert guidance["next_action"] == {
        "action": "resume_keyframes",
        "label": "系统自动补抽3张并AI选优（1镜）",
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
