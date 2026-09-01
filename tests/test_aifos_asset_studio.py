"""资产工坊:自定义命名的自建资产库(人物/画风/场景/物品)。

覆盖:四类资产的生产与单一职责、自建资产在后续制作里的参考复用、
改一改的版本叠加、提示词审核豁免、AI 代写提示词、跨作品复用、
资产中心索引与 Web API/前端契约。
"""

import http.client
import json
import threading
from pathlib import Path

import pytest

from aifos.adapters.claude_script import (build_prompt, validate_asset_prompt)
from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.errors import AifosError
from aifos.production.mock import MockProvider
from aifos.web.server import _image_asset_catalog, serve

STATIC = Path(__file__).parents[1] / "aifos" / "web" / "static"


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    instance.projects.get_or_create_project("雨夜凶杀", style="写实电影感")
    yield instance
    instance.close()


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw)


# ---- 生产:四类资产各自的用途与产物 ----

def test_studio_generates_freely_named_character_asset(app):
    result = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚·冷艳版",
        "二十七岁东方女性,冷艳短发,黑色风衣,锐利眼神,纯色灰底棚拍")
    assert result["asset_type"] == "character"
    assert result["reference_role"] == "identity"
    # 用户自己起的名字直接成为关联对象:剧本里出现这个角色时自动生效
    assert result["attach_to"] == "林晚·冷艳版"
    item = result["items"][0]
    assert Path(item["uri"]).exists()
    project = app.projects.get_project("雨夜凶杀")
    row = app.assets.latest(project["id"], "reference", "林晚·冷艳版")
    meta = app.assets.meta(row)
    assert meta["studio"] is True
    assert meta["studio_asset_type"] == "character"
    assert meta["reference_role"] == "identity"
    assert meta["prompt"].startswith("二十七岁东方女性")
    assert meta["quality_source"] == "asset_studio"


@pytest.mark.parametrize("asset_type,name,role,attached", [
    ("style", "夜雨霓虹基准", "style", ""),
    ("scene", "雨夜天台", "scene", "雨夜天台"),
    ("prop", "青铜罗盘", "manual", ""),
])
def test_studio_covers_style_scene_and_prop(app, asset_type, name, role,
                                            attached):
    result = app.director.generate_studio_asset(
        "雨夜凶杀", asset_type, name,
        "冷蓝霓虹与湿反光,高对比,电影级质感,干净构图")
    assert result["reference_role"] == role
    assert result["attach_to"] == attached
    assert Path(result["items"][0]["uri"]).exists()


def test_studio_prop_bound_to_character_becomes_wardrobe_reference(app):
    result = app.director.generate_studio_asset(
        "雨夜凶杀", "prop", "青铜罗盘",
        "手掌大小的青铜罗盘,篆刻刻度,磨损铜绿,单体展示",
        attach_to="林晚")
    assert result["reference_role"] == "wardrobe"
    assert result["attach_to"] == "林晚"


def test_studio_multi_count_writes_distinct_files(app):
    result = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣", count=3)
    assert len(result["items"]) == 3
    names = [item["name"] for item in result["items"]]
    assert names == ["林晚", "林晚 02", "林晚 03"]
    files = {item["uri"] for item in result["items"]}
    assert len(files) == 3
    assert all(Path(uri).exists() for uri in files)


def test_studio_name_never_overwrites_existing_asset(app):
    first = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣,雨夜霓虹")
    second = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "换一套造型的同名角色,米色风衣")
    assert first["items"][0]["name"] == "林晚"
    assert second["items"][0]["name"] == "林晚 02"
    assert first["items"][0]["uri"] != second["items"][0]["uri"]


# ---- 改一改:同名资产叠新版本,原图作为待修改基底 ----

