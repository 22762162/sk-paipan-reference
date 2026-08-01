"""AI 质检中心测试:各检查项与自动重跑联动。"""

import base64
import shutil
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from aifos.app import App
from aifos.config import Config
from aifos.errors import AifosError
from aifos.qc_center import QcCenter
from aifos.selection_mode import build_candidate_set_version

SCRIPT = {
    "project_title": "测试剧",
    "episode_number": 1,
    "logline": "平平无奇的一集",
    "characters": [{"name": "阿甲", "role": "主角"},
                   {"name": "阿乙", "role": "同伴"}],
    "scenes": [{
        "scene_no": 1,
        "location": "空谷",
        "characters": ["阿甲", "阿乙"],
        "action": "两人对话",
        "lines": [
            {"character": "阿甲", "dialogue": "你好"},
            {"character": "阿乙", "dialogue": "再见"},
        ],
    }],
}

STORYBOARD = {"shots": [
    {"shot_no": 1, "scene_no": 1, "duration": 2.5,
     "characters": ["阿甲", "阿乙"], "prompt": "p1"},
    {"shot_no": 2, "scene_no": 1, "duration": 3.0,
     "characters": ["阿甲"], "prompt": "p2"},
]}


def _qc(overrides=None):
    data = Config.load("/nonexistent", overrides=overrides or {})
    return QcCenter(data)


def _ok_ctx(tmp_path):
    files = {}
    for name in ["v1", "v2", "a1", "a2"]:
        path = tmp_path / f"{name}.json"
        path.write_text("{}", encoding="utf-8")
        files[name] = str(path)
    return {
        "cast": ["阿甲", "阿乙"],
        "videos": [{"shot_no": 1, "uri": files["v1"]},
                   {"shot_no": 2, "uri": files["v2"]}],
        "voices": [{"line_no": 1, "uri": files["a1"]},
                   {"line_no": 2, "uri": files["a2"]}],
        "subtitles": [{"line_no": 1, "text": "你好"},
                      {"line_no": 2, "text": "再见"}],
    }


def test_clean_episode_passes(tmp_path):
    report = _qc().run(SCRIPT, STORYBOARD, _ok_ctx(tmp_path))
    assert report["score"] == 100
    assert report["passed"]
    assert report["issues"] == []


def test_missing_video_flags_rerun(tmp_path):
    ctx = _ok_ctx(tmp_path)
    ctx["videos"] = [ctx["videos"][0]]  # 镜头2视频缺失
    report = _qc().run(SCRIPT, STORYBOARD, ctx)
    assert not report["passed"] or report["score"] < 100
    assert report["rerun_shots"] == [2]


def test_missing_voice_flags_rerun(tmp_path):
    ctx = _ok_ctx(tmp_path)
    ctx["voices"] = [ctx["voices"][0]]
    report = _qc().run(SCRIPT, STORYBOARD, ctx)
    assert report["rerun_lines"] == [2]


def test_unknown_character_is_error(tmp_path):
    ctx = _ok_ctx(tmp_path)
    ctx["cast"] = ["阿甲"]  # 阿乙未登记
    report = _qc().run(SCRIPT, STORYBOARD, ctx)
    checks = {i["check"] for i in report["issues"]}
    assert "character_consistency" in checks
    # 角色不一致不可通过重跑镜头修复
    assert report["rerun_shots"] == []


def test_sensitive_word_detected(tmp_path):
    qc = _qc({"qc": {"sensitive_words": ["再见"]}})
    report = qc.run(SCRIPT, STORYBOARD, _ok_ctx(tmp_path))
    assert any(i["check"] == "sensitive" for i in report["issues"])


def test_long_subtitle_warns(tmp_path):
    ctx = _ok_ctx(tmp_path)
    ctx["subtitles"][0]["text"] = "超" * 40
    report = _qc().run(SCRIPT, STORYBOARD, ctx)
    assert any(i["check"] == "subtitle" and i["severity"] == "warn"
               for i in report["issues"])


def test_integrated_audio_requires_runtime_evidence():
    ctx = {
        "voice_mode": "jimeng_builtin", "lip_sync": True,
        "videos": [{"provider": "jimeng", "uri": "https://cdn.x/v.mp4"}],
    }
    issues = QcCenter._check_integrated_audio(ctx)
    assert issues and issues[0]["check"] == "integrated_audio"
    ctx["voice_carried"] = True
    assert QcCenter._check_integrated_audio(ctx) == []


def test_discontinuous_shots_error(tmp_path):
    bad = {"shots": [dict(STORYBOARD["shots"][0], shot_no=1),
                     dict(STORYBOARD["shots"][1], shot_no=5)]}
    report = _qc().run(SCRIPT, bad, _ok_ctx(tmp_path))
    assert any(i["check"] == "continuity" for i in report["issues"])


