"""题材系统测试:关键词识别、女团模板、人设名提取、类型选择入口。

核心回归场景:用户输入《苏念的一天》+ 前提「偶像女团」,
必须产出女团内容,绝不能出现仙侠模板。
"""

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.production.mock import (
    _detect_genre, _persona_from_title, _strip_genre_words)

XIANXIA_WORDS = ("妖气", "封印", "图录", "妖王")


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _script_of(app, title, number):
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    script, _ = app.projects.latest_document(episode["id"], "script")
    return script


def _all_text(script):
    parts = [script.get("logline", ""), script.get("episode_title", "")]
    for scene in script["scenes"]:
        parts.append(scene.get("action", ""))
        parts += [ln["dialogue"] for ln in scene["lines"]]
    return "\n".join(parts)


def test_persona_from_title():
    assert _persona_from_title("苏念的一天") == "苏念"
    assert _persona_from_title("小澜同学") == "小澜同学"
    assert _persona_from_title("阿柒的舞台日记") == "阿柒"


def test_detect_genre():
    assert _detect_genre({"premise": "偶像女团"}) == "idol"
    assert _detect_genre({"project_title": "都市白领日记"}) == "urban"
    assert _detect_genre({"premise": "校园篮球比赛"}) == "campus"
    assert _detect_genre({"project_title": "万妖图录"}) == "xianxia"
    assert _detect_genre({"template": "idol"}) == "idol"


def test_strip_genre_words():
    assert _strip_genre_words("偶像女团") == ""
    assert _strip_genre_words("偶像女团的新歌打歌舞台")[:2] != "偶像"


def test_su_nian_idol_group_not_xianxia(app):
    """回归:《苏念的一天》+ 偶像女团 → 女团内容,无任何仙侠元素。"""
    summary = app.director.produce("苏念的一天", 1, premise="偶像女团")
    assert summary["status"] == "awaiting_cast"
    script = _script_of(app, "苏念的一天", 1)
    text = _all_text(script)
    for word in XIANXIA_WORDS:
        assert word not in text, f"女团内容混入仙侠词「{word}」"
    # 苏念是队长,共 3 名成员
    roles = {c["role"]: c["name"] for c in script["characters"]}
    assert roles["队长"] == "苏念"
    assert len(script["characters"]) == 3
    locations = [s["location"] for s in script["scenes"]]
    assert "练习室" in locations and "舞台" in locations


def test_urban_premise_gets_urban_flavor(app):
    app.director.produce("逆袭日记", 1, premise="都市职场")
    text = _all_text(_script_of(app, "逆袭日记", 1))
    for word in XIANXIA_WORDS:
        assert word not in text
    assert "方案" in text or "项目" in text


def test_campus_premise(app):
    app.director.produce("青春纪", 1, premise="校园比赛")
    text = _all_text(_script_of(app, "青春纪", 1))
    for word in XIANXIA_WORDS:
        assert word not in text


def test_xianxia_title_keeps_xianxia(app):
    app.director.produce("万妖图录", 1)
    text = _all_text(_script_of(app, "万妖图录", 1))
    assert "妖气" in text


def test_explicit_kind_overrides(app):
    """显式选类型:即使无关键词也走偶像模板,且项目类型更新。"""
    summary = app.director.produce("晨间日记", 1, kind="idol")
    assert summary["status"] == "awaiting_cast"
    project = app.projects.get_project("晨间日记")
    assert project["kind"] == "idol"
    script = _script_of(app, "晨间日记", 1)
    assert all(c["role"] in ("主角", "队长", "主唱", "舞担")
               for c in script["characters"])


def test_cli_kind_flag(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "produce", "--title", "苏念的一天",
                 "--episode", "1", "--kind", "idol"]) == 0
    assert main(["--workspace", ws, "project", "list"]) == 0
    assert "偶像" in capsys.readouterr().out
