"""剧本导入测试:文本/JSON 解析与自带剧本生产。"""

import json

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.script_import import (
    ScriptImportError, parse_any, parse_text_script)

SAMPLE = """第1场 古镇长街
夜色渐深,妖气翻涌。
林昭:这股妖气不对劲。
小狐:小心,它就在附近!

第2场 藏经阁
妖王现身。
妖王:你们来晚了。
林昭:封印,现在!
"""


def test_parse_text_script():
    script = parse_text_script(SAMPLE, "万妖图录", 20)
    assert len(script["scenes"]) == 2
    assert script["scenes"][0]["location"] == "古镇长街"
    assert script["scenes"][0]["action"] == "夜色渐深,妖气翻涌。"
    assert script["scenes"][1]["lines"][0] == {
        "character": "妖王", "dialogue": "你们来晚了。"}
    # 台词最多者为主角
    roles = {c["name"]: c["role"] for c in script["characters"]}
    assert roles["林昭"] == "主角"
    assert roles["小狐"] == "配角"
    assert script["episode_number"] == 20


def test_parse_without_scene_header():
    script = parse_text_script("甲:你好\n乙:再见", "T", 1)
    assert len(script["scenes"]) == 1
    assert script["scenes"][0]["location"] == "主场景"


def test_parse_rejects_no_dialogue():
    with pytest.raises(ScriptImportError):
        parse_text_script("只有描写没有台词", "T", 1)
    with pytest.raises(ScriptImportError):
        parse_any("", "T", 1)


def test_parse_json_passthrough():
    script = {"characters": [{"name": "甲", "role": "主角"}],
              "scenes": [{"scene_no": 1, "location": "x",
                          "lines": [{"character": "甲", "dialogue": "y"}]}]}
    parsed = parse_any(json.dumps(script, ensure_ascii=False), "题", 3)
    assert parsed["project_title"] == "题"
    with pytest.raises(ScriptImportError):
        parse_any('{"scenes": []}', "题", 3)


def test_produce_with_provided_script(tmp_path):
    app = App(tmp_path / "ws")
    try:
        script = parse_text_script(SAMPLE, "万妖图录", 20)
        summary = app.director.produce("万妖图录", 20, script=script)
        assert summary["status"] == "done"
        project = app.projects.get_project("万妖图录")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=20",
            (project["id"],))
        saved, _ = app.projects.latest_document(episode["id"], "script")
        assert saved["scenes"][0]["location"] == "古镇长街"
        # 人物从剧本自动登记为 IP 资产
        assert app.assets.latest(project["id"], "character", "林昭")
        assert app.assets.latest(project["id"], "character", "妖王")
        # 分镜从剧本推导:2 场 + 4 句台词 = 6 镜
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        assert len(storyboard["shots"]) == 6
    finally:
        app.close()


def test_cli_script_file(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    script_file = tmp_path / "my_script.txt"
    script_file.write_text(SAMPLE, encoding="utf-8")
    code = main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "20", "--script-file", str(script_file)])
    out = capsys.readouterr().out
    assert code == 0
    assert "已导入剧本:2 场,3 个角色" in out
    assert "制作完成" in out

    bad = tmp_path / "bad.txt"
    bad.write_text("没有台词", encoding="utf-8")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "21", "--script-file", str(bad)]) == 2
