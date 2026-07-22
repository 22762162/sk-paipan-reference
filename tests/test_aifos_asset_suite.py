"""资产中心:人物完整资产套件 + 参考图上传与出图复用。"""

import json
from pathlib import Path

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
    summary = app.director.produce(
        title, number, pause_for_confirm=True)   # 人物候选停
    if summary["status"] == "awaiting_cast":
        project = app.projects.get_project(title)
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], number))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                title, number, character["name"], 1)
        app.director.produce(title, number, pause_for_confirm=True)
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


def test_produced_image_soft_delete_preserves_history(app):
    """资产中心删图写墓碑版本，不物理删除历史文件。"""
    project = _preproduce(app)
    row = app.assets.active_list(project["id"], "scene_art")[0]
    original = Path(row["uri"])
    history_before = len(app.assets.history(
        project["id"], row["kind"], row["name"]))
    result = app.director.delete_image_asset(project["title"], row["id"])
    latest = app.assets.latest(
        project["id"], row["kind"], row["name"], include_deleted=True)
    assert result["history_preserved"] is True
    assert app.assets.is_deleted(latest)
    assert latest["uri"] == ""
    assert original.exists()
    assert len(app.assets.history(
        project["id"], row["kind"], row["name"])) == history_before + 1
    assert row["name"] not in {
        item["name"] for item in app.assets.active_list(
            project["id"], "scene_art")}
    with pytest.raises(AifosError):
        app.director.delete_image_asset(project["title"], row["id"])


