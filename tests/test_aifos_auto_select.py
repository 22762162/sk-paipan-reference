"""一组四张 → CODEX 评选 → 自动确认其中一张；人工可改选重新确认。"""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.auto_select import (CANDIDATE_GROUP_SIZE, build_selection_payload,
                               normalize_selection, selection_decision,
                               validate_image_select)
from aifos.errors import AifosError, ProviderUnavailable

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# 假 codex:出图照常落盘，评选请求返回一行结构化评选 JSON
FAKE_SELECT_CODEX = '''#!/usr/bin/env python3
import json, re, sys
instruction = sys.argv[-1]
if "图片资产评选员" in instruction:
    indexes = [int(i) for i in re.findall(r"候选(\\d+):", instruction)]
    print(json.dumps({
        "best_index": indexes[1] if len(indexes) > 1 else 1,
        "confidence": 0.9,
        "needs_human": False,
        "reason": "第二张最贴合剧本身份与时代",
        "scores": [{"index": i, "score": 90 - 10 * n,
                    "match": ["身份一致"], "violations": []}
                   for n, i in enumerate(indexes)],
    }, ensure_ascii=False))
    sys.exit(0)
for p in re.findall(r"(/\\S+?\\.png)", instruction):
    open(p, "wb").write(b"fake-png")
print("codex done")
'''


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _episode(app, title, number=1):
    project = app.projects.get_project(title)
    return project, app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))


def _confirm_script(app, title="万妖图录", number=1):
    """剧本确认 = 图片资产开始生产的起点。"""
    first = app.director.produce(title, number, pause_for_confirm=True)
    assert first["status"] == "awaiting_script"
    return app.director.produce(title, number, pause_for_confirm=True)


# ---- 合同层:评选应答的校验、归一化与自动确认判定 ----

def test_validate_rejects_unusable_verdicts():
    assert validate_image_select({"best_index": 2, "confidence": 0.8}) == ""
    assert "best_index" in validate_image_select({})
    assert "整数" in validate_image_select({"best_index": True})
    assert "负数" in validate_image_select({"best_index": -1})
    assert "0-1" in validate_image_select(
        {"best_index": 1, "confidence": 4})
    assert "布尔" in validate_image_select(
        {"best_index": 1, "needs_human": "no"})
    assert "数组" in validate_image_select({"best_index": 1, "scores": 3})
    assert "index 与 score" in validate_image_select(
        {"best_index": 1, "scores": [{"index": 1}]})


def test_normalize_fills_ranking_and_confidence():
    verdict = normalize_selection({
        "best_index": 3,
        "scores": [
            {"index": 1, "score": 60, "match": ["还行"]},
            {"index": 3, "score": 92, "violations": []},
            {"index": 2, "score": 71},
        ],
    }, CANDIDATE_GROUP_SIZE)
    assert verdict["best_index"] == 3
    assert verdict["runner_up_index"] == 2
    # 未给置信度时按第一、二名分差推导:0.5 + (92-71)/40 = 1.025，封顶 1.0
    assert verdict["confidence"] == 1.0
    assert verdict["scores"][0]["index"] == 3
    assert verdict["needs_human"] is False


def test_normalize_falls_back_to_scores_and_flags_human():
    verdict = normalize_selection({
        "best_index": 9,           # 超出候选数,不可信
        "scores": [{"index": 2, "score": 80}],
    }, CANDIDATE_GROUP_SIZE)
    assert verdict["best_index"] == 2
    empty = normalize_selection({"reason": "四张都跑脸"}, 4)
    assert empty["best_index"] == 0 and empty["needs_human"] is True
    assert normalize_selection("not json", 4)["needs_human"] is True


def test_decision_thresholds_route_uncertain_groups_to_human():
    confident = normalize_selection(
        {"best_index": 1, "confidence": 0.82, "reason": "身份最准"}, 4)
    assert selection_decision(confident, 4)["auto_confirm"] is True
    unsure = normalize_selection(
        {"best_index": 1, "confidence": 0.4, "reason": "难分伯仲"}, 4)
    decision = selection_decision(unsure, 4)
    assert decision["auto_confirm"] is False
    assert decision["blocked_by"] == "low_confidence"
    refused = normalize_selection(
        {"best_index": 2, "confidence": 0.95, "needs_human": True,
         "reason": "四张都有硬伤"}, 4)
    assert selection_decision(refused, 4)["blocked_by"] == "needs_human"
    assert selection_decision(
        normalize_selection({"best_index": 0}, 4), 4)[
            "blocked_by"] == "no_choice"