def test_studio_revision_stacks_new_version_and_keeps_history(app, monkeypatch):
    base = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣")
    base_id = base["items"][0]["asset_id"]
    seen = {}
    original = app.router.call

    def spy(capability, payload, out_dir, cancel=None):
        if capability == "image":
            seen["manifest"] = payload.get("reference_manifest") or []
            seen["feedback"] = payload.get("feedback")
        return original(capability, payload, out_dir, cancel=cancel)

    monkeypatch.setattr(app.router, "call", spy)
    revised = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣",
        feedback="头发再长一点,改成齐肩", base_asset_id=base_id, count=3)
    # 同名改图一次只出一张,避免历史版本被连叠淹没
    assert len(revised["items"]) == 1
    assert revised["items"][0]["name"] == "林晚"
    assert revised["items"][0]["version"] == 2
    assert seen["feedback"] == "头发再长一点,改成齐肩"
    labels = [entry["label"] for entry in seen["manifest"]]
    assert any("待修改基底" in label for label in labels)
    bindings = " ".join(entry["binding"] for entry in seen["manifest"])
    assert "只修正修改意见指出的问题" in bindings
    project = app.projects.get_project("雨夜凶杀")
    history = app.assets.history(project["id"], "reference", "林晚")
    assert [row["version"] for row in history] == [1, 2]
    assert Path(history[0]["uri"]).exists()   # 旧版本原文件仍在


# ---- 后续制作:自建资产真的能当参考图用 ----

def test_studio_character_feeds_production_as_identity_reference(app):
    app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣")
    project = app.projects.get_project("雨夜凶杀")
    payload = app.director._user_reference_payload(
        project["id"], ["林晚"], allowed_roles={"identity"})
    assert len(payload["reference_images"]) == 1
    assert payload["asset_matches"][0]["reference_role"] == "identity"
    # 没点名的角色不会被这张图污染
    other = app.director._user_reference_payload(
        project["id"], ["周队"], allowed_roles={"identity"})
    assert other["reference_images"] == []


def test_studio_style_asset_is_global_project_style(app):
    app.director.generate_studio_asset(
        "雨夜凶杀", "style", "夜雨霓虹基准", "冷蓝霓虹,湿反光,高对比")
    project = app.projects.get_project("雨夜凶杀")
    rows = app.director._reference_rows(
        project["id"], [], allowed_roles={"style"},
        include_global_style=True)
    assert [row["name"] for row in rows] == ["夜雨霓虹基准"]


def test_studio_reference_selection_reaches_the_image_payload(app,
                                                              monkeypatch):
    anchor = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣")
    anchor_id = anchor["items"][0]["asset_id"]
    seen = {}
    original = app.router.call

    def spy(capability, payload, out_dir, cancel=None):
        if capability == "image":
            seen["refs"] = list(payload.get("reference_images") or [])
            seen["manifest"] = payload.get("reference_manifest") or []
        return original(capability, payload, out_dir, cancel=cancel)

    monkeypatch.setattr(app.router, "call", spy)
    app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚·战损版", "同一人物,脸上擦伤,衣服破损",
        reference_asset_ids=[anchor_id])
    assert seen["refs"] == [anchor["items"][0]["uri"]]
    assert seen["manifest"][0]["role"] == "identity"


# ---- 提示词:豁免剧集审核门禁 + AI 代写 ----

def test_studio_prompt_is_exempt_from_episode_prompt_review(app, tmp_path):
    payload = {"prompt": "青铜罗盘单体展示", "prompt_compact": "青铜罗盘单体展示",
               "prompt_review_exempt": True}
    assert app.router.review_image_prompt("image", payload, tmp_path) is None
    review = payload["prompt_review"]
    assert review["status"] == "not_applicable_user_studio"
    assert review["optimized_prompt"] == "青铜罗盘单体展示"


def test_studio_generate_marks_exemption_unless_optimize_requested(
        app, monkeypatch):
    seen = []
    original = app.router.call

    def spy(capability, payload, out_dir, cancel=None):
        if capability == "image":
            seen.append(payload.get("prompt_review_exempt"))
        return original(capability, payload, out_dir, cancel=cancel)

    monkeypatch.setattr(app.router, "call", spy)
    app.director.generate_studio_asset(
        "雨夜凶杀", "character", "甲", "冷艳短发女警,黑风衣")
    app.director.generate_studio_asset(
        "雨夜凶杀", "character", "乙", "冷艳短发女警,黑风衣",
        optimize_prompt=True)
    assert seen == [True, False]


def test_studio_ai_prompt_writes_a_usable_image_prompt(app):
    result = app.director.studio_prompt(
        "雨夜凶杀", "character", name="林晚",
        brief="二十七岁女警,冷艳短发,黑风衣")
    assert len(result["image_prompt"]) >= 20
    assert result["asset_type_label"] == "人物形象"