def test_preview_qc_bypass_skips_image_and_total_qc(tmp_path):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"preview_qc_bypass": True}})
    try:
        project, _ = app.projects.get_or_create_project("预览片测试")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root,
        }
        app.director.qc.run = lambda *_args, **_kwargs: pytest.fail(
            "预览模式不应调用总质检")

        assert app.director._image_qc_enabled() is False
        assert app.director._prompt_review_enabled() is False
        app.director.router.review_image_prompt = (
            lambda *_args, **_kwargs: pytest.fail(
                "预览模式不应调用生成前提示词审核"))
        assert app.director._review_image_tasks(ctx, [{
            "capability": "image", "payload": {"prompt": "测试"},
            "item_id": "shot:1", "sub_dir": "images", "tag": 1,
        }]) == []
        result = app.director._stage_qc(ctx)

        assert result["passed"] is True
        assert result["formal_passed"] is False
        assert result["preview_only"] is True
        assert ctx["qc_report"]["bypassed_checks"] == [
            "image_qc", "frames_qc", "video_qc",
            "content_review", "delivery_verifier"]
        assert (out_root / "qc_report.json").exists()
        assert ctx["video_qc_report"]["passed"] is False
    finally:
        app.close()


def test_director_autonomy_mode_is_final_composite_not_preview(tmp_path):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"director_autonomy_mode": True}})
    try:
        project, _ = app.projects.get_or_create_project("导演自主成片测试")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root,
        }

        assert app.director._image_qc_enabled() is False
        assert app.director._prompt_review_enabled() is False
        task = {
            "capability": "image",
            "payload": {"prompt": "使用冻结稿直接生成"},
            "item_id": "shot:1",
        }
        assert app.director._review_image_tasks(ctx, [task]) == []
        assert task["payload"]["director_autonomy_mode"] is True
        assert task["payload"]["prompt_review"]["status"] == \
            "not_applicable_director_autonomy"
        task["payload"].pop("prompt_review")
        assert app.director.router.review_image_prompt(
            "image", task["payload"], out_root) is None
        assert task["payload"]["prompt_review"]["approved"] is False
        dispatch = app.director._build_dispatch_contract(
            {"item_id": "shot:1", "capability": "image", "payload": {
                "prompt": "冻结镜头直接生成",
                "director_autonomy_mode": True,
                "prompt_review": {
                    "status": "not_applicable_director_autonomy"},
                "prompt_contract": {},
            }},
            {"category": "shot_image"})
        assert dispatch["passed"] is True
        result = app.director._stage_qc(ctx)

        assert result["passed"] is True
        assert result["preview_only"] is False
        assert result["inspection_waived"] is True
        assert result["final_output_allowed"] is True
        assert ctx["qc_report"]["schema"] == \
            "aifos.director-autonomy/v1"
        assert ctx["qc_report"]["formal_passed"] is False
        assert ctx["video_qc_report"]["inspection_waived"] is True
    finally:
        app.close()


def test_director_autonomy_skips_storyboard_repair_gates(
        tmp_path, monkeypatch):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"director_autonomy_mode": True}})
    try:
        monkeypatch.setattr(
            "aifos.director.preflight_storyboard",
            lambda *_args, **_kwargs: pytest.fail(
                "导演自主模式不应执行分镜可拍性复检"))
        ctx = {"storyboard": {}, "blocking": {}}

        shootability = app.director._preflight_storyboard_shootability(ctx)
        temporal = app.director._preflight_temporal_previz(ctx)

        assert shootability["repair_skipped"] == "director_autonomy"
        assert temporal["repair_skipped"] == "director_autonomy"
        assert shootability["inspection_waived"] is True
        assert temporal["inspection_waived"] is True
    finally:
        app.close()


def test_director_autonomy_selects_best_video_references_once(tmp_path):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"director_autonomy_mode": True}})
    try:
        rows = [
            {"id": 1, "name": "空间调度图", "kind": "spatial_blocking"},
            *[
                {"id": index + 2, "name": f"人物{index}",
                 "kind": "character_art"}
                for index in range(3)
            ],
            *[
                {"id": index + 5, "name": f"核心道具{index}",
                 "kind": "prop_art"}
                for index in range(4)
            ],
        ]

        selected = app.director._director_select_video_reference_rows(
            rows, 13)

        assert len(selected) == 7
        assert all(row["kind"] != "spatial_blocking" for row in selected)
        assert [row["name"] for row in selected] == [
            "人物0", "人物1", "人物2",
            "核心道具0", "核心道具1", "核心道具2", "核心道具3"]
    finally:
        app.close()


