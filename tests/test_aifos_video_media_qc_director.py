"""Director integration for always-on media QC and optional frame review."""

from pathlib import Path
from types import SimpleNamespace

import aifos.director as director_module
from aifos.adapters.claude_script import build_qc_prompt
from aifos.app import App


def _ctx(app, tmp_path):
    project, _ = app.projects.get_or_create_project("视频媒体接线")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = Path(tmp_path) / "artifacts"
    out_root.mkdir(parents=True, exist_ok=True)
    shot = {
        "shot_no": 1,
        "duration": 5,
        "characters": ["林昭"],
        "description": "林昭将密诏放在桌面。",
    }
    return {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": out_root,
        "script": {"scenes": []},
        "storyboard": {"shots": [shot]},
        "videos": [{
            "shot_no": 1,
            "uri": str(out_root / "shot001.mp4"),
            "video_resolution": "720p",
            "audio_in_video": True,
        }],
        "aspect": "9:16",
        "production_profile": {
            "voice": "jimeng_builtin",
            "lip_sync": True,
        },
    }


def _passed_technical():
    return {
        "passed": True,
        "issues": [],
        "probe": {"width": 720, "height": 1280},
        "reference_chain_eligible": False,
    }


def test_video_sequence_prompt_requires_joint_physical_motion_review():
    prompt = build_qc_prompt({
        "image_uri": "/tmp/sample_000.png",
        "characters": [],
        "video_sequence_samples": [
            {"label": f"{percent}%", "timestamp": index,
             "uri": f"/tmp/sample_{percent:03d}.png"}
            for index, percent in enumerate((0, 25, 50, 75, 100))
        ],
    })

    assert "【真实视频五点抽帧联合质检】" in prompt
    assert "道具瞬移" in prompt
    assert "穿模悬浮" in prompt
    assert "设备方向反转" in prompt
    assert "严禁登记为资产" in prompt


def test_video_content_qc_off_never_extracts_or_calls_ai(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path)
        monkeypatch.setattr(
            director_module, "probe_video",
            lambda *_args, **_kwargs: {"probed": True, "probe_ok": True})
        monkeypatch.setattr(
            director_module, "evaluate_video_technical",
            lambda *_args, **_kwargs: _passed_technical())
        monkeypatch.setattr(
            director_module, "extract_video_qc_frames",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("content QC off must not extract frames")))
        monkeypatch.setattr(
            app.director, "_video_sample_qc_spec",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("content QC off must not build visual spec")))
        monkeypatch.setattr(
            app.director.router, "call",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("content QC off must not call image QC")))

        report = app.director._run_video_media_qc(
            ctx, content_enabled=False)

        assert report["technical_qc_enabled"] is True
        assert report["content_qc_enabled"] is False
        assert report["content_qc_waived"] is True
        assert report["shots"][0]["frame_evidence"] is None
        assert report["shots"][0]["visual_verdicts"] == []
        assert report["reference_chain_eligible"] is False
    finally:
        app.close()


def test_video_technical_failure_is_reported_even_with_content_qc_off(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path)
        monkeypatch.setattr(
            director_module, "probe_video",
            lambda *_args, **_kwargs: {"probed": False})
        monkeypatch.setattr(
            director_module, "evaluate_video_technical",
            lambda *_args, **_kwargs: {
                "passed": False,
                "issues": [{
                    "check": "video_technical",
                    "code": "resolution_mismatch",
                    "severity": "error",
                    "rerunnable": True,
                    "message": "实际分辨率不是720x1280",
                }],
            })

        report = app.director._run_video_media_qc(
            ctx, content_enabled=False)

        assert report["passed"] is False
        assert report["issues"][0]["check"] == "video_technical"
        assert report["issues"][0]["shot_no"] == 1
    finally:
        app.close()


def test_video_sample_qc_uses_frozen_physical_and_spatial_contract(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path)
        frozen_physical = {
            "schema": "aifos.physical-space/v1",
            "required": True,
            "rules": ["密诏先接触桌面，手再松开，过程中不得瞬移"],
            "objects": ["密诏：林昭手部→桌面"],
        }
        frozen_spatial = {
            "空间站位": "林昭位于桌子北侧",
            "空间裁决": "密诏落点保持在林昭前方桌面中央",
        }
        captured = {}

        def capture_spec(project_id, characters, **kwargs):
            captured.update(kwargs)
            return {"project_id": project_id, "characters": characters,
                    **kwargs}

        monkeypatch.setattr(app.director, "_qc_spec", capture_spec)
        spec = app.director._video_sample_qc_spec(
            ctx, ctx["storyboard"]["shots"][0],
            generation_snapshot={
                "prompt_contract": {
                    "physical": frozen_physical,
                    "spatial_staging": frozen_spatial,
                },
            })

        assert spec["physical_logic_required"] is True
        assert spec["physical_contract"] == frozen_physical
        assert spec["spatial_staging"] == frozen_spatial
        # QC owns a copy; it must not mutate the frozen Seedance audit record.
        assert spec["physical_contract"] is not frozen_physical
        assert spec["spatial_staging"] is not frozen_spatial
    finally:
        app.close()