def test_asset_prompt_bridge_contract():
    prompt = build_prompt("script", {
        "asset_prompt": True, "asset_type": "prop",
        "asset_type_label": "物品道具", "asset_name": "青铜罗盘",
        "brief": "手掌大小,篆刻刻度", "references": ["/tmp/a.png"],
    })
    assert "青铜罗盘" in prompt and "物品道具" in prompt
    assert "/tmp/a.png" in prompt
    assert validate_asset_prompt({"image_prompt": "短"}) is not None
    data = {"image_prompt": "手掌大小的青铜罗盘,篆刻刻度,磨损铜绿,居中单体展示"}
    assert validate_asset_prompt(data) is None
    assert data["negative_prompt"] == "" and data["notes"] == []
    assert validate_asset_prompt({
        "image_prompt": "```这是围栏包起来的提示词,不能直接生图```",
    }) is not None


# ---- Provider 契约:四类资产的单一职责必须下发到产线 ----

@pytest.mark.parametrize("kind,rule", [
    ("character", "纯净无场景背景"),
    ("style", "不绑定具体人物身份"),
    ("scene", "画面中不出现任何人物"),
    ("prop", "居中单体展示"),
])
def test_codex_bridge_carries_studio_single_responsibility(tmp_path, kind,
                                                          rule):
    instruction, targets, data = build_instruction("image", {
        "studio_asset": kind, "studio_asset_label": "自建资产",
        "art_name": "测试资产", "prompt_compact": "一条用户自己写的提示词",
        "width": 1080, "height": 1920, "aspect": "9:16",
        "prompt_contract_complete": True,
    }, tmp_path)
    assert rule in instruction
    assert "$imagegen" in instruction          # 仍然强制真实出图
    assert targets[0].name == f"studio_{kind}_测试资产.png"
    assert data["studio_asset"] == kind


def test_mock_provider_handles_studio_payload(tmp_path):
    provider = MockProvider("mock", {"enabled": True})
    result = provider.generate("image", {
        "studio_asset": "scene", "studio_asset_label": "场景空间",
        "art_name": "雨夜天台", "prompt": "雨夜写字楼天台",
        "aspect": "16:9",
    }, tmp_path)
    assert Path(result.uri).exists()
    assert result.data["studio_asset"] == "scene"


# ---- 资产中心索引与跨作品复用 ----

def test_asset_catalog_separates_studio_assets(app):
    app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣")
    project = app.projects.get_project("雨夜凶杀")
    items = _image_asset_catalog(app, project["id"])
    entry = next(item for item in items if item["name"] == "林晚")
    assert entry["studio"] is True
    assert entry["board_group"] == "studio"
    assert entry["board_group_label"] == "自建资产库"
    assert entry["category"] == "character"
    assert entry["studio_asset_type"] == "character"
    assert entry["prompt_status"] == "recorded"
    assert "关联林晚" in entry["usage_label"]


def test_studio_asset_copies_to_another_project_without_duplicating_file(app):
    app.projects.get_or_create_project("凡人修仙传", style="国风漫剧")
    created = app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣")
    asset_id = created["items"][0]["asset_id"]
    copied = app.director.copy_studio_asset("雨夜凶杀", asset_id, "凡人修仙传")
    assert copied["project"] == "凡人修仙传"
    assert copied["uri"] == created["items"][0]["uri"]
    target = app.projects.get_project("凡人修仙传")
    row = app.assets.latest(target["id"], "reference", copied["name"])
    meta = app.assets.meta(row)
    assert meta["copied_from_project"] == "雨夜凶杀"
    assert meta["reference_role"] == "identity"


def test_studio_options_expose_types_roles_and_attach_targets(app):
    app.director.generate_studio_asset(
        "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣")
    options = app.director.studio_asset_options("雨夜凶杀")
    assert [item["value"] for item in options["asset_types"]] == [
        "character", "style", "scene", "prop"]
    assert "identity" in {item["value"] for item in options["reference_roles"]}
    # 2:1 是场景全景母版专用比例,不进工坊画幅下拉
    assert options["aspects"] == ["16:9", "9:16"]
    assert options["max_count"] == 4
    assert "林晚" in options["attach_options"]


# ---- 失败必须在界面可见 ----

