import copy
import sqlite3

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.knowledge_brain import (
    DEPTH_STRUCTURE_SEED,
    SCRIPT_DEVELOPMENT_SEED,
)


def _candidate(key="camera-axis-control"):
    return {
        "knowledge_key": key,
        "title": "双人对话轴线控制",
        "kind": "skill",
        "domain": "blocking",
        "summary": "用站位、视线和同侧机位保持双人对话空间连续。",
        "content": {
            "principles": [
                "先锁人物站位与视线，再布置机位。",
                "换侧拍摄前必须提供可见的越轴动机。",
            ],
            "workflow": [
                "建立人物站位、视线和动作轴。",
                "把候选机位放在轴线同侧并检查屏幕方向。",
                "逐镜核对上一镜结尾与下一镜起点。",
            ],
            "limitations": [
                "有意越轴的情绪镜头必须记录导演覆盖理由。",
            ],
            "quality_gates": [
                "相邻镜头人物屏幕方向和视线匹配。",
            ],
            "validation_plan": [
                "用三镜正反打样例检查站位、视线和屏幕方向。",
            ],
            "standard_refs": [
                "rules.storyboard", "rules.quality_gates.spatial",
            ],
        },
        "applicability": {
            "stages": ["storyboard", "blocking"],
            "task_types": ["dialogue_blocking"],
            "triggers": ["双人对话", "正反打", "越轴"],
            "tags": ["轴线", "视线"],
            "exclusions": ["主观混乱镜头需人工导演确认，不能机械套用。"],
        },
        "provenance": {
            "source_url": "https://example.com/axis",
            "source_title": "双人对话轴线案例",
            "author": "AIFOS 研究室",
            "published_at": "2026-07-29",
            "checked_at": "2026-07-30",
            "evidence": [
                "三组错误与正确对照图。",
                "逐镜站位和视线复核记录。",
            ],
        },
    }


def test_seed_knowledge_is_scored_active_and_callable(tmp_path):
    app = App(tmp_path)
    try:
        overview = app.firefire.overview()
        assert overview["counts"]["knowledge_active"] == 2
        items = {
            item["knowledge_key"]: item for item in overview["knowledge"]}
        assert set(items) == {
            DEPTH_STRUCTURE_SEED["knowledge_key"],
            SCRIPT_DEVELOPMENT_SEED["knowledge_key"],
        }
        assert all(
            item["state_status"] == "active"
            and item["standard_status"] == "current"
            and item["assessment"]["score"] >= 90
            for item in items.values())

        resolved = app.firefire.resolve_knowledge(
            stage="video", task_type="depth_control",
            query="用深度视频复刻动作和运镜")
        assert [match["knowledge_key"] for match in resolved["matches"]] == [
            "depth-structure-control"]
        assert "每份参考素材只承担一个主要控制职责" in (
            resolved["matches"][0]["callable_context"])

        script_matches = app.firefire.resolve_knowledge(
            stage="script", task_type="idea_expansion",
            query="点子写成剧本，先锁目标阻力失败代价和伏笔回收")
        assert [match["knowledge_key"] for match in
                script_matches["matches"]] == [
                    "idea-to-shootable-script"]
        assert "主角要完成什么、什么直接阻止他" in (
            script_matches["matches"][0]["callable_context"])
    finally:
        app.close()


def test_water_content_is_rejected_before_database_entry(tmp_path):
    app = App(tmp_path)
    try:
        with pytest.raises(AifosError, match="价值门禁"):
            app.firefire.create_knowledge({
                "knowledge_key": "water",
                "title": "万能高级感",
                "summary": "做得高级一点。",
                "provenance": {
                    "source_url": "https://example.com/water",
                },
            })
        count = app.db.query_one(
            "SELECT COUNT(*) AS n FROM firefire_knowledge_versions "
            "WHERE knowledge_key='water'")["n"]
        assert count == 0
    finally:
        app.close()


def test_candidate_requires_human_activation_and_versions_are_immutable(
        tmp_path):
    app = App(tmp_path)
    try:
        candidate = app.firefire.create_knowledge(_candidate())
        assert candidate["state_status"] == "review"
        assert app.firefire.resolve_knowledge(
            stage="blocking", task_type="dialogue_blocking",
            query="双人对话正反打")["matches"] == []

        active = app.firefire.publish_knowledge(
            candidate["knowledge_key"], approved_by="tester")
        assert active["state_status"] == "active"
        assert app.firefire.resolve_knowledge(
            stage="blocking", task_type="dialogue_blocking",
            query="双人对话正反打")["matches"][0][
                "knowledge_key"] == "camera-axis-control"

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            app.db.execute(
                "UPDATE firefire_knowledge_versions SET title='tampered' "
                "WHERE id=?", (candidate["id"],))
    finally:
        app.close()


def test_standard_change_pauses_call_until_new_version_is_reviewed(tmp_path):
    app = App(tmp_path)
    try:
        candidate = app.firefire.create_knowledge(_candidate())
        app.firefire.publish_knowledge(
            candidate["knowledge_key"], approved_by="tester")
        standard = app.standards.active()
        changed = copy.deepcopy(standard["content"])
        changed["description"] = (
            changed.get("description", "") + "；知识标准同步测试")
        newer = app.standards.save(
            changed, change_note="测试知识标准失效保护")

        result = app.firefire.resolve_knowledge(
            stage="blocking", task_type="dialogue_blocking",
            query="双人对话")
        assert "camera-axis-control" in result["skipped_stale"]
        assert all(
            item["knowledge_key"] != "camera-axis-control"
            for item in result["matches"])

        refreshed = app.firefire.refresh_knowledge("camera-axis-control")
        assert refreshed["version"] == 2
        with pytest.raises(AifosError, match="相同知识内容"):
            app.firefire.refresh_knowledge("camera-axis-control")
        app.firefire.publish_knowledge(
            "camera-axis-control", approved_by="tester-v2")
        matched = app.firefire.resolve_knowledge(
            stage="blocking", task_type="dialogue_blocking",
            query="双人对话")
        assert any(
            item["knowledge_key"] == "camera-axis-control"
            and item["version"] == 2
            for item in matched["matches"])
    finally:
        app.close()
