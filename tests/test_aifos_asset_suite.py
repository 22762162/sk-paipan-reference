"""资产中心:人物完整资产套件 + 参考图上传与出图复用。"""

import json
from pathlib import Path

import pytest

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.director import (
    CHARACTER_SHEETS,
    character_candidate_target,
    resolve_character_asset_policy,
)
from aifos.errors import AifosError
from aifos.production.base import ProviderResult

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="万妖图录", number=1, asset_mode=None):
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
        if asset_mode:
            app.director.update_character_asset_policy(
                episode["id"], asset_mode)
        app.director.produce(title, number, pause_for_confirm=True)
    return app.projects.get_project(title)


def test_story_bible_stays_in_continuity_but_not_shot_provider_prompt(app):
    project = _preproduce(app, title="归途")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    continuity, _ = app.projects.latest_document(
        episode["id"], "continuity")
    assert script["story_world"]["overview"]
    assert script["story_background"]["core_conflict"]
    assert all(character["introduction"]
               for character in script["characters"])
    assert continuity["story_world"] == script["story_world"]
    assert continuity["story_background"] == script["story_background"]
    assert continuity["characters"][0]["introduction"]

    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out_root / "render_plan.json").read_text(
        encoding="utf-8"))
    shot = next(item for item in plan["items"]
                if item["category"] == "shot_image")
    assert "【镜头合同v2】" in shot["prompt"]
    assert "【单一主动作】" in shot["prompt"]
    assert "故事世界硬约束" not in shot["prompt"]
    assert "本集故事背景" not in shot["prompt"]


def test_background_extras_skip_design_and_generic_support_gets_one_candidate(app):
    script = {
        "project_title": "轻量角色测试",
        "episode_number": 1,
        "episode_title": "车站",
        "logline": "林昭在车站找到线索。",
        "characters": [
            {"name": "林昭", "role": "主角"},
            {"name": "小陈", "role": "配角"},
            {"name": "站台路人", "role": "背景路人"},
        ],
        "scenes": [{
            "scene_no": 1, "location": "车站", "action": "人群短暂让路",
            "characters": ["林昭", "小陈", "站台路人"],
            "lines": [
                {"character": "林昭", "dialogue": "线索就在这里。"},
                {"character": "小陈", "dialogue": "我去确认。"},
                {"character": "站台路人", "dialogue": "借过。"},
            ],
        }],
    }
    summary = app.director.produce(
        "轻量角色测试", 1, script=script, pause_for_confirm=True)
    assert summary["status"] == "awaiting_script"
    summary = app.director.produce(
        "轻量角色测试", 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_cast"
    project = app.projects.get_project("轻量角色测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    saved, _ = app.projects.latest_document(episode["id"], "script")
    extra = next(c for c in saved["characters"]
                 if c["name"] == "站台路人")
    assert extra["crowd_function"]
    assert "introduction" not in extra
    assert app.assets.latest(
        project["id"], "character", "站台路人") is None
    extra_candidates = [
        row for row in app.assets.list(
            project["id"], "character_candidate")
        if app.assets.meta(row).get("character") == "站台路人"
    ]
    assert extra_candidates == []

    selection = app.director.character_selection_status(
        project["id"], saved["characters"])
    by_name = {item["character"]: item
               for item in selection["characters"]}
    assert set(by_name) == {"林昭", "小陈"}
    assert by_name["林昭"]["candidate_target"] == 5
    assert by_name["小陈"]["candidate_target"] == 1


def test_character_suite_generated(app):
    """定版后生成审核板、四张独立母资产及细节套件。"""
    project = _preproduce(app, asset_mode="full")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    assert len(CHARACTER_SHEETS) == 9
    assert [key for key, _label, _desc in CHARACTER_SHEETS[:5]] == [
        "turnaround", "closeup", "front", "profile", "back"]
    for character in script["characters"]:
        name = character["name"]
        for key, label, _desc in CHARACTER_SHEETS:
            row = app.assets.latest(
                project["id"], "character_sheet", f"{name}:{key}")
            assert row is not None, f"缺少 {name}:{key}"
            meta = json.loads(row["meta"])
            assert meta["label"] == label
            if key == "turnaround":
                assert meta["review_only"] is True
                assert meta["aspect"] == "16:9"
            if key in ("closeup", "front", "profile", "back"):
                assert meta["canonical"] is True
    # 生产清单同步登记(看板/图片清单可见)
    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out_root / "render_plan.json").read_text(
        encoding="utf-8"))
    sheet_items = [i for i in plan["items"]
                   if i["category"] == "character_sheet"]
    assert len(sheet_items) == len(script["characters"]) * 9
    assert all(i["status"] in ("done", "reused") for i in sheet_items)


