"""资产中心:人物完整资产套件 + 参考图上传与出图复用。"""

import json

import pytest

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.director import CHARACTER_SHEETS
from aifos.errors import AifosError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="万妖图录", number=1):
    app.director.produce(title, number, pause_for_confirm=True)   # 剧本停
    app.director.produce(title, number, pause_for_confirm=True)   # 预生产停
    return app.projects.get_project(title)


def test_character_suite_generated(app):
    """每个角色除立绘外生成完整资产套件:四视图/特写/特征/妆容/服装。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    assert len(CHARACTER_SHEETS) == 6
    for character in script["characters"]:
        name = character["name"]
        for key, label, _desc in CHARACTER_SHEETS:
            row = app.assets.latest(
                project["id"], "character_sheet", f"{name}:{key}")
            assert row is not None, f"缺少 {name}:{key}"
            assert json.loads(row["meta"])["label"] == label
    # 生产清单同步登记(看板/图片清单可见)
    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out_root / "render_plan.json").read_text(
        encoding="utf-8"))
    sheet_items = [i for i in plan["items"]
                   if i["category"] == "character_sheet"]
    assert len(sheet_items) == len(script["characters"]) * 6
    assert all(i["status"] in ("done", "reused") for i in sheet_items)


def test_character_suite_reused_across_episodes(app):
    """资产套件是项目级资产,第二集不再重画。"""
    project = _preproduce(app)
    first = {r["name"]: r["uri"]
             for r in app.assets.list(project["id"], "character_sheet")}
    assert first
    _preproduce(app, number=2)
    after = {r["name"]: r["uri"]
             for r in app.assets.list(project["id"], "character_sheet")}
    for name, uri in first.items():
        assert after[name] == uri


def test_reference_upload_and_injection(app):
    """参考图上传后自动注入出图参考:全局与按角色关联都生效。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    other = "不存在的角色"
    app.director.add_reference(project["title"], "官方设定", PNG, ".png",
                               attach_to=hero)
    app.director.add_reference(project["title"], "画风参考", PNG, ".png")
    app.director.add_reference(project["title"], "别人的图", PNG, ".png",
                               attach_to=other)
    uris = app.director._reference_uris(project["id"], [hero])
    names = [u.split("/")[-1] for u in uris]
    assert any("官方设定" in n or n.startswith("____") for n in names) \
        or len(uris) == 2   # 关联本角色 + 全局,共 2 张
    assert len(uris) == 2
    # 场景镜头的参考也带上用户参考图
    ctx = {"project": dict(project)}
    refs = app.director._art_refs(ctx, [hero], "")
    assert len(refs.get("reference_images", [])) == 2
    # 删除后不再注入
    app.director.delete_reference(project["title"], "画风参考")
    assert len(app.director._reference_uris(project["id"], [hero])) == 1
    with pytest.raises(AifosError):
        app.director.delete_reference(project["title"], "画风参考")


def test_regen_character_sheet_with_prompt(app):
    """套件单张可按自定义提示词重画,清单标记已改词。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    before = app.assets.latest(
        project["id"], "character_sheet", f"{name}:makeup")
    app.director.regen_image(
        project["title"], 1,
        {"kind": "character_sheet", "name": f"{name}:makeup"},
        prompt_override="烟熏妆,银色眼影,唇色枣红")
    after = app.assets.latest(
        project["id"], "character_sheet", f"{name}:makeup")
    assert after["version"] == before["version"] + 1
    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out_root / "render_plan.json").read_text(
        encoding="utf-8"))
    item = next(i for i in plan["items"]
                if i["id"] == f"sheet:{name}:makeup")
    assert item["custom_prompt"] is True
    assert "烟熏妆" in item["prompt"]


def test_codex_instruction_covers_sheets_and_references(tmp_path):
    """Codex 出图指令包含套件目标文件与用户参考图。"""
    instruction, targets, data = build_instruction("image", {
        "character_sheet": "turnaround", "sheet_label": "四视图",
        "art_name": "周鹿", "prompt": "角色四视图:周鹿",
        "character_refs": ["/tmp/portrait.png"],
        "reference_images": ["/tmp/ref1.png"],
        "width": 1080, "height": 1920,
    }, tmp_path)
    assert "四视图" in instruction
    assert "用户参考图 /tmp/ref1.png" in instruction
    assert "人物设定图 /tmp/portrait.png" in instruction
    assert targets[0].name == "sheet_周鹿_turnaround.png"
    assert data["sheet"] == "turnaround"


def test_modern_otome_instruction_does_not_force_2d(tmp_path):
    instruction, _, _ = build_instruction("image", {
        "portrait": True, "art_name": "周鹿", "role": "主角",
        "style": "现代都市乙女游戏CG，精致3D半写实；禁止古装、汉服",
        "width": 1080, "height": 1920,
    }, tmp_path)
    assert "现代都市乙女游戏CG" in instruction
    assert "禁止古装、汉服" in instruction
    assert "2D 动画质感" not in instruction


def test_codex_bridge_declares_managed_model(monkeypatch, tmp_path):
    from aifos.adapters import codex_image

    target = tmp_path / "portrait_周鹿.png"

    class Reply:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*_args, **_kwargs):
        target.write_bytes(PNG)
        return Reply()

    monkeypatch.setattr(codex_image.subprocess, "run", fake_run)
    reply = codex_image.run({
        "capability": "image",
        "payload": {"portrait": True, "art_name": "周鹿"},
        "out_dir": str(tmp_path),
    }, "codex", 30, [])
    assert reply["ok"] is True
    assert reply["model"].startswith("codex/image_gen-managed")