def test_video_references_are_versioned_and_used(app):
    """按镜头选择资产图后，视频资产记录实际引用的 asset_id。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    shot_no = storyboard["shots"][0]["shot_no"]
    scene = app.assets.active_list(project["id"], "character_art")[0]
    document = app.director.set_video_references(
        episode["id"], shot_no, [scene["id"]])
    assert document["shots"][str(shot_no)][0]["asset_id"] == scene["id"]
    summary = app.director.produce(project["title"], 1)
    assert summary["status"] == "done"
    video = app.assets.latest(
        project["id"], "video", f"e001_shot{shot_no:03d}")
    meta = json.loads(video["meta"])
    assert meta["reference_assets"][0]["asset_id"] == scene["id"]


def test_shot_generation_prioritizes_matching_asset_center_images(app):
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    script, _ = app.projects.latest_document(episode["id"], "script")
    shot = storyboard["shots"][0]
    location = next(scene["location"] for scene in script["scenes"]
                    if scene["scene_no"] == shot["scene_no"])
    ctx = {"project": dict(project)}
    refs = app.director._art_refs(
        ctx, shot["characters"], location, shot_no=9999)
    matches = [item for item in refs["asset_matches"]
               if item["kind"] in ("image", "first_frame", "last_frame")]
    assert matches, "应优先匹配同人物/同场景的已生产资产"
    assert refs["reference_images"][0] == matches[0]["uri"]
    trace = app.director._reference_inputs(refs)
    assert any(item["source"] == "asset_center" for item in trace["items"])


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

    class FakePopen:
        def __init__(self, args, **_kwargs):
            self.args = args
            self.returncode = 0

        def communicate(self, timeout=None):
            target.write_bytes(PNG)
            return "", ""

    monkeypatch.setattr(codex_image.subprocess, "Popen", FakePopen)
    # 本测试不依赖本机真的装了 codex(CI/沙盒环境同样可跑)
    monkeypatch.setattr(codex_image.shutil, "which",
                        lambda _cmd: "/usr/bin/codex")
    reply = codex_image.run({
        "capability": "image",
        "payload": {"portrait": True, "art_name": "周鹿"},
        "out_dir": str(tmp_path),
    }, "codex", 30, [])
    assert reply["ok"] is True
    assert reply["model"] == "gpt-image-2 (Codex 内置 image_gen)"


def test_codex_frames_reuse_keyframe_and_lock_space(tmp_path):
    keyframe = tmp_path / "shot_001.keyframe.png"
    keyframe.write_bytes(PNG)
    (tmp_path / "frames").mkdir()
    instruction, targets, data = build_instruction("frames", {
        "shot_no": 1, "image_uri": str(keyframe), "prompt": "三人走进大厅",
        "characters": ["甲", "乙", "丙"], "character_count": 3,
        "spatial_constraint": "空间调度锁：严格 3 人；P01 左→中；机位不越轴。",
        "width": 1080, "height": 1920,
    }, tmp_path / "frames")
    assert targets[0].read_bytes() == PNG
    assert data["first_source"] == "keyframe"
    assert "只生成本镜尾帧" in instruction
    assert "最终画面不得画出坐标、节点、箭头" in instruction
    assert "$imagegen" in instruction


def test_production_board_images_are_selectable():
    """直播看板和图片清单都应支持选中、键盘操作与大图预览。"""
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "aifos/web/static/app.js").read_text(encoding="utf-8")
    css = (root / "aifos/web/static/style.css").read_text(encoding="utf-8")
    assert 'data-plan-select="${esc(item.id)}"' in app_js
    assert 'role="button" tabindex="0" aria-pressed="false"' in app_js
    assert "function bindPlanSelection" in app_js
    assert "function showPlanItemPreview" in app_js
    assert "bindPlanSelection(app, data, episodeId)" in app_js
    assert "bindPlanSelection(overlay, data, episodeId)" in app_js
    assert ".plan-selectable.selected" in css
    assert ".plan-preview-main" in css


def test_batch_redraw_is_live_and_auditable_on_mobile():
    """图片清单必须实时展示批量进度、自动改词与实际参考图。"""
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "aifos/web/static/app.js").read_text(encoding="utf-8")
    css = (root / "aifos/web/static/style.css").read_text(encoding="utf-8")
    assert "function watchBatchRedraw" in app_js
    assert "function updateBatchRedrawProgress" in app_js
    assert "function refreshOpenPlanOverlay" in app_js
    assert "提示词已自动修正" in app_js
    assert "本次实际交给出图产线的参考图" in app_js
    assert ".batch-job-progress" in css
    assert ".plan-trace" in css


def test_regen_always_produces_new_image(app):
    """重画必然产生新画面:同一意见连续重画两次,内容也不能相同。
    (占位产线是确定性生成,靠 payload 里的 revision 保证变化。)"""
    from pathlib import Path
    project = _preproduce(app)
    name = app.assets.list(project["id"], "scene_art")[0]["name"]
    target = {"kind": "scene_art", "name": name}
    before = Path(app.assets.latest(
        project["id"], "scene_art", name)["uri"]).read_text(
        encoding="utf-8")
    app.director.regen_image(project["title"], 1, target,
                             feedback="换成红色衣服")
    first = Path(app.assets.latest(
        project["id"], "scene_art", name)["uri"]).read_text(
        encoding="utf-8")
    app.director.regen_image(project["title"], 1, target,
                             feedback="换成红色衣服")
    second = Path(app.assets.latest(
        project["id"], "scene_art", name)["uri"]).read_text(
        encoding="utf-8")
    assert before != first
    assert first != second      # 同意见再画一次也必须换新画面


def test_character_designs_enrich_prompts(app):
    """编剧 AI 人物设定:性格/外貌/服装细节进入立绘与套件提示词。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    design = app.director._character_design(project["id"], name)
    assert design, "人物设定未生成"
    for field in ("personality", "appearance", "costume", "makeup",
                  "palette", "signature"):
        assert design.get(field), f"设定缺少 {field}"
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    portrait = next(i for i in plan["items"] if i["id"] == f"char:{name}")
    assert design["personality"] in portrait["prompt"]
    assert design["costume"] in portrait["prompt"]
    makeup = next(i for i in plan["items"]
                  if i["id"] == f"sheet:{name}:makeup")
    assert design["makeup"] in makeup["prompt"]
    detail = next(i for i in plan["items"]
                  if i["id"] == f"sheet:{name}:costume_detail")
    assert design["costume_detail"] in detail["prompt"]