def test_auto_character_asset_policy_is_conservative():
    simple = resolve_character_asset_policy({"mode": "auto"}, {
        "characters": [{"name": "林昭", "role": "主角"}],
        "scenes": [{"location": "书房", "action": "对镜头说一句话"}],
    })
    assert simple["resolved_mode"] == "simple"
    complex_policy = resolve_character_asset_policy({"mode": "auto"}, {
        "characters": [
            {"name": "林昭", "role": "主角"},
            {"name": "周鹿", "role": "重要配角"},
        ],
        "scenes": [{"location": "书房", "action": "两人对话"}],
    })
    assert complex_policy["resolved_mode"] == "full"


def test_simple_character_assets_skip_all_sheets_and_keep_identity_qc(app):
    """简化版真正省掉全部四视图/细节图，不削弱最终立绘门禁。"""
    project = _preproduce(app, title="单人短片", asset_mode="simple")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    policy = app.director.character_asset_policy(episode["id"])
    assert policy["mode"] == "simple"
    assert policy["resolved_mode"] == "simple"
    assert policy["generate_sheets"] is False
    assert app.assets.active_list(project["id"], "character_sheet") == []
    out_root = (app.workspace.artifacts_dir
                / f"p{project['id']:03d}" / "e001")
    plan = json.loads((out_root / "render_plan.json").read_text(
        encoding="utf-8"))
    assert not [item for item in plan["items"]
                if item["category"] == "character_sheet"]
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    refs = app.director._art_refs(
        {"project": dict(project), "character_asset_policy": policy},
        [hero], "")
    assert any(item["kind"] == "character_identity"
               for item in refs["asset_matches"])
    assert not any(item["kind"] == "character_sheet"
                   for item in refs["asset_matches"])
    with pytest.raises(AifosError, match="简化人物资产模式"):
        app.director.regen_image(
            project["title"], 1,
            {"kind": "character_sheet", "name": f"{hero}:turnaround"})


def test_simple_upload_shot_frames_never_reinject_old_turnaround(app):
    """上传替换镜头重做首尾帧也必须遵守简化版参考链。"""
    project = _preproduce(app, title="上传镜头", asset_mode="simple")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    old_turnaround = (app.workspace.artifacts_dir / "legacy-turnaround.png")
    old_turnaround.write_bytes(PNG)
    app.assets.register(
        project["id"], "character_sheet", f"{hero}:turnaround",
        uri=str(old_turnaround), meta={"image_quality": "high"})
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    shot_no = next(shot["shot_no"] for shot in storyboard["shots"]
                   if hero in shot.get("characters", []))
    captured = {}

    def fake_call(_ctx, capability, payload, _sub_dir):
        assert capability == "frames"
        captured.update(payload)
        first = app.workspace.artifacts_dir / "uploaded-first.png"
        last = app.workspace.artifacts_dir / "uploaded-last.png"
        first.write_bytes(PNG)
        last.write_bytes(PNG)
        return ProviderResult(
            provider="mock", model="mock", cost=0,
            data={"first": str(first), "last": str(last)},
            uri=str(first))

    app.director._call = fake_call
    app.director.import_image(
        project["title"], 1, {"kind": "shot", "shot_no": shot_no},
        PNG, ".png")
    assert str(old_turnaround) not in captured.get("character_refs", [])
    assert captured.get("identity_references")


def test_character_asset_policy_rejects_active_persistent_run(app):
    """即使内存 Job 尚未登记，持久运行记录也能阻断模式竞态。"""
    app.director.produce("并发策略", 1, pause_for_confirm=True)
    app.director.produce("并发策略", 1, pause_for_confirm=True)
    project = app.projects.get_project("并发策略")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    app.history.create_run(project["title"], 1, action="regen_image")
    with pytest.raises(AifosError, match="正在生产"):
        app.director.update_character_asset_policy(
            episode["id"], "simple")


def test_legacy_sheet_plan_migrates_to_full_without_silent_downgrade(app):
    project, _ = app.projects.get_or_create_project("旧项目")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.director._episode_dir(project, episode)}
    app.director._plan_write(ctx, {"items": [{
        "id": "sheet:林昭:turnaround", "category": "character_sheet",
        "status": "done",
    }]})
    policy = app.director.character_asset_policy(episode["id"])
    assert policy["mode"] == "full"
    assert policy["source"] == "legacy_migration"
    assert policy["generate_sheets"] is True


