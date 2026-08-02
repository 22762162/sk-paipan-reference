"""Regression coverage for the one-shot Episode 29 contract migration."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from aifos.app import App
from aifos.prompt_contract import compile_shot_prompt


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_episode29_contracts.py"
SPEC = importlib.util.spec_from_file_location("repair_episode29_contracts", SCRIPT)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)


def _actor(position, prop="无持物", direction="面向前方", pose="站立"):
    return {
        "position": position,
        "prop": prop,
        "direction": direction,
        "pose": pose,
        "condition": {
            "life_state": "alive", "consciousness_state": "awake",
            "embodiment": "physical", "mobility": "active",
        },
    }


def _shot(number, scene, characters):
    start = {name: _actor("起点") for name in characters}
    end = {name: _actor("终点") for name in characters}
    return {
        "shot_no": number,
        "scene_no": number,
        "characters": list(characters),
        "character_count": len(characters),
        "visible_figure_count": len(characters),
        "description": f"镜头{number}的完整视频动作时间线",
        "camera": "中景，平视，50mm，固定",
        "start_state": start,
        "end_state": end,
        "frame_target": {"phase": "end", "state": "旧终点", "fallback": False},
        "frame_targets": {
            "keyframe": {"phase": "end", "state": "旧关键帧", "fallback": False},
            "first_frame": {"phase": "start", "state": "旧首帧", "fallback": False},
            "last_frame": {"phase": "end", "state": "旧尾帧", "fallback": False},
        },
        "readable_text": {"required": False, "whitelist": []},
        "frame_props": [],
        "shot_contract": {
            "景别": "中景", "角度": "平视", "焦段": "50mm",
            "机位": "侧面", "运镜": "固定", "构图": "三分法",
        },
        "prompt_contract": {"scene": scene},
        "prompt": f"场景：{scene}",
    }


def _baseline_storyboard():
    shot1 = _shot(1, "明代宫殿内景", ["虞寻歌", "柳争流"])
    shot1["readable_text"] = {
        "required": True,
        "carrier": "手机锁屏",
        "whitelist": ["2078年2月21日", "23:00"],
    }
    shot2 = _shot(2, "轿车内·高速公路", ["虞寻歌"])
    shot2["functional_figures"] = [
        {"name": "小吴", "count": 1, "state": state, "function": "司机"}
        for state in ("驾驶位", "加速", "便利店", "车外递交")
    ]
    shot2["visible_figure_count"] = 5
    shot3 = _shot(3, "虞家别墅·虞寻欢卧室", ["虞寻歌", "虞寻欢"])
    shot4 = _shot(4, "虞家别墅·虞寻欢卧室", ["虞寻歌", "虞寻欢"])
    shot4["readable_text"] = {
        "required": True,
        "carrier": "手机屏幕",
        "whitelist": ["02:21:59", "02:22:00", "万族之劫：恐惧裂缝"],
    }
    shot4["frame_props"] = [
        {
            "prop_id": "prop_game_phone_01", "phase": phase,
            "physical_state": "旧跨阶段屏幕", "holder": "虞寻歌双手",
            "location": "沙发胸前", "support": "双手",
            "visibility": "visible", "representation": "physical",
        }
        for phase in ("start", "freeze", "end")
    ]
    return {
        "episode_title": "盗神重生",
        "shots": [shot1, shot2, shot3, shot4],
    }


@pytest.fixture()
def episode29_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    app = App(workspace)
    project, _ = app.projects.get_or_create_project(
        "游戏入侵", style="错误古风模板")
    ts = 1.0
    app.db.execute(
        "INSERT INTO episodes(id,project_id,number,title,premise,status,cost,created_at,updated_at) "
        "VALUES(29,?,?,?,?,?,?,?,?)",
        (project["id"], 1, "盗神重生", "", "failed", 0.0, ts, ts),
    )
    board = _baseline_storyboard()
    for _ in range(13):
        app.projects.save_document(29, "storyboard", board)

    files = {}
    for shot_no in range(1, 5):
        name = f"e001_shot{shot_no:03d}"
        for kind, suffix in (("first_frame", "first"), ("last_frame", "last")):
            path = workspace / "artifacts" / "p001" / "e001" / "frames" / f"shot_{shot_no:03d}.{suffix}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{kind}-{shot_no}".encode())
            files[(kind, name)] = path
            app.assets.register(project["id"], kind, name, uri=str(path))
    for shot_no in range(1, 5):
        name = f"e001_shot{shot_no:03d}"
        if shot_no == 4:
            path = (
                workspace / "artifacts" / "p001" / "e001" / "images"
                / "candidate_sets" / "selected" / "candidate_01"
                / "shot_004.keyframe.png")
        else:
            path = workspace / "artifacts" / "p001" / "e001" / "images" / f"shot_{shot_no:03d}.keyframe.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"image-{shot_no}".encode())
        files[("image", name)] = path
        app.assets.register(project["id"], "image", name, uri=str(path))
    canonical_shot4 = (
        workspace / "artifacts" / "p001" / "e001"
        / "images" / "shot_004.keyframe.png")
    canonical_shot4.write_bytes(b"orphan-canonical-shot4")
    other = workspace / "artifacts" / "p001" / "e002" / "frames" / "shot_001.first.png"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"other-episode")
    app.assets.register(project["id"], "first_frame", "e002_shot001", uri=str(other))
    app.close()
    return workspace, files, other, canonical_shot4


def _counts(workspace):
    app = App(workspace)
    try:
        return (
            app.db.query_one("SELECT COUNT(*) AS n FROM documents")["n"],
            app.db.query_one("SELECT COUNT(*) AS n FROM assets")["n"],
        )
    finally:
        app.close()


def test_default_dry_run_does_not_change_documents_assets_or_files(
        episode29_workspace):
    workspace, files, other, canonical_shot4 = episode29_workspace
    before = _counts(workspace)

    report = repair.run_repair(
        workspace, apply=False, quarantine_assets=True)

    assert report["mode"] == "dry-run"
    assert report["document_write"] is False
    assert report["after_assertions"]["passed"] is True
    assert len(report["asset_plan"]) == 11
    orphan = [item for item in report["asset_plan"]
              if item.get("file_only")]
    assert len(orphan) == 1
    assert orphan[0]["move"] is True
    assert orphan[0]["soft_delete"] is False
    assert _counts(workspace) == before
    assert all(path.is_file() for path in files.values())
    assert canonical_shot4.is_file()
    assert other.is_file()


def test_apply_writes_v14_and_exact_authoritative_phase_contracts(
        episode29_workspace):
    workspace, _files, _other, _canonical_shot4 = episode29_workspace

    report = repair.run_repair(workspace, apply=True)

    assert report["document_write"] is True
    app = App(workspace)
    try:
        board, version = app.projects.latest_document(29, "storyboard")
    finally:
        app.close()
    assert version == 14
    assert board["repair_metadata"]["repair_id"] == repair.REPAIR_ID
    assert repair.after_assertions(board)["passed"] is True
    shots = {shot["shot_no"]: shot for shot in board["shots"]}
    assert shots[1]["location"] == "酒店房间内·走廊"
    assert shots[1]["prompt_contract"]["scene"] == "酒店房间内·走廊"
    assert shots[1]["readable_text"]["phases"]["start"]["whitelist"] == [
        "2078年2月21日", "23:00"]
    assert not shots[1]["readable_text"]["phases"]["end"]["required"]
    assert shots[1]["frame_targets"]["first_frame"]["location"] == (
        "酒店房间内")
    assert shots[1]["frame_targets"]["keyframe"]["location"] == (
        "酒店房间外·现代走廊")
    assert shots[1]["frame_targets"]["last_frame"]["location"] == (
        "酒店房间外·现代走廊")
    assert "柳争流" not in shots[1]["frame_targets"]["first_frame"]["state"]
    assert shots[2]["vehicle_topology"]["drive_side"] == "左舵"
    assert shots[2]["frame_targets"]["first_frame"]["characters"] == ["虞寻歌"]
    assert shots[2]["frame_targets"]["first_frame"]["visible_figure_count"] == 2
    assert shots[2]["frame_targets"]["first_frame"]["functional_figures"] == [{
        "name": "小吴", "count": 1,
        "state": "同一名小吴坐在左前驾驶位，双手自然握住方向盘",
        "function": "本镜司机；仅此一具真人身体",
    }]
    assert "驾驶侧车外" in shots[2]["frame_targets"]["last_frame"][
        "functional_figures"][0]["state"]
    assert shots[3]["phase_locations"]["start"].endswith("门外走廊")
    assert shots[3]["frame_targets"]["first_frame"]["characters"] == ["虞寻歌"]
    assert shots[3]["frame_targets"]["first_frame"]["visible_figure_count"] == 1
    assert shots[3]["frame_targets"]["first_frame"]["location"] == (
        "虞家别墅·卧室门外走廊")
    assert shots[3]["frame_targets"]["keyframe"]["location"] == (
        "虞家别墅·虞寻欢卧室")
    assert shots[3]["frame_targets"]["last_frame"]["location"] == (
        "虞家别墅·虞寻欢卧室")
    assert shots[4]["frame_targets"]["keyframe"]["phase"] == "freeze"
    assert shots[4]["readable_text"]["phases"]["freeze"]["whitelist"] == [
        "SS级", "盗神"]
    assert "不可读" in shots[4]["frame_targets"]["last_frame"]["state"]

    shot1_first = copy.deepcopy(shots[1])
    shot1_first["frame_kind"] = "first_frame"
    contract, prompt = compile_shot_prompt(
        shot1_first, location=shots[1]["location"], mode="image",
        references=[
            {"index": 1, "label": "虞寻歌身份图", "role": "identity",
             "character": "虞寻歌"},
            {"index": 2, "label": "柳争流身份图", "role": "identity",
             "character": "柳争流"},
        ])
    assert len(contract["subject"]["actors"]) == 1
    assert contract["subject"]["actors"][0].endswith("虞寻歌")
    assert contract["subject"]["visible_count"] == 1
    assert contract["scene"] == "酒店房间内"
    assert [ref["character"] for ref in contract["references"]] == ["虞寻歌"]
    assert "柳争流" not in prompt
    assert "2078年2月21日" in prompt and "23:00" in prompt

    shot1_last = copy.deepcopy(shots[1])
    shot1_last["frame_kind"] = "last_frame"
    contract, prompt = compile_shot_prompt(
        shot1_last, location=shots[1]["location"], mode="image")
    assert contract["scene"] == "酒店房间外·现代走廊"
    assert "枕边" not in prompt
    assert "取手机" not in prompt

    shot2_first = copy.deepcopy(shots[2])
    shot2_first["frame_kind"] = "first_frame"
    contract, prompt = compile_shot_prompt(
        shot2_first, location=shots[2]["location"], mode="image")
    assert len(contract["subject"]["actors"]) == 1
    assert contract["subject"]["actors"][0].endswith("虞寻歌")
    assert contract["subject"]["visible_count"] == 2
    assert len(contract["subject"]["functional_figures"]) == 1
    assert "左前驾驶位" in prompt

    shot3_first = copy.deepcopy(shots[3])
    shot3_first["frame_kind"] = "first_frame"
    contract, prompt = compile_shot_prompt(
        shot3_first,
        location=shots[3]["location"],
        mode="image",
        references=[
            {"index": 1, "label": "虞寻歌身份图", "role": "identity",
             "character": "虞寻歌"},
            {"index": 2, "label": "虞寻欢身份图", "role": "identity",
             "character": "虞寻欢"},
        ])
    assert contract["scene"] == "虞家别墅·卧室门外走廊"
    assert contract["subject"]["count"] == 1
    assert contract["subject"]["visible_count"] == 1
    assert len(contract["subject"]["actors"]) == 1
    assert contract["subject"]["actors"][0].endswith("虞寻歌")
    assert [ref["character"] for ref in contract["references"]] == ["虞寻歌"]
    assert "虞寻欢" not in contract["subject"]["actors"]
    assert "虞寻欢" not in prompt
    assert "碰杯" not in prompt
    assert "饮尽" not in prompt
    assert "失衡" not in prompt


def test_apply_with_quarantine_is_idempotent_and_isolated(
        episode29_workspace):
    workspace, files, other, canonical_shot4 = episode29_workspace

    first = repair.run_repair(
        workspace, apply=True, quarantine_assets=True)
    second = repair.run_repair(
        workspace, apply=True, quarantine_assets=True)

    assert first["document_write"] is True
    assert len(first["asset_result"]["tombstones"]) == 10
    assert len(first["asset_result"]["moved"]) == 10
    assert second["already_applied"] is True
    assert second["document_write"] is False
    assert second["asset_result"] == {"moved": [], "tombstones": []}

    app = App(workspace)
    try:
        board_rows = app.db.query(
            "SELECT version FROM documents WHERE episode_id=29 "
            "AND kind='storyboard' ORDER BY version")
        assert [row["version"] for row in board_rows][-1] == 14
        assert len(board_rows) == 14
        project = app.projects.get_project("游戏入侵")
        for shot_no in range(1, 5):
            name = f"e001_shot{shot_no:03d}"
            for kind in ("first_frame", "last_frame"):
                row = app.assets.latest(
                    project["id"], kind, name, include_deleted=True)
                meta = app.assets.meta(row)
                assert meta["deleted"] is True
                assert meta["physical_invalid"] is True
                assert meta["reference_eligible"] is False
        for shot_no in (2, 4):
            row = app.assets.latest(
                project["id"], "image", f"e001_shot{shot_no:03d}",
                include_deleted=True)
            assert app.assets.meta(row)["physical_invalid"] is True
        assert app.assets.latest(
            project["id"], "image", "e001_shot001") is not None
        assert app.assets.latest(
            project["id"], "image", "e001_shot003") is not None
        assert app.assets.latest(
            project["id"], "first_frame", "e002_shot001") is not None
    finally:
        app.close()

    # All current first/last files and only shot4's canonical keyframe moved.
    for (kind, name), path in files.items():
        shot_no = int(name[-3:])
        if kind in {"first_frame", "last_frame"} or (
                kind == "image" and shot_no == 4):
            assert not path.exists()
        else:
            assert path.exists()
    assert other.is_file()
    assert not canonical_shot4.exists()
    quarantined = list((workspace / "artifacts" / "quarantine").rglob("*.png"))
    assert len(quarantined) == 10


def test_refuses_any_episode_or_storyboard_outside_audited_scope(
        episode29_workspace):
    workspace, _files, _other, _canonical_shot4 = episode29_workspace
    app = App(workspace)
    try:
        app.projects.save_document(29, "storyboard", _baseline_storyboard())
    finally:
        app.close()

    with pytest.raises(repair.RepairError, match="v13.*v14"):
        repair.run_repair(workspace)
