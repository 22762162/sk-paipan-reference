"""关键帧四候选 Web 契约：CAS、选片续产、重生成与安全 URL。"""

import http.client
import json
import threading
import time

import pytest

from aifos.app import App
from aifos.director import Director
from aifos.errors import AifosError
from aifos.web.server import (
    PRODUCTION_ACTIONS,
    _candidate_group_current_selection,
    _sanitize_candidate_group_urls,
    serve,
)


@pytest.fixture()
def server(tmp_path):
    workspace = tmp_path / "ws"
    app = App(workspace)
    try:
        project, _ = app.projects.get_or_create_project("四候选剧")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        episode_id = int(episode["id"])
        project_id = int(project["id"])
    finally:
        app.close()
    httpd = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {
        "port": httpd.server_address[1],
        "workspace": workspace,
        "episode_id": episode_id,
        "project_id": project_id,
    }
    httpd.shutdown()
    httpd.server_close()


def _request(server, method, path, body=None):
    conn = http.client.HTTPConnection(
        "127.0.0.1", server["port"], timeout=30)
    encoded = None
    headers = {}
    if body is not None:
        encoded = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=encoded, headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    status = response.status
    conn.close()
    return status, payload


def _wait_job(server, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, job = _request(server, "GET", f"/api/jobs/{job_id}")
        if job["status"] not in {"running", "queued"}:
            return job
        time.sleep(0.02)
    raise TimeoutError(job_id)


def _cas_body(server, **updates):
    body = {
        "episode_id": server["episode_id"],
        "shot_no": 1,
        "candidate_set_id": "set-1",
        "candidate_set_token": "token-1",
        "contract_revision": 3,
        "candidate_revision": 7,
        "candidate_id": "token-1#2",
        "candidate_index": 2,
        "source": "ai",
    }
    body.update(updates)
    return body


def _candidate_group(root, *, token="token-1", revision=7,
                     selected=True, complete=True):
    candidates = []
    for index in range(1, 5):
        uri = root / f"candidate-{index}.png"
        uri.parent.mkdir(parents=True, exist_ok=True)
        uri.write_bytes(b"png")
        candidates.append({
            "candidate_id": f"{token}#{index}",
            "candidate_index": index,
            "candidate_set_token": token,
            "uri": str(uri),
        })
    group = {
        "schema": "aifos.shot-candidate-group/v1",
        "candidate_set_id": "set-1",
        "candidate_set_token": token,
        "contract_revision": 3,
        "candidate_revision": revision,
        "complete": complete,
        "technical_incomplete": not complete,
        "candidates": candidates,
    }
    if selected:
        group["selection"] = {
            "candidate_set_id": "set-1",
            "candidate_set_token": token,
            "candidate_revision": revision,
            "candidate_id": f"{token}#2",
            "candidate_index": 2,
            "selected_uri": candidates[1]["uri"],
            "source": "manual",
        }
    return group


def test_select_is_manual_idempotent_and_resumes_only_once(
        server, monkeypatch):
    calls = []
    produced = threading.Event()

    def select(_self, title, number, shot_no, expected_set_id,
               expected_token, expected_revision, candidate_id,
               candidate_index, source="manual"):
        calls.append((title, number, shot_no, expected_set_id,
                      expected_token, expected_revision, candidate_id,
                      candidate_index, source))
        duplicate = len(calls) > 1
        return {
            "status": "selected",
            "shot_no": shot_no,
            "candidate_set_id": expected_set_id,
            "candidate_set_token": expected_token,
            "candidate_revision": expected_revision,
            "candidate_id": candidate_id,
            "candidate_index": candidate_index,
            "selected_uri": "https://cdn.example/selected.png",
            "source": source,
            "asset_id": 9,
            "asset_version": 1,
            "already_selected": duplicate,
            "remaining": 0,
            "all_selected": True,
            "need_resume": True,
            "last_pending": not duplicate,
        }

    def produce(_self, title, number, **kwargs):
        produced.set()
        return {"status": "done", "title": title, "number": number}

    monkeypatch.setattr(Director, "select_shot_candidate", select,
                        raising=False)
    monkeypatch.setattr(Director, "produce", produce)

    status, first = _request(
        server, "POST", "/api/shot-candidates/select",
        _cas_body(server))
    assert status == 200
    assert first["source"] == "manual"
    assert first["selected_url"] == "https://cdn.example/selected.png"
    assert "selected_uri" not in first
    assert first["resume_job_id"]
    assert produced.wait(timeout=5)
    _wait_job(server, first["resume_job_id"])
    assert calls[0][-1] == "manual", "客户端 source=ai 不得伪造选择来源"

    status, second = _request(
        server, "POST", "/api/shot-candidates/select",
        _cas_body(server))
    assert status == 200
    assert second["already_selected"] is True
    assert second["resume_job_id"] is None
    _, jobs = _request(server, "GET", "/api/jobs")
    assert sum(job.get("action") == "image_selection_resume"
               for job in jobs) == 1
    assert "image_selection_resume" in PRODUCTION_ACTIONS
    assert "regenerate_shot_candidates" in PRODUCTION_ACTIONS


def test_selection_mode_rejects_legacy_single_shot_regen(
        server, monkeypatch):
    calls = []

    def legacy_regen(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "done"}

    monkeypatch.setattr(Director, "regen_image", legacy_regen)
    status, result = _request(server, "POST", "/api/regen_image", {
        "episode_id": server["episode_id"],
        "target": {"kind": "shot", "shot_no": 1},
        "prompt": "旧接口不应生成单张正式图",
    })

    assert status == 409
    assert "/api/shot-candidates/regenerate" in result["error"]
    assert "/api/shot-candidates/select" in result["error"]
    assert "整组重新生成4张" in result["error"]
    assert calls == []

    status, setting = _request(
        server, "POST", "/api/selection-mode", {"enabled": False})
    assert status == 200 and setting["selection_mode"] is False
    status, queued = _request(server, "POST", "/api/regen_image", {
        "episode_id": server["episode_id"],
        "target": {"kind": "shot", "shot_no": 1},
        "prompt": "关闭选片模式后保留旧接口兼容",
    })
    assert status == 202
    assert _wait_job(server, queued["job_id"])["status"] == "done"
    assert len(calls) == 1


@pytest.mark.parametrize("marker", [
    "stale_candidate_set: 请刷新", "selection_conflict: 已被另一选择覆盖",
])
def test_select_maps_cas_conflicts_to_409(server, monkeypatch, marker):
    def reject(*_args, **_kwargs):
        raise AifosError(marker)

    monkeypatch.setattr(
        Director, "select_shot_candidate", reject, raising=False)
    status, result = _request(
        server, "POST", "/api/shot-candidates/select",
        _cas_body(server))
    assert status == 409
    assert marker in result["error"]


def test_regenerate_requires_confirmation_full_cas_and_runs_in_queue(
        server, monkeypatch):
    regenerate_calls = []
    resume_calls = []

    def state(_self, _title, _number, shot_no=None):
        return {
            "status": "done", "total": 1, "selected": 0,
            "remaining": 1, "all_selected": False, "need_resume": False,
            "items": [{
                "shot_no": shot_no,
                "candidate_set_id": "set-1",
                "candidate_set_token": "token-1",
                "contract_revision": 3,
                "candidate_revision": 7,
            }],
        }

    def regenerate(_self, *args, **kwargs):
        regenerate_calls.append((args, kwargs))
        return {
            "status": "regenerating_candidates",
            "shot_no": 1,
            "candidate_set_id": "",
            "candidate_revision": 8,
            "contract_revision": 4,
            "history_count": 1,
            "remaining": 1,
            "all_selected": False,
            "need_resume": True,
        }

    def produce(_self, title, number, **kwargs):
        resume_calls.append((title, number, kwargs))
        return {"status": "awaiting_selection"}

    monkeypatch.setattr(
        Director, "shot_candidate_selection_state", state, raising=False)
    monkeypatch.setattr(
        Director, "regenerate_shot_candidates", regenerate, raising=False)
    monkeypatch.setattr(Director, "produce", produce)

    body = _cas_body(server, prompt="只保留本镜动作，重新生成四张",
                     confirm_regenerate=False)
    status, _ = _request(
        server, "POST", "/api/shot-candidates/regenerate", body)
    assert status == 400

    body["confirm_regenerate"] = True
    body.pop("contract_revision")
    status, _ = _request(
        server, "POST", "/api/shot-candidates/regenerate", body)
    assert status == 400

    body["contract_revision"] = 3
    status, queued = _request(
        server, "POST", "/api/shot-candidates/regenerate", body)
    assert status == 202
    job = _wait_job(server, queued["job_id"])
    assert job["status"] == "done"
    assert len(regenerate_calls) == 1
    args, kwargs = regenerate_calls[0]
    assert args[2:6] == (1, "set-1", "token-1", 7)
    assert args[6] == "只保留本镜动作，重新生成四张"
    assert kwargs == {"confirm": True}
    assert len(resume_calls) == 1

    stale = _cas_body(
        server, prompt="新提示词", confirm_regenerate=True,
        candidate_set_token="old-token")
    status, result = _request(
        server, "POST", "/api/shot-candidates/regenerate", stale)
    assert status == 409
    assert "stale_candidate_set" in result["error"]


def test_episode_candidate_urls_are_sanitized_and_progress_uses_selection(
        server):
    app = App(server["workspace"])
    try:
        episode = app.projects.get_episode(server["episode_id"])
        out_root = (app.workspace.artifacts_dir
                    / f"p{server['project_id']:03d}" / "e001")
        app.projects.save_document(server["episode_id"], "script", {
            "characters": [],
            "scenes": [{"scene_no": 1, "location": "书房"}],
        })
        app.projects.save_document(server["episode_id"], "storyboard", {
            "shots": [
                {"shot_no": value, "scene_no": 1, "characters": [],
                 "description": f"镜头{value}"}
                for value in range(1, 6)
            ],
        })
        current = _candidate_group(out_root / "candidate-set-1")
        stale = _candidate_group(
            out_root / "candidate-set-2", token="token-2", revision=8)
        stale["selection"]["candidate_set_token"] = "old-token"
        awaiting = _candidate_group(
            out_root / "candidate-set-3", token="token-3", revision=1,
            selected=False)
        technical = _candidate_group(
            out_root / "candidate-set-4", token="token-4", revision=1,
            selected=False, complete=False)
        regenerating = _candidate_group(
            out_root / "candidate-set-5", token="token-5", revision=2,
            selected=False, complete=False)
        for shot_no, group in ((1, current), (2, stale)):
            selected = group["candidates"][1]["uri"]
            app.assets.register(
                server["project_id"], "image",
                f"e001_shot{shot_no:03d}", uri=selected)
        app.director._plan_write({"out_root": out_root}, {"items": [
            {"id": "shot:1", "category": "shot_image", "shot_no": 1,
             "status": "done", "candidate_group": current,
             "candidate_uris": [row["uri"] for row in current["candidates"]],
             "qc": {"passed": False, "issues": ["建议性构图问题"]}},
            {"id": "shot:2", "category": "shot_image", "shot_no": 2,
             "status": "done", "candidate_group": stale,
             "qc": {"passed": True}},
            {"id": "shot:3", "category": "shot_image", "shot_no": 3,
             "status": "awaiting_selection", "candidate_group": awaiting},
            {"id": "shot:4", "category": "shot_image", "shot_no": 4,
             "status": "technical_incomplete", "candidate_group": technical},
            {"id": "shot:5", "category": "shot_image", "shot_no": 5,
             "status": "regenerating_candidates",
             "candidate_group": regenerating},
        ]})
        # 让 regenerating_candidates 保持实时状态，而不是被视作陈旧认领。
        app.history.create_run(
            "四候选剧", 1, action="regenerate_shot_candidates",
            request={"shot_no": 5}, source="test")
    finally:
        app.close()

    status, detail = _request(
        server, "GET", f"/api/episode/{server['episode_id']}")
    assert status == 200
    items = detail["render_plan"]["items"]
    first = next(item for item in items if item["shot_no"] == 1)
    assert all("uri" not in row
               for row in first["candidate_group"]["candidates"])
    assert all(str(row.get("url") or "").startswith("/artifacts/")
               for row in first["candidate_group"]["candidates"])
    assert "selected_uri" not in first["candidate_group"]["selection"]
    assert first["candidate_group"]["selection"][
        "selected_url"].startswith("/artifacts/")
    assert "candidate_uris" not in first
    assert len(first["candidate_urls"]) == 4

    shots = next(
        row for row in detail["production_progress"]["categories"]
        if row["category"] == "shot_image")
    assert shots["total"] == 5
    assert shots["usable"] == 1, (
        "当前 token 的明确选择+正式资产才可用；旧 token 即使 QC PASS 也无效")
    assert shots["awaiting_selection"] == 1
    assert shots["technical_incomplete"] == 1
    assert shots["regenerating_candidates"] == 1
    assert shots["awaiting_review"] >= 1
    assert shots["needs_repair"] >= 1


def test_candidate_selection_and_sanitizer_accept_url_only_payload(server):
    """刷新后的浏览器数据只有 URL，也必须保持选择态，不能误判为未选。"""
    app = App(server["workspace"])
    try:
        remote = "https://cdn.example/candidate-2.png"
        candidates = [{
            "candidate_id": f"token-url#{index}",
            "candidate_index": index,
            "candidate_set_token": "token-url",
            "url": (remote if index == 2
                    else f"https://cdn.example/candidate-{index}.png"),
        } for index in range(1, 5)]
        item = {"candidate_group": {
            "complete": True,
            "technical_incomplete": False,
            "candidate_set_id": "set-url",
            "candidate_set_token": "token-url",
            "candidate_revision": 1,
            "candidates": candidates,
            "selection": {
                "candidate_set_id": "set-url",
                "candidate_set_token": "token-url",
                "candidate_revision": 1,
                "candidate_id": "token-url#2",
                "candidate_index": 2,
                "selected_url": remote,
                "source": "manual",
            },
        }}

        selection = _candidate_group_current_selection(item)
        assert selection is not None
        _sanitize_candidate_group_urls(app, item)
        assert item["candidate_group"]["selection"]["selected_url"] == remote
        assert item["candidate_group"]["candidates"][1]["url"] == remote
    finally:
        app.close()
