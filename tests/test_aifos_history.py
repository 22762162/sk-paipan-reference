"""生产历史中心：跨重启持久化、旧记录回填与幽灵任务恢复。"""

import os
import socket
import threading
import time

from aifos.app import App
from aifos.db import now
from aifos.job_center import JobCenter
from aifos.web.server import JobRegistry


def test_run_history_persists_and_exposes_stage_detail(tmp_path):
    ws = tmp_path / "ws"
    app = App(ws)
    run_id = app.history.create_run(
        "历史测试剧", 3, action="produce", request={"review": True})
    project, _ = app.projects.get_or_create_project("历史测试剧")
    episode, _ = app.projects.get_or_create_episode(project["id"], 3)
    stamp = now()
    app.db.execute(
        "INSERT INTO tasks(episode_id, run_id, stage, name, status, "
        "provider, cost, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (episode["id"], run_id, "script", "剧本", "done", "mock", 0.2,
         stamp, stamp))
    app.history.finish_run(run_id, summary={
        "project": "历史测试剧", "episode": 3,
        "status": "awaiting_script",
        "stages": [{"stage": "script", "status": "done", "cost": 0.2,
                    "providers": ["mock"]}],
    })
    app.close()

    reopened = App(ws)
    listing = reopened.history.list()
    assert listing["stats"]["total"] == 1
    assert listing["stats"]["paused"] == 1
    assert listing["items"][0]["action"] == "produce"
    assert listing["items"][0]["providers"] == ["mock"]
    detail = reopened.history.get(run_id)
    assert detail["summary"]["status"] == "awaiting_script"
    assert detail["tasks"][0]["stage"] == "script"
    assert detail["episode_id"] == episode["id"]
    reopened.close()


def test_bootstrap_recovers_stale_episode_and_backfills_history(tmp_path):
    ws = tmp_path / "ws"
    app = App(ws)
    project, _ = app.projects.get_or_create_project("失联任务")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    app.projects.save_document(episode["id"], "script", {"scenes": []})
    app.projects.set_episode_status(episode["id"], "cancelling")
    stamp = now() - JobRegistry.LEASE_SECONDS - 1
    app.db.execute(
        "INSERT INTO tasks(episode_id, stage, name, status, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?)",
        (episode["id"], "storyboard", "五维分镜", "running", stamp,
         stamp))
    app.close()

    # 新 Web 进程没有旧线程，启动时必须恢复为可继续状态并留下历史。
    JobRegistry(ws)
    recovered = App(ws)
    episode_row = recovered.projects.get_episode(episode["id"])
    task = recovered.db.query_one(
        "SELECT * FROM tasks WHERE episode_id=?", (episode["id"],))
    history = recovered.history.list()
    assert episode_row["status"] == "paused"
    assert task["status"] == "interrupted"
    assert history["stats"]["total"] == 1
    assert history["items"][0]["status"] == "interrupted"
    assert history["items"][0]["action"] == "legacy_import"
    recovered.close()


def test_job_registry_hydrates_durable_completed_job_after_restart(tmp_path):
    ws = tmp_path / "ws"
    seed = App(ws)
    project, _ = seed.projects.get_or_create_project("持久任务剧")
    seed.projects.get_or_create_episode(project["id"], 1)
    seed.close()
    registry = JobRegistry(ws)
    job_id = registry.start_task(
        "持久任务剧", 1,
        lambda _app, _run_id: {"status": "done", "shot_no": 2},
        action="regen_image", request={"shot_no": 2})
    for _ in range(200):
        if registry.get(job_id)["status"] == "done":
            break
        time.sleep(0.01)
    assert registry.get(job_id)["status"] == "done"

    reopened = JobRegistry(ws)
    restored = reopened.get(job_id)
    assert restored["status"] == "done"
    assert restored["title"] == "持久任务剧"
    assert restored["episode"] == 1
    assert restored["summary"]["shot_no"] == 2

    center = JobCenter(registry._ledger_db)
    durable = center.get(job_id)
    assert durable["episode_id"] is not None
    assert any(link["relation_type"] == "episode"
               for link in durable["links"])

    app = App(ws)
    try:
        run = app.history.get(restored["run_id"])
        assert run["jobs"][0]["id"] == job_id
        assert any(link["relation_type"] == "shot"
                   for link in run["jobs"][0]["links"])
    finally:
        app.close()