def test_character_designs_reused_across_episodes(app):
    """人物设定项目级复用:第二集不重新生成,形象稳定。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    first = app.director._character_design(project["id"], name)
    _preproduce(app, number=2)
    assert app.director._character_design(project["id"], name) == first


def test_design_prompt_and_validation(tmp_path):
    """Claude 设定提示词与校验:名单齐全,空泛设定被拒。"""
    from aifos.adapters.claude_script import (build_prompt,
                                              validate_script)
    payload = {"character_design": True, "project_title": "雪夜狐仙",
               "style": "水墨国风",
               "characters": [{"name": "洛尘", "role": "主角"}]}
    prompt = build_prompt("script", payload)
    assert "人物设定" in prompt and "洛尘" in prompt and "designs" in prompt
    ok = {"designs": [{"name": "洛尘", "personality": "外冷内热",
                       "appearance": "瓜子脸冷白皮", "costume": "交领长衫"}]}
    assert validate_script(ok, payload) is None
    assert ok["designs"][0]["makeup"] == ""      # 缺省字段自动补空
    bad = {"designs": [{"name": "洛尘", "personality": "好"}]}
    assert "空泛" in validate_script(bad, payload)
    missing = {"designs": [{"name": "别人", "personality": "x",
                            "appearance": "y", "costume": "z"}]}
    assert "缺少角色设定" in validate_script(missing, payload)


def test_asset_center_has_design_and_lightbox():
    """资产中心展示人物设定,图片可点击放大。"""
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "aifos/web/static/app.js").read_text(encoding="utf-8")
    css = (root / "aifos/web/static/style.css").read_text(encoding="utf-8")
    assert "function designHtml" in app_js
    assert "function showImageLightbox" in app_js
    assert "bindLightbox(app)" in app_js
    assert ".lightbox-box img" in css
    assert "cursor: zoom-in" in css


def test_style_anchor_unifies_all_art(app):
    """风格统一:候选定版后，套件/场景/镜头全部引用最终锚点。"""
    payloads = []
    original = app.director.router.call

    def recording(capability, payload, out_dir, cancel=None):
        if capability == "image":
            payloads.append(dict(payload))
        return original(capability, payload, out_dir, cancel=cancel)

    app.director.router.call = recording
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    anchor = app.director._anchor_character(project["id"])
    assert anchor  # 主角优先
    anchor_uri = app.assets.latest(
        project["id"], "character_art", anchor)["uri"]
    portraits = [p for p in payloads if p.get("portrait")]
    assert len(portraits) == len(script["characters"]) * 5
    assert all(p.get("portrait_candidate") for p in portraits)
    for p in payloads:
        if p.get("character_sheet") or p.get("scene_art"):
            assert p.get("style_ref") == anchor_uri
    # 分镜画面同样携带风格基准
    shots = [p for p in payloads if p.get("shot_no")]
    assert shots and all(p.get("style_ref") == anchor_uri for p in shots)


def test_restyle_project_regenerates_all_art(app):
    """换画风:旧身份失效，重新生成5张候选并等待人工定版。"""
    project = _preproduce(app)
    before = {(r["kind"], r["name"]): r["version"]
              for r in app.assets.list(project["id"])
              if r["kind"] in ("character_candidate", "character_sheet",
                               "scene_art", "character_identity")}
    summary = app.director.restyle_project(
        project["title"], 1, style="赛博朋克霓虹,冷紫主色")
    assert summary["status"] == "awaiting_cast"
    assert summary["style"] == "赛博朋克霓虹,冷紫主色"
    assert app.projects.get_project(
        project["title"])["style"] == "赛博朋克霓虹,冷紫主色"
    after = {(r["kind"], r["name"]): r["version"]
             for r in app.assets.list(project["id"])
             if r["kind"] in ("character_candidate", "character_sheet",
                              "scene_art", "character_identity")}
    assert after.keys() == before.keys()
    for key, version in after.items():
        assert version == before[key] + 1, f"{key} 未重做"
    # 新画风必须重新人工定版，不能自动覆盖最终立绘
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    assert episode["status"] == "awaiting_cast"
    # 新画风进入清单提示词
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    item = next(i for i in plan["items"]
                if i["category"] == "character_candidate")
    assert "赛博朋克霓虹" in item["prompt"]


def test_codex_instruction_includes_style_anchor(tmp_path):
    """Codex 指令包含风格基准图硬约束。"""
    instruction, _, _ = build_instruction("image", {
        "portrait": True, "art_name": "石头", "role": "同伴",
        "prompt": "角色立绘:石头", "style_ref": "/tmp/anchor.png",
        "width": 1080, "height": 1920,
    }, tmp_path)
    assert "风格基准图 /tmp/anchor.png" in instruction
    assert "禁止任何风格漂移" in instruction