@pytest.mark.parametrize("kwargs,message", [
    ({"asset_type": "unknown", "name": "甲", "prompt": "一条足够长的提示词"},
     "资产类型"),
    ({"asset_type": "character", "name": "", "prompt": "一条足够长的提示词"},
     "起个名字"),
    ({"asset_type": "character", "name": "甲", "prompt": "太短"}, "提示词太短"),
])
def test_studio_rejects_bad_input_with_readable_message(app, kwargs, message):
    with pytest.raises(AifosError) as excinfo:
        app.director.generate_studio_asset(
            "雨夜凶杀", kwargs["asset_type"], kwargs["name"],
            kwargs["prompt"])
    assert message in str(excinfo.value)


def test_studio_rejects_reference_from_another_project(app):
    app.projects.get_or_create_project("凡人修仙传", style="国风漫剧")
    other = app.director.generate_studio_asset(
        "凡人修仙传", "character", "韩立", "少年修士,粗布衣,竹林晨雾")
    with pytest.raises(AifosError) as excinfo:
        app.director.generate_studio_asset(
            "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣",
            reference_asset_ids=[other["items"][0]["asset_id"]])
    assert "不属于本作品" in str(excinfo.value)


@pytest.mark.parametrize("aspect", ["4:3", "2:1"])
def test_studio_rejects_non_shot_aspect(app, aspect):
    """2:1 虽在 ASPECT_DIMS 里,但它是全景母版专用,工坊不许单独选。"""
    with pytest.raises(AifosError) as excinfo:
        app.director.generate_studio_asset(
            "雨夜凶杀", "character", "林晚", "冷艳短发女警,黑风衣",
            aspect=aspect)
    assert "画幅" in str(excinfo.value)


# ---- Web API ----

def test_asset_studio_web_endpoints(tmp_path):
    workspace = tmp_path / "workspace"
    app = App(workspace)
    app.projects.get_or_create_project("雨夜凶杀", style="写实电影感")
    app.projects.get_or_create_project("凡人修仙传", style="国风漫剧")
    app.close()
    server = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, options = _request(
            port, "GET", "/api/asset-studio/options?project=%E9%9B%A8%E5%A4%9C"
                         "%E5%87%B6%E6%9D%80")
        assert status == 200
        assert options["max_count"] == 4

        status, missing = _request(port, "GET", "/api/asset-studio/options")
        assert status == 400 and "project" in missing["error"]

        status, created = _request(port, "POST", "/api/asset-studio/generate", {
            "project": "雨夜凶杀", "asset_type": "character",
            "name": "林晚·冷艳版",
            "prompt": "二十七岁东方女性,冷艳短发,黑色风衣,纯色灰底棚拍",
        })
        assert status == 201
        assert created["reference_role"] == "identity"
        asset_id = created["items"][0]["asset_id"]

        status, catalog = _request(
            port, "GET", "/api/asset-images?project=%E9%9B%A8%E5%A4%9C"
                         "%E5%87%B6%E6%9D%80")
        assert status == 200
        entry = next(item for item in catalog["items"]
                     if item["asset_id"] == asset_id)
        assert entry["studio"] is True and entry["board_group"] == "studio"

        status, written = _request(port, "POST", "/api/asset-studio/prompt", {
            "project": "雨夜凶杀", "asset_type": "scene",
            "brief": "雨夜写字楼天台,霓虹反光",
        })
        assert status == 200 and len(written["image_prompt"]) >= 20

        status, copied = _request(port, "POST", "/api/asset-studio/copy", {
            "project": "雨夜凶杀", "asset_id": asset_id,
            "target_project": "凡人修仙传",
        })
        assert status == 201 and copied["project"] == "凡人修仙传"

        status, rejected = _request(
            port, "POST", "/api/asset-studio/generate", {
                "project": "雨夜凶杀", "asset_type": "character",
                "name": "林晚", "prompt": "短",
            })
        assert status == 400 and "提示词太短" in rejected["error"]
    finally:
        server.shutdown()
        server.server_close()


# ---- 前端契约 ----

def test_asset_studio_ui_contract():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "assetStudioHtml(studioOptions, studioDraft, boardItems)" in js
    assert "/api/asset-studio/options" in js
    assert "/api/asset-studio/prompt" in js
    assert "/api/asset-studio/generate" in js
    assert "/api/asset-studio/copy" in js
    assert 'data-studio-type="' in js
    assert "让 AI 写提示词" in js
    assert "data-studio-edit" in js and "data-studio-copy" in js
    assert 'key: "studio", label: "自建资产库"' in js
    assert ".asset-studio {" in css
    assert ".studio-type.active" in css
    assert ".studio-ref-card.on" in css
    assert ".studio-copy-box" in css