def test_codex_instruction_lists_candidates_and_script_requirements():
    payload = build_selection_payload(
        "character", "林昭",
        requirements=["性别:男", "时代与世界观:明初"],
        candidates=[{"index": i, "uri": f"/tmp/c{i}.png",
                     "variant_label": f"造型{i}"}
                    for i in range(1, CANDIDATE_GROUP_SIZE + 1)],
        style="国风漫剧", project_title="万妖图录", episode_number=3)
    instruction, targets, data = build_instruction(
        "image_select", payload, Path("/tmp"))
    assert targets == [] and data == {"select": True}
    assert "图片资产评选员" in instruction
    assert "人物立绘「林昭」" in instruction
    for index in range(1, CANDIDATE_GROUP_SIZE + 1):
        assert f"候选{index}:/tmp/c{index}.png" in instruction
    assert "性别:男" in instruction and "时代与世界观:明初" in instruction
    assert "best_index" in instruction and "needs_human" in instruction


def test_codex_bridge_returns_selection_and_fails_closed(tmp_path):
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text(FAKE_SELECT_CODEX, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    payload = build_selection_payload(
        "scene", "古镇长街",
        requirements=["地点:古镇长街"],
        candidates=[{"index": i, "uri": f"/tmp/s{i}.png"}
                    for i in range(1, 5)])
    request = {"capability": "image_select", "payload": payload,
               "out_dir": str(tmp_path / "out")}
    proc = subprocess.run(
        [sys.executable, "-m", "aifos.adapters.codex_image",
         "--codex", str(binary)],
        input=json.dumps(request), capture_output=True, text=True,
        env={"PYTHONPATH": REPO_ROOT, "PATH": "/usr/bin:/bin"})
    reply = json.loads(proc.stdout)
    assert reply["ok"], reply
    assert reply["data"]["best_index"] == 2
    assert reply["model"] == "Codex 视觉评选"

    # 评选不返回可解析结论时失败关闭,交路由回退,绝不猜测定版
    silent = tmp_path / "bin" / "silent"
    silent.write_text("#!/usr/bin/env python3\nprint('no json here')\n",
                      encoding="utf-8")
    silent.chmod(silent.stat().st_mode | stat.S_IXUSR)
    proc = subprocess.run(
        [sys.executable, "-m", "aifos.adapters.codex_image",
         "--codex", str(silent)],
        input=json.dumps(request), capture_output=True, text=True,
        env={"PYTHONPATH": REPO_ROOT, "PATH": "/usr/bin:/bin"})
    reply = json.loads(proc.stdout)
    assert reply["ok"] is False and "候选评选" in reply["error"]


# ---- 产线层:剧本确认 → 四张候选 → 自动确认 ----

def test_first_script_confirm_produces_four_and_auto_confirms_one(app):
    summary = _confirm_script(app)
    assert summary["status"] == "awaiting_confirm"
    project, episode = _episode(app, "万妖图录")
    selection, _ = app.projects.latest_document(
        episode["id"], "cast_selection")
    assert selection["passed"] is True
    for group in (selection["characters"] + selection["props"]
                  + selection["scenes"]):
        assert group["candidate_count"] == CANDIDATE_GROUP_SIZE
        assert group["locked"] is True
        assert group["selection_source"] == "auto_codex"
        assert group["auto_selection"]["reason"]
        assert 0 <= group["auto_selection"]["confidence"] <= 1
        assert sum(1 for c in group["candidates"] if c["selected"]) == 1
    report, _ = app.projects.latest_document(
        episode["id"], "cast_auto_selection")
    assert report["enabled"] is True
    assert report["auto_confirmed"] == len(report["items"])
    assert report["pending"] == 0
    kinds = {item["kind"] for item in report["items"]}
    assert {"character", "scene"} <= kinds
    assert all(item["scores"] for item in report["items"])


def test_scene_concept_art_is_locked_from_its_own_four_candidates(app):
    _confirm_script(app, "万妖图录", 2)
    project, episode = _episode(app, "万妖图录", 2)
    script, _ = app.projects.latest_document(episode["id"], "script")
    scenes = app.director.scene_selection_status(project["id"], script)
    assert scenes["total"] >= 1 and scenes["passed"] is True
    for item in scenes["scenes"]:
        assert len(item["candidates"]) == CANDIDATE_GROUP_SIZE
        chosen = next(c for c in item["candidates"] if c["selected"])
        locked = app.assets.latest(
            project["id"], "scene_art", item["scene"])
        # 定版图就是选中的那张候选，下游镜头继续按 scene_art 取图
        assert locked["uri"] == chosen["uri"]
        assert json.loads(locked["meta"])["locked"] is True


def test_scene_candidate_passes_the_dispatch_contract(app):
    """场景候选的产出文件名带序号，但提示词对象必须是地点本身。"""
    _confirm_script(app, "候选合同")
    project, episode = _episode(app, "候选合同")
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}"
         / f"e{episode['number']:03d}" / "render_plan.json")
        .read_text(encoding="utf-8"))
    item = next(i for i in plan["items"]
                if i["category"] == "scene_candidate")
    location = item["name"]
    task = {
        "item_id": item["id"], "capability": "image",
        "payload": {
            "scene_art": True, "scene_candidate": True,
            "scene_name": location,
            "art_name": f"{location}_candidate_01",
            "characters": [], "location": location,
            "prompt": item["prompt"], "shot_no": 0,
        },
    }
    contract = app.director._build_dispatch_contract(task, item)
    assert contract["issues"] == []
    assert contract["category"] == "scene_candidate"


