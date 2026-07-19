"""CLI 测试:一句话指令解析与主要子命令。"""

from aifos.cli import main, parse_produce_sentence


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
    assert main(["--workspace", ws, "produce", "做点什么"]) == 2


def test_cli_viewer_cannot_produce(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "user", "add",
                 "--name", "guest", "--role", "viewer"]) == 0
    code = main(["--workspace", ws, "--user", "guest",
                 "produce", "开始制作《万妖图录》第1集"])
    assert code == 1
    assert "无权" in capsys.readouterr().err
