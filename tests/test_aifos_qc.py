"""AI 质检中心测试:各检查项与自动重跑联动。"""

import base64
import copy
import shutil
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from aifos.app import App
from aifos.config import Config
from aifos.director import Director
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
        dispatch = app.director._build_dispatch_contract(
            {"item_id": "shot:1", "capability": "image", "payload": {
                "prompt": "免检恢复镜头",
                "director_autonomy_mode": True,
                "prompt_contract": {},
            }},
            {"category": "shot_image"})
        assert dispatch["passed"] is True
        assert dispatch["prompt_review"]["status"] == \
            "not_applicable_preview_qc_bypass"
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


def test_director_autonomy_still_runs_storyboard_physics_preflight(
        tmp_path, monkeypatch):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {"director_autonomy_mode": True}})
    try:
        calls = []

        def shootability(*_args, **_kwargs):
            calls.append("shootability")
            return {"passed": True, "issues": [], "by_shot": {}}

        def temporal(*_args, **_kwargs):
            calls.append("temporal")
            return {"passed": True, "issues": [], "by_shot": {}}

        monkeypatch.setattr(
            "aifos.director.preflight_storyboard", shootability)
        monkeypatch.setattr("aifos.previz_checks.previz_report", temporal)
        ctx = {"storyboard": {}, "blocking": {}}

        shootability = app.director._preflight_storyboard_shootability(ctx)
        temporal = app.director._preflight_temporal_previz(ctx)

        assert shootability == {"issues": [], "repaired": 0}
        assert temporal == {"issues": [], "repaired": 0}
        assert calls == ["shootability", "temporal"]
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


def test_director_autonomy_records_dialogue_mapping_risk_without_stopping(
        tmp_path, monkeypatch):
    app = App(
        tmp_path / "ws",
        config_overrides={"defaults": {
            "preview_qc_bypass": True,
            "director_autonomy_mode": True,
        }})
    try:
        project, _ = app.projects.get_or_create_project("导演自动台词映射")
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
                "passed": False, "units": 4,
                "gates": [{
                    "id": "dialogue", "label": "台词与语速",
                    "passed": False, "severity": "block",
                }, {
                    "id": "people", "label": "人物数量",
                    "passed": True, "severity": "block",
                }],
            })

        result = app.director._stage_preflight(ctx)

        assert result["passed"] is True
        assert result["formal_passed"] is False
        assert result["preview_only"] is False
        bypass = ctx["preflight"]["preview_qc_bypass"]
        assert bypass["bypassed_gates"] == ["dialogue"]
        assert "dialogue_editorial_mapping" in bypass["scope"]
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


