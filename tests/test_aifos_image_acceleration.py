"""尚未开工图片的严格 API 批量加速。"""

import json

import pytest

from aifos.app import App
from aifos.errors import AifosError, ProviderUnavailable
from aifos.production.base import ProviderResult
from aifos.prompt_contract import build_shot_prompt_contract


def _png(path, marker=b"r"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker * 32)
    return str(path)


def _setup(tmp_path, *, with_reference=True):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "image_api": {
                "enabled": True, "api_key": "test-key",
                "model": "gpt-image-2", "reference_images": True,
            },
            "seedream5_lite": {"enabled": False},
            "codex": {"enabled": False},
        },
    })
    project, _ = app.projects.get_or_create_project("加速测试")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
                / "e001")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": out_root}
    item = {"id": "shot:1", "category": "shot_image",
            "label": "镜头 01", "status": "pending",
            "prompt": "林昭在办公室看向镜头"}
    app.director._plan_write(ctx, {"items": [item]})
    identity = _png(out_root / "identity.png") if with_reference else ""
    payload = {
        "shot_no": 1, "characters": ["林昭"],
        "prompt": "林昭在办公室看向镜头", "aspect": "9:16",
        "frame_targets": {"keyframe": {
            "phase": "freeze", "state": "林昭独自在办公室看向镜头",
            "fallback": False,
        }},
        "character_facts": {"林昭": {
            "gender": "女性", "age_range": "24至28岁",
        }},
        "prop_registry": [], "frame_props": [],
        "image_task_class": "batch", "image_quality": "medium",
        "quality_decision": {"level": "medium", "source": "auto"},
        "identity_references": ([{
            "character": "林昭", "asset_id": 7, "uri": identity,
        }] if identity else []),
        "character_refs": [identity] if identity else [],
        "require_reference_images": bool(identity),
    }
    shot = {
        "shot_no": 1, "scene_no": 1,
        "characters": ["林昭"],
        "description": "林昭在办公室看向镜头",
        "frame_targets": {"keyframe": {
            "phase": "freeze", "state": "林昭独自在办公室看向镜头",
            "fallback": False,
        }},
        "character_facts": {"林昭": {
            "gender": "女性", "age_range": "24至28岁",
        }},
        "prop_registry": [], "frame_props": [],
    }
    payload["prompt_contract"] = build_shot_prompt_contract(
        shot, location="办公室", mode="image")
    app.director._attach_reference_manifest(payload)
    review_context = app.router._prompt_review_context("image", payload)
    reviewed_prompt = payload.get("prompt_compact") or payload["prompt"]
    payload["prompt_review"] = {
        "schema": app.router.PROMPT_REVIEW_SCHEMA,
        "approved": True, "status": "approved",
        "optimized_hash": app.router._stable_hash(reviewed_prompt),
        "reviewed_input_hash": app.router._stable_hash({
            "prompt": reviewed_prompt,
            "context": review_context,
            "schema": app.router.PROMPT_REVIEW_SCHEMA,
        }),
    }
    task = {"item_id": "shot:1", "capability": "image",
            "payload": payload, "sub_dir": "images", "tag": 1,
            "priority": 1}
    app.director._prepare_dispatch_contracts(ctx, [task])
    return app, project, episode, ctx, task


def test_preflight_locks_prompt_refs_provider_model_and_defaults_medium(
        tmp_path):
    app, _project, episode, _ctx, _task = _setup(tmp_path)
    try:
        options = app.director.image_acceleration_options("加速测试", 1)
        assert options["default_provider"] == "image_api"
        assert options["default_model"] == "gpt-image-2"
        assert options["default_quality"] == "medium"
        item = next(value for value in options["items"]
                    if value["item_id"] == "shot:1")
        assert item["status"] == "ready"
        assert item["references"]["count"] == 1
        assert item["identity_map"] == {"林昭": item["references"]["items"][0]["uri"]}

        report = app.director.preflight_image_acceleration(
            "加速测试", 1, ["shot:1"], "image_api", "gpt-image-2",
            contract_tokens={"shot:1": item["contract_token"]})
        assert report["passed"] is True
        assert report["quality"] == "medium"
        assert "林昭独自在办公室看向镜头" in \
            report["items"][0]["prompt"]
    finally:
        app.close()


def test_preflight_blocks_text_only_and_wrong_model(tmp_path):
    app, _project, _episode, _ctx, _task = _setup(
        tmp_path, with_reference=False)
    try:
        options = app.director.image_acceleration_options("加速测试", 1)
        item = next(value for value in options["items"]
                    if value["item_id"] == "shot:1")
        assert item["status"] == "blocked"
        assert any("不能只使用文字" in issue for issue in item["issues"])
        report = app.director.preflight_image_acceleration(
            "加速测试", 1, ["shot:1"], "image_api", "wrong-model")
        assert report["passed"] is False
        assert report["items"][0]["status"] == "blocked"
        with pytest.raises(AifosError, match="未通过放行"):
            app.director.queue_image_acceleration(
                "加速测试", 1, ["shot:1"], "image_api", "wrong-model")
    finally:
        app.close()


