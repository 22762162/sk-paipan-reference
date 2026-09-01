"""剧本导入测试:文本/JSON 解析与自带剧本生产。"""

import json

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.script_import import (
    ScriptImportError, parse_any, parse_text_script,
    sanitize_script_entities)

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


def test_markdown_screenplay_controls_never_become_cast():
    source = """## 《冒名入仕》
【镜头1】（3秒）
烈日下，林川背着包袱走在明朝官道。
**旁白：**
> 我叫林川，一个现代公务员。
**林川怒骂：**
> “赶着投胎啊！”
**音效：**
> 砰！
**字幕：**
> 未完待续
**优势：**
> 高冲突、高信息密度
"""
    script = parse_text_script(source, "冒名入仕", 1)
    assert [item["name"] for item in script["characters"]] == [
        "旁白（画外声）", "林川"]
    roles = {item["name"]: item["role"] for item in script["characters"]}
    assert roles["旁白（画外声）"] == "背景人物"
    assert roles["林川"] == "主角"
    assert [line["dialogue"] for line in script["scenes"][0]["lines"]] == [
        "我叫林川，一个现代公务员。", "赶着投胎啊！"]
    assert script["scenes"][0]["lines"][1]["character"] == "林川"
    assert script["scenes"][0]["sound_cues"] == [{
        "text": "砰！", "source_label": "音效"}]
    assert script["scenes"][0]["screen_text_cues"] == [{
        "text": "未完待续", "source_label": "字幕"}]
    assert not any(
        token in item["name"]
        for item in script["characters"]
        for token in ("音效", "字幕", "优势", "**"))


def test_markdown_shot_outline_becomes_shootable_scenes_not_analysis_noise():
    source = """如果只是测试流程，这里是编辑建议。
## 《冒名入仕》
### 第1集（15秒）
【镜头1】（3秒）
烈日下，林川背着包袱走在明朝官道。
> 我叫林川，一个现代公务员。
---
【镜头2】（3秒）
林川怒骂：
> “赶着投胎啊！”
---
【镜头3】（4秒）
树林里，林川撞见几名黑衣人。
> 我只是路过，却撞见了一场凶杀。
---
【镜头4】（3秒）
木棍从林川身后砸下。
> 砰！
---
【镜头5】（2秒）
旁白：
> 我，竟成了死去官员。
### 总时长（约15秒）
- 人物少、场景少，适合测试。
"""
    script = parse_text_script(source, "冒名入仕", 1)
    assert len(script["scenes"]) == 5
    assert [scene["duration"] for scene in script["scenes"]] == [
        3.0, 3.0, 4.0, 3.0, 2.0]
    assert script["scenes"][1]["lines"][0] == {
        "character": "林川", "dialogue": "赶着投胎啊！"}
    assert script["scenes"][3]["lines"] == []
    assert script["scenes"][3]["sound_cues"] == [{
        "text": "砰！", "source_label": "音效"}]
    assert script["scenes"][4]["lines"][0]["character"] == "旁白（画外声）"
    assert "编辑建议" not in script["scenes"][0]["action"]
    assert "适合测试" not in script["scenes"][-1]["action"]


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


def test_entity_cleanup_leaves_malformed_scene_for_validator_not_crash():
    script = {
        "characters": [{"name": "甲", "role": "主角"}],
        "scenes": [
            {"scene_no": 1, "location": "客厅", "characters": ["甲"],
             "lines": [{"character": "甲", "dialogue": "开始。"}]},
            "第2场 卧室",
        ],
        "import_analysis": "模型误写成字符串",
    }

    assert sanitize_script_entities(script) is script
    assert script["scenes"][1] == "第2场 卧室"
    assert script["import_analysis"]["character_count"] == 1


def test_produce_with_provided_script(tmp_path):
    app = App(tmp_path / "ws")
    try:
        script = parse_text_script(SAMPLE, "万妖图录", 20)
        summary = app.director.produce("万妖图录", 20, script=script)
        assert summary["status"] == "awaiting_cast"
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
        # 四句台词各自保留为一个8-15秒长镜头；听者反应与场尾留白
        # 折进同镜 setup/main/settle，不再拆成3-5秒碎镜头。
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        shots = storyboard["shots"]
        assert len([s for s in shots if s.get("dialogue")]) == 4
        assert {s["kind"] for s in shots} == {"dialogue"}
        assert all(8 <= float(s["duration"]) <= 15 for s in shots)
        assert all(
            [beat["phase"] for beat in s["temporal_beats"]]
            == ["setup", "main", "settle"]
            for s in shots)
        assert len(shots) == 4
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
    assert "制作人物/道具待选" not in out
    assert "制作完成" in out

    bad = tmp_path / "bad.txt"
    bad.write_text("没有台词", encoding="utf-8")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "21", "--script-file", str(bad)]) == 2


def test_pure_narration_raises_no_dialogue_error():
    """纯叙述梗概(无一句对白)→ NoDialogueError,供上层自动转 AI 编剧。"""
    from aifos.script_import import NoDialogueError, ScriptImportError, parse_any
    synopsis = (
        "洪武二十四年，二十四岁的穿越者林川进京谋生，途中撞见黑衣人"
        "杀死一名书童。他正想逃走，却被人从身后打晕。醒来时，林川发现"
        "自己换上了举人青袍，手里还多了一份任命江浦县主簿的吏部札付。"
        "远处马蹄声骤然逼近，官差已经赶到。")
    with pytest.raises(NoDialogueError):
        parse_any(synopsis, "冒名入仕", 1)
    # NoDialogueError 必须是 ScriptImportError 子类:旧调用方行为不变
    assert issubclass(NoDialogueError, ScriptImportError)
