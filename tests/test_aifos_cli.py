"""CLI 测试:一句话指令解析与主要子命令。"""

from types import SimpleNamespace

from aifos.app import App
from aifos.cli import (
    _cmd_batch,
    _cmd_confirm,
    _cmd_produce,
    main,
    parse_produce_sentence,
)


def test_parse_produce_sentence():
    assert parse_produce_sentence("开始制作《万妖图录》第15集") == ("万妖图录", 15)
    assert parse_produce_sentence("《山海食堂》 第 3 集,拜托了") == ("山海食堂", 3)
    assert parse_produce_sentence("随便说点什么") is None
    assert parse_produce_sentence("") is None


def test_cli_init_and_produce(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "init"]) == 0
    assert (tmp_path / "ws" / "config.json").exists()

    code = main(["--workspace", ws, "produce", "开始制作《万妖图录》第15集"])
    out = capsys.readouterr().out
    assert code == 0
    assert "制作完成" in out
    assert "网页逐个定版" not in out
    assert "手机逐张" not in out
    app = App(ws)
    try:
        project = app.projects.get_project("万妖图录")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=15",
            (project["id"],))
        script, _ = app.projects.latest_document(episode["id"], "script")
        selection = app.director.production_asset_selection_status(
            project["id"], script)
        assert selection["passed"] is True
        assert selection["required"] is False
        assert selection["asset_locked"] == selection["asset_total"]
    finally:
        app.close()
    assert "质检得分: 100" in out

    assert main(["--workspace", ws, "status"]) == 0
    assert "万妖图录" in capsys.readouterr().out

    assert main(["--workspace", ws, "stats"]) == 0
    assert "mock" in capsys.readouterr().out

    assert main(["--workspace", ws, "asset", "stats",
                 "--project", "万妖图录"]) == 0
    assert "character" in capsys.readouterr().out

    assert main(["--workspace", ws, "qc",
                 "--project", "万妖图录", "--episode", "15"]) == 0
    assert "通过" in capsys.readouterr().out

    export = tmp_path / "dump.jsonl"
    assert main(["--workspace", ws, "archive", "export",
                 "--out", str(export)]) == 0
    assert export.exists()


def test_cli_produce_rejects_unparsable(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    # 自由识别后,随便一句话都能开工;只有空输入才拒绝
    assert main(["--workspace", ws, "produce", ""]) == 2


def test_cli_viewer_cannot_produce(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "user", "add",
                 "--name", "guest", "--role", "viewer"]) == 0
    code = main(["--workspace", ws, "--user", "guest",
                 "produce", "开始制作《万妖图录》第1集"])
    assert code == 1
    assert "无权" in capsys.readouterr().err


def _done_summary():
    return {
        "status": "done", "qc_score": 100.0, "cost": 0.0,
        "budget": 0.0, "stages": [],
        "outputs": {"final": "", "cover": "", "titles": []},
        "artifacts_dir": "/tmp/aifos-cli-test",
    }


class _FakeDirector:
    def __init__(self):
        self.calls = []

    def produce(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _done_summary()


class _FakeSystem:
    @staticmethod
    def require(_user, _capability):
        return None


def test_cli_produce_and_batch_explicitly_enable_ai_asset_selection():
    director = _FakeDirector()
    app = SimpleNamespace(director=director, system=_FakeSystem())
    produce_args = SimpleNamespace(
        title="自动选优剧", episode=1, sentence="", premise="", style="",
        force=False, script_file=None, review=False, kind="drama",
        user="admin",
    )
    assert _cmd_produce(app, produce_args) == 0

    batch_args = SimpleNamespace(
        title="自动选优剧", start=2, end=3, style="", force=False,
        user="admin",
    )
    assert _cmd_batch(app, batch_args) == 0
    assert len(director.calls) == 3
    assert all(call[1]["auto_select_assets"] is True
               for call in director.calls)


def test_cli_confirm_resumes_legacy_awaiting_cast_without_mobile_gate(capsys):
    director = _FakeDirector()
    project = {"id": 7, "title": "旧人物断点"}
    app = SimpleNamespace(
        director=director,
        system=_FakeSystem(),
        projects=SimpleNamespace(get_project=lambda _title: project),
        db=SimpleNamespace(query_one=lambda _sql, _params: {
            "id": 11, "project_id": 7, "number": 1,
            "status": "awaiting_cast",
        }),
    )
    args = SimpleNamespace(project="旧人物断点", episode=1, user="admin")

    assert _cmd_confirm(app, args) == 0
    out = capsys.readouterr().out
    assert "AI 将自动选优" in out
    assert "无需" not in capsys.readouterr().err
    assert director.calls == [(('旧人物断点', 1), {
        "pause_for_confirm": True,
        "auto_select_assets": True,
    })]
