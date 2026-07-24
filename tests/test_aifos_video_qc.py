"""视频生成后的质检门与一次自动返工上限。"""

import json

from aifos.app import App


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