def test_switching_to_simple_keeps_history_but_excludes_old_turnaround(app):
    """切到简化版不删旧资产，但旧四视图不再污染后续参考链。"""
    app.director.produce("保留历史", 1, pause_for_confirm=True)
    app.director.produce("保留历史", 1, pause_for_confirm=True)
    project = app.projects.get_project("保留历史")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    candidate = app.assets.latest(
        project["id"], "character_candidate", f"{hero}:01")
    app.assets.register(
        project["id"], "character_sheet", f"{hero}:turnaround",
        uri=candidate["uri"], meta={"image_quality": "high"})
    for character in script["characters"]:
        app.director.select_character_candidate(
            project["title"], 1, character["name"], 1)
    app.director.update_character_asset_policy(episode["id"], "simple")
    app.director.produce(project["title"], 1, pause_for_confirm=True)
    policy = app.director.character_asset_policy(episode["id"])
    refs = app.director._art_refs(
        {"project": dict(project), "character_asset_policy": policy},
        [hero], "")
    assert len(app.assets.active_list(
        project["id"], "character_sheet")) == 1
    assert not any(item["kind"] == "character_sheet"
                   for item in refs["asset_matches"])


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
    """参考图按单一用途注入；全局画风不混入人物/服装参考列表。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    hero = script["characters"][0]["name"]
    other = "不存在的角色"
    app.director.add_reference(project["title"], "官方设定", PNG, ".png",
                               attach_to=hero, reference_role="identity")
    app.director.add_reference(
        project["title"], "画风参考", PNG, ".png",
        reference_role="style")
    app.director.add_reference(project["title"], "别人的图", PNG, ".png",
                               attach_to=other)
    uris = app.director._reference_uris(project["id"], [hero])
    names = [u.split("/")[-1] for u in uris]
    assert any("官方设定" in n or n.startswith("____") for n in names)
    assert len(uris) == 1
    # 画风作为独立 style_ref，不得伪装成人物/服装参考。
    ctx = {"project": dict(project)}
    refs = app.director._art_refs(ctx, [hero], "")
    assert len(refs.get("reference_images", [])) == 1
    assert refs.get("style_ref")
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


def test_corrected_asset_supersedes_old_version_without_deleting_history(app):
    """修正版进入当前资产，错误旧图只隐藏并保留可回溯关系。"""
    project = _preproduce(app, title="修正版替代")
    active_before = app.assets.active_list(project["id"], "scene_art")
    old = active_before[0]
    replacement = app.assets.register(
        project["id"], "scene_art", old["name"], uri=old["uri"],
        meta={"revision": 2}, new_version=True)
    superseded = app.assets.mark_superseded(
        old["id"], replacement["id"], reason="scene_revision")

    active_after = app.assets.active_list(project["id"], "scene_art")
    assert len(active_after) == len(active_before)
    assert replacement in active_after
    assert old not in active_after
    metadata = app.assets.meta(superseded)
    assert metadata["superseded"] is True
    assert metadata["superseded_by_asset_id"] == replacement["id"]
    assert Path(old["uri"]).exists()
    assert len(app.assets.history(
        project["id"], "scene_art", old["name"])) == 2


def test_video_references_are_versioned_and_used(app):
    """按镜头选择资产图后，视频资产记录实际引用的 asset_id。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    shot = storyboard["shots"][0]
    shot_no = shot["shot_no"]
    scene = app.assets.latest(
        project["id"], "character_sheet",
        f"{shot['characters'][0]}:turnaround")
    document = app.director.set_video_references(
        episode["id"], shot_no, [scene["id"]])
    assert document["shots"][str(shot_no)][0]["asset_id"] == scene["id"]
    summary = app.director.produce(project["title"], 1)
    assert summary["status"] == "done"
    video = app.assets.latest(
        project["id"], "video", f"e001_shot{shot_no:03d}")
    meta = json.loads(video["meta"])
    assert any(
        item["asset_id"] == scene["id"]
        for item in meta["reference_assets"])
    effective = app.director.effective_video_references(episode["id"])
    if effective["shots"][str(shot_no)]["spatial_reference_required"]:
        assert meta["reference_assets"][0]["kind"] == "spatial_blocking"
        assert meta["reference_manifest"][0]["index"] == 3


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


def test_character_portrait_is_not_reused_as_cross_character_style_ref(app):
    """主角立绘只决定生成顺序，不得把主角脸污染其他人物和场景。"""
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
    portraits = [p for p in payloads if p.get("portrait")]
    from aifos.director import character_candidate_target
    expected = sum(character_candidate_target(c) for c in script["characters"])
    assert len(portraits) == expected
    assert all(p.get("portrait_candidate") for p in portraits)
    for p in payloads:
        if p.get("character_sheet") or p.get("scene_art"):
            assert not p.get("style_ref")
    # 分镜画面由项目文字画风、人物最终立绘与场景基准共同约束，
    # 不再额外上传另一个角色的脸作为“画风图”。
    shots = [p for p in payloads if p.get("shot_no")]
    assert shots and all(not p.get("style_ref") for p in shots)


def test_restyle_project_regenerates_all_art(app):
    """换画风:旧身份失效，按角色重要度重生成候选并等待人工定版。"""
    project = _preproduce(app)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
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
    # 历史候选槽位继续保留为版本记录；有效候选数量由角色重要度控制。
    assert set(before).issubset(after)
    for key, version in before.items():
        assert after[key] == version + 1, f"{key} 未重做"
    selection = app.director.character_selection_status(
        project["id"], script["characters"])
    assert all(item["candidate_count"] == character_candidate_target(
        next(c for c in script["characters"]
             if c["name"] == item["character"]))
               for item in selection["characters"])
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
