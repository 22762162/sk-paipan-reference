"""项目中心 / IP 资产中心 / 数据中心 / 系统中心单元测试。"""

import json

import pytest

from aifos.app import App
from aifos.errors import PermissionDenied


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def test_project_and_episode_lifecycle(app):
    project, created = app.projects.get_or_create_project("测试项目", style="国风")
    assert created
    _, created_again = app.projects.get_or_create_project("测试项目")
    assert not created_again

    episode, _ = app.projects.get_or_create_episode(
        project["id"], 1, premise="第一集")
    app.projects.set_episode_status(episode["id"], "script")
    app.projects.add_episode_cost(episode["id"], 1.5)
    app.projects.set_qc_score(episode["id"], 92)
    row = app.projects.get_episode(episode["id"])
    assert (row["status"], row["cost"], row["qc_score"]) == ("script", 1.5, 92)

    v1 = app.projects.save_document(episode["id"], "script", {"a": 1})
    v2 = app.projects.save_document(episode["id"], "script", {"a": 2})
    assert (v1, v2) == (1, 2)
    content, version = app.projects.latest_document(episode["id"], "script")
    assert content == {"a": 2} and version == 2


def test_asset_versioning_and_reuse(app):
    project, _ = app.projects.get_or_create_project("资产项目")
    pid = project["id"]

    first, reused = app.assets.acquire(pid, "character", "林昭")
    assert not reused and first["version"] == 1
    again, reused = app.assets.acquire(pid, "character", "林昭")
    assert reused and again["reuse_count"] == 1

    v2 = app.assets.register(pid, "character", "林昭", uri="v2.png",
                             new_version=True)
    assert v2["version"] == 2
    assert app.assets.latest(pid, "character", "林昭")["version"] == 2
    assert len(app.assets.history(pid, "character", "林昭")) == 2


def test_data_center_archive_and_export(app, tmp_path):
    app.data.record("prompt", "success", prompt="p1", episode_id=None)
    app.data.record("case", "failure", prompt="c1", meta={"k": "v"})
    assert len(app.data.cases(label="failure")) == 1
    out = tmp_path / "export.jsonl"
    count = app.data.export_jsonl(out)
    assert count == 2
    lines = [json.loads(line) for line in
             out.read_text(encoding="utf-8").splitlines()]
    assert lines[1]["meta"] == {"k": "v"}


def test_permissions(app):
    app.system.ensure_user("guest", "viewer")
    app.system.require("guest", "read")
    with pytest.raises(PermissionDenied):
        app.system.require("guest", "produce")
    with pytest.raises(PermissionDenied):
        app.system.require("不存在的人", "read")
    app.system.require("admin", "manage")


def test_logs_recorded(app):
    app.logger.info("test", "你好日志")
    rows = app.logger.tail(5)
    assert any("你好日志" in row["message"] for row in rows)
    assert (app.workspace.logs_dir / "aifos.log").exists()
