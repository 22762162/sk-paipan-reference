"""预生产确认流测试:暂停 → 确认 → 自动完成;人物/场景图资产。"""

from pathlib import Path

import pytest

from aifos.app import App
from aifos.cli import main


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def test_review_pauses_at_script_first(app):
    """第一道确认:剧本写完先停,一张图都还没画(不花出图额度)。"""
    summary = app.director.produce("万妖图录", 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_script"
    assert [s["stage"] for s in summary["stages"]] == ["script"]
    project = app.projects.get_project("万妖图录")
    assert app.assets.list(project["id"], "character_art") == []
    assert app.assets.latest(project["id"], "image", "e001_shot001") is None


def test_script_confirm_pauses_before_video(app):
    """第二道确认:剧本确认后画人物/场景/分镜/首尾帧,再停等开拍。"""
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    summary = app.director.produce("万妖图录", 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    stages = [s["stage"] for s in summary["stages"]]
    assert stages == [
        "script", "continuity", "cast", "storyboard", "images",
        "text_assets", "frames", "preflight"]
    # 剧本复用不再重写
    script = next(s for s in summary["stages"] if s["stage"] == "script")
    assert script["cost"] == 0
    # 预生产阶段不消耗视频产线
    project = app.projects.get_project("万妖图录")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    preflight, _ = app.projects.latest_document(episode["id"], "preflight")
    assert preflight["passed"]
    assert len(preflight["gates"]) == 11
    # 预生产阶段不消耗视频产线
    assert app.assets.latest(project["id"], "video", "e001_shot001") is None
    # 但人物立绘/场景概念图与首尾帧已就绪
    assert app.assets.latest(project["id"], "character_art",
                             app.assets.list(project["id"],
                                             "character")[0]["name"])
    assert app.assets.list(project["id"], "scene_art")
    assert app.assets.latest(project["id"], "first_frame", "e001_shot001")


def test_confirm_continues_to_done(app):
    app.director.produce("万妖图录", 2, pause_for_confirm=True)   # 剧本停
    app.director.produce("万妖图录", 2, pause_for_confirm=True)   # 预生产停
    summary = app.director.produce("万妖图录", 2)  # 开拍确认 = 无暂停再次调用
    assert summary["status"] == "done"
    # 预生产产物全部复用,只补视频之后的部分
    for stage in ("images", "frames"):
        report = next(s for s in summary["stages"] if s["stage"] == stage)
        assert report["cost"] == 0
    videos = next(s for s in summary["stages"] if s["stage"] == "videos")
    assert videos["cost"] > 0
    project = app.projects.get_project("万妖图录")
    assert Path(app.assets.latest(
        project["id"], "video", "e002_shot001")["uri"]).exists()


def test_provided_script_skips_script_pause(app):
    """用户自带剧本(自己写的不用再过目)→ 直接停在开拍确认。"""
    script = {
        "project_title": "万妖图录", "episode_number": 9,
        "episode_title": "自带剧本", "logline": "测试",
        "characters": [{"name": "阿云", "role": "主角"}],
        "scenes": [{"scene_no": 1, "location": "山门",
                    "characters": ["阿云"], "action": "起手式",
                    "lines": [{"character": "阿云",
                               "dialogue": "开始吧"}]}],
    }
    summary = app.director.produce(
        "万妖图录", 9, script=script, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"


def test_cast_art_reused_across_episodes(app):
    app.director.produce("万妖图录", 1)
    project = app.projects.get_project("万妖图录")
    first_arts = {r["name"]: r["uri"]
                  for r in app.assets.list(project["id"], "character_art")}
    summary = app.director.produce("万妖图录", 3)
    cast = next(s for s in summary["stages"] if s["stage"] == "cast")
    # 第二集与第一集若出现相同角色/场景则复用其立绘
    arts = {r["name"]: r["uri"]
            for r in app.assets.list(project["id"], "character_art")}
    for name, uri in first_arts.items():
        assert arts[name] == uri
    assert cast["status"] == "done"


def test_cli_review_and_confirm(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    code = main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "1", "--review"])
    out = capsys.readouterr().out
    assert code == 0
    # 第一停:剧本先过目,还没开始画图
    assert "剧本" in out and "confirm" in out
    code = main(["--workspace", ws, "confirm", "--project", "万妖图录",
                 "--episode", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "剧本已确认" in out
    # 第二停:预生产完成 → 再确认开拍
    code = main(["--workspace", ws, "confirm", "--project", "万妖图录",
                 "--episode", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "完成" in out


def test_stop_lands_back_to_reviewable_state(app):
    """停止生成:落回最近的可调整检查点,不算失败。"""
    # 剧本已有 → 停止后落回剧本确认
    app.director.produce("停一下", 1, pause_for_confirm=True)
    project = app.projects.get_project("停一下")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], 1))
    app.projects.set_episode_status(episode["id"], "cancelling")
    summary = app.director.produce("停一下", 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_script"
    # 预生产门禁已过 → 停止后落回开拍确认
    app.director.produce("停一下", 1, pause_for_confirm=True)  # 剧本确认
    app.projects.set_episode_status(episode["id"], "cancelling")
    summary = app.director.produce("停一下", 1)
    assert summary["status"] == "awaiting_confirm"
    # 确认后可正常继续到完成
    summary = app.director.produce("停一下", 1)
    assert summary["status"] == "done"


def test_cli_stop(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "1", "--review"]) == 0
    capsys.readouterr()
    # 稳定状态没有可停的生成
    assert main(["--workspace", ws, "stop", "--project", "万妖图录",
                 "--episode", "1"]) == 1


def _plan_of(app, title, number):
    import json
    project = app.projects.get_project(title)
    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / f"e{number:03d}")
    return json.loads(
        (out_root / "render_plan.json").read_text(encoding="utf-8"))


def test_render_plan_lists_every_image_with_prompt(app):
    """图片生产清单:四类图全部列出,每张带提示词,状态全部就绪。"""
    app.director.produce("万妖图录", 1, pause_for_confirm=True)   # 剧本停
    app.director.produce("万妖图录", 1, pause_for_confirm=True)   # 预生产停
    plan = _plan_of(app, "万妖图录", 1)
    cats = {i["category"] for i in plan["items"]}
    assert cats == {"character_art", "scene_art", "shot_image", "frames"}
    assert all(i["prompt"] for i in plan["items"])
    assert all(i["status"] in ("done", "reused") for i in plan["items"])
    # 现画的图都有单张耗时(前端据此估算剩余时间)
    drawn = [i for i in plan["items"] if i["status"] == "done"]
    assert drawn and all(i.get("duration") is not None for i in drawn)
    shots = [i for i in plan["items"] if i["category"] == "shot_image"]
    storyboard, _ = app.projects.latest_document(
        app.db.query_one(
            "SELECT e.id FROM episodes e JOIN projects p "
            "ON p.id=e.project_id WHERE p.title=? AND e.number=1",
            ("万妖图录",))["id"], "storyboard")
    assert len(shots) == len(storyboard["shots"])


def test_regen_image_prompt_override(app):
    """单张图可改提示词重画:清单记录自定义提示词并标记已改词。"""
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    name = _plan_of(app, "万妖图录", 1)["items"][0]["name"]
    app.director.regen_image(
        "万妖图录", 1, {"kind": "character_art", "name": name},
        prompt_override="红衣持剑,雪夜屋顶,月光逆光,全身立绘")
    plan = _plan_of(app, "万妖图录", 1)
    item = next(i for i in plan["items"] if i["id"] == f"char:{name}")
    assert item["status"] == "done"
    assert item["custom_prompt"] is True
    assert item["prompt"] == "红衣持剑,雪夜屋顶,月光逆光,全身立绘"


def test_plan_marks_mock_images_with_fallback_reason(app):
    """真假产线透明:占位图必须标注 mock 与回退原因,SVG 自带水印。"""
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    plan = _plan_of(app, "万妖图录", 1)
    done = [i for i in plan["items"] if i["status"] == "done"]
    assert done
    for item in done:
        assert item["provider"] == "mock"
        assert item["real"] is False
        assert any(f["provider"] == "codex" for f in item["fallbacks"])
    # 占位图片自带水印,播放器/画布里也能一眼识别
    art = next(i for i in plan["items"]
               if i["category"] == "character_art")
    project = app.projects.get_project("万妖图录")
    row = app.assets.latest(project["id"], "character_art", art["name"])
    assert "占位示意图" in Path(row["uri"]).read_text(encoding="utf-8")


def test_stop_interrupts_long_provider_call(tmp_path):
    """停止信号能在 2 秒级中断外部产线子进程,而不是等它跑完。"""
    import stat
    import time as _time

    from aifos.errors import ProduceCancelled
    from aifos.production.external import CliProvider

    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    provider = CliProvider("slowcli", {
        "type": "cli", "enabled": True, "capabilities": ["image"],
        "command": ["python3", str(slow)], "timeout": 120})
    start = _time.monotonic()
    with pytest.raises(ProduceCancelled):
        provider.generate("image", {"shot_no": 1}, tmp_path,
                          cancel=lambda: True)
    assert _time.monotonic() - start < 10   # 不等 60 秒,秒级返回
