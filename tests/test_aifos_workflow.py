"""SK 漫剧 V5 工业流契约与真实交付复核。"""

import json
import subprocess
import sys
from pathlib import Path

from aifos.app import App
from aifos.web.server import _episode_payload


def test_five_dimension_preflight_and_delivery(tmp_path):
    app = App(tmp_path / "ws")
    try:
        paused = app.director.produce(
            "逆光成团", 1, premise="女团第一次直播前的后台危机",
            pause_for_confirm=True)
        assert paused["status"] == "awaiting_script"
        paused = app.director.produce(
            "逆光成团", 1, pause_for_confirm=True)  # 剧本确认
        assert paused["status"] == "awaiting_confirm"

        project = app.projects.get_project("逆光成团")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        continuity, _ = app.projects.latest_document(
            episode["id"], "continuity")
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        preflight, _ = app.projects.latest_document(
            episode["id"], "preflight")

        profile = storyboard["profile"]
        assert profile == continuity["production_profile"]
        assert profile["video_model"] == "seedance2.0fast_vip"
        assert profile["resolution"] == "720p"
        assert profile["voice"] == "jimeng_builtin"
        assert profile["lip_sync"] is True
        assert profile["burn_subtitles"] is False
        assert preflight["passed"]
        assert [gate["id"] for gate in preflight["gates"]] == [
            "continuity", "five_dimensions", "duration", "dialogue",
            "performance", "camera", "people", "text", "frames",
            "audio", "profile"]

        shots = storyboard["shots"]
        assert {"reaction", "beat"} <= {shot["kind"] for shot in shots}
        assert all(0 < shot["duration"] <= 15 for shot in shots)
        assert all(shot["duration"] * 2 == int(shot["duration"] * 2)
                   for shot in shots)
        assert all(shot["character_count"] == len(shot["characters"])
                   for shot in shots)
        assert all(shot["start_state"] and shot["end_state"]
                   and shot["five_dimensions"] and shot["seedance_prompt"]
                   for shot in shots)

        out = Path(paused["artifacts_dir"])
        first_keyframe = out / "images" / "shot_001.keyframe.svg"
        # Mock 预览也遵守正式产线：镜头画面无名牌、说明文字和对白字幕。
        assert "<text" not in first_keyframe.read_text(encoding="utf-8")

        done = app.director.produce("逆光成团", 1)
        assert done["status"] == "done"
        first_video = json.loads(
            (out / "videos" / "shot_001.video.json").read_text(
                encoding="utf-8"))
        assert first_video["voice"] == "jimeng_builtin"
        assert first_video["lip_sync"] is True
        assert first_video["forbid_subtitles"] is True

        draft = json.loads(
            (out / "edit" / "draft_content.json").read_text(
                encoding="utf-8"))
        assert draft["tracks"]["audio"] == []
        assert draft["tracks"]["subtitle"] == []

        qc = json.loads((out / "qc_report.json").read_text(encoding="utf-8"))
        assert qc["passed"] and qc["technical_passed"]
        assert qc["content_passed"]
        assert qc["delivery_check"]["passed"]
        assert qc["delivery_check"]["executed"]
        verifier = out / "check-delivery.py"
        rerun = subprocess.run(
            [sys.executable, str(verifier)], capture_output=True, text=True)
        assert rerun.returncode == 0

        payload = _episode_payload(app, episode["id"])
        assert payload["continuity"]
        assert payload["preflight"]["passed"]
        assert payload["content_review"]["passed"]
        assert payload["artifacts"]["review_board"]
    finally:
        app.close()
