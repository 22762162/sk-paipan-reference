"""Persistent job ledger: idempotency, state, restart and output links."""

import os

import pytest

from aifos.db import Database, now
from aifos.errors import AifosError
from aifos.job_center import JobCenter


def _seed(db, *, title="持久任务测试剧", episode_number=1):
    stamp = now()
    project = db.execute(
        "INSERT INTO projects(title, created_at) VALUES(?,?)",
        (title, stamp),
    ).lastrowid
    episode = db.execute(
        "INSERT INTO episodes(project_id, number, created_at, updated_at) "
        "VALUES(?,?,?,?)", (project, episode_number, stamp, stamp),
    ).lastrowid
    run = db.execute(
        "INSERT INTO production_runs(episode_id, project_title, "
        "episode_number, action, status, started_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (episode, title, episode_number, "produce", "running", stamp, stamp),
    ).lastrowid
    return project, episode, run


@pytest.fixture()
def ledger(tmp_path):
    db = Database(tmp_path / "aifos.db")
    center = JobCenter(db)
    yield db, center
    db.close()


def test_create_is_idempotent_by_job_run_and_key_and_links_outputs(ledger):
    db, center = ledger
    _project, episode, run = _seed(db)

    created = center.create(
        "job-fixed", run_id=run, episode_id=episode,
        idempotency_key="produce:episode:1", action="produce",
        status="queued", queue_key=f"episode:{episode}", queue_position=2,
        request={"force": False}, progress={"completed": 0},
        links=[
            {"relation_type": "shot", "relation_id": "shot:2"},
            {"relation_type": "artifact", "relation_id": "asset:9",
             "role": "reference"},
            {"relation_type": "output", "relation_id": "candidate:2:1",
             "uri": "/tmp/candidate.png", "meta": {"round": 1}},
        ],
    )

    assert created["id"] == "job-fixed"
    assert created["request"] == {"force": False}
    assert created["progress"] == {"completed": 0}
    assert created["queue_position"] == 2
    assert {(link["relation_type"], link["relation_id"])
            for link in created["links"]} == {
                ("episode", str(episode)),
                ("shot", "shot:2"),
                ("artifact", "asset:9"),
                ("output", "candidate:2:1"),
            }

    # A retry may have a newly allocated transient Web id, but the durable run
    # id and idempotency key must return the already-created job.
    retried = center.create(
        "different-transient-id", run_id=run, episode_id=episode,
        idempotency_key="produce:episode:1", action="produce")
    assert retried["id"] == "job-fixed"
    assert db.query_one("SELECT COUNT(*) AS n FROM production_jobs")["n"] == 1

    patched = center.upsert(
        idempotency_key="produce:episode:1",
        progress={"completed": 3}, result={"candidate_count": 4})
    assert patched["id"] == "job-fixed"
    assert patched["progress"] == {"completed": 3}
    assert patched["result"] == {"candidate_count": 4}

    updated_link = center.upsert_link(
        "job-fixed", "output", "candidate:2:1",
        uri="/tmp/revised.png", meta={"round": 2})
    assert updated_link["uri"] == "/tmp/revised.png"
    assert updated_link["meta"] == {"round": 2}
    assert len(center.links("job-fixed", "output")) == 1


def test_json_parsing_validation_state_queue_and_cancellation(ledger):
    db, center = ledger
    _project, episode, _run = _seed(db)
    job = center.create(
        "job-state", episode_id=episode, status="queued", queue_position=1)

    assert center.queue_position(job["id"]) == 1
    center.progress(job["id"], {"completed": 1, "total": 4})
    assert center.progress(job["id"]) == {"completed": 1, "total": 4}

    running = center.state(
        job["id"], "running", expected_status="queued")
    assert running["started_at"] is not None
    assert running["queue_position"] is None
    assert center.state(job["id"]) == "running"
    with pytest.raises(AifosError, match="只有 queued"):
        center.set_queue_position(job["id"], 3)

    cancelling = center.request_cancel(job["id"], "operator stop")
    assert cancelling["status"] == "cancelling"
    assert cancelling["cancel_reason"] == "operator stop"
    assert center.cancel_requested(job["id"]) is True
    cancelled = center.state(job["id"], "cancelled")
    assert cancelled["finished_at"] is not None
    with pytest.raises(AifosError, match="不允许任务状态"):
        center.state(job["id"], "running")

    queued = center.create("job-queued-cancel", episode_id=episode)
    immediate = center.request_cancel(queued["id"])
    assert immediate["status"] == "cancelled"
    assert immediate["queue_position"] is None

    upserted = center.create(
        "job-upsert-state", episode_id=episode, queue_position=4)
    upserted = center.upsert("job-upsert-state", status="running")
    assert upserted["status"] == "running"
    assert upserted["started_at"] is not None
    assert upserted["queue_position"] is None

    with pytest.raises(AifosError, match="无效任务状态"):
        center.create("job-bad-state", status="mystery")
    with pytest.raises(AifosError, match="JSON 对象"):
        center.create("job-bad-json", request=["not", "an", "object"])
    with pytest.raises(AifosError, match="关联类型"):
        center.upsert_link("job-state", "unknown", "x")

    # Corrupt legacy JSON must never crash a status/list endpoint.
    db.execute(
        "UPDATE production_jobs SET progress='[1,2]', request='{bad', "
        "result='null' WHERE id='job-state'")
    recovered = center.get("job-state")
    assert recovered["progress"] == {}
    assert recovered["request"] == {}
    assert recovered["result"] == {}