def test_video_content_qc_on_checks_five_real_samples_without_assets(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path)
        samples = [{
            "label": f"{percent}%",
            "timestamp": index + 0.1,
            "uri": str(ctx["out_root"] / f"sample_{percent:03d}.png"),
            "reference_chain_eligible": False,
            "asset_registration_allowed": False,
        } for index, percent in enumerate((0, 25, 50, 75, 100))]
        monkeypatch.setattr(
            director_module, "probe_video",
            lambda *_args, **_kwargs: {"probed": True, "probe_ok": True})
        monkeypatch.setattr(
            director_module, "evaluate_video_technical",
            lambda *_args, **_kwargs: _passed_technical())
        monkeypatch.setattr(
            director_module, "extract_video_qc_frames",
            lambda *_args, **_kwargs: {
                "passed": True,
                "samples": samples,
                "issues": [],
                "reference_chain_eligible": False,
                "asset_registration_allowed": False,
            })
        monkeypatch.setattr(
            app.director, "_video_sample_qc_spec",
            lambda *_args, **_kwargs: {})
        monkeypatch.setattr(
            app.director, "_video_input_for_qc",
            lambda *_args: ({
                "prompt_sent": "林昭把密诏放到桌面",
                "prompt_contract": {},
                "reference_manifest": [],
            }, "input-signature", []))
        called = []

        def fake_call(_capability, payload, _out_dir, cancel=None):
            called.append(payload)
            return SimpleNamespace(
                cost=0.0, provider="qc-provider",
                data={"bad": True})

        monkeypatch.setattr(app.director.router, "call", fake_call)
        monkeypatch.setattr(
            app.director, "_assess_image_qc",
            lambda _spec, data, _attempts: {
                "passed": not data.get("bad"),
                "issues": (["道具悬浮，未接触桌面"]
                           if data.get("bad") else []),
            })
        assets_before = len(app.assets.list(ctx["project"]["id"]))

        report = app.director._run_video_media_qc(
            ctx, content_enabled=True)

        assert len(called) == 1
        assert [item["label"] for item in called[0][
            "video_sequence_samples"]] == [
                "0%", "25%", "50%", "75%", "100%"]
        assert len([
            item for item in called[0]["reference_manifest"]
            if item.get("role") == "video_qc_sequence"]) == 5
        assert report["passed"] is False
        visual = next(
            issue for issue in report["issues"]
            if issue["check"] == "video_visual")
        assert "道具悬浮" in visual["message"]
        assert visual["input_diagnosis"]["decision"]["input_changed"] is True
        assert report["shots"][0]["frame_evidence"][
            "asset_registration_allowed"] is False
        assert len(app.assets.list(ctx["project"]["id"])) == assets_before
    finally:
        app.close()


def test_video_content_qc_requires_structured_physical_and_spatial_verdicts(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        ctx = _ctx(app, tmp_path)
        samples = [{
            "label": f"{percent}%",
            "timestamp": index + 0.1,
            "uri": str(ctx["out_root"] / f"missing_gate_{percent:03d}.png"),
        } for index, percent in enumerate((0, 25, 50, 75, 100))]
        monkeypatch.setattr(
            director_module, "probe_video",
            lambda *_args, **_kwargs: {"probed": True, "probe_ok": True})
        monkeypatch.setattr(
            director_module, "evaluate_video_technical",
            lambda *_args, **_kwargs: _passed_technical())
        monkeypatch.setattr(
            director_module, "extract_video_qc_frames",
            lambda *_args, **_kwargs: {
                "passed": True, "samples": samples, "issues": []})
        monkeypatch.setattr(
            app.director, "_video_input_for_qc",
            lambda *_args: ({
                "prompt_sent": "林昭将密诏放在桌面",
                "prompt_contract": {
                    "physical": {"rules": ["密诏必须由桌面支撑"]},
                    "spatial_staging": {"空间站位": "林昭在桌后"},
                },
                "reference_manifest": [],
            }, "input-signature", []))
        monkeypatch.setattr(
            app.director, "_video_sample_qc_spec",
            lambda *_args, **_kwargs: {
                "physical_logic_required": True,
                "physical_contract": {"rules": ["密诏必须由桌面支撑"]},
                "spatial_staging": {"空间站位": "林昭在桌后"},
                "identity_required": False,
                "gender_required": False,
                "wardrobe_required": False,
                "count_required": False,
                "overlay_count_required": False,
            })
        monkeypatch.setattr(
            app.director.router, "call",
            lambda *_args, **_kwargs: SimpleNamespace(
                cost=0.0, provider="qc-provider", data={
                    "pass": True,
                    "visual_pass": True,
                    "input_contract_pass": True,
                    # Deliberately omit the four required structured gates.
                    "issues": [],
                }))

        report = app.director._run_video_media_qc(
            ctx, content_enabled=True)

        assert report["passed"] is False
        verdict = report["shots"][0]["visual_verdicts"][0]
        assert verdict["physical_logic_checked"] is False
        assert verdict["physical_logic_match"] is False
        assert verdict["spatial_logic_checked"] is False
        assert verdict["spatial_logic_match"] is False
        visual = next(
            issue for issue in report["issues"]
            if issue["check"] == "video_visual")
        assert "未核对道具、人物与镜头的物理关系" in visual["message"]
        assert "未核对人物、道具、镜头的空间关系" in visual["message"]
    finally:
        app.close()