def test_removed_plan_item_cannot_reappear_in_acceleration_queue(tmp_path):
    """模式切换清掉计划后，历史派发契约只能审计、不能继续生产。"""
    app, _project, _episode, ctx, _task = _setup(tmp_path)
    try:
        app.director._plan_write(ctx, {"items": []})
        options = app.director.image_acceleration_options("加速测试", 1)
        assert not any(item["item_id"] == "shot:1"
                       for item in options["items"])
        report = app.director.preflight_image_acceleration(
            "加速测试", 1, ["shot:1"], "image_api", "gpt-image-2")
        assert report["passed"] is False
        assert "不在当前生产计划" in "；".join(
            report["items"][0]["issues"])
        with pytest.raises(AifosError, match="未通过放行"):
            app.director.queue_image_acceleration(
                "加速测试", 1, ["shot:1"],
                "image_api", "gpt-image-2")
    finally:
        app.close()


def test_prune_removes_only_orphan_rows_from_the_selected_category(tmp_path):
    app, _project, episode, _ctx, _task = _setup(tmp_path)
    try:
        app.director.image_acceleration.register(episode["id"], [{
            "item_id": "shot:22", "category": "shot_image",
            "capability": "image", "contract_token": "orphan-token",
            "contract": {"prompt": "orphan"}, "never_started": True,
        }, {
            "item_id": "frames:1", "category": "frames",
            "capability": "image", "contract_token": "frame-token",
            "contract": {"prompt": "frame"}, "never_started": True,
        }])

        removed = app.director.image_acceleration.prune(
            episode["id"], "shot_image", ["shot:1"])

        assert removed == ["shot:22"]
        assert app.director.image_acceleration.get(
            episode["id"], "shot:1") is not None
        assert app.director.image_acceleration.get(
            episode["id"], "shot:22") is None
        assert app.director.image_acceleration.get(
            episode["id"], "frames:1") is not None
    finally:
        app.close()


def test_simple_policy_blocks_stale_character_sheet_contract(tmp_path):
    """即使旧套件计划尚未刷新，简化策略也必须立即阻断 API 派发。"""
    app, _project, episode, _ctx, _task = _setup(tmp_path)
    try:
        app.db.execute(
            "UPDATE image_dispatch_contracts SET category='character_sheet' "
            "WHERE episode_id=? AND item_id='shot:1'", (episode["id"],))
        app.projects.save_document(episode["id"], "character_asset_policy", {
            "schema": "aifos.character-assets/v1",
            "mode": "simple", "source": "user",
        })
        options = app.director.image_acceleration_options("加速测试", 1)
        assert not any(item["item_id"] == "shot:1"
                       for item in options["items"])
        report = app.director.preflight_image_acceleration(
            "加速测试", 1, ["shot:1"], "image_api", "gpt-image-2")
        assert report["passed"] is False
        assert "简化人物资产模式" in "；".join(
            report["items"][0]["issues"])
    finally:
        app.close()


def test_original_scheduler_claims_queued_api_without_fallback(tmp_path):
    app, _project, episode, ctx, task = _setup(tmp_path)
    calls = []
    try:
        options = app.director.image_acceleration_options("加速测试", 1)
        item = next(value for value in options["items"]
                    if value["item_id"] == "shot:1")
        preflight = app.director.preflight_image_acceleration(
            "加速测试", 1, ["shot:1"], "image_api", "gpt-image-2",
            contract_tokens={"shot:1": item["contract_token"]})
        queued = app.director.queue_image_acceleration(
            "加速测试", 1, ["shot:1"], "image_api", "gpt-image-2",
            fingerprint=preflight["fingerprint"],
            contract_tokens={"shot:1": item["contract_token"]})
        assert queued["quality"] == "medium"

        provider = app.router.providers["image_api"]
        provider.available = lambda capability: (True, "")

        def generate(capability, payload, out_dir, cancel=None):
            calls.append((capability, dict(payload)))
            uri = _png(out_dir / "shot_001.keyframe.png", b"a")
            return ProviderResult(
                provider="image_api", model="gpt-image-2", cost=0.28,
                data={"image_quality": payload["image_quality"]}, uri=uri)

        provider.generate = generate
        app.router.providers["mock"].generate = lambda *args, **kwargs: (
            pytest.fail("严格 API 加速不得回退 mock"))
        app.director._task_cost = 0
        app.director._task_providers = set()
        result = app.director._run_parallel(ctx, [task], line="测试加速")
        assert result[1].provider == "image_api"
        assert calls[0][1]["strict_provider"] == "image_api"
        assert calls[0][1]["model_override"] == "gpt-image-2"
        assert calls[0][1]["image_quality"] == "medium"
        assert calls[0][1]["require_reference_images"] is True
        dispatch = app.director.image_acceleration.get(episode["id"], "shot:1")
        assert dispatch["production_state"] == "generated"
        assert dispatch["acceleration_status"] == "done"
        plan = json.loads((ctx["out_root"] / "render_plan.json").read_text())
        plan_item = plan["items"][0]
        assert plan_item["status"] == "done"
        assert plan_item["acceleration"]["provider"] == "image_api"
    finally:
        app.close()


def test_strict_router_rejects_model_mismatch_before_any_fallback(tmp_path):
    app, _project, _episode, ctx, task = _setup(tmp_path)
    called = []
    try:
        app.router.providers["mock"].generate = lambda *args, **kwargs: called.append("mock")
        payload = dict(task["payload"])
        payload.update({"strict_provider": "image_api",
                        "model_override": "not-configured",
                        "require_reference_images": True})
        with pytest.raises(ProviderUnavailable, match="配置不匹配"):
            app.router.call("image", payload, ctx["out_root"])
        assert called == []
    finally:
        app.close()
