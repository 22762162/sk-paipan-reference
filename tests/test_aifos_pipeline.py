"""AIFOS 端到端流水线测试(全部走内置 mock Provider,离线确定性)。"""

import json
from pathlib import Path

import pytest

from aifos.app import App


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _finish(app, title, number, **kwargs):
    summary = app.director.produce(title, number, **kwargs)
    if summary["status"] != "awaiting_cast":
        return summary
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    script, _ = app.projects.latest_document(episode["id"], "script")
    for character in script["characters"]:
        app.director.select_character_candidate(
            title, number, character["name"], 1)
    return app.director.produce(title, number)


def test_full_pipeline_produces_episode(app):
    summary = _finish(app, "万妖图录", 15)

    assert summary["status"] == "done"
    assert summary["qc_score"] == 100
    assert all(s["status"] == "done" for s in summary["stages"])
    assert [s["stage"] for s in summary["stages"]] == [
        "script", "continuity", "cast", "storyboard", "blocking", "images",
        "text_assets", "frames", "preflight", "videos", "voices",
        "edit", "qc", "package", "archive"]

    # 产物落盘
    out = Path(summary["artifacts_dir"])
    assert Path(summary["outputs"]["final"]).exists()
    assert Path(summary["outputs"]["cover"]).exists()
    assert (out / "qc_report.json").exists()
    assert (out / "summary.json").exists()
    assert len(summary["outputs"]["titles"]) == 3
    assert summary["outputs"]["clips"]

    # 剧本/分镜文档入库
    project = app.projects.get_project("万妖图录")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=15",
        (project["id"],))
    script, version = app.projects.latest_document(episode["id"], "script")
    assert version == 1 and script["scenes"]
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    assert len(storyboard["shots"]) > len(script["scenes"])

    # 成本记账:总成本还包含上一轮生成的五选一候选；配置了预算时才校验上限。
    stage_cost = round(sum(s["cost"] for s in summary["stages"]), 2)
    assert summary["cost"] >= stage_cost > 0
    if summary["budget"] > 0:
        assert summary["cost"] <= summary["budget"]

    # 数据沉淀:prompt/image/video/voice/case 均有记录
    kinds = {row["kind"] for row in app.data.cases(episode_id=episode["id"])}
    assert {"prompt", "image", "video", "voice", "case"} <= kinds
    assert app.data.cases(label="success", kind="case")


def test_asset_reuse_across_episodes(app):
    _finish(app, "万妖图录", 1)
    _finish(app, "万妖图录", 2)
    project = app.projects.get_project("万妖图录")
    scenes = app.assets.list(project["id"], kind="scene")
    # 场景池有限,两集之间必然存在复用
    assert sum(row["reuse_count"] for row in scenes) > 0


def test_deterministic_script(app):
    s1 = _finish(app, "万妖图录", 3)
    project = app.projects.get_project("万妖图录")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=3",
        (project["id"],))
    script_a, _ = app.projects.latest_document(episode["id"], "script")
    # force 重跑:相同输入 → 相同剧本(确定性),文档版本 +1
    app.director.produce("万妖图录", 3, force=True)
    script_b, version = app.projects.latest_document(episode["id"], "script")
    assert version == 2
    assert script_a == script_b
    assert s1["status"] == "done"


def test_sensitive_premise_fails_qc(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "qc": {"pass_score": 95, "sensitive_words": ["禁忌之血"]}})
    try:
        summary = _finish(
            app, "万妖图录", 7, premise="主角误饮禁忌之血,妖化失控")
        assert summary["status"] == "qc_failed"
        assert summary["qc_score"] < 95
        report = json.loads(
            (Path(summary["artifacts_dir"]) / "qc_report.json").read_text(
                encoding="utf-8"))
        assert any(i["check"] == "sensitive" for i in report["issues"])
        # 质检未过 → 跳过运营包装,沉淀为失败案例
        assert summary["outputs"]["cover"] == ""
        assert app.data.cases(label="failure", kind="case")
    finally:
        app.close()


def test_budget_exceeded_stops_pipeline(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "budget": {"per_episode": 0.15}})
    try:
        summary = app.director.produce("万妖图录", 9)
        assert summary["status"] == "failed"
        failed = [s for s in summary["stages"] if s["status"] == "failed"]
        assert failed and "预算" in failed[0]["error"]
        # 预算之后的阶段不再执行
        assert len(summary["stages"]) < 14
    finally:
        app.close()