@pytest.mark.skipif(
    not (shutil.which("ffmpeg")
         or (Path.home() / ".local/bin/ffmpeg").exists()),
    reason="本机无 ffmpeg")
def test_director_autonomy_uses_frames_when_video_model_returns_nothing(
        tmp_path):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"director_autonomy_mode": True}})
    try:
        project, _ = app.projects.get_or_create_project("导演技术镜头测试")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        out_root = tmp_path / "artifacts"
        frames_dir = out_root / "frames"
        frames_dir.mkdir(parents=True)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
            "QVQIHWP4zwAAAgEBAGTz9rQAAAAASUVORK5CYII=")
        first = frames_dir / "shot_013.first.png"
        last = frames_dir / "shot_013.last.png"
        first.write_bytes(png)
        last.write_bytes(png)
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root,
        }
        task = {"payload": {
            "shot_no": 13, "duration": 1.5,
            "first": str(first), "last": str(last),
            "video_quality": "medium", "forbid_subtitles": True,
        }}

        result = app.director._technical_frame_video_result(
            ctx, task, RuntimeError("generation failed"))

        assert result.cost == 0
        assert result.data["technical_frame_fallback"] is True
        assert result.data["audio_in_video"] is True
        assert Path(result.uri).exists()
        assert Path(result.uri).stat().st_size > 0
    finally:
        app.close()


def test_preview_qc_bypass_only_waives_frames_qc_gate(
        tmp_path, monkeypatch):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"preview_qc_bypass": True}})
    try:
        project, _ = app.projects.get_or_create_project("预览门禁测试")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        out_root = tmp_path / "artifacts"
        out_root.mkdir()
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "out_root": out_root, "script": {}, "storyboard": {"shots": []},
            "continuity": {}, "text_assets": {}, "frames": [],
            "production_profile": {}, "blocking": {},
            "quality_policy": {}, "character_asset_policy": {},
            "cast_selection": {},
        }

        monkeypatch.setattr(
            "aifos.director.build_preflight",
            lambda *_args, **_kwargs: {
                "passed": False, "units": 0,
                "gates": [{
                    "id": "frames", "label": "首尾帧",
                    "passed": False, "severity": "block"}, {
                    "id": "people", "label": "人物数量",
                    "passed": True, "severity": "block"}],
            })
        result = app.director._stage_preflight(ctx)
        assert result["passed"] is True
        assert result["formal_passed"] is False
        assert result["preview_only"] is True

        monkeypatch.setattr(
            "aifos.director.build_preflight",
            lambda *_args, **_kwargs: {
                "passed": False, "units": 0,
                "gates": [{
                    "id": "people", "label": "人物数量",
                    "passed": False, "severity": "block"}],
            })
        with pytest.raises(AifosError, match="人物数量"):
            app.director._stage_preflight(ctx)
    finally:
        app.close()


def test_auto_rerun_repairs_missing_video(tmp_path):
    """删除一个镜头视频后重跑质检阶段,导演中心应自动重生成并通过。"""
    app = App(tmp_path / "ws")
    try:
        summary = app.director.produce("重跑剧", 1)
        assert summary["status"] == "awaiting_cast"
        project = app.projects.get_project("重跑剧")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                "重跑剧", 1, character["name"], 1)
        summary = app.director.produce("重跑剧", 1)
        assert summary["status"] == "done"
        script, _ = app.projects.latest_document(episode["id"], "script")
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        continuity, _ = app.projects.latest_document(
            episode["id"], "continuity")
        preflight, _ = app.projects.latest_document(
            episode["id"], "preflight")
        out_root = app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
        # 构造质检上下文,人为删除镜头1的视频
        import pathlib
        video_dir = out_root / "videos"
        victims = sorted(video_dir.glob("shot_001*"))
        for v in victims:
            v.unlink()
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "out_root": out_root,
            "script": script,
            "storyboard": storyboard,
            "continuity": continuity,
            "preflight": preflight,
            "production_profile": storyboard["profile"],
            "cast": [c["name"] for c in script["characters"]],
            "images": [], "frames": [], "videos": [], "voices": [],
            "subtitles": [],
            "voice_mode": "jimeng_builtin", "lip_sync": True,
            "final_uri": summary["outputs"]["final"], "edit_data": {},
            "aspect": "9:16",
            "dims": {"width": 1080, "height": 1920},
        }
        # 重建产物索引(videos 索引指向已删除文件 → 触发重跑)
        ctx["frames"] = [
            {"shot_no": s["shot_no"],
             "first": str(out_root / "frames" / f"shot_{s['shot_no']:03d}.first.svg"),
             "last": str(out_root / "frames" / f"shot_{s['shot_no']:03d}.last.svg")}
                for s in storyboard["shots"]]
        ctx["images"] = [
            {"shot_no": s["shot_no"],
             # 关键帧进入四候选自动选优后，正式 URI 来自资产中心，
             # 不再假定旧版固定 canonical 文件名。
             "uri": app.assets.latest(
                 project["id"], "image",
                 app.director._shot_name(ctx, s["shot_no"]))["uri"]}
            for s in storyboard["shots"]]
        ctx["videos"] = [
            {"shot_no": s["shot_no"],
             "uri": str(video_dir / f"shot_{s['shot_no']:03d}.video.json"),
             "duration": s["duration"]}
                for s in storyboard["shots"]]
        app.director._task_cost = 0.0
        app.director._task_providers = set()
        result = app.director._stage_qc(ctx)
        assert result["passed"]
        assert pathlib.Path(ctx["videos"][0]["uri"]).exists()
    finally:
        app.close()


