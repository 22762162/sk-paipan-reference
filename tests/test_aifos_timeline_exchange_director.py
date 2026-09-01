"""Director persists the neutral timeline before invoking an editor."""

from pathlib import Path
from types import SimpleNamespace

from aifos.app import App


def test_edit_stage_persists_and_passes_timeline_exchange(tmp_path, monkeypatch):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("中立剪辑时间线")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        video = tmp_path / "shot-001.mp4"
        video.write_bytes(b"test-video")
        final = tmp_path / "final.mp4"
        final.write_bytes(b"test-final")
        captured = {}

        def fake_call(_ctx, capability, payload, sub_dir):
            assert (capability, sub_dir) == ("edit", "edit")
            captured.update(payload)
            return SimpleNamespace(
                provider="fake-editor", cost=0.0, uri=str(final), data={})

        monkeypatch.setattr(app.director, "_call", fake_call)
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "out_root": tmp_path / "artifacts",
            "storyboard_version": 3,
            "storyboard": {"shots": [{
                "shot_no": 1, "unit_id": "U01", "duration": 2.5,
            }]},
            "videos": [{
                "shot_no": 1, "uri": str(video), "duration": 2.5,
                "provider": "dreamina", "model": "seedance-2.0-fast",
                "audio_in_video": True,
            }],
            "voices": [], "subtitles": [],
            "voice_mode": "jimeng_builtin", "lip_sync": True,
            "production_profile": {"burn_subtitles": False},
            "aspect": "9:16", "dims": {"width": 720, "height": 1280},
        }

        result = app.director._stage_edit(ctx)

        assert captured["timeline_exchange"]["source_shot_numbers"] == [1]
        assert Path(captured["timeline_exchange_uri"]).is_file()
        stored, version = app.projects.latest_document(
            episode["id"], "timeline_exchange")
        assert version == result["timeline_exchange_version"] == 1
        assert stored["timeline_hash"] == result["timeline_hash"]
        assert ctx["final_uri"] == str(final)
    finally:
        app.close()
