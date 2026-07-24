"""视频生成后的质检门与一次自动返工上限。"""

import json

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.production.base import ProviderResult


def _ctx(app, root):
    root.mkdir(parents=True, exist_ok=True)
    project, _ = app.projects.get_or_create_project("视频质检测试")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    script = {
        "characters": [{"name": "甲"}],
        "scenes": [{"scene_no": 1, "lines": []}],
    }
    storyboard = {"shots": [{
        "shot_no": 1, "scene_no": 1, "unit_id": "U01",
        "duration": 2.5, "characters": [], "script_reference": "动作",
        "shot_function": "动作",
    }]}
    video = root / "shot-001.video.json"
    video.write_text("{}", encoding="utf-8")
    first = root / "shot-001.first.svg"
    last = root / "shot-001.last.svg"
    first.write_text("<svg></svg>", encoding="utf-8")
    last.write_text("<svg></svg>", encoding="utf-8")
    return {
        "project": dict(project), "episode": dict(episode),
        "out_root": root, "script": script, "storyboard": storyboard,
        "continuity": {"characters": [], "scenes": [{}]},
        "production_profile": {}, "images": [],
        "frames": [{"shot_no": 1, "first": str(first), "last": str(last)}],
        "videos": [{"shot_no": 1, "uri": str(video)}],
        "voices": [], "subtitles": [], "dims": {"width": 1080, "height": 1920},
        "final_uri": str(video), "edit_data": {},
    }


