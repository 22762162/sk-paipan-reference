"""人物5选1定版、参考图硬门禁与视觉身份质检。"""

from pathlib import Path

import pytest

from aifos.app import App
from aifos.adapters.codex_image import _ref_line, _style_line
from aifos.director import character_candidate_target
from aifos.errors import AifosError, ProviderUnavailable
from aifos.production.base import ProviderResult


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _to_cast_selection(app, title="人物定版测试"):
    first = app.director.produce(title, 1, pause_for_confirm=True)
    assert first["status"] == "awaiting_script"
    second = app.director.produce(title, 1, pause_for_confirm=True)
    assert second["status"] == "awaiting_cast"
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    return project, episode, script


def _lock_all(app, title, script, index=3):
    for character in script["characters"]:
        chosen = min(index, character_candidate_target(character))
        app.director.select_character_candidate(
            title, 1, character["name"], chosen)


def test_role_based_candidates_pause_before_downstream_images(app):
    project, episode, script = _to_cast_selection(app)
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    assert status["candidate_target"] == 5
    assert status["locked"] == 0
    assert all(item["candidate_count"] == character_candidate_target(
        next(c for c in script["characters"] if c["name"] == item["character"]))
               for item in status["characters"])
    for character in status["characters"]:
        target = character["candidate_target"]
        assert len({item["variant_id"] for item in character["candidates"]}) == target
        assert len({item["variant_label"] for item in character["candidates"]}) == target
        assert all(item["variant_source"] == "generated"
                   for item in character["candidates"])
        assert all(set(item["look_variant"]) == {
            "hair", "makeup", "costume", "temperament"}
                   for item in character["candidates"])
    assert app.projects.latest_document(episode["id"], "storyboard")[0] is None
    assert app.assets.list(project["id"], "character_sheet") == []
    assert app.assets.list(project["id"], "scene_art") == []


def test_candidate_count_is_strictly_role_tiered():
    assert character_candidate_target({"role": "主角"}) == 5
    assert character_candidate_target({"role": "重要配角"}) == 3
    assert character_candidate_target({"role": "配角"}) == 1
    assert character_candidate_target({"role": "非重要配角"}) == 1
    assert character_candidate_target({"role": "背景路人"}) == 0
    assert character_candidate_target({"role": "跑龙套"}) == 0


def test_portrait_prompt_prioritizes_identity_over_reference_clothing(app):
    prompt = app.director._portrait_prompt(
        "林昭", "主角", "现代都市3D半写实",
        design={"hair": "长直发", "makeup": "清透妆", "costume": "通勤装"})
    assert "发型轮廓" in prompt and "眉眼妆" in prompt
    assert "允许与参考图服装不同" in prompt


def test_candidate_prompts_create_real_look_variants_without_locking_style(app):
    design = {
        "species": "人类", "appearance": "暖白肤色鹅蛋脸，约25岁",
        "eyes": "深棕杏眼", "personality": "认真但有亲和力",
        "hair": "齐肩内扣短发", "makeup": "清透妆",
        "costume": "浅灰通勤套装", "temperament": "温柔克制",
    }
    variants = [app.director._candidate_variant(i, design) for i in range(1, 6)]
    prompts = [app.director._candidate_portrait_prompt(
        "林昭", "主角", "现代都市半写实", design, variant)
        for variant in variants]
    assert len({item["variant_id"] for item in variants}) == 5
    assert len({tuple(item["look_variant"].values()) for item in variants}) == 5
    assert all("不是同一套衣服只换动作" in prompt for prompt in prompts)
    assert all("人物立绘必须是纯净、无文字的单人物资产背景" in prompt
               for prompt in prompts)
    assert all("无参考图时" in prompt for prompt in prompts)
    assert "齐肩内扣短发" in prompts[0]


def test_reference_portrait_locks_face_and_hair_and_workwear(app):
    design = {"appearance": "鹅蛋脸", "hair": "齐肩短发", "costume": "外卖制服"}
    variant = app.director._candidate_variant(1, design)
    prompt = app.director._candidate_portrait_prompt(
        "外卖小哥", "非重要配角", "现代半写实", design, variant,
        has_reference=True)
    assert "人物脸和发型是最高标准" in prompt
    assert "严格保持参考图发型轮廓" in prompt
    assert "外卖小哥" in prompt and "工作服/制服" in prompt
    assert "纯净、无文字的单人物资产背景" in prompt


def test_candidate_reference_semantics_lock_face_but_release_look():
    payload = {
        "portrait_candidate": True,
        "style": "现代都市半写实",
        "reference_images": ["/tmp/identity.png"],
    }
    ref_line = _ref_line(payload)
    assert "脸和发型是最高标准" in ref_line
    assert "不得改脸或改发型" in ref_line
    assert "妆容、服装、配色、姿态可按本候选造型方向设计" in ref_line
    style_line = _style_line(payload)
    assert "不得用同一造型只换动作" in style_line
    normal = _ref_line({"reference_images": ["/tmp/identity.png"]})
    assert "禁止换脸或换发型" in normal