def test_job_registry_maps_returned_failure_and_checkpoint_summaries(tmp_path):
    """正常 return 不代表成功；业务失败/检查点不能被外层统一写成 done。"""
    ws = tmp_path / "ws"
    seed = App(ws)
    project, _ = seed.projects.get_or_create_project("作业终态剧")
    seed.projects.get_or_create_episode(project["id"], 1)
    seed.close()
    registry = JobRegistry(ws)

    failed_id = registry.start_task(
        "作业终态剧", 1,
        lambda _app, _run_id: {
            "status": "failed",
            "stages": [{
                "stage": "preflight", "status": "failed",
                "error": "空间调度指纹不一致",
            }],
        }, action="produce")
    for _ in range(200):
        if registry.get(failed_id)["status"] == "failed":
            break
        time.sleep(0.01)
    failed = registry.get(failed_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "空间调度指纹不一致"
    assert registry._job_center.get(failed_id)["status"] == "failed"
    assert registry._history_center.get(failed["run_id"])["status"] == "failed"

    checkpoint_id = registry.start_task(
        "作业终态剧", 1,
        lambda _app, _run_id: {"status": "awaiting_confirm"},
        action="produce")
    for _ in range(200):
        if registry.get(checkpoint_id)["status"] != "running":
            break
        time.sleep(0.01)
    assert registry.get(checkpoint_id)["status"] == "done"
    assert registry.get(checkpoint_id)["outcome_status"] == "awaiting_confirm"
    # Ledger 使用其受支持的成功终态，但 Web hydration 必须恢复业务检查点。
    assert registry._job_center.get(checkpoint_id)["status"] == "done"
    reopened = JobRegistry(ws)
    assert reopened.get(checkpoint_id)["status"] == "done"
    assert reopened.get(checkpoint_id)["outcome_status"] == "awaiting_confirm"
    assert reopened._history_center.get(
        registry.get(checkpoint_id)["run_id"])["status"] == "paused"
    registry._lease_stop.set()
    reopened._lease_stop.set()


def test_hydrates_legacy_done_job_whose_summary_says_failed(tmp_path):
    """升级前 ledger=done/summary=failed 的记录对外必须恢复为失败。"""
    ws = tmp_path / "ws"
    app = App(ws)
    try:
        project, _ = app.projects.get_or_create_project("旧假完成任务剧")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        run_id = app.history.create_run(
            "旧假完成任务剧", 1, action="produce")
        center = JobCenter(app.db)
        center.create(
            "j1", run_id=run_id, episode_id=episode["id"],
            action="produce", status="running",
            request={"title": "旧假完成任务剧", "episode": 1})
        center.set_state("j1", "done", result={
            "status": "failed",
            "stages": [{
                "stage": "preflight", "status": "failed",
                "error": "生产门禁未通过：空间调度",
            }],
        })
    finally:
        app.close()

    registry = JobRegistry(ws)
    restored = registry.get("j1")
    assert restored["status"] == "failed"
    assert restored["outcome_status"] == "failed"
    assert restored["error"] == "生产门禁未通过：空间调度"
    assert restored["summary"]["status"] == "failed"
    # 这是只读兼容映射；不篡改旧 ledger，后续历史审计仍能看见原事实。
    assert registry._job_center.get("j1")["status"] == "done"
    registry._lease_stop.set()


def test_job_registry_keeps_interrupted_job_visible_without_replaying(tmp_path):
    ws = tmp_path / "ws"
    app = App(ws)
    try:
        run_id = app.history.create_run("重启可见剧", 1)
        center = JobCenter(
            app.db, owner_id="dead-web-owner", owner_pid=2_147_483_647,
            owner_host=socket.gethostname())
        center.create(
            "j7", run_id=run_id, action="produce", status="running",
            request={"title": "重启可见剧", "episode": 1})
    finally:
        app.close()

    registry = JobRegistry(ws)
    restored = registry.get("j7")
    assert restored["status"] == "interrupted"
    assert restored["recoverable"] is True
    assert registry.running_for("重启可见剧", 1) == []


def test_second_registry_preserves_live_first_registry_job(tmp_path):
    ws = tmp_path / "ws"
    first = JobRegistry(ws)
    run_id = first._create_history("滚动重启剧", 1, "produce")
    first._seq += 1
    job_id = f"j{first._seq}"
    job = {
        "id": job_id, "status": "running", "title": "滚动重启剧",
        "episode": 1, "action": "produce", "started_at": now(),
        "run_id": run_id,
    }
    first._jobs[job_id] = job
    first._persist_job_create(
        job, first._job_request("滚动重启剧", 1))

    second = JobRegistry(ws)

    assert first._job_center.get(job_id)["status"] == "running"
    assert second._history_center.get(run_id)["status"] == "running"
    assert second.get(job_id)["status"] == "running"
    assert second.get(job_id)["recoverable"] is False
    assert second.running_for("滚动重启剧", 1)

    first._persist_job_state(job_id, "done", result={"status": "done"})
    assert first._job_center.get(job_id)["status"] == "done"


def test_registry_periodically_retires_stale_legacy_unowned_job(
        tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    app = App(ws)
    try:
        project, _ = app.projects.get_or_create_project("旧任务租约剧")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.set_episode_status(episode["id"], "producing")
        run_id = app.history.create_run("旧任务租约剧", 1)
        stamp = now()
        app.db.execute(
            "INSERT INTO tasks(episode_id, run_id, stage, name, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (episode["id"], run_id, "images", "旧版任务", "running",
             stamp, stamp))
        JobCenter(app.db).create(
            "j8", run_id=run_id, action="produce", status="running",
            request={"title": "旧任务租约剧", "episode": 1})
    finally:
        app.close()

    monkeypatch.setattr(JobRegistry, "HEARTBEAT_SECONDS", 0.01)
    registry = JobRegistry(ws)
    assert registry.get("j8")["status"] == "running"

    registry._ledger_db.execute(
        "UPDATE production_jobs SET updated_at=? WHERE id='j8'",
        (now() - registry.LEASE_SECONDS - 1,))
    for _ in range(200):
        if registry.get("j8")["status"] == "interrupted":
            break
        time.sleep(0.01)

    restored = registry.get("j8")
    assert restored["status"] == "interrupted"
    assert restored["recoverable"] is True
    assert registry.running_for("旧任务租约剧", 1) == []
    run = registry._history_center.get(run_id)
    task = registry._ledger_db.query_one(
        "SELECT status FROM tasks WHERE run_id=?", (run_id,))
    episode_row = registry._ledger_db.query_one(
        "SELECT status FROM episodes WHERE id=?", (episode["id"],))
    assert run["status"] == "interrupted"
    assert task["status"] == "interrupted"
    assert episode_row["status"] == "paused"
    registry._lease_stop.set()


def test_stale_running_job_releases_and_drains_same_episode_queue(
        tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    app = App(ws)
    try:
        project, _ = app.projects.get_or_create_project("租约排队剧")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        app.projects.set_episode_status(episode["id"], "producing")
        stale_run = app.history.create_run("租约排队剧", 1)
        stamp = now()
        app.db.execute(
            "INSERT INTO tasks(episode_id, run_id, stage, name, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (episode["id"], stale_run, "images", "失联任务", "running",
             stamp, stamp))
        foreign = JobCenter(
            app.db, owner_id="foreign-live", owner_pid=os.getpid(),
            owner_host=socket.gethostname(), lease_seconds=60)
        foreign.create(
            "j7", run_id=stale_run, episode_id=episode["id"],
            action="regen_image", status="running",
            request={"title": "租约排队剧", "episode": 1})
    finally:
        app.close()

    monkeypatch.setattr(JobRegistry, "HEARTBEAT_SECONDS", 0.01)
    registry = JobRegistry(ws)
    executed = threading.Event()
    queued_id = registry.start_task(
        "租约排队剧", 1,
        lambda _app, _run_id: executed.set() or {"status": "done"},
        action="regen_image", queue=True)
    assert registry.get(queued_id)["status"] == "queued"

    registry._ledger_db.execute(
        "UPDATE production_jobs SET lease_expires_at=? WHERE id='j7'",
        (now() - 1,))
    for _ in range(300):
        if registry.get(queued_id)["status"] == "done":
            break
        time.sleep(0.01)

    assert executed.is_set()
    assert registry.get("j7")["status"] == "interrupted"
    assert registry.get(queued_id)["status"] == "done"
    assert registry.queued_for("租约排队剧", 1) == []
    assert registry._history_center.get(stale_run)["status"] == "interrupted"
    task = registry._ledger_db.query_one(
        "SELECT status FROM tasks WHERE run_id=?", (stale_run,))
    assert task["status"] == "interrupted"
    registry._lease_stop.set()


def test_registry_cancel_intent_is_durable_without_releasing_live_slot(
        tmp_path):
    ws = tmp_path / "ws"
    registry = JobRegistry(ws)
    run_id = registry._create_history("取消持久化剧", 1, "produce")
    registry._seq += 1
    job_id = f"j{registry._seq}"
    job = {
        "id": job_id, "status": "running", "title": "取消持久化剧",
        "episode": 1, "action": "produce", "started_at": now(),
        "run_id": run_id,
    }
    registry._jobs[job_id] = job
    registry._persist_job_create(
        job, registry._job_request("取消持久化剧", 1))

    registry.request_cancel(job_id)

    assert registry.get(job_id)["status"] == "running"
    assert registry.get(job_id)["cancel_requested"] is True
    assert registry.running_for("取消持久化剧", 1)
    durable = registry._job_center.get(job_id)
    assert durable["status"] == "cancelling"
    assert durable["cancel_requested"] is True


def test_queued_cancel_is_removed_and_never_executes(tmp_path):
    ws = tmp_path / "ws"
    seed = App(ws)
    project, _ = seed.projects.get_or_create_project("取消排队剧")
    seed.projects.get_or_create_episode(project["id"], 1)
    seed.close()
    registry = JobRegistry(ws)
    release = threading.Event()
    first_started = threading.Event()
    second_executed = threading.Event()

    def first_task(_app, _run_id):
        first_started.set()
        release.wait(2)
        return {"status": "done"}

    first_id = registry.start_task(
        "取消排队剧", 1, first_task, action="regen_image", queue=True)
    assert first_started.wait(1)
    second_id = registry.start_task(
        "取消排队剧", 1,
        lambda _app, _run_id: second_executed.set()
        or {"status": "done"},
        action="regen_image", queue=True)
    assert registry.get(second_id)["status"] == "queued"

    cancelled = registry.request_cancel(second_id, "不再重画")
    release.set()
    for _ in range(200):
        if registry.get(first_id)["status"] == "done":
            break
        time.sleep(0.01)
    time.sleep(0.05)

    assert cancelled["status"] == "cancelled"
    assert registry.get(second_id)["status"] == "cancelled"
    assert not second_executed.is_set()
    assert registry.queued_for("取消排队剧", 1) == []
    durable = registry._job_center.get(second_id)
    assert durable["status"] == "cancelled"
    assert durable["lease_expires_at"] is None
    history = registry._history_center.get(durable["run_id"])
    assert history["status"] == "cancelled"
    registry._lease_stop.set()
