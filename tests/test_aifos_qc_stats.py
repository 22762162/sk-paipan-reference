"""质检台账:失败原因归类、落盘聚合与 director 挂钩。"""

import json

from aifos.qc_stats import (
    FILENAME, classify_issue, classify_issues, format_qc_table,
    record_qc, summarize_qc)


def _workspace(tmp_path):
    out_root = tmp_path / "workspace" / "artifacts" / "p001" / "e001"
    out_root.mkdir(parents=True)
    return out_root, tmp_path / "workspace" / "logs" / FILENAME


def test_classify_matches_historical_failure_phrases():
    # 用历史日志里的真实表述验证归类优先级
    assert classify_issue(
        "画面人物身份与锁定最终立绘不一致") == "identity_drift"
    assert classify_issue(
        "画面人数与要求的3人不一致") == "figure_count"
    assert classify_issue(
        "人物性别/性别表达与锁定最终立绘不一致") == "gender"
    assert classify_issue(
        "视角按back_silhouette核验，但待检图实际为正面") == "camera"
    assert classify_issue(
        "构图更接近膝上或大半身中景，不符合合同要求") == "camera"
    # 发色/发型与锁定立绘不一致本质是身份漂移(锁定立绘被违背)
    assert classify_issue(
        "待检图为亮棕色齐肩内扣短发，发色和发型轮廓与最终立绘不一致"
    ) == "identity_drift"
    assert classify_issue("妆容浓艳，与服装设定不符") == "wardrobe"
    assert classify_issue("画面出现提示词表格，属于提示词泄漏") == "onscreen_text"
    assert classify_issue("多余人物且形象不统一") == "figure_count"
    assert classify_issue("完全无法归类的神秘问题") == "other"
    # 去重且保持出现顺序
    assert classify_issues([
        "画面人数与要求的2人不一致",
        "画面人物身份与锁定最终立绘不一致",
        "又一个人数问题",
    ]) == ["figure_count", "identity_drift"]
    assert classify_issues([
        "画面人物身份与锁定最终立绘不一致",
        "画面人数与要求的2人不一致",
    ]) == ["identity_drift", "figure_count"]


def test_record_and_summarize(tmp_path):
    out_root, stats_file = _workspace(tmp_path)
    record_qc(out_root, episode_id=1, item_id="shot_001",
              category="keyframe", provider="codex", task_class="batch",
              passed=True, issues=[])
    record_qc(out_root, episode_id=1, item_id="shot_002",
              category="keyframe", provider="image_api", task_class="batch",
              passed=False, attempts=2,
              issues=["画面人物身份与锁定最终立绘不一致",
                      "画面人数与要求的2人不一致"])
    record_qc(out_root, episode_id=1, item_id="shot_003",
              category="frames", provider="image_api",
              passed=False, issues=["首帧:人物性别/性别表达与锁定最终立绘不一致"])

    summary = summarize_qc(stats_file.parent)
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 2
    assert summary["by_issue_type"]["identity_drift"]["count"] == 1
    assert summary["by_issue_type"]["figure_count"]["providers"] == {
        "image_api": 1}
    assert summary["by_provider"]["image_api"]["failed"] == 2
    table = format_qc_table(summary)
    assert "身份漂移" in table and "人数/多余人物" in table

    entry = json.loads(stats_file.read_text().splitlines()[1])
    assert entry["issue_types"] == ["identity_drift", "figure_count"]
    assert entry["attempts"] == 2


def test_record_outside_workspace_is_silent(tmp_path):
    record_qc(tmp_path / "elsewhere", passed=False, issues=["x"])
    assert not (tmp_path / "logs").exists()


def test_director_plan_mark_records_qc(tmp_path):
    from aifos.app import App
    app = App(tmp_path / "ws")
    try:
        project, _created = app.projects.get_or_create_project("台账测试")
        episode, _created = app.projects.get_or_create_episode(
            project["id"], 1, premise="p")
        out_root = (app.workspace.artifacts_dir
                    / f"p{project['id']:03d}" / "e001")
        out_root.mkdir(parents=True, exist_ok=True)
        ctx = {"project": project, "episode": episode,
               "out_root": out_root}
        plan = {"items": [{
            "id": "shot_001", "label": "镜头1", "status": "pending",
            "category": "keyframe", "capability": "image",
            "prompt": "p", "error": ""}]}
        app.director._plan_write(ctx, plan)
        app.director._plan_mark(
            ctx, "shot_001", "failed", error="qc",
            extra={"qc": {"passed": False, "attempts": 2,
                          "issues": ["画面人物身份与锁定最终立绘不一致"]},
                   "provider": "image_api"})
        stats_file = app.workspace.logs_dir / FILENAME
        entry = json.loads(stats_file.read_text().splitlines()[-1])
        assert entry["passed"] is False
        assert entry["provider"] == "image_api"
        assert entry["issue_types"] == ["identity_drift"]
        assert entry["episode_id"] == episode["id"]
    finally:
        app.close()
