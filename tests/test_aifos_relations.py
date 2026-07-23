"""生产画布关系线 + 参考图为最高标准的人物设定。"""

import json

import pytest

from aifos.app import App
from aifos.relations import build_relations, relation_lines

# 1x1 透明 PNG(参考图上传用)
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
       b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc"
       b"\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`"
       b"\x82")


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


SCRIPT = {
    "characters": [{"name": "周鹿", "role": "主角"},
                   {"name": "小满", "role": "同伴"},
                   {"name": "黑袍人", "role": "反派"}],
    "scenes": [
        {"scene_no": 1, "location": "山门",
         "characters": ["周鹿", "小满"]},
        {"scene_no": 2, "location": "密林",
         "characters": ["周鹿", "小满", "黑袍人"]},
    ],
}
STORYBOARD = {
    "shots": [
        {"shot_no": 1, "scene_no": 1, "characters": ["周鹿"]},
        {"shot_no": 2, "scene_no": 1, "characters": ["周鹿", "小满"]},
        {"shot_no": 3, "scene_no": 2, "characters": ["黑袍人"]},
    ],
}


def test_build_relations_nodes_and_lines():
    rel = build_relations(SCRIPT, STORYBOARD)
    ids = {n["id"] for n in rel["nodes"]}
    assert {"char:周鹿", "char:小满", "scene:1", "scene:2",
            "shot:1", "shot:2", "shot:3"} <= ids
    kinds = {}
    for edge in rel["edges"]:
        kinds.setdefault(edge["type"], []).append(edge)
    # 人物↔人物同场线(关联性牵引)
    co = {(e["from"], e["to"]): e for e in kinds["co_scene"]}
    assert ("char:周鹿", "char:小满") in co \
        or ("char:小满", "char:周鹿") in co
    pair = co.get(("char:周鹿", "char:小满")) \
        or co.get(("char:小满", "char:周鹿"))
    assert pair["scenes"] == [1, 2] and "同场2场" in pair["label"]
    # 人物→场景 / 场景→镜头 / 同场镜头帧链
    assert any(e["from"] == "char:黑袍人" and e["to"] == "scene:2"
               for e in kinds["appears"])
    assert any(e["from"] == "scene:1" and e["to"] == "shot:2"
               for e in kinds["contains"])
    assert any(e["from"] == "shot:1" and e["to"] == "shot:2"
               for e in kinds["sequence"])
    # 不同场之间没有帧链(场景独立)
    assert not any(e["from"] == "shot:2" and e["to"] == "shot:3"
                   for e in kinds["sequence"])


def test_relation_lines_only_for_present_pairs():
    rel = build_relations(SCRIPT, STORYBOARD)
    lines = relation_lines(rel, ["周鹿", "小满"])
    assert lines and "周鹿" in lines[0] and "小满" in lines[0]
    assert "不得互换形象" in lines[0]
    assert relation_lines(rel, ["周鹿"]) == []      # 单人镜头无需关系线
    assert relation_lines(None, ["周鹿", "小满"]) == []


def _lock_all(app, title):
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    for character in script["characters"]:
        app.director.select_character_candidate(
            title, 1, character["name"], 1)


def _preproduce(app, title):
    app.director.produce(title, 1, pause_for_confirm=True)   # 剧本停
    app.director.produce(title, 1, pause_for_confirm=True)   # 选角停
    _lock_all(app, title)                                    # 锁定立绘
    app.director.produce(title, 1, pause_for_confirm=True)   # 预生产停
    project = app.projects.get_project(title)
    out = (app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001")
    return project, out


def test_canvas_written_and_prompts_carry_relation_lines(app):
    """出图阶段落盘 relations.json;多人物镜头提示词带人物关系线。"""
    project, out = _preproduce(app, "万妖图录")
    rel_path = out / "relations.json"
    assert rel_path.exists()
    rel = json.loads(rel_path.read_text(encoding="utf-8"))
    assert rel["schema"] == "aifos.relations/v1"
    assert any(n["type"] == "character" for n in rel["nodes"])
    assert any(e["type"] == "co_scene" for e in rel["edges"])
    plan = json.loads((out / "render_plan.json").read_text(encoding="utf-8"))
    multi = [i for i in plan["items"] if i["category"] == "shot_image"
             and "画面严格共2人" in i.get("prompt", "")
             and "人物关系线" in i.get("prompt", "")]
    # 至少有一个多人物镜头带了关系线(占位剧本必有双人同场镜头)
    assert multi, "没有任何镜头提示词携带人物关系线"


def test_designs_follow_reference_as_top_standard(app):
    """先传参考图再开画:人物设定按参考图脸部特征与风格撰写。"""
    title = "参考图优先"
    app.director.produce(title, 1, pause_for_confirm=True)   # 剧本就绪
    project = app.projects.get_project(title)
    script, _ = app.projects.latest_document(
        app.db.query_one(
            "SELECT id FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))["id"], "script")
    hero = script["characters"][0]["name"]
    app.director.add_reference(title, "定妆照", PNG, ".png",
                               attach_to=hero)
    app.director.produce(title, 1, pause_for_confirm=True)   # 画人物
    design = app.director._character_design(project["id"], hero)
    assert design["reference_locked"] is True
    assert "参考图" in design["appearance"]
    assert "参考图" in design["hair"]


def test_reference_upload_rewrites_existing_design(app):
    """设定已生成后再传参考图:立即按参考图重写该角色设定。"""
    title = "后补参考图"
    app.director.produce(title, 1, pause_for_confirm=True)
    app.director.produce(title, 1, pause_for_confirm=True)   # 设定已生成
    project = app.projects.get_project(title)
    script, _ = app.projects.latest_document(
        app.db.query_one(
            "SELECT id FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))["id"], "script")
    hero = script["characters"][0]["name"]
    before = app.director._character_design(project["id"], hero)
    assert not before.get("reference_locked")
    result = app.director.add_reference(title, "定妆照", PNG, ".png",
                                        attach_to=hero)
    assert result["design_refreshed"] is True
    after = app.director._character_design(project["id"], hero)
    assert after["reference_locked"] is True
    assert "参考图" in after["appearance"]
    # 关联到非角色(如场景)不触发设定重写
    result2 = app.director.add_reference(title, "场景参考", PNG, ".png",
                                         attach_to="不存在的角色")
    assert result2["design_refreshed"] is False


def test_wardrobe_reference_never_rewrites_character_face_design(app):
    """服装图只管服装，不能触发人物身份设定重写。"""
    title = "服装参考分工"
    app.director.produce(title, 1, pause_for_confirm=True)
    app.director.produce(title, 1, pause_for_confirm=True)
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT id FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    before = app.director._character_design(project["id"], hero)
    result = app.director.add_reference(
        title, "服装版型", PNG, ".png", attach_to=hero,
        reference_role="wardrobe")
    after = app.director._character_design(project["id"], hero)
    assert result["design_refreshed"] is False
    assert after == before


def test_relations_exposed_in_episode_payload(app):
    from aifos.web.server import _episode_payload
    project, out = _preproduce(app, "万妖图录")
    episode = app.db.query_one(
        "SELECT id FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    payload = _episode_payload(app, episode["id"])
    assert payload["relations"]["schema"] == "aifos.relations/v1"
    assert any(e["type"] == "co_scene"
               for e in payload["relations"]["edges"])
