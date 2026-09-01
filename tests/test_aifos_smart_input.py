"""作品名自由识别 + 项目改名测试。"""

import http.client
import json
import threading

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.errors import AifosError
from aifos.smart_input import (cn_to_int, parse_free_text,
                               resolve_produce_target)
from aifos.web.server import serve


def test_cn_to_int():
    assert cn_to_int("三") == 3
    assert cn_to_int("十") == 10
    assert cn_to_int("十五") == 15
    assert cn_to_int("二十") == 20
    assert cn_to_int("九十九") == 99
    assert cn_to_int("两") == 2
    assert cn_to_int("abc") is None


def test_parse_free_text_formats():
    known = ["万妖图录", "苏念的一天"]
    # 标准格式保持支持
    assert parse_free_text("开始制作《万妖图录》第15集", known) == \
        ("万妖图录", 15, True)
    # 不写《》
    assert parse_free_text("万妖图录第16集", known) == ("万妖图录", 16, True)
    assert parse_free_text("制作万妖图录 第2集", known) == ("万妖图录", 2, True)
    # 中文数字 / 期 / EP
    assert parse_free_text("万妖图录第十五集", known) == ("万妖图录", 15, True)
    assert parse_free_text("苏念的一天 第3期", known) == ("苏念的一天", 3, True)
    assert parse_free_text("万妖图录 EP7", known) == ("万妖图录", 7, True)
    # 已有项目随便说,不带集数
    assert parse_free_text("把万妖图录再来一集", known) == \
        ("万妖图录", None, True)
    # 新作品:剥掉指令词
    assert parse_free_text("开始制作星海旅人第1集", known) == \
        ("星海旅人", 1, False)


def test_parse_free_text_new_title_stripping():
    title, number, matched = parse_free_text("开始制作星海旅人", [])
    assert (title, number, matched) == ("星海旅人", None, False)
    title, number, _ = parse_free_text("生成《星海旅人》第2集", [])
    assert (title, number) == ("星海旅人", 2)
    title, number, _ = parse_free_text("星海旅人吧", [])
    assert (title, number) == ("星海旅人", None)
    assert parse_free_text("", []) == (None, None, False)


def test_resolve_defaults_to_next_episode(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("万妖图录")
        app.projects.get_or_create_episode(project["id"], 1)
        app.projects.get_or_create_episode(project["id"], 2)
        # 不写集数 → 自动接着做第3集
        title, number, note = resolve_produce_target(app, "把万妖图录再来一集")
        assert (title, number) == ("万妖图录", 3)
        assert "第3集" in note
        # 写了集数 → 用写的
        title, number, _ = resolve_produce_target(app, "万妖图录第16集")
        assert (title, number) == ("万妖图录", 16)
        # 部分名字也能模糊匹配到已有项目
        title, number, note = resolve_produce_target(app, "妖图录 第4集")
        assert (title, number) == ("万妖图录", 4)
        assert "万妖图录" in note
        # 新作品 → 第1集
        title, number, note = resolve_produce_target(app, "星海旅人")
        assert (title, number) == ("星海旅人", 1)
        assert "新作品" in note
        with pytest.raises(AifosError):
            resolve_produce_target(app, "")
    finally:
        app.close()


def test_cli_produce_free_form(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    # 无《》无集数:新作品从第1集开始
    assert main(["--workspace", ws, "produce", "星海旅人",
                 "--kind", "drama"]) == 0
    out = capsys.readouterr().out
    assert "第1集" in out and "星海旅人" in out
    # 再来一次 → 自动第2集
    assert main(["--workspace", ws, "produce", "星海旅人"]) == 0
    out = capsys.readouterr().out
    assert "第2集" in out
    # 认不出目标 → 报错并给示例
    assert main(["--workspace", ws, "produce", ""]) == 2


def test_rename_project(tmp_path):
    app = App(tmp_path / "ws")
    try:
        app.projects.get_or_create_project("旧名字")
        app.projects.get_or_create_project("占位")
        row = app.projects.rename_project("旧名字", "新名字")
        assert row["title"] == "新名字"
        assert app.projects.get_project("旧名字") is None
        with pytest.raises(AifosError):
            app.projects.rename_project("不存在", "x")
        with pytest.raises(AifosError):
            app.projects.rename_project("新名字", "占位")
        with pytest.raises(AifosError):
            app.projects.rename_project("新名字", "  ")
    finally:
        app.close()


def test_cli_project_rename(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "project", "create",
                 "--title", "万妖图录"]) == 0
    capsys.readouterr()
    assert main(["--workspace", ws, "project", "rename",
                 "--title", "万妖图录", "--new-title", "万妖图录·重启"]) == 0
    assert "已改名" in capsys.readouterr().out
    assert main(["--workspace", ws, "project", "list"]) == 0
    assert "万妖图录·重启" in capsys.readouterr().out


def test_web_produce_free_form_and_rename(tmp_path):
    ws = tmp_path / "ws"
    boot = App(ws)
    boot.director.produce("万妖图录", 1)
    boot.close()
    httpd = serve(ws, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=60)

        def post(path, body):
            conn.request("POST", path, body=json.dumps(body),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))

        # 自由文本,不带《》不带集数 → 自动接着做第2集
        status, reply = post("/api/produce",
                             {"sentence": "把万妖图录再来一集"})
        assert status == 202
        assert reply["title"] == "万妖图录" and reply["episode"] == 2
        assert "第2集" in reply["note"]
        # 认不出 → 400 且有引导语
        status, reply = post("/api/produce", {"sentence": ""})
        assert status == 400 and "作品名" in reply["error"]
        # 项目改名
        status, reply = post("/api/project/rename", {
            "title": "万妖图录", "new_title": "万妖图录2077"})
        assert status == 200 and reply["title"] == "万妖图录2077"
        status, reply = post("/api/project/rename", {
            "title": "没有这个", "new_title": "x"})
        assert status == 400
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