def test_video_qc_only_retries_once_then_waits_for_human(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        calls = []

        def always_fail(_script, _storyboard, _ctx):
            return {
                "score": 0, "pass_score": 80, "passed": False,
                "issues": [{"check": "video", "severity": "error",
                             "shot_no": 1, "rerunnable": True,
                             "message": "测试：动作与尾帧不一致"}],
                "rerun_shots": [1], "rerun_lines": [],
            }

        app.director.qc.run = always_fail

        def count_only(_ctx, _report, video_shots=None):
            calls.append(list(video_shots or []))

        app.director._rerun = count_only
        app.director._task_cost = 0.0
        app.director._task_providers = set()
        result = app.director._stage_qc(ctx)

        assert calls == [[1]]
        assert result["passed"] is False
        report = json.loads(
            (ctx["out_root"] / "video_qc_report.json").read_text(
                encoding="utf-8"))
        assert report["awaiting_human"] is True
        assert report["awaiting_human_shots"] == [1]
        assert report["shots"][0]["auto_retries_used"] == 1
        assert report["shots"][0]["generation_attempts"] == 2
        assert "【自动优化修订】" in report["shots"][0]["revision_feedback"]
        assert "首帧" in report["shots"][0]["revision_feedback"]
        assert "尾帧" in report["shots"][0]["revision_feedback"]
        # 断点恢复/再次进入质检也不能把第三次自动返工偷偷触发。
        app.director._stage_qc(ctx)
        assert calls == [[1]]
    finally:
        app.close()


def test_global_integrated_audio_failure_does_not_retry_every_video(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        report = {
            "score": 0, "pass_score": 80, "passed": False,
            "issues": [{
                "check": "integrated_audio", "severity": "error",
                "shot_no": None, "rerunnable": False,
                "message": "全局配音配置缺失",
            }],
            "rerun_shots": [], "rerun_lines": [],
        }

        video_qc = app.director._build_video_qc_report(ctx, report)

        assert video_qc["passed"] is False
        assert video_qc["awaiting_human"] is True
        assert video_qc["global_issues"] == ["全局配音配置缺失"]
        assert video_qc["shots"][0]["passed"] is True
        assert app.director._video_retry_candidates(video_qc) == []
    finally:
        app.close()


def test_source_frame_diagnosis_blocks_seedance_retry(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        ctx["video_input_diagnoses"] = {
            1: {
                "issues": ["首帧人物身份错误"],
                "frame_audit": {
                    "first_valid": False,
                    "last_valid": True,
                    "continuity_valid": False,
                },
                "decision": {
                    "action": "direct_video_retry",
                    "safe_to_auto_retry": True,
                    "input_changed": True,
                    "prompt_patch": {"subject": "修正人物"},
                },
            },
        }
        report = {
            "score": 0, "pass_score": 80, "passed": False,
            "issues": [{
                "check": "video", "severity": "error",
                "shot_no": 1, "rerunnable": True,
                "message": "人物身份错误",
            }],
            "rerun_shots": [1], "rerun_lines": [],
        }

        video_qc = app.director._build_video_qc_report(ctx, report)
        shot = video_qc["shots"][0]

        assert shot["status"] == "repair_frames_first"
        assert shot["decision"]["action"] == "repair_frames_first"
        assert video_qc["repair_frames_first_shots"] == [1]
        assert app.director._video_retry_candidates(video_qc) == []
        with pytest.raises(AifosError, match="必须先修复上游帧"):
            app.director._prepare_video_call(
                ctx, ctx["storyboard"]["shots"][0],
                {1: ctx["frames"][0]})
    finally:
        app.close()


def test_unchanged_video_inputs_fail_safe_to_human(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        ctx["video_input_diagnoses"] = {
            1: {
                "issues": ["动作不正确"],
                "prompt_diagnosis": {"valid": True},
                "reference_diagnosis": {"valid": True},
                "frame_audit": {"source_frames_valid": True},
                "decision": {
                    "action": "direct_video_retry",
                    "safe_to_auto_retry": True,
                    "input_changed": False,
                },
            },
        }
        report = {
            "score": 0, "pass_score": 80, "passed": False,
            "issues": [{
                "check": "video", "severity": "error",
                "shot_no": 1, "rerunnable": True,
                "message": "动作不正确",
            }],
            "rerun_shots": [1], "rerun_lines": [],
        }

        video_qc = app.director._build_video_qc_report(ctx, report)
        shot = video_qc["shots"][0]

        assert shot["status"] == "awaiting_human"
        assert shot["decision"]["action"] == "awaiting_human"
        assert "禁止原样重试" in shot["decision"]["reason"]
        assert app.director._video_retry_candidates(video_qc) == []
    finally:
        app.close()


def test_video_asset_persists_input_signature_and_attempt_history(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        ctx["project"]["style"] = ""
        task = {
            "shot": ctx["storyboard"]["shots"][0],
            "quality": {
                "level": "medium", "resolution": "720p", "source": "default"},
            "reference_assets": [{
                "asset_id": 7, "kind": "character_identity",
                "name": "甲", "version": 1,
            }],
            "reference_manifest": [{
                "index": 3, "asset_id": 7, "kind": "character_identity",
                "name": "甲", "version": 1, "uri": "/tmp/identity.png",
                "binding": "只锁身份",
            }],
            "payload": {
                "shot_no": 1,
                "prompt": "完整提示",
                "prompt_compact": "短提示A",
                "prompt_contract": {"schema": "aifos.shot-prompt/v1"},
                "first": ctx["frames"][0]["first"],
                "last": ctx["frames"][0]["last"],
                "reference_images": ["/tmp/identity.png"],
                "reference_manifest": [{
                    "index": 3, "asset_id": 7,
                    "kind": "character_identity", "name": "甲",
                    "version": 1, "uri": "/tmp/identity.png",
                    "binding": "只锁身份",
                }],
                "duration": 2.5,
                "video_quality": "medium",
                "video_resolution": "720p",
                "standard_fingerprint": "std",
            },
        }
        first_video = tmp_path / "artifacts" / "generated-1.video.json"
        first_video.write_text("{}", encoding="utf-8")
        result = ProviderResult(
            provider="mock", cost=0, uri=str(first_video),
            data={"voice": "jimeng_builtin", "lip_sync": True},
            model="mock-video-v1")

        first = app.director._finish_video_call(ctx, task, result)
        task["payload"]["prompt_compact"] = "短提示B"
        second_video = tmp_path / "artifacts" / "generated-2.video.json"
        second_video.write_text("{}", encoding="utf-8")
        result.uri = str(second_video)
        second = app.director._finish_video_call(ctx, task, result)

        assert first["input_signature"] != second["input_signature"]
        assert len(second["attempt_history"]) == 2
        assert second["attempt_history"][0]["prompt_sent_hash"] != (
            second["attempt_history"][1]["prompt_sent_hash"])
        row = app.assets.latest(
            ctx["project"]["id"], "video", "e001_shot001")
        meta = json.loads(row["meta"])
        assert meta["input_signature"] == second["input_signature"]
        assert meta["input_snapshot"]["prompt_sent"] == "短提示B"
        assert meta["model"] == "mock-video-v1"
    finally:
        app.close()


def test_failed_video_actually_diagnoses_source_frames_with_image_qc(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        ctx["storyboard"]["shots"][0]["description"] = "质检必挂"
        report = {
            "score": 0, "pass_score": 80, "passed": False,
            "issues": [{
                "check": "video", "severity": "error",
                "shot_no": 1, "rerunnable": True,
                "message": "人物在视频中漂移",
            }],
            "rerun_shots": [1], "rerun_lines": [],
        }

        video_qc = app.director._build_video_qc_report(ctx, report)
        shot = video_qc["shots"][0]
        diagnosis = shot["input_diagnosis"]

        assert diagnosis["frame_audit"]["visual_checked"] is True
        assert diagnosis["frame_audit"]["first_valid"] is False
        assert diagnosis["frame_audit"]["last_valid"] is False
        assert diagnosis["decision"]["action"] == "repair_frames_first"
        assert app.director._video_retry_candidates(video_qc) == []
    finally:
        app.close()


def test_prepare_video_rejects_claimed_change_when_signature_is_identical(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        ctx.update({
            "aspect": "9:16",
            "quality_policy": {},
            "blocking": {},
            "production_profile": {
                "voice": "jimeng_builtin",
                "lip_sync": True,
                "burn_subtitles": False,
                "standard_fingerprint": "std",
            },
        })
        shot = ctx["storyboard"]["shots"][0]
        frame_map = {1: {
            **ctx["frames"][0], "image_quality": "medium"}}
        original = app.director._prepare_video_call(ctx, shot, frame_map)
        app.assets.register(
            ctx["project"]["id"], "video", "e001_shot001",
            uri=str(ctx["out_root"] / "old.video.json"),
            meta={"input_signature":
                  original["payload"]["input_signature"]})
        ctx["video_input_diagnoses"] = {
            1: {
                "issues": ["动作错误"],
                "decision": {
                    "action": "direct_video_retry",
                    "safe_to_auto_retry": True,
                    "input_changed": True,
                    "prompt_patch": {"action": "只做一次抬手"},
                },
            },
        }

        with pytest.raises(AifosError, match="禁止原样重复调用"):
            app.director._prepare_video_call(ctx, shot, frame_map)
    finally:
        app.close()


def test_video_qc_accepts_shared_generation_diagnostics_contract(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        ctx["video_input_diagnoses"] = {
            1: {
                "schema": "aifos.visual-input-diagnosis/v1",
                "diagnosis_complete": True,
                "image_error": {
                    "summary": "人物中途换脸",
                    "evidence": ["第1.5秒五官漂移"],
                },
                "prompt_diagnosis": {
                    "status": "needs_patch",
                    "issues": ["人物身份锁定不够靠前"],
                },
                "reference_diagnosis": {
                    "status": "correct", "issues": []},
                "targeted_prompt_patch": {
                    "instructions": ["全过程保持甲的脸和性别不变"],
                    "preserve": ["首尾帧", "动作", "机位"],
                },
                "reference_adjustments": [],
                "frame_audit": {
                    "source_frames_valid": True,
                    "first_valid": True,
                    "last_valid": True,
                },
            },
        }
        report = {
            "score": 0, "pass_score": 80, "passed": False,
            "issues": [{
                "check": "video", "severity": "error",
                "shot_no": 1, "rerunnable": True,
                "message": "人物中途换脸",
            }],
            "rerun_shots": [1], "rerun_lines": [],
        }

        video_qc = app.director._build_video_qc_report(ctx, report)
        shot = video_qc["shots"][0]

        assert shot["decision"]["action"] == "direct_video_retry"
        assert shot["decision"]["input_changed"] is True
        assert "全过程保持甲" in json.dumps(
            shot["decision"]["prompt_patch"], ensure_ascii=False)
        assert app.director._video_retry_candidates(video_qc) == [1]
    finally:
        app.close()