def test_selector_failure_keeps_the_human_gate(app, monkeypatch):
    """评选不可用时绝不猜测定版:整组退回人工四选一。"""
    original = app.router.call

    def refuse(capability, payload, out_dir, cancel=None):
        if capability == "image_select":
            raise ProviderUnavailable("没有可用的评选产线")
        return original(capability, payload, out_dir, cancel=cancel)

    monkeypatch.setattr(app.router, "call", refuse)
    summary = _confirm_script(app, "评选失败")
    assert summary["status"] == "awaiting_cast"
    project, episode = _episode(app, "评选失败")
    report, _ = app.projects.latest_document(
        episode["id"], "cast_auto_selection")
    assert report["auto_confirmed"] == 0 and report["pending"] > 0
    assert all(item["blocked_by"] == "selector_error"
               for item in report["items"])
    selection, _ = app.projects.latest_document(
        episode["id"], "cast_selection")
    assert selection["passed"] is False
    # 候选仍然是四张,人工可以直接定版继续
    assert all(item["candidate_count"] == CANDIDATE_GROUP_SIZE
               for item in selection["characters"])


def test_low_confidence_group_waits_for_human(app, monkeypatch):
    original = app.router.call

    def hesitate(capability, payload, out_dir, cancel=None):
        result = original(capability, payload, out_dir, cancel=cancel)
        if capability == "image_select":
            result.data.update({"confidence": 0.2,
                                "reason": "四张差别太小"})
        return result

    monkeypatch.setattr(app.router, "call", hesitate)
    summary = _confirm_script(app, "把握不足")
    assert summary["status"] == "awaiting_cast"
    _project, episode = _episode(app, "把握不足")
    report, _ = app.projects.latest_document(
        episode["id"], "cast_auto_selection")
    assert all(item["blocked_by"] == "low_confidence"
               for item in report["items"])
    assert all("低于自动确认阈值" in item["reason"]
               for item in report["items"])


def test_disabled_switch_returns_to_full_manual_selection(app):
    app.config.data["defaults"]["auto_select_candidates"] = False
    summary = _confirm_script(app, "全人工")
    assert summary["status"] == "awaiting_cast"
    _project, episode = _episode(app, "全人工")
    report, _ = app.projects.latest_document(
        episode["id"], "cast_auto_selection")
    assert report["enabled"] is False
    assert all(item["blocked_by"] == "disabled" for item in report["items"])