def test_bootstrap_interrupts_unreplayable_jobs_and_filters_list(ledger):
    db, center = ledger
    _project, episode, _run = _seed(db)
    center.create(
        "job-queued", episode_id=episode, action="regen_image",
        status="queued", queue_key=f"episode:{episode}", queue_position=1)
    center.create(
        "job-running", episode_id=episode, action="regen_image",
        status="running", queue_key=f"episode:{episode}")
    center.create(
        "job-cancelling", episode_id=episode, action="redo_video",
        status="running")
    center.request_cancel("job-cancelling", "restart race")
    center.create(
        "job-done", episode_id=episode, action="produce", status="done")

    # Fresh ownerless rows may belong to a concurrently running legacy
    # process.  They only become recoverable after a full lease window.
    assert center.bootstrap_interrupt("too soon") == 0
    assert center.bootstrap_interrupt(
        "test restart", at=now() + center.lease_seconds + 1) == 3
    for job_id in ("job-queued", "job-running", "job-cancelling"):
        item = center.get(job_id)
        assert item["status"] == "interrupted"
        assert item["error"] == "test restart"
        assert item["finished_at"] is not None
        assert item["queue_position"] is None
    assert center.get("job-done")["status"] == "done"

    interrupted = center.list(
        statuses="interrupted", episode_id=episode, action="regen_image",
        include_links=True)
    assert {item["id"] for item in interrupted} == {
        "job-queued", "job-running"}
    assert all(item["links"] for item in interrupted)

    # First version does not replay a callable, but an explicit dispatcher may
    # reuse the same durable id after inspecting the interrupted record.
    resumed = center.state(
        "job-queued", "queued", expected_status="interrupted")
    assert resumed["status"] == "queued"
    assert resumed["finished_at"] is None


def test_bootstrap_respects_live_owner_and_recovers_expired_or_dead(ledger):
    db, _center = ledger
    _project, episode, _run = _seed(db)
    live = JobCenter(
        db, owner_id="owner-live", owner_pid=os.getpid(),
        owner_host="test-host", lease_seconds=60)
    dead = JobCenter(
        db, owner_id="owner-dead", owner_pid=2_147_483_647,
        owner_host="test-host", lease_seconds=60)
    recovery = JobCenter(
        db, owner_id="owner-recovery", owner_pid=os.getpid(),
        owner_host="test-host", lease_seconds=60)

    live.create("job-live-owner", episode_id=episode, status="running")
    live.create("job-expired-owner", episode_id=episode, status="running")
    dead.create("job-dead-owner", episode_id=episode, status="running")
    db.execute(
        "UPDATE production_jobs SET lease_expires_at=? WHERE id=?",
        (now() - 1, "job-expired-owner"))

    assert recovery.bootstrap_interrupt("stale owner") == 2
    assert recovery.get("job-live-owner")["status"] == "running"
    assert recovery.get("job-expired-owner")["status"] == "interrupted"
    assert recovery.get("job-dead-owner")["status"] == "interrupted"

    # A diagnostic/rolling-restart instance cannot complete another live
    # process's row.  The actual owner can atomically persist its result.
    with pytest.raises(AifosError, match="其他服务实例"):
        recovery.set_state(
            "job-live-owner", "done", result={"wrong_owner": True})
    completed = live.set_state(
        "job-live-owner", "done", result={"delivered": True})
    assert completed["status"] == "done"
    assert completed["result"] == {"delivered": True}
    assert completed["lease_expires_at"] is None
    assert recovery.bootstrap_interrupt("later restart") == 0


def test_owned_progress_renews_lease_without_stealing(ledger):
    db, _center = ledger
    _project, episode, _run = _seed(db)
    owner = JobCenter(
        db, owner_id="owner-a", owner_pid=os.getpid(),
        owner_host="test-host", lease_seconds=60)
    other = JobCenter(
        db, owner_id="owner-b", owner_pid=os.getpid(),
        owner_host="test-host", lease_seconds=60)
    created = owner.create(
        "job-heartbeat", episode_id=episode, status="running")
    original_lease = created["lease_expires_at"]
    db.execute(
        "UPDATE production_jobs SET lease_expires_at=? WHERE id=?",
        (original_lease - 10, created["id"]))

    owner.set_progress(created["id"], {"completed": 1})
    renewed = owner.get(created["id"])
    assert renewed["lease_expires_at"] > original_lease - 10
    assert renewed["owner_id"] == "owner-a"
    with pytest.raises(AifosError, match="其他服务实例"):
        other.set_progress(created["id"], {"completed": 2})


def test_explicit_job_identity_cannot_be_rebound(ledger):
    db, center = ledger
    _project, episode_one, run_one = _seed(db, episode_number=1)
    stamp = now()
    episode_two = db.execute(
        "INSERT INTO episodes(project_id, number, created_at, updated_at) "
        "SELECT project_id, 2, ?, ? FROM episodes WHERE id=?",
        (stamp, stamp, episode_one),
    ).lastrowid
    run_two = db.execute(
        "INSERT INTO production_runs(episode_id, project_title, "
        "episode_number, action, status, started_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (episode_two, "持久任务测试剧", 2, "produce", "running",
         stamp, stamp),
    ).lastrowid
    center.create(
        "stable-job", run_id=run_one, episode_id=episode_one,
        idempotency_key="stable-key")

    with pytest.raises(AifosError, match="不同 run_id"):
        center.create("stable-job", run_id=run_two, episode_id=episode_two)
    with pytest.raises(AifosError, match="不同 episode_id"):
        center.create("stable-job", episode_id=episode_two)
    with pytest.raises(AifosError, match="不同幂等键"):
        center.create("stable-job", idempotency_key="other-key")
