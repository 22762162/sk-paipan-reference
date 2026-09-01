"""首尾帧修订被审核阻断时的保留现状兜底(12星座 frames:1 熔断回归)。

事故:j60 frames 阶段,镜头1的首尾帧早已生成且 visual_pass=true,
一次打磨性修订(Codex 升级指令要求补足当前不存在的参考职责)被生成
前审核阻断,整轮 run 被拖死。修复后:阻断且有过检成品帧时保留现状
继续生产,修订原因留痕可见。
"""

import pytest

from aifos.app import App

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _ctx_with_passed_pair(app, tmp_path, shot_no=1, qc_passed=True,
                          quality="high"):
    project, _ = app.projects.get_or_create_project("帧兜底测试")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    name = f"e{episode['number']:03d}_shot{shot_no:03d}"
    artifacts = tmp_path / "ws" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    for kind in ("first_frame", "last_frame"):
        target = artifacts / f"{name}_{kind}.png"
        target.write_bytes(PNG)
        app.assets.register(
            project["id"], kind, name, uri=str(target),
            meta={"qc_passed": qc_passed, "image_quality": quality})
    return {
        "project": {"id": project["id"]},
        "episode": {"id": episode["id"], "number": episode["number"]},
        "frames": [],
        "out_root": artifacts,
    }


def _task(shot_no=1):
    return {
        "item_id": f"frames:{shot_no}", "tag": shot_no, "scene": "g1",
        "capability": "frames",
        "payload": {"quality_decision": {"level": "medium"}},
    }


def test_revision_blocked_keeps_passed_pair(app, tmp_path):
    ctx = _ctx_with_passed_pair(app, tmp_path)
    kept = app.director._frames_keep_current_pair(
        ctx, _task(), "真实图片已被阻止：无法补足修订要求的参考职责")
    assert kept is not None
    assert kept["shot_no"] == 1
    assert kept["qc_passed"] is True
    assert kept["revision_blocked_keep_current"] is True
    assert "审核阻断" in kept["revision_note"]
    # 帧对文件存在
    assert kept["first"].endswith(".png") and kept["last"].endswith(".png")


def test_no_passed_pair_returns_none(app, tmp_path):
    """没有过检成品帧时维持原失败路径,不放行。"""
    ctx = _ctx_with_passed_pair(app, tmp_path, qc_passed=False)
    assert app.director._frames_keep_current_pair(ctx, _task(), "阻断") is None


def test_missing_pair_returns_none(app, tmp_path):
    project, _ = app.projects.get_or_create_project("空剧集")
    episode, _ = app.projects.get_or_create_episode(project["id"], 2)
    ctx = {"project": {"id": project["id"]},
           "episode": {"id": episode["id"], "number": 2},
           "frames": []}
    assert app.director._frames_keep_current_pair(
        ctx, _task(shot_no=9), "阻断") is None


def test_instruction_parser_bridges_labelled_reference_removal(app):
    """「移除标示"S1·0人"的参考图1」这类带修饰语的指令也要解析到。"""
    payload = {
        "reference_manifest": [
            {"index": 1, "role": "spatial_scene_clean",
             "uri": "/ws/clean.png"},
            {"index": 2, "role": "scene", "uri": "/ws/scene.png"},
        ],
    }
    actions = app.director._codex_instruction_reference_actions(
        '先从本轮参考清单移除标示“S1·0人”的参考图1；参考图2保留。',
        payload)
    removes = [a for a in actions if a["action"] == "remove"]
    assert [a["target_index"] for a in removes] == [1]
    assert removes[0]["_codex_explicit"] is True
