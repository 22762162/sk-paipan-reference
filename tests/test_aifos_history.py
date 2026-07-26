"""生产历史中心：跨重启持久化、旧记录回填与幽灵任务恢复。"""

from aifos.app import App
from aifos.db import now
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
    stamp = now()
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
