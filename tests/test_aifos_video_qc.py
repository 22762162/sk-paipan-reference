"""视频生成后的质检门与一次自动返工上限。"""

import json

import pytest

import aifos.director as director_module
import aifos.video_temporal_qc as temporal_qc_module
from aifos.app import App
from aifos.errors import AifosError
from aifos.production.base import ProviderResult


def _ctx(app, root):
    root.mkdir(parents=True, exist_ok=True)
    project, _ = app.projects.get_or_create_project("视频质检测试")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    script = {
        "characters": [{"name": "甲"}],
        "scenes": [{"scene_no": 1, "location": "测试室内", "lines": []}],
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
    scene = root / "scene.png"
    scene.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    app.assets.register(
        project["id"], "scene_art", "测试室内", uri=str(scene),
        meta={"image_quality": "high", "base_location": "测试室内"})
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
        # 本用例只验证显式开启的内容质检返工上限；新默认选片模式会
        # 关闭内容判定，因此这里关闭选片模式并隔离另有专项覆盖的
        # 真实媒体探测。
        app.config.data.setdefault("defaults", {})["selection_mode"] = False
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
        app.director._run_video_media_qc = lambda *_args, **_kwargs: {
            "schema": "aifos.video-media-qc/v1",
            "passed": True,
            "issues": [],
            "shots": [],
            "technical_qc_enabled": True,
            "content_qc_enabled": True,
        }

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


def test_finish_video_rejects_provider_that_dropped_canonical_scene(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        app.router.providers["jimeng"] = type("ProviderStub", (), {
            "conf": {"type": "dreamina", "audio_in_video": True},
        })()
        task = {
            "shot": {"shot_no": 1, "duration": 2.5},
            "quality": {
                "level": "medium", "resolution": "720p", "source": "auto"},
            "reference_assets": [], "reference_manifest": [],
            "payload": {
                "canonical_scene_reference_required": True,
                "canonical_scene_reference_uri": "/tmp/room-master.png",
            },
        }
        result = ProviderResult(
            provider="jimeng", model="seedance2", cost=0.0,
            uri=str(ctx["out_root"] / "bad.mp4"),
            data={"reference_images_used": []})

        with pytest.raises(AifosError, match="没有实际使用统一场景母图"):
            app.director._finish_video_call(ctx, task, result)
        assert app.assets.latest(
            ctx["project"]["id"], "video", "e001_shot001") is None
    finally:
        app.close()


def test_finish_video_accepts_technical_fallback_inheriting_frozen_frames(
        tmp_path):
    """技术兜底不直传参考图，但其画面已由冻结首尾帧继承场景。"""
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        video = ctx["out_root"] / "fallback.mp4"
        video.write_bytes(b"technical-video")
        task = {
            "shot": {"shot_no": 1, "duration": 2.5},
            "quality": {
                "level": "medium", "resolution": "720p", "source": "auto"},
            "reference_assets": [], "reference_manifest": [],
            "payload": {
                "shot_no": 1, "duration": 2.5,
                "first": ctx["frames"][0]["first"],
                "last": ctx["frames"][0]["last"],
                "canonical_scene_reference_required": True,
                "canonical_scene_reference_uri": "/tmp/room-master.png",
                "video_quality": "medium", "video_resolution": "720p",
            },
        }
        result = ProviderResult(
            provider="technical_frame_fallback", model="ffmpeg-xfade",
            cost=0.0, uri=str(video), data={
                "technical_frame_fallback": True,
                "reference_images_used": [], "audio_in_video": True,
            })

        saved = app.director._finish_video_call(ctx, task, result)

        assert saved["provider"] == "technical_frame_fallback"
        assert result.data["canonical_scene_reference_enforced"] == (
            "inherited_from_frozen_first_last_frames")
        assert app.assets.latest(
            ctx["project"]["id"], "video", "e001_shot001") is not None
    finally:
        app.close()


def _checkpoint_frame_record(row, shot_no=1):
    return {
        "asset_id": row["id"], "shot_no": shot_no,
        "kind": row["kind"], "name": row["name"],
        "version": row["version"], "uri": row["uri"],
    }


def test_image_scene_ref_keeps_canonical_master_and_demotes_camera_slice(
        tmp_path):
    """机位切片只能辅助构图，不能成为首尾帧的场景身份。"""
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        canonical = app.assets.latest(
            ctx["project"]["id"], "scene_art", "测试室内")
        camera_slice = tmp_path / "artifacts" / "reverse-slice.png"
        camera_slice.write_bytes(b"\x89PNG\r\n\x1a\n" + b"slice" * 8)
        app.director._scene_slice_for_shot = (
            lambda *_args, **_kwargs: str(camera_slice))

        refs = app.director._art_refs(
            ctx, [], "测试室内", shot_no=1, camera={})

        assert refs["scene_ref"] == canonical["uri"]
        assert refs["canonical_scene_asset_id"] == canonical["id"]
        assert refs["canonical_scene_asset_version"] == canonical["version"]
        assert refs["canonical_scene_reference_uri"] == canonical["uri"]
        assert refs["canonical_scene_file_sha256"] == (
            app.director._file_sha256(canonical["uri"]))
        assert str(camera_slice) in refs.get("reference_images", [])
        assert any(
            item.get("uri") == str(camera_slice)
            and item.get("reference_role") == "scene_view"
            for item in refs["asset_matches"])
    finally:
        app.close()


def _checkpoint_with_scene_proof(app, ctx, *, snapshot=None):
    first = app.assets.register(
        ctx["project"]["id"], "first_frame", "e001_shot001",
        uri=ctx["frames"][0]["first"],
        meta={"input_snapshot": snapshot} if snapshot is not None else {})
    last = app.assets.register(
        ctx["project"]["id"], "last_frame", "e001_shot001",
        uri=ctx["frames"][0]["last"],
        meta={"input_snapshot": snapshot} if snapshot is not None else {})
    scene = app.assets.latest(
        ctx["project"]["id"], "scene_art", "测试室内")
    return {
        "schema": "aifos.preflight-checkpoint/v1",
        "document_versions": {},
        "assets": {
            "first_frames": [_checkpoint_frame_record(first)],
            "last_frames": [_checkpoint_frame_record(last)],
        },
        "video_reference_asset_ids": {"1": [scene["id"]]},
    }


def test_checkpoint_rejects_missing_canonical_scene_instead_of_hot_swap(
        tmp_path):
    """旧快照缺母场景时不能只更新视频参考并继续使用旧帧。"""
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        legacy = {
            "schema": "aifos.preflight-checkpoint/v1",
            "document_versions": {},
            "video_reference_asset_ids": {"1": []},
        }

        with pytest.raises(
                AifosError, match="禁止只热换视频场景参考"):
            app.director._refresh_checkpoint_video_references(
                ctx, legacy, 0)

        assert app.projects.latest_document(
            ctx["episode"]["id"], "preflight_checkpoint") == (None, 0)
    finally:
        app.close()


def test_checkpoint_rejects_frames_without_canonical_scene_snapshot(tmp_path):
    """即使参考链已有当前母场景，旧首尾帧没有来源证明也不可复用。"""
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        checkpoint = _checkpoint_with_scene_proof(app, ctx)

        with pytest.raises(AifosError, match="缺少场景输入快照"):
            app.director._refresh_checkpoint_video_references(
                ctx, checkpoint, 1)
    finally:
        app.close()


def test_checkpoint_rejects_frames_after_canonical_scene_bytes_change(
        tmp_path):
    """母图稳定 URI 被覆写也必须重建首尾帧，不能仅按 asset id 放行。"""
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        scene = app.assets.latest(
            ctx["project"]["id"], "scene_art", "测试室内")
        snapshot = {
            "canonical_scene_asset_id": scene["id"],
            "canonical_scene_file_sha256": app.director._file_sha256(
                scene["uri"]),
        }
        checkpoint = _checkpoint_with_scene_proof(
            app, ctx, snapshot=snapshot)
        (tmp_path / "artifacts" / "scene.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"changed-room" * 2)

        with pytest.raises(AifosError, match="场景文件摘要已变化"):
            app.director._refresh_checkpoint_video_references(
                ctx, checkpoint, 1)
    finally:
        app.close()


def test_checkpoint_rejects_frames_bound_to_another_canonical_scene_asset(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        scene = app.assets.latest(
            ctx["project"]["id"], "scene_art", "测试室内")
        snapshot = {
            "canonical_scene_asset_id": scene["id"] + 999,
            "canonical_scene_file_sha256": app.director._file_sha256(
                scene["uri"]),
        }
        checkpoint = _checkpoint_with_scene_proof(
            app, ctx, snapshot=snapshot)

        with pytest.raises(AifosError, match="场景资产.*≠"):
            app.director._refresh_checkpoint_video_references(
                ctx, checkpoint, 1)
    finally:
        app.close()


def test_checkpoint_accepts_frames_proven_against_current_canonical_scene(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        scene = app.assets.latest(
            ctx["project"]["id"], "scene_art", "测试室内")
        snapshot = {
            "canonical_scene_asset_id": scene["id"],
            "canonical_scene_file_sha256": app.director._file_sha256(
                scene["uri"]),
        }
        checkpoint = _checkpoint_with_scene_proof(
            app, ctx, snapshot=snapshot)

        current, version = app.director._refresh_checkpoint_video_references(
            ctx, checkpoint, 7)

        assert current is checkpoint
        assert version == 7
    finally:
        app.close()


def test_video_scene_anchor_prefers_one_shared_wide_master(tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        wide = tmp_path / "wide-room.png"
        wide.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
        panorama = app.assets.register(
            ctx["project"]["id"], "scene_art",
            "测试室内::view:panorama", uri=str(wide),
            meta={"image_quality": "high", "base_location": "测试室内",
                  "equirectangular_validated": False})

        selected = app.director._required_video_scene_reference(
            ctx, ctx["storyboard"]["shots"][0])

        assert selected["id"] == panorama["id"]
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


def test_video_media_qc_compares_canonical_scene_and_previous_same_scene(
        tmp_path, monkeypatch):
    """每条视频不能只自洽；还要对照母场景和上一条同场视频。"""
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        second_video = ctx["out_root"] / "shot-002.mp4"
        second_video.write_bytes(b"video-2")
        ctx["storyboard"]["shots"].append({
            "shot_no": 2, "scene_no": 1, "unit_id": "U02",
            "duration": 2.5, "characters": [],
            "script_reference": "继续动作", "shot_function": "动作",
        })
        ctx["videos"] = [
            {"shot_no": 1, "uri": str(ctx["videos"][0]["uri"])},
            {"shot_no": 2, "uri": str(second_video)},
        ]

        monkeypatch.setattr(
            director_module, "probe_video", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(
            director_module, "evaluate_video_technical",
            lambda *_args, **_kwargs: {"passed": True, "issues": []})

        def fake_samples(uri, cache_root, **_kwargs):
            shot_no = 2 if "002" in str(uri) else 1
            samples = []
            for index, label in enumerate(("0%", "25%", "50%", "75%", "100%")):
                path = cache_root / f"shot-{shot_no}-{index}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"frame")
                samples.append({
                    "label": label, "timestamp": float(index),
                    "uri": str(path),
                })
            return {"passed": True, "issues": [], "samples": samples}

        monkeypatch.setattr(
            director_module, "extract_video_qc_frames", fake_samples)
        monkeypatch.setattr(
            temporal_qc_module, "analyze_temporal_samples",
            lambda *_args, **_kwargs: {"warnings": []})
        calls = []

        def pass_scene_qc(capability, payload, *_args, **_kwargs):
            assert capability == "image_qc"
            calls.append(payload)
            return ProviderResult(
                provider="mock", model="mock-qc", cost=0.0, data={
                    "pass": True, "visual_pass": True,
                    "input_contract_pass": True,
                    "count_checked": True, "count_match": True,
                    "detected_count": 0,
                    "overlay_count_checked": True,
                    "overlay_count_match": True,
                    "detected_overlay_count": 0,
                    "physical_logic_checked": True,
                    "physical_logic_match": True,
                    "spatial_logic_checked": True,
                    "spatial_logic_match": True,
                    "scene_topology_checked": True,
                    "scene_topology_match": True,
                    "technical_quality_pass": True,
                    "critical_failures": [], "advisory_issues": [],
                    "issues": [],
                    "prompt_diagnosis": {"status": "correct"},
                    "reference_diagnosis": {"status": "correct"},
                })

        monkeypatch.setattr(app.router, "call", pass_scene_qc)

        report = app.director._run_video_media_qc(ctx, True)

        assert report["passed"] is True
        assert len(calls) == 2
        for payload in calls:
            refs = payload["reference_manifest"]
            assert sum(
                row.get("label") == "统一场景母图" for row in refs) == 1
            assert payload["scene_topology_required"] is True
            assert payload["video_cross_shot_context"][
                "canonical_scene"]["uri"].endswith("scene.png")
        second_refs = calls[1]["reference_manifest"]
        assert sum(
            row.get("label") == "同场上一镜视频尾帧"
            for row in second_refs) == 1
        assert calls[1]["video_cross_shot_context"][
            "previous_same_scene_shot"]["shot_no"] == 1
        assert report["shots"][1]["visual_verdicts"][0][
            "scene_topology_match"] is True
    finally:
        app.close()


def test_video_topology_qc_cannot_pass_without_explicit_topology_verdict(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path / "artifacts")
        spec = app.director._video_sample_qc_spec(
            ctx, ctx["storyboard"]["shots"][0])
        verdict = {
            "pass": True, "visual_pass": True,
            "count_checked": True, "count_match": True,
            "overlay_count_checked": True, "overlay_count_match": True,
            "physical_logic_checked": True, "physical_logic_match": True,
            "spatial_logic_checked": True, "spatial_logic_match": True,
            "technical_quality_pass": True,
        }

        assessed = app.director._assess_image_qc(spec, verdict, 1)

        assert assessed["passed"] is False
        assert assessed["scene_topology_checked"] is False
        assert "统一场景母图" in "；".join(assessed["issues"])
    finally:
        app.close()
