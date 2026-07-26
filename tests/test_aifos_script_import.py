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


def test_parse_chinese_novel_dialogue_and_preserve_original_words():
    source = """大明第一集
乾清宫内烛影摇曳。
朱慈烺咬牙道：“父皇，儿臣请战！”
“你可知此去凶险？”崇祯沉声问道。
王承恩忙道：“陛下，城门急报。”
"""
    script = parse_text_script(source, "大明", 1)
    lines = script["scenes"][0]["lines"]
    assert lines == [
        {"character": "朱慈烺", "dialogue": "父皇，儿臣请战！",
         "performance": "咬牙"},
        {"character": "崇祯", "dialogue": "你可知此去凶险？",
         "performance": "沉声"},
        {"character": "王承恩", "dialogue": "陛下，城门急报。",
         "performance": "忙"},
    ]
    assert script["import_analysis"] == {
        "source_format": "novel",
        "dialogue_count": 3,
        "explicit_dialogue_count": 3,
        "inferred_dialogue_count": 0,
        "unresolved_dialogue_count": 0,
        "performance_cue_count": 3,
        "character_count": 3,
        "scene_count": 1,
        "dialogue_preserved_verbatim": True,
    }


def test_parse_standalone_novel_dialogue_uses_context_instead_of_dropping_it():
    source = """苏念说道：“你终于来了。”
“路上堵车。”顾屿答道。
“我等了你三年。”
"""
    script = parse_text_script(source, "重逢", 1)
    lines = script["scenes"][0]["lines"]
    assert [line["dialogue"] for line in lines] == [
        "你终于来了。", "路上堵车。", "我等了你三年。"]
    assert lines[2]["character"] in {"苏念", "顾屿"}
    assert script["import_analysis"]["unresolved_dialogue_count"] == 0


def test_narration_states_are_performance_not_fake_characters():
    source = """朱慈烺试探着问道：“锦盒里是什么？”
“是西洋奇物。”李继周小心翼翼地答道。
“拿来。”朱慈烺冷声道。
“奴婢遵命。”李继周连忙躬身回话道。
"""
    script = parse_text_script(source, "大明", 1)
    assert [line["character"] for line in script["scenes"][0]["lines"]] == [
        "朱慈烺", "李继周", "朱慈烺", "李继周"]
    assert [line.get("performance") for line
            in script["scenes"][0]["lines"]] == [
        "试探着", "小心翼翼地", "冷声", "连忙躬身回话"]
    assert {item["name"] for item in script["characters"]} == {
        "朱慈烺", "李继周"}
    assert script["import_analysis"]["performance_cue_count"] == 4


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
        for character in saved["characters"]:
            app.director.select_character_candidate(
                "万妖图录", 20, character["name"], 1)
        summary = app.director.produce("万妖图录", 20)
        assert summary["status"] == "done"
        saved, _ = app.projects.latest_document(episode["id"], "script")
        assert saved["scenes"][0]["location"] == "古镇长街"
        # 人物从剧本自动登记为 IP 资产
        assert app.assets.latest(project["id"], "character", "林昭")
        assert app.assets.latest(project["id"], "character", "妖王")
        # 原 6 个叙事镜完整保留，并自动补听者反应镜和场尾留白镜。
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        shots = storyboard["shots"]
        assert len([s for s in shots if s.get("dialogue")]) == 4
        assert {"reaction", "beat"} <= {s["kind"] for s in shots}
        assert len(shots) > 6
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
    # 剧本导入后图片资产自动定版，整集直接跑完
    assert "制作完成" in out
    app = App(ws)
    try:
        project = app.projects.get_project("万妖图录")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=20",
            (project["id"],))
        saved, _ = app.projects.latest_document(episode["id"], "script")
        for character in saved["characters"]:
            app.director.select_character_candidate(
                "万妖图录", 20, character["name"], 1)
    finally:
        app.close()
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "20"]) == 0
    assert "制作完成" in capsys.readouterr().out

    bad = tmp_path / "bad.txt"
    bad.write_text("没有台词", encoding="utf-8")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "21", "--script-file", str(bad)]) == 2