def test_all_four_explicit_failures_trigger_four_candidate_repair_until_pass(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        initial = _candidate_result(
            tmp_path, expected=4, available=4, passed=False,
            revision=1, cost=4.0)
        repaired = _candidate_result(
            tmp_path, expected=4, available=4, passed=True,
            revision=2, cost=4.0)
        queue = [initial, repaired]
        calls = []

        def generate(_capability, payload, _out_dir, _cancel, qc_spec):
            calls.append((copy.deepcopy(payload), copy.deepcopy(qc_spec)))
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
        assert group["expected_count"] == 4
        assert group["generation_round"] == 2
        assert group["max_candidate_rounds"] == 10
        assert len(group["candidate_round_history"]) == 2
        assert result.cost == 8.5
    finally:
        app.close()


def test_candidate_repair_stops_immediately_when_round_seven_has_pass(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        queue = [
            _candidate_result(
                tmp_path, expected=4, available=4,
                passed=round_no == 7, revision=round_no, cost=4.0)
            for round_no in range(1, 8)
        ]
        calls = []
        reference_rounds = []

        def generate(_capability, payload, _out_dir, _cancel, qc_spec):
            calls.append(copy.deepcopy(payload))
            return queue.pop(0)

        def escalate(report, *_args, **_kwargs):
            report = dict(report)
            round_no = int(report["consecutive_failures"])
            report["codex_escalation"] = {
                "instruction_to_aifos":
                    f"第{round_no + 1}轮改用清晰可拍的静态构图"}
            return report, 0.1

        def adjust(_payload, _qc_spec, _diagnostics, **_kwargs):
            reference_rounds.append(len(reference_rounds) + 1)
            return {"applied": [], "skipped": []}

        monkeypatch.setattr(app.director, "_generate_image_gacha", generate)
        monkeypatch.setattr(
            app.director, "_escalate_failed_image_to_codex", escalate)
        monkeypatch.setattr(
            app.director, "_apply_image_reference_adjustments", adjust)

        result = app.director._generate_shot_candidate_group(
            "image", {
                "_episode_id": "episode-test", "shot_no": 7,
                "_candidate_revision": 1, "_contract_revision": 1,
                "prompt": "第1轮镜头合同",
            }, tmp_path, None, {})

        assert len(calls) == 7
        assert len(reference_rounds) == 6
        assert result.qc["passed"] is True
        group = result.data["candidate_group"]
        assert group["generation_round"] == 7
        assert group["round_status"] == "qualified"
        assert group["total_generated_candidates"] == 28
        assert len(group["candidate_round_history"]) == 7
        assert queue == []
    finally:
        app.close()


def test_candidate_repair_caps_at_ten_rounds_and_promotes_best_without_gate(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        queue = [
            _candidate_result(
                tmp_path, expected=4, available=4, passed=False,
                revision=round_no, cost=4.0)
            for round_no in range(1, 11)
        ]
        calls = []

        def generate(_capability, payload, _out_dir, _cancel, _qc_spec):
            calls.append(copy.deepcopy(payload))
            return queue.pop(0)

        def escalate(report, *_args, **_kwargs):
            report = dict(report)
            round_no = int(report["consecutive_failures"])
            report["codex_escalation"] = {
                "instruction_to_aifos":
                    f"第{round_no + 1}轮替换为另一种物理可拍方案"}
            return report, 0.1

        monkeypatch.setattr(app.director, "_generate_image_gacha", generate)
        monkeypatch.setattr(
            app.director, "_escalate_failed_image_to_codex", escalate)

        result = app.director._generate_shot_candidate_group(
            "image", {
                "_episode_id": "episode-test", "shot_no": 10,
                "_candidate_revision": 1, "_contract_revision": 1,
                "prompt": "高难镜头合同",
            }, tmp_path, None, {})

        assert len(calls) == 10
        assert queue == []
        assert [row["_candidate_generation_round"] for row in calls] == \
            list(range(1, 11))
        assert all(row["_gacha_pulls_override"] == 4 for row in calls)
        assert result.uri
        assert result.qc["best_effort_promoted"] is True
        group = result.data["candidate_group"]
        assert group["round_status"] == "exhausted"
        assert group["repair_exhausted"] is True
        assert group["max_candidate_rounds"] == 10
        assert group["total_generated_candidates"] == 40
        assert len(group["candidate_round_history"]) == 10
    finally:
        app.close()


def test_candidate_repair_resume_keeps_round_cap_history_and_prior_best(
        tmp_path, monkeypatch):
    """A restart in round 7 must not redraw rounds 1-6 or lose their winner."""
    app = App(tmp_path / "ws")
    try:
        history = []
        best_result = None
        for round_no in range(1, 7):
            prior = _candidate_result(
                tmp_path, expected=4, available=4, passed=False,
                revision=round_no, cost=4.0)
            if round_no == 2:
                prior.data["candidate_group"]["candidates"][0][
                    "score"] = 250.0
            promoted = app.director._ai_promote_generated_candidate_group(
                prior)
            group = copy.deepcopy(promoted.data["candidate_group"])
            group["generation_round"] = round_no
            group["max_candidate_rounds"] = 10
            history.append(group)
            if round_no == 2:
                best_result = promoted

        best_snapshot = \
            app.director._candidate_best_provisional_snapshot(
                best_result, 250.0)
        queue = [
            _candidate_result(
                tmp_path, expected=4, available=4, passed=False,
                revision=round_no, cost=4.0)
            for round_no in range(7, 11)
        ]
        calls = []

        def generate(_capability, payload, _out_dir, _cancel, _qc_spec):
            calls.append(copy.deepcopy(payload))
            return queue.pop(0)

        def escalate(report, *_args, **_kwargs):
            report = dict(report)
            round_no = int(report["consecutive_failures"])
            report["codex_escalation"] = {
                "instruction_to_aifos":
                    f"第{round_no + 1}轮使用新的物理可拍静态方案"}
            return report, 0.1

        monkeypatch.setattr(app.director, "_generate_image_gacha", generate)
        monkeypatch.setattr(
            app.director, "_escalate_failed_image_to_codex", escalate)

        result = app.director._generate_shot_candidate_group(
            "image", {
                "_episode_id": "episode-test", "shot_no": 1,
                "_candidate_generation_round": 7,
                "_candidate_revision": 7, "_contract_revision": 7,
                "_candidate_round_history": history,
                "_candidate_best_provisional": best_snapshot,
                "prompt": "第7轮重启前已修订的镜头合同",
            }, tmp_path, None, {})

        assert len(calls) == 4
        assert [row["_candidate_generation_round"] for row in calls] == \
            [7, 8, 9, 10]
        assert calls[0]["prompt"] == "第7轮重启前已修订的镜头合同"
        assert result.uri.endswith("r2-candidate-1.svg")
        group = result.data["candidate_group"]
        assert group["round_status"] == "exhausted"
        assert group["total_generated_candidates"] == 40
        assert len(group["candidate_round_history"]) == 10
        assert queue == []
    finally:
        app.close()


def test_four_draw_repair_replaces_conflicting_old_static_contract(
        tmp_path, monkeypatch):
    """Regression: episode 29 shot 02 must not append to its five-person text."""
    app = App(tmp_path / "ws")
    try:
        initial = _candidate_result(
            tmp_path, expected=4, available=4, passed=False,
            revision=1, cost=4.0)
        for row in initial.data["candidate_group"]["candidates"]:
            row["issues"] = [
                "同一小吴的驾驶、加速、解安全带、递交四个时间状态被错误拆成四具真人",
                "手机同时要求右手亮屏显示23:10和锁屏隐藏于右袋",
                "画面总人数同时规定严格5人和最新严格2人",
            ]
        repaired = _candidate_result(
            tmp_path, expected=4, available=4, passed=True,
            revision=2, cost=4.0)
        queue = [initial, repaired]
        calls = []

        def generate(_capability, payload, _out_dir, _cancel, qc_spec):
            calls.append((copy.deepcopy(payload), copy.deepcopy(qc_spec)))
            return queue.pop(0)

        instruction = (
            "只生成当前镜头递交完成后的唯一静态终点：画面严格仅2名真人，"
            "虞寻歌1人、小吴1人。虞寻歌系安全带坐在右前副驾驶；"
            "小吴双手为空，站在左前驾驶侧车外，车内驾驶位为空。"
            "手机锁屏并完全隐藏在虞寻歌风衣右袋，不显示任何时间；"
            "白酒完全隐藏在风衣左袋。采用现代夜间轿车场景，竖屏全景、"
            "平视、35mm、侧面固定机位。禁止增加人物、分身、亮屏手机、"
            "递交过程、古室宫灯、字幕、Logo和水印。")

        def escalate(report, *_args, **_kwargs):
            report = dict(report)
            report["codex_escalation"] = {
                "instruction_to_aifos": instruction}
            return report, 0.5

        monkeypatch.setattr(
            app.director, "_generate_image_gacha", generate)
        monkeypatch.setattr(
            app.director, "_escalate_failed_image_to_codex", escalate)
        old_prompt = (
            "【主体】虞寻歌1人；画面可见真人严格共5人。"
            "【功能人物】小吴清醒驾驶；小吴加速；小吴解安全带；"
            "小吴递交白酒。【文字】虞寻歌右手持亮屏手机显示23:10。"
            "【道具定格】手机锁屏隐藏于右袋。")
        payload = {
            "_episode_id": "episode-test", "shot_no": 2,
            "_candidate_revision": 1, "_contract_revision": 1,
            "prompt": old_prompt, "prompt_compact": old_prompt,
            "_reference_prompt_base": old_prompt,
            "characters": ["虞寻歌"], "character_count": 1,
            "visible_figure_count": 5,
            "functional_figures": [
                {"name": "小吴", "count": 1, "state": "清醒驾驶"},
                {"name": "小吴", "count": 1, "state": "平稳加速"},
                {"name": "小吴", "count": 1, "state": "解开安全带"},
                {"name": "小吴", "count": 1,
                 "state": "站在驾驶侧车外完成递交"},
            ],
            "readable_text": {"whitelist": ["23:10"],
                              "carrier": "手机锁屏"},
            "frame_target": {"phase": "end", "state": "递交完成"},
            # These legacy fields remain in the audit payload.  The reference
            # manifest refresh must not compile them back into the replacement
            # prompt after Codex has deleted them.
            "camera": "近景跟拍",
            "start_state": {"虞寻歌": {"pose": "站姿"}},
            "end_state": {"虞寻歌": {"pose": "卧姿",
                                      "support": "床榻"}},
            "spatial_staging": {"camera_motion": "跟拍"},
            "reference_manifest": [{
                "index": 1, "role": "identity", "character": "虞寻歌",
                "label": "虞寻歌最终立绘", "uri": "/refs/yxg.png",
            }, {
                "index": 2, "role": "scene", "label": "现代轿车基准图",
                "uri": "/refs/car.png",
            }],
            "composition_contract": {
                "expected_visible_figure_count": 5,
                "functional_figure_count": 4,
            },
        }
        qc_spec = {
            "characters": ["虞寻歌"], "count": 5,
            "functional_figures": copy.deepcopy(
                payload["functional_figures"]),
            "readable_text": copy.deepcopy(payload["readable_text"]),
        }

        result = app.director._generate_shot_candidate_group(
            "image", payload, tmp_path, None, qc_spec)

        assert len(calls) == 2
        repair_payload, repair_qc = calls[1]
        sent = repair_payload["prompt_compact"]
        assert sent.startswith("【返工静态合同v1】")
        assert "只生成当前镜头递交完成后的唯一静态终点" in sent
        assert old_prompt not in sent
        assert "严格共5人" not in sent
        assert "小吴清醒驾驶" not in sent
        assert "小吴加速" not in sent
        assert "右手持亮屏手机显示23:10" not in sent
        assert "站姿" not in sent
        assert "卧姿" not in sent
        assert "床榻" not in sent
        assert "跟拍" not in sent
        assert "画面严格仅2名真人" in sent
        assert repair_payload["feedback"] == ""
        assert repair_payload["visible_figure_count"] == 2
        assert repair_payload["functional_figures"] == [{
            "name": "小吴", "count": 1,
            "state": "", "function": ""}]
        assert repair_payload["camera"] == ""
        assert repair_payload["readable_text"] == {}
        assert repair_payload["prompt_review_context"][
            "visible_figure_count"] == 2
        assert repair_qc["count"] == 2
        assert repair_qc["readable_text"] == {}
        assert result.data["candidate_group"]["expected_count"] == 4
        assert result.data["candidate_group"]["selection"]["source"] == "ai"
        assert result.data["candidate_group"]["selection"][
            "best_effort_risk"] is False
        assert result.qc["best_effort_promoted"] is False
    finally:
        app.close()


def test_repair_instruction_keeps_only_codex_sole_final_wording():
    instruction = (
        "【Codex 通知 AIFOS】"
        "删除虞寻歌‘卧姿、支撑=床榻’的旧空间标签；"
        "删除跟拍、对视、贴近、耳语、指尖触碰等通用动作；"
        "以场景图为唯一基准，清除旧坐标；"
        "修复合同后按以下唯一表述定向重画："
        "平视35mm全景严格侧面静态关键帧；"
        "虞寻歌全身站在床左侧、双手空置；"
        "虞寻欢闭眼仰躺床中央，四肢全部由床垫支撑；"
        "禁止跟拍感、运动模糊、对视、耳语、贴近、指尖触碰、"
        "主动表情、高脚杯和酒瓶。"
        "旧空间调度图中的P02卧姿信息不得执行。"
        "【范围】只修改当前镜头")

    executable = Director._repair_instruction_text(instruction)

    assert executable.startswith("平视35mm全景严格侧面静态关键帧")
    assert "卧姿" not in executable
    assert "对视" not in executable
    assert "跟拍" not in executable
    assert "耳语" not in executable
    assert "贴近" not in executable
    assert "指尖触碰" not in executable
    assert "旧坐标" not in executable
    assert "旧空间调度图" not in executable
    assert "运动模糊" in executable
    assert "主动表情" in executable
    assert "高脚杯和酒瓶" in executable


def test_repair_static_contract_projects_current_named_props_for_qc():
    """返工短合同的 QC 判项必须带当前可见杯具，不能投影为空。"""
    from aifos.adapters.claude_script import static_image_qc_projection

    payload = {
        "characters": ["虞寻歌", "虞寻欢"],
        "location": "虞家别墅·虞寻欢卧室",
        "frame_target": {"phase": "end", "state": "旧终点"},
        "prop_registry": [
            {"prop_id": "water", "name": "水杯", "kind": "story_critical"},
            {"prop_id": "wine", "name": "白酒玻璃杯",
             "kind": "story_critical"},
            {"prop_id": "phone", "name": "手机", "kind": "story_critical"},
            {"prop_id": "bottle", "name": "酒瓶", "kind": "story_critical"},
        ],
        "frame_props": [
            {"prop_id": "water", "phase": "start", "visibility": "visible",
             "holder": "虞寻歌左手", "location": "卧室门外"},
            {"prop_id": "water", "phase": "end", "visibility": "visible",
             "holder": "none", "location": "床头柜"},
            {"prop_id": "wine", "phase": "end", "visibility": "visible",
             "holder": "none", "location": "边柜"},
            {"prop_id": "phone", "phase": "end", "visibility": "hidden",
             "location": "风衣口袋"},
            {"prop_id": "bottle", "phase": "end", "visibility": "absent"},
        ],
    }
    instruction = (
        "只生成终点静态画面：虞寻歌站在床左侧，虞寻欢闭眼卧床；"
        "水杯放在床头柜，白酒玻璃杯放在边柜，手机隐藏；"
        "严格两人，不出现酒瓶。")

    repaired, revised_qc = Director._replace_repair_static_contract(
        payload, {}, instruction)
    projection = static_image_qc_projection({
        "generation_input": {
            "prompt_contract": repaired["prompt_contract"],
        },
    })

    assert projection is not None
    assert projection["phase"] == "end"
    assert [row["name"] for row in projection["frame_props"]] == [
        "水杯", "白酒玻璃杯"]
    assert projection["frame_props"][0]["location"] == "床头柜"
    assert projection["frame_props"][1]["location"] == "边柜"
    assert [row["prop_id"] for row in projection["audit_only"][
        "frame_props_hidden_or_absent"]] == ["phone", "bottle"]
    assert [row["name"] for row in projection["physical"][
        "frame_props"]] == ["水杯", "白酒玻璃杯"]
    assert repaired["frame_props"] == projection["frame_props"]
    assert revised_qc["frame_props"] == projection["frame_props"]
