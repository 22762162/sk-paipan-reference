"""资产中心 SQL 侧最新版本去重:active_list/stats 语义回归。"""

import json

import pytest

from aifos.app import App


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _legacy_active_list(assets, project_id, kind=None):
    """优化前的 Python 侧去重逻辑,作为语义对拍基准。"""
    rows = assets.list(project_id, kind=kind)
    latest = {}
    for row in rows:
        latest[(row["kind"], row["name"])] = row
    return [row for row in latest.values() if not assets.is_deleted(row)]


def _create_project(app, title):
    project, _ = app.projects.get_or_create_project(title, kind="drama")
    return project


def _populate(app, project_id):
    assets = app.assets
    # 同一资产三个版本,最新版生效
    assets.register(project_id, "character", "林川", uri="v1.png",
                    meta={"mark": "v1"})
    assets.register(project_id, "character", "林川", uri="v2.png",
                    meta={"mark": "v2"}, new_version=True)
    assets.register(project_id, "character", "林川", uri="v3.png",
                    meta={"mark": "v3"}, new_version=True)
    # 单版本资产
    assets.register(project_id, "character", "苏晚晴", uri="su.png")
    assets.register(project_id, "scene", "主场景", uri="scene.png")
    # 被替换资产:reuse_count 只统计当前版本
    assets.register(project_id, "prop", "文匣", uri="box.png")
    app.db.execute(
        "UPDATE assets SET reuse_count=? WHERE kind='prop' AND name='文匣'",
        (7,))
    # 墓碑资产:删除后不再出现
    assets.register(project_id, "prop", "旧印", uri="seal.png")
    assets.soft_delete(project_id, "prop", "旧印")


def test_active_list_matches_legacy_semantics(app):
    project = _create_project(app, "语义对拍")
    _populate(app, project["id"])

    got = app.assets.active_list(project["id"])
    want = _legacy_active_list(app.assets, project["id"])
    assert [(r["kind"], r["name"], r["version"], r["uri"])
            for r in got] == [
                (r["kind"], r["name"], r["version"], r["uri"]) for r in want]

    marks = {r["name"]: json.loads(r["meta"]).get("mark") for r in got}
    assert marks["林川"] == "v3"
    assert {r["name"] for r in got} == {"林川", "苏晚晴", "主场景", "文匣"}


def test_active_list_kind_filter_and_order(app):
    project = _create_project(app, "过滤排序")
    _populate(app, project["id"])

    rows = app.assets.active_list(project["id"], kind="character")
    assert [r["name"] for r in rows] == ["林川", "苏晚晴"]
    kinds = [r["kind"] for r in app.assets.active_list(project["id"])]
    assert kinds == sorted(kinds)


def test_tombstone_hidden_and_rebirth_creates_new_version(app):
    project = _create_project(app, "墓碑重生")
    _populate(app, project["id"])

    names = {r["name"] for r in app.assets.active_list(project["id"])}
    assert "旧印" not in names

    # 同名再生产:另起新版本,墓碑与历史版本仍可溯源
    app.assets.register(project["id"], "prop", "旧印", uri="seal2.png")
    rows = [r for r in app.assets.active_list(project["id"], kind="prop")
            if r["name"] == "旧印"]
    assert len(rows) == 1 and rows[0]["uri"] == "seal2.png"
    assert rows[0]["version"] == 3
    history = app.assets.history(project["id"], "prop", "旧印")
    assert len(history) == 3
    assert app.assets.is_deleted(history[1])


def test_stats_counts_active_only_and_sums_reuse(app):
    project = _create_project(app, "统计口径")
    _populate(app, project["id"])

    stats = {row["kind"]: row for row in app.assets.stats(project["id"])}
    assert stats["character"]["total"] == 2
    assert stats["scene"]["total"] == 1
    assert stats["prop"]["total"] == 1   # 墓碑不计入
    assert stats["prop"]["reused"] == 7


def test_latest_rows_isolate_projects(app):
    one = _create_project(app, "项目甲")
    two = _create_project(app, "项目乙")
    _populate(app, one["id"])
    app.assets.register(two["id"], "character", "独角", uri="solo.png")

    assert {r["name"] for r in app.assets.active_list(two["id"])} == {"独角"}
    assert len(app.assets.active_list(one["id"])) == 4
