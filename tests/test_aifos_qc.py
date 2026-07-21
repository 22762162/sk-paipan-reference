"""AI 质检中心测试:各检查项与自动重跑联动。"""

import pytest

from aifos.app import App
from aifos.config import Config
from aifos.qc_center import QcCenter

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


def test_auto_rerun_repairs_missing_video(tmp_path):
    """删除一个镜头视频后重跑质检阶段,导演中心应自动重生成并通过。"""
    app = App(tmp_path / "ws")
    try:
        summary = app.director.produce("重跑剧", 1)
        assert summary["status"] == "done"
        project = app.projects.get_project("重跑剧")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
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
             "uri": str(out_root / "images" /
                        f"shot_{s['shot_no']:03d}.keyframe.svg")}
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