def test_legacy_candidate_is_not_given_a_fake_look_label(app):
    project, _episode, script = _to_cast_selection(app, "历史候选兼容")
    name = script["characters"][0]["name"]
    old = app.assets.latest(project["id"], "character_candidate", f"{name}:01")
    app.assets.register(
        project["id"], "character_candidate", f"{name}:01",
        uri=old["uri"],
        meta={"character": name, "candidate_index": 1},
        new_version=True,
    )
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    candidate = status["characters"][0]["candidates"][0]
    assert candidate["variant_source"] == "legacy"
    assert candidate["variant_label"] == ""
    assert candidate["look_variant"] is None


def test_locked_candidate_becomes_only_identity_reference(app):
    title = "最终立绘锁定测试"
    project, episode, script = _to_cast_selection(app, title)
    _lock_all(app, title, script, index=4)
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    assert status["passed"]
    for character in status["characters"]:
        selected = next(c for c in character["candidates"] if c["selected"])
        identity = app.assets.latest(
            project["id"], "character_identity", character["character"])
        portrait = app.assets.latest(
            project["id"], "character_art", character["character"])
        assert identity["uri"] == selected["uri"] == portrait["uri"]

    summary = app.director.produce(title, 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "script": script, "storyboard": storyboard,
           "aspect": "9:16", "dims": {"width": 1080, "height": 1920}}
    payload = app.director._shot_payload(ctx, storyboard["shots"][0])
    assert payload["require_reference_images"] is True
    assert len(payload["identity_references"]) \
        == len(storyboard["shots"][0]["characters"])
    locked_uris = {ref["uri"] for ref in payload["identity_references"]}
    assert locked_uris.issubset(set(payload["character_refs"]))


def test_missing_identity_blocks_character_generation_and_qc(app):
    project, _episode, script = _to_cast_selection(app, "缺锚点阻断")
    name = script["characters"][0]["name"]
    with pytest.raises(AifosError, match="尚未锁定最终立绘"):
        app.director._art_refs({"project": dict(project)}, [name], "")
    with pytest.raises(AifosError, match="尚未锁定最终立绘"):
        app.director._qc_spec(project["id"], [name])


def test_router_rejects_text_only_provider_for_people(app, tmp_path):
    router = app.router
    # 临时把通用 API 放到链首且声明不支持参考图；必须跳过它而走 mock。
    api = router.providers["api"]
    api.enabled = True
    api.reference_images = False
    api.conf["endpoint"] = "http://127.0.0.1:1"
    api.conf["api_key"] = "x"
    app.config.data["routing"]["image"] = ["api", "mock"]
    ref = tmp_path / "identity.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    result = router.call("image", {
        "portrait": True, "art_name": "测试", "prompt": "x",
        "character_refs": [str(ref)], "require_reference_images": True,
    }, tmp_path)
    assert result.provider == "mock"
    assert any("不支持真实参考图" in item["reason"]
               for item in result.fallbacks)
    with pytest.raises(ProviderUnavailable, match="未携带任何图片"):
        router.call("image", {
            "portrait": True, "art_name": "测试", "prompt": "x",
            "require_reference_images": True,
        }, tmp_path)


def test_visual_qc_requires_identity_ack_and_reuses_signature(app, tmp_path):
    title = "视觉身份质检"
    project, episode, script = _to_cast_selection(app, title)
    _lock_all(app, title, script)
    summary = app.director.produce(title, 1, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    storyboard, _ = app.projects.latest_document(episode["id"], "storyboard")
    name = script["characters"][0]["name"]
    shot = next(item for item in storyboard["shots"]
                if name in item.get("characters", []))
    target = tmp_path / "target.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    calls = []

    class Router:
        def call(self, capability, payload, out_dir, cancel=None):
            calls.append(payload)
            return ProviderResult(
                provider="vision", cost=0.2,
                data={"pass": True, "identity_checked": True,
                      "identity_match": True,
                      "gender_checked": True, "gender_match": True,
                      "count_checked": True, "count_match": True,
                      "issues": []})

    app.director.router = Router()
    app.assets.register(
        project["id"], "image", f"e001_shot{shot['shot_no']:03d}",
        uri=str(target), meta={"shot_no": shot["shot_no"]}, new_version=True)
    report1 = app.director.qc_item(title, 1, f"shot:{shot['shot_no']}")
    report2 = app.director.qc_item(title, 1, f"shot:{shot['shot_no']}")
    assert report1["passed"] and report1["identity_checked"]
    assert report2["cached"] is True
    assert len(calls) == 1
    assert calls[0]["identity_required"] is True
    assert calls[0]["gender_required"] is True
    assert calls[0]["identity_references"][0]["character"] == name


def test_systemic_identity_failures_trip_redraw_circuit_breaker(app):
    title = "人物漂移熔断"
    project, episode, script = _to_cast_selection(app, title)
    ctx = {"episode": dict(episode),
           "out_root": app.director._episode_dir(project, episode)}
    items = []
    for index in range(1, 4):
        items.append({"id": f"shot:{index}", "category": "shot_image",
                      "shot_no": index, "label": f"镜头{index}",
                      "prompt": "x", "status": "done",
                      "qc": {"passed": False,
                             "issues": ["发型和脸与最终立绘不一致"]}})
    app.director._plan_write(ctx, {"items": items})
    result = app.director.redo_items(title, 1, only_failed=True)
    assert result["status"] == "blocked"
    assert result["reason"] == "systemic_identity_failure"
    assert result["redone"] == 0