# ---- 人工改选:自动确认之后随时可以改并重新确认 ----

def test_human_can_reselect_after_auto_confirm(app):
    _confirm_script(app, "人工改选")
    project, episode = _episode(app, "人工改选")
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    auto_locked = app.assets.latest(
        project["id"], "character_identity", hero)
    auto_index = json.loads(auto_locked["meta"])["candidate_index"]
    other = next(index for index in range(1, CANDIDATE_GROUP_SIZE + 1)
                 if index != auto_index)
    sheets_before = [
        row["uri"] for row in app.assets.active_list(
            project["id"], "character_sheet")
        if row["uri"]
        and json.loads(row["meta"] or "{}").get("character") == hero]

    status = app.director.select_character_candidate(
        "人工改选", 1, hero, other)
    changed = next(item for item in status["characters"]
                   if item["character"] == hero)
    assert changed["selection_source"] == "human"
    assert next(c for c in changed["candidates"] if c["selected"])[
        "index"] == other
    locked = app.assets.latest(project["id"], "character_identity", hero)
    assert locked["version"] == auto_locked["version"] + 1
    # 改掉母资产后退回定版检查点，由它派生的人物套件同时作废
    episode = app.projects.get_episode(episode["id"])
    assert episode["status"] == "awaiting_cast"
    sheets_after = [
        row["uri"] for row in app.assets.active_list(
            project["id"], "character_sheet")
        if row["uri"]
        and json.loads(row["meta"] or "{}").get("character") == hero]
    assert sheets_before and not sheets_after

    # 确认后从断点继续,只补受影响的下游画面
    summary = app.director.produce("人工改选", 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"


def test_reselecting_the_same_candidate_does_not_reopen_the_gate(app):
    _confirm_script(app, "同一张")
    project, episode = _episode(app, "同一张")
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    index = json.loads(app.assets.latest(
        project["id"], "character_identity", hero)["meta"])[
            "candidate_index"]
    app.director.select_character_candidate("同一张", 1, hero, index)
    assert app.projects.get_episode(
        episode["id"])["status"] == "awaiting_confirm"


def test_scene_can_be_reselected_and_downstream_follows(app):
    _confirm_script(app, "换场景")
    project, episode = _episode(app, "换场景")
    script, _ = app.projects.latest_document(episode["id"], "script")
    location = app.director._script_locations(script)[0]
    before = app.assets.latest(project["id"], "scene_art", location)
    index = json.loads(before["meta"])["candidate_index"]
    other = next(value for value in range(1, CANDIDATE_GROUP_SIZE + 1)
                 if value != index)
    status = app.director.select_scene_candidate(
        "换场景", 1, location, other)
    scene = next(item for item in status["scenes"]
                 if item["scene"] == location)
    assert scene["selection_source"] == "human"
    after = app.assets.latest(project["id"], "scene_art", location)
    assert after["uri"] != before["uri"]
    assert after["uri"] == next(
        c["uri"] for c in scene["candidates"] if c["selected"])


def test_reselect_is_refused_while_the_episode_is_running(app):
    _confirm_script(app, "生产中")
    project, episode = _episode(app, "生产中")
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    app.projects.set_episode_status(episode["id"], "images")
    with pytest.raises(AifosError, match="正在生产中"):
        app.director.select_character_candidate("生产中", 1, hero, 2)


def test_regenerate_redraws_four_and_asks_codex_again(app):
    _confirm_script(app, "重做候选")
    project, episode = _episode(app, "重做候选")
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    before = app.assets.latest(
        project["id"], "character_candidate", f"{hero}:01")["version"]
    result = app.director.regenerate_character_candidates(
        "重做候选", 1, character_name=hero)
    assert result["status"] == "awaiting_cast"
    assert result["auto_confirmed"] >= 1
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    entry = next(item for item in status["characters"]
                 if item["character"] == hero)
    assert len(entry["candidates"]) == CANDIDATE_GROUP_SIZE
    assert entry["locked"] is True
    assert entry["selection_source"] == "auto_codex"
    # 旧候选保留为历史版本，新一轮四张写入更高版本
    assert app.assets.latest(
        project["id"], "character_candidate",
        f"{hero}:01")["version"] > before