def _candidate_result(tmp_path, *, expected, available, passed=False,
                      revision=1, cost=0.0):
    version = build_candidate_set_version(
        episode_id="episode-test", shot_no=1, contract_revision=revision,
        candidate_revision=revision, prompt=f"prompt-{revision}",
        reference_manifest=[])
    candidates = []
    for index in range(1, available + 1):
        uri = tmp_path / f"r{revision}-candidate-{index}.svg"
        uri.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>",
                       encoding="utf-8")
        candidates.append({
            "candidate_index": index,
            "candidate_id": f"{version.token}#{index}",
            "candidate_set_token": version.token,
            "uri": str(uri),
            "passed": bool(passed),
            "score": float(100 - index),
            "issues": [] if passed else ["主体动作与镜头合同不一致"],
            "ranking_unavailable": False,
        })
    group = {
        "schema": "aifos.shot-candidate-group/v1",
        "version": asdict(version),
        "candidate_set_id": f"set-{revision}",
        "candidate_set_token": version.token,
        "contract_revision": revision,
        "candidate_revision": revision,
        "candidate_count": available,
        "expected_count": expected,
        "selection_required": available > 0,
        "complete": available > 0,
        "slot_complete": available == expected,
        "technical_incomplete": available == 0,
        "candidate_errors": [],
        "same_prompt": True,
        "same_references": True,
        "ranking_unavailable": False,
        "recommended_candidate_index": 1 if available else None,
        "candidates": candidates,
    }
    return SimpleNamespace(
        provider="mock", model="mock", cost=cost, fallbacks=[], uri="",
        data={"candidate_group": group}, qc={"passed": bool(passed)})


def test_partial_candidate_group_ai_selects_and_only_zero_is_incomplete(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        partial = _candidate_result(
            tmp_path, expected=4, available=3, passed=True)
        selected = app.director._ai_promote_generated_candidate_group(
            partial)
        assert selected.uri.endswith("r1-candidate-1.svg")
        assert selected.data["candidate_group"]["selection"]["source"] == "ai"
        assert app.director._candidate_group_technical_incomplete(
            selected) is False

        empty = _candidate_result(
            tmp_path, expected=4, available=0, passed=False)
        assert app.director._candidate_group_technical_incomplete(empty) is True
        assert app.director._ai_promote_generated_candidate_group(empty).uri == ""
    finally:
        app.close()


def test_all_four_explicit_failures_trigger_one_three_candidate_repair(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        initial = _candidate_result(
            tmp_path, expected=4, available=4, passed=False,
            revision=1, cost=4.0)
        repaired = _candidate_result(
            tmp_path, expected=3, available=3, passed=False,
            revision=2, cost=3.0)
        queue = [initial, repaired]
        calls = []

        def generate(*_args, **_kwargs):
            calls.append(1)
            return queue.pop(0)

        def escalate(report, *_args, **_kwargs):
            report = dict(report)
            report["codex_escalation"] = {
                "instruction_to_aifos": "只修正主体动作，不改变人物场景构图"}
            return report, 0.5

        monkeypatch.setattr(
            app.director, "_generate_image_gacha", generate)
        monkeypatch.setattr(
            app.director, "_escalate_failed_image_to_codex", escalate)
        result = app.director._generate_shot_candidate_group(
            "image", {
                "_episode_id": "episode-test", "shot_no": 1,
                "_candidate_revision": 1, "_contract_revision": 1,
                "prompt": "主体执行镜头动作",
            }, tmp_path, None, {})

        assert len(calls) == 2
        assert result.uri.endswith("r2-candidate-1.svg")
        group = result.data["candidate_group"]
        assert group["repair_batch"] is True
        assert group["expected_count"] == 3
        assert group["initial_candidate_count"] == 4
        assert group["initial_all_failed"] is True
        assert "只修正主体动作" in group["repair_instruction"]
        assert result.cost == 7.5
    finally:
        app.close()
