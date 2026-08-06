"""Seedance 参考图自动选入 + 已锁定角色缺失候选不再永远待生成。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aifos.app import App
from aifos.errors import AifosError


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _lock_all(app, title, number=1):
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    script, _ = app.projects.latest_document(episode["id"], "script")
    for character in script["characters"]:
        app.director.select_character_candidate(
            title, number, character["name"], 1)
    return project, episode


def _preproduce(app, title):
    app.director.produce(title, 1, pause_for_confirm=True)   # 剧本停
    app.director.produce(title, 1, pause_for_confirm=True)   # 选角停
    project, episode = _lock_all(app, title)
    app.director.produce(title, 1, pause_for_confirm=True)   # 预生产停
    out = (app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001")
    return project, episode, out


def test_auto_video_references_include_necessary_images(app):
    """人工未选择时:分镜图/人物最终立绘/场景图自动选入交给 Seedance。"""
    project, episode, out = _preproduce(app, "万妖图录")
    effective = app.director.effective_video_references(episode["id"])
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    assert effective["shots"], "没有任何镜头的参考图集合"
    for shot in storyboard["shots"]:
        entry = effective["shots"][str(shot["shot_no"])]
        assert entry["mode"] == "auto"
        kinds = [item["kind"] for item in entry["items"]]
        assert "image" in kinds, f"镜头{shot['shot_no']}缺分镜图"
        if shot.get("characters"):
            assert "character_identity" in kinds, \
                f"镜头{shot['shot_no']}缺人物最终立绘"
        if entry["spatial_reference_required"]:
            assert entry["spatial_reference_ready"]
            assert "spatial_blocking" in kinds, \
                f"镜头{shot['shot_no']}缺 Seedance 必传空间图"
        assert "scene_art" in kinds, \
            f"镜头{shot['shot_no']}缺统一物理场景母图"
        assert len(entry["items"]) <= 7


def test_manual_selection_overrides_auto(app):
    """人工只调整额外参考，空间图与出场人物最终立绘不可取消。"""
    project, episode, out = _preproduce(app, "万妖图录")
    row = app.assets.latest(project["id"], "image", "e001_shot002")
    app.director.set_video_references(episode["id"], 1, [row["id"]])
    effective = app.director.effective_video_references(episode["id"])
    entry = effective["shots"]["1"]
    assert entry["mode"] == "manual"
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    shot = storyboard["shots"][0]
    identity_ids = {
        app.director._locked_identity(project["id"], name)["id"]
        for name in shot["characters"]}
    manual_ids = [item["asset_id"] for item in entry["items"]
                  if item["kind"] not in {"spatial_blocking", "scene_art"}]
    assert set(manual_ids) == identity_ids | {row["id"]}
    assert any(item["kind"] == "scene_art" for item in entry["items"])
    if entry["spatial_reference_required"]:
        assert entry["items"][0]["kind"] == "spatial_blocking"
    # 清空 = 不再使用额外参考，但硬身份图与空间图仍保留。
    app.director.set_video_references(episode["id"], 1, [])
    effective = app.director.effective_video_references(episode["id"])
    assert effective["shots"]["1"]["mode"] == "manual"
    remaining = effective["shots"]["1"]["items"]
    if effective["shots"]["1"]["spatial_reference_required"]:
        assert remaining[0]["kind"] == "spatial_blocking"
        remaining = remaining[1:]
    assert any(item["kind"] == "scene_art" for item in remaining)
    remaining = [item for item in remaining if item["kind"] != "scene_art"]
    assert {item["asset_id"] for item in remaining} == identity_ids
    # 其他镜头仍是自动
    assert effective["shots"]["2"]["mode"] == "auto"


def test_episode_payload_carries_effective_references(app):
    from aifos.web.server import _episode_payload
    project, episode, out = _preproduce(app, "万妖图录")
    payload = _episode_payload(app, episode["id"])
    shots = payload["video_references_effective"]["shots"]
    first = shots["1"]["items"]
    assert first and all("url" in item for item in first)


def test_locked_character_missing_candidates_not_stuck_pending(app):
    """已锁定角色缺失的候选图不再挂在清单里永远"待生成"。"""
    title = "万妖图录"
    app.director.produce(title, 1, pause_for_confirm=True)
    app.director.produce(title, 1, pause_for_confirm=True)   # 候选已生成
    project, episode = _lock_all(app, title)
    # 模拟历史断点:删掉某角色 2 张候选文件(相当于当年没画完就锁定)
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    removed = 0
    for index in (3, 4):
        row = app.assets.latest(project["id"], "character_candidate",
                                f"{hero}:{index:02d}")
        if row and row["uri"] and Path(row["uri"]).exists():
            Path(row["uri"]).unlink()
            removed += 1
    assert removed == 2
    app.director.produce(title, 1, pause_for_confirm=True)   # 续跑预生产
    out = (app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out / "render_plan.json").read_text(encoding="utf-8"))
    candidates = [i for i in plan["items"]
                  if i["category"] == "character_candidate"]
    stuck = [i for i in candidates if i["status"] == "pending"]
    assert not stuck, f"仍有候选图卡在待生成: {[i['id'] for i in stuck]}"
    # 已锁定角色缺失的槽位直接不在清单里(不算进总进度)
    ids = {i["id"] for i in candidates}
    assert f"candidate:{hero}:3" not in ids
    assert f"candidate:{hero}:4" not in ids


def test_low_quality_shot_image_is_not_a_formal_video_reference(app):
    """低质量试错图留在资产中心，但不得固化进 Seedance 参考链。"""
    project, episode, out = _preproduce(app, "万妖图录")
    row = app.assets.latest(project["id"], "image", "e001_shot001")
    assert row is not None
    # 模拟该分镜图被质量策略归为低质量(试错级)
    app.assets.register(
        project["id"], "image", "e001_shot001", uri=row["uri"],
        meta={"image_quality": "low"}, new_version=True)
    effective = app.director.effective_video_references(episode["id"])
    kinds = [item["kind"] for item in effective["shots"]["1"]["items"]]
    assert "image" not in kinds


@pytest.mark.parametrize("bad_meta", [
    {"reference_eligible": False},
    {"physical_invalid": True},
    {"selection_qc_passed": False},
    {"qc": {
        "physical_logic_checked": True,
        "physical_logic_match": False,
    }},
    {"qc": {
        "critical_failures": ["驾驶座椅缺失，人物悬空坐在车内"],
    }},
])
def test_unfit_asset_meta_is_rejected_by_unified_video_policy(
        app, tmp_path, bad_meta):
    project, _ = app.projects.get_or_create_project("视频参考资格统一门禁")
    image = tmp_path / "bad-reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    row = app.assets.register(
        project["id"], "image", "bad-reference", uri=str(image),
        meta={"image_quality": "high", **bad_meta})

    assert app.director._video_reference_rejection(row)


def test_historical_best_effort_is_allowed_after_current_physical_pass(
        app, tmp_path):
    """历史相对最优标记不能永久误伤已经修好的正式参考图。"""
    project, _ = app.projects.get_or_create_project("修复后参考资格")
    image = tmp_path / "repaired-reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    row = app.assets.register(
        project["id"], "image", "repaired-reference", uri=str(image),
        meta={
            "image_quality": "high",
            "qc": {
                "best_effort_promoted": True,
                "nonblocking_risk": {
                    "best_effort": True,
                    "issues": ["历史候选曾需人工复核"],
                },
                "visual_pass": True,
                "technical_quality_pass": True,
                "physical_logic_match": True,
                "spatial_logic_match": True,
                "critical_failures": [],
            },
        })

    assert app.director._video_reference_rejection(row) == ""


def test_auto_reference_skips_physical_failure_and_accepts_clean_new_version(
        app):
    """旧问题图仍可预览；同名干净新版本生成后自动恢复正式参考资格。"""
    project, episode, out = _preproduce(app, "万妖图录")
    original = app.assets.latest(project["id"], "image", "e001_shot001")
    bad = app.assets.register(
        project["id"], "image", "e001_shot001", uri=original["uri"],
        meta={
            "image_quality": "high",
            "qc": {
                "best_effort_promoted": True,
                "physical_logic_match": False,
                "issues": ["手机屏幕朝向与人物视线相反"],
            },
        }, new_version=True)

    effective = app.director.effective_video_references(episode["id"])
    ids = {item["asset_id"] for item in effective["shots"]["1"]["items"]}
    assert bad["id"] not in ids
    assert app.assets.get(bad["id"]) is not None, "问题图不应从资产/UI删除"

    fixed = app.assets.register(
        project["id"], "image", "e001_shot001", uri=original["uri"],
        meta={"image_quality": "high", "reference_eligible": True},
        new_version=True)
    effective = app.director.effective_video_references(episode["id"])
    ids = {item["asset_id"] for item in effective["shots"]["1"]["items"]}
    assert fixed["id"] in ids


def test_manual_video_reference_rejects_known_physical_failure(app):
    """人工点击也不能绕过与自动装配相同的正式参考资格门禁。"""
    project, episode, out = _preproduce(app, "万妖图录")
    original = app.assets.latest(project["id"], "image", "e001_shot002")
    bad = app.assets.register(
        project["id"], "image", "e001_shot002", uri=original["uri"],
        meta={
            "image_quality": "high",
            "physical_invalid": True,
            "qc": {"issues": ["安全带没有经过肩部与胸前"]},
        }, new_version=True)

    with pytest.raises(AifosError, match="物理或空间逻辑错误"):
        app.director.set_video_references(
            episode["id"], 1, [bad["id"]])

    # 升级前已写进文档的手选记录也必须在读取时被隔离，不能只防新点击。
    app.projects.save_document(episode["id"], "video_references", {
        "schema": "aifos.video-references/v1",
        "shots": {"1": [{
            "asset_id": bad["id"], "kind": bad["kind"],
            "name": bad["name"], "version": bad["version"],
        }]},
    })
    effective = app.director.effective_video_references(episode["id"])
    ids = {item["asset_id"] for item in effective["shots"]["1"]["items"]}
    assert bad["id"] not in ids


@pytest.mark.parametrize(("phase", "first_source", "last_source"), [
    ("start", "keyframe", None),
    ("end", "generated", "keyframe"),
    ("freeze", "generated", None),
])
def test_keyframe_reuse_matches_authored_boundary_phase(
        app, tmp_path, phase, first_source, last_source):
    """end/freeze 关键图绝不能再被适配器误当成本镜首帧。"""
    project, _ = app.projects.get_or_create_project("关键图边界相位")
    keyframe = tmp_path / f"keyframe-{phase}.png"
    keyframe.write_bytes(b"KEYFRAME-" + phase.encode())
    row = app.assets.register(
        project["id"], "image", f"shot-{phase}", uri=str(keyframe),
        meta={"image_quality": "high"})
    payload = {
        "reference_images": [], "asset_matches": [],
        "frame_kind": "frames", "frame_targets": {
            "keyframe": {"phase": phase, "state": "明确静态状态"},
        },
    }
    shot = {"frame_targets": payload["frame_targets"]}

    bound = app.director._bind_keyframe_for_frames(payload, shot, row)
    if phase == "start":
        assert bound == "start"
        assert payload["image_uri"] == str(keyframe)
    elif phase == "end":
        assert bound == "end"
        assert "image_uri" not in payload
        assert payload["keyframe_last_uri"] == str(keyframe)
    else:
        assert bound == ""
        assert "image_uri" not in payload
        assert "keyframe_last_uri" not in payload

    first = tmp_path / f"{phase}.first.png"
    last = tmp_path / f"{phase}.last.png"
    first.write_bytes(b"GENERATED-FIRST")
    last.write_bytes(b"GENERATED-LAST")
    result = SimpleNamespace(data={
        "first": str(first), "last": str(last),
        "first_source": "generated",
    })
    app.director._apply_keyframe_boundary_to_frame_result(payload, result)

    assert result.data["first_source"] == first_source
    assert result.data.get("last_source") == last_source
    assert first.read_bytes() == (
        keyframe.read_bytes() if phase == "start" else b"GENERATED-FIRST")
    assert last.read_bytes() == (
        keyframe.read_bytes() if phase == "end" else b"GENERATED-LAST")
    assert any(
        item["uri"] == str(keyframe)
        for item in payload["reference_manifest"])


def test_previous_tail_beats_start_keyframe_for_continuous_scene(
        app, tmp_path):
    """同场连续镜头不得用本镜 start 图覆盖上一镜真实尾帧。"""
    project, _ = app.projects.get_or_create_project("帧链优先级")
    keyframe = tmp_path / "start.png"
    keyframe.write_bytes(b"KEYFRAME-START")
    previous_tail = tmp_path / "previous-tail.png"
    previous_tail.write_bytes(b"PREVIOUS-TAIL")
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(previous_tail.read_bytes())
    last.write_bytes(b"GENERATED-LAST")
    row = app.assets.register(
        project["id"], "image", "shot-start", uri=str(keyframe),
        meta={"image_quality": "high"})
    payload = {"frame_kind": "frames", "frame_targets": {
        "keyframe": {"phase": "start", "state": "开始"},
    }}
    app.director._bind_keyframe_for_frames(
        payload, {"frame_targets": payload["frame_targets"]}, row)
    payload["chain_first_uri"] = str(previous_tail)
    result = SimpleNamespace(data={
        "first": str(first), "last": str(last),
        "first_source": "previous_tail",
    })

    app.director._apply_keyframe_boundary_to_frame_result(payload, result)

    assert result.data["first_source"] == "previous_tail"
    assert first.read_bytes() == b"PREVIOUS-TAIL"


def test_reset_manual_selection_falls_back_to_auto(app):
    """人工选择可一键撤销,回落自动选入。"""
    project, episode, out = _preproduce(app, "万妖图录")
    app.director.set_video_references(episode["id"], 1, [])   # 人工清空
    assert app.director.effective_video_references(
        episode["id"])["shots"]["1"]["mode"] == "manual"
    app.director.set_video_references(episode["id"], 1, [], reset=True)
    effective = app.director.effective_video_references(episode["id"])
    assert effective["shots"]["1"]["mode"] == "auto"
    assert any(item["kind"] == "image"
               for item in effective["shots"]["1"]["items"])


def test_historical_wrong_character_manual_reference_is_filtered(app):
    """升级前保存的错角色参考图也不能继续偷偷送入 Seedance。"""
    project, episode, out = _preproduce(app, "万妖图录")
    wrong = out / "wrong-character.png"
    wrong.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    row = app.assets.register(
        project["id"], "character_art", "未出场角色",
        uri=str(wrong), meta={
            "character": "未出场角色",
            "image_quality": "high",
        })
    app.projects.save_document(episode["id"], "video_references", {
        "schema": "aifos.video-references/v1",
        "shots": {"1": [{
            "asset_id": row["id"], "kind": row["kind"],
            "name": row["name"], "version": row["version"],
        }]},
    })
    effective = app.director.effective_video_references(episode["id"])
    ids = {item["asset_id"] for item in effective["shots"]["1"]["items"]}
    assert row["id"] not in ids
    assert all(item["binding"] for item in
               effective["shots"]["1"]["items"])
