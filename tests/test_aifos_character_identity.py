"""人物5选1定版、参考图硬门禁与视觉身份质检。"""

from pathlib import Path

import pytest

from aifos.app import App
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
        app.director.select_character_candidate(
            title, 1, character["name"], index)


def test_five_candidates_pause_before_downstream_images(app):
    project, episode, script = _to_cast_selection(app)
    status = app.director.character_selection_status(
        project["id"], script["characters"])
    assert status["candidate_target"] == 5
    assert status["locked"] == 0
    assert all(item["candidate_count"] == 5
               for item in status["characters"])
    assert app.projects.latest_document(episode["id"], "storyboard")[0] is None
    assert app.assets.list(project["id"], "character_sheet") == []
    assert app.assets.list(project["id"], "scene_art") == []


def test_portrait_prompt_prioritizes_identity_over_reference_clothing(app):
    prompt = app.director._portrait_prompt(
        "林昭", "主角", "现代都市3D半写实",
        design={"hair": "长直发", "makeup": "清透妆", "costume": "通勤装"})
    assert "发型轮廓" in prompt and "眉眼妆" in prompt
    assert "允许与参考图服装不同" in prompt


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
