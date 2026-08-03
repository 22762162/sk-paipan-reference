"""Codex 给了修改意见就自动执行,不再推给人工。

《雨夜凶杀》第1集实测:21 个关键帧里 15 个锁死在待人工,其中 6 个的失败
原因是「Codex 已完成升级分析并通知 AIFOS 执行 repair_contract」——修复
指令一字不差写好了,却没有任何环节去执行它。另有 1 个(shot:8)问题全是
[建议·不影响通过]、结构化核验逐项达标,仍被合同诊断一票否决锁死。
"""

import pytest
from types import SimpleNamespace

from aifos.app import App
from aifos.director import Director
from aifos.errors import ProviderError


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    # 这些用例覆盖严格QC后的Codex自动修订链；创作选片模式默认关闭
    # 内容QC，故在本测试域显式启用旧严格路径。
    instance.config.data.setdefault("defaults", {})[
        "selection_mode"] = False
    instance.config.data["defaults"]["image_content_qc"] = True
    yield instance
    instance.close()


class _Result:
    def __init__(self, qc):
        self.qc = qc


def _escalation_qc(action="repair_contract", instruction="将本镜焦段统一为50mm"):
    return {
        "passed": False,
        "codex_escalation": {
            "triggered": True,
            "status": "completed",
            "aifos_action": action,
            "instruction_to_aifos": instruction,
        },
    }


def _ctx_and_task(app, monkeypatch, *, item_id="shot:2", shot_no=2):
    ctx = {
        "project": {"id": 1, "title": "雨夜凶杀", "style": "写实电影感"},
        "episode": {"id": 1, "number": 1},
        "script": {"scenes": [{"scene_no": 1, "location": "废茶棚"}]},
        "storyboard": {"shots": [
            {"shot_no": shot_no, "scene_no": 1,
             "camera": {"景别": "近景", "角度": "平视", "机位": "正面"},
             "description": "原始镜头描述"}]},
    }
    task = {"item_id": item_id, "tag": shot_no,
            "payload": {"prompt": "原提示词", "shot_no": shot_no},
            "qc_spec": {"item_id": item_id}}
    # 编剧就地修:返回改过的 camera/description,与真实 shot_repair 同形
    def fake_call(_self, _ctx, capability, payload, _sub):
        assert capability == "script" and payload.get("shot_repair")
        assert "50mm" in payload["blocking_reason"]
        class R:
            data = {"camera": {"景别": "中近景", "角度": "平视",
                               "机位": "正面", "焦段": "50mm"},
                    "description": "按 Codex 指令统一为 50mm 后的描述",
                    "repair_summary": "焦段统一为50mm"}
            cost = 0.0
            provider = "deepseek"
        return R()
    monkeypatch.setattr(Director, "_call", fake_call)
    # 真实 _shot_payload 会按改过的 camera/description 重编镜头合同,
    # 生成输入随之变化;假件必须同样让 prompt_compact 变,否则哈希不动。
    monkeypatch.setattr(
        Director, "_shot_payload",
        lambda _s, _c, shot: {"prompt": shot["description"],
                              "prompt_compact": shot["description"],
                              "shot_no": shot["shot_no"]})
    monkeypatch.setattr(Director, "_shot_qc_spec",
                        lambda _s, _c, _p: {"item_id": item_id})
    # 本单测只管「Codex 指令有没有被真正执行」,不测镜头合同编译器:
    # 生成输入直接按 prompt 取哈希,避免假件编译出同一份样板导致误判。
    monkeypatch.setattr(
        Director, "_image_generation_input",
        lambda _s, payload, qc_spec=None: {
            "prompt": str((payload or {}).get("prompt") or ""),
            "input_hash": Director._stable_hash(
                str((payload or {}).get("prompt") or "")),
            "reference_manifest": []})
    monkeypatch.setattr(
        app.projects, "save_document", lambda *a, **k: 1)
    return ctx, task


def test_repair_contract_instruction_is_applied_instead_of_waiting_for_human(
        app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch)
    summary = app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc()))
    assert summary and "50mm" in summary
    # 分镜合同真的被改了,不是只记了个待办
    assert ctx["storyboard"]["shots"][0]["camera"]["焦段"] == "50mm"
    assert "50mm" in ctx["storyboard"]["shots"][0]["description"]
    # 重建后的 payload 参与下一轮出图,且带上 Codex 指令作为修改意见
    assert "50mm" in task["payload"]["prompt"]
    assert "50mm" in task["payload"]["feedback"]
    # 旧的升级结论必须清掉,否则出图前会被既有熔断按旧哈希拦住
    assert "qc_escalation" not in task["payload"]


@pytest.mark.parametrize("action", [
    "repair_contract", "split_shot", "manual_review"])
def test_every_non_redraw_action_with_an_instruction_is_auto_applied(
        app, monkeypatch, action):
    ctx, task = _ctx_and_task(app, monkeypatch)
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc(action=action)))


def test_targeted_redraw_instruction_is_persisted_for_next_candidate_group(
        app, monkeypatch):
    """并行/断点入口也必须落实定向重画指令，不能只靠内层循环。"""
    ctx, task = _ctx_and_task(app, monkeypatch)
    summary = app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc(action="targeted_redraw")))
    assert summary
    assert "Codex自动修订" in task["payload"]["prompt"]
    assert "50mm" in task["payload"]["feedback"]


def test_escalation_without_a_concrete_instruction_still_goes_to_human(
        app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch)
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc(instruction=""))) == ""


def test_repair_is_bounded_to_nine_four_draw_batches(app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch)
    # Codex 每轮给的是不同诊断,合同每次都真的变;这样才走得到上限,
    # 否则第二次修出同一份合同会先被「输入未变化」挡掉。
    rounds = {"n": 0}

    def varying_call(_self, _ctx, capability, payload, _sub):
        rounds["n"] += 1
        class R:
            data = {"camera": {"焦段": f"{40 + rounds['n']}mm"},
                    "description": f"第{rounds['n']}轮修复后的 50mm 描述",
                    "repair_summary": f"第{rounds['n']}轮:焦段统一为50mm"}
            cost = 0.0
            provider = "deepseek"
        return R()

    monkeypatch.setattr(Director, "_call", varying_call)
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc()))
    assert task["payload"]["_codex_contract_repair_count"] == 1
    assert task["payload"]["_auto_repair_batches_used"] == 1
    # 初始四抽后允许最多 9 个自动修复批次；每批仍由外层候选器固定
    # 生成 4 张，总上限 10 轮/40 张。超过上限后不再继续付费抽卡。
    repair_limit = app.director._shot_auto_repair_batches()
    assert repair_limit == 9
    for expected in range(2, repair_limit + 1):
        assert app.director._auto_apply_codex_escalation(
            ctx, task, _Result(_escalation_qc()))
        assert task["payload"]["_auto_repair_batches_used"] == expected
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc())) == ""


def test_fourth_repair_instruction_is_applied_without_human_confirmation(
        app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch)
    ctx["_codex_contract_repairs"] = {"shot:2": 3}
    task["payload"]["_codex_contract_repair_count"] = 3

    def fourth_repair(_ctx, _task, reason):
        assert "深度合同瘦身" in reason
        _task["payload"]["prompt"] = "第4轮：赵典吏移到右后层并补背面锚"
        _task["payload"]["prompt_compact"] = _task["payload"]["prompt"]
        return "已执行第4轮空间与参考图修复"

    monkeypatch.setattr(
        app.director, "_repair_blocked_prompt_shot", fourth_repair)
    summary = app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc(action="repair_contract")))
    assert summary
    assert task["payload"]["_codex_contract_repair_count"] == 4
    assert "赵典吏移到右后层" in task["payload"]["prompt"]


def test_shot_plan_content_change_preserves_repair_round_and_instruction(app):
    ctx = {
        "out_root": app.workspace.artifacts_dir / "p001" / "e001",
    }
    old_qc = _escalation_qc(
        instruction="第4轮：赵典吏移到右后层并补背面锚")
    app.director._plan_write(ctx, {"items": [{
        "id": "shot:20",
        "category": "shot_image",
        "content_hash": "old-contract",
        "status": "failed",
        "autonomous_repair_seeded": True,
        "codex_contract_repair_count": 3,
        "codex_contract_repair": "前三轮已执行",
        "qc": old_qc,
    }]})

    app.director._plan_seed(ctx, "shot_image", [{
        "id": "shot:20",
        "category": "shot_image",
        "content_hash": "new-contract",
        "prompt": "新合同",
    }])

    stored = app.director._plan_read(ctx)["items"][0]
    assert stored["status"] == "pending"
    assert stored["autonomous_repair_seeded"] is True
    assert stored["codex_contract_repair_count"] == 3
    assert stored["codex_contract_repair"] == "前三轮已执行"
    assert stored["qc"]["codex_escalation"]["instruction_to_aifos"].startswith(
        "第4轮")


def test_third_targeted_redraw_becomes_deep_contract_slimming(
        app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch)
    ctx["_codex_contract_repairs"] = {"shot:2": 2}
    seen = {}

    def deep_repair(_ctx, _task, reason):
        seen["reason"] = reason
        _task["payload"]["prompt"] = "第三轮精简后的唯一50mm合同"
        _task["payload"]["prompt_compact"] = _task["payload"]["prompt"]
        return "已完成深度合同瘦身"

    monkeypatch.setattr(
        app.director, "_repair_blocked_prompt_shot", deep_repair)
    summary = app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc(action="targeted_redraw")))
    assert summary
    assert "深度合同瘦身" in seen["reason"]
    assert "家具每条腿必须全显" in seen["reason"]
    assert task["payload"]["_codex_contract_repair_count"] == 3


def test_repair_that_does_not_change_the_input_uses_codex_override(
        app, monkeypatch):
    """编剧漏改时也要执行 Codex 指令，不得重新推回人工确认。"""
    ctx, task = _ctx_and_task(app, monkeypatch)
    monkeypatch.setattr(Director, "_shot_payload",
                        lambda _s, _c, _shot: dict(task["payload"]))  # 合同没变
    monkeypatch.setattr(
        Director, "_image_generation_input",
        lambda _s, payload, qc_spec=None: {
            "prompt": (
                str((payload or {}).get("prompt") or "")
                + str((payload or {}).get("feedback") or "")),
            "input_hash": Director._stable_hash(
                str((payload or {}).get("prompt") or "")
                + str((payload or {}).get("feedback") or "")),
            "reference_manifest": []})
    summary = app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc()))
    assert summary and "最终修复覆盖层" in summary
    assert task["payload"]["_codex_contract_repair_count"] == 1
    assert "取代并作废" in task["payload"]["feedback"]
    assert "50mm" in task["payload"]["feedback"]


def test_nested_prompt_review_block_is_repaired_and_keeps_four_draw_mode(
        app, monkeypatch):
    """合同改完后的内层预审冲突也必须自动修，不能把镜头直接标失败。"""
    ctx, task = _ctx_and_task(app, monkeypatch, item_id="shot:12", shot_no=12)
    ctx["_codex_contract_repairs"] = {"shot:12": 3}
    task["payload"].update({
        "_codex_contract_repair_count": 3,
        "_autonomous_repair_seeded": True,
        "camera": "135mm微俯压缩近景",
        "shot_contract": {"景别": "近景", "焦段": "135mm"},
        "action": "铜符落案后的唯一静态终点",
    })

    def repair(_ctx, current, reason):
        assert "中景" in reason and "近景" in reason
        current["payload"].update({
            "prompt": "最新135mm近景合同",
            "prompt_compact": "最新135mm近景合同",
            "camera": "135mm微俯压缩近景",
            "shot_contract": {"景别": "近景", "焦段": "135mm"},
            "action": "铜符落案后的唯一静态终点",
        })
        return "删除旧中景模型约束，只执行最新近景合同"

    monkeypatch.setattr(
        app.director, "_repair_blocked_prompt_shot", repair)
    monkeypatch.setattr(
        app.director, "_shot_qc_spec",
        lambda _ctx, _payload: {"item_id": "shot:12"})
    monkeypatch.setattr(
        app.director, "_attach_reference_manifest", lambda _payload: None)

    summary = app.director._auto_repair_prompt_review_block(
        ctx, task, ProviderError(
            "真实图片已被阻止：最新135mm近景与旧景别锁定为中景互斥"))

    assert summary
    payload = task["payload"]
    assert payload["_codex_contract_repair_count"] == 4
    # 合同预审修复发生在首次出图前；不能误标成“视觉失败后的三张
    # 返工批”，否则会跳过首次四张候选。
    assert "_autonomous_repair_seeded" not in payload
    assert payload["prompt_conflict_resolution"]["shot_contract"]["景别"] \
        == "近景"
    assert "替代并作废旧提示词" in payload["feedback"]


def test_repaired_prompt_review_context_explicitly_voids_stale_contract(app):
    payload = {
        "prompt": "旧模型约束写中景",
        "shot_no": 12,
        "camera": "135mm微俯压缩近景",
        "shot_contract": {"景别": "近景", "焦段": "135mm"},
        "action": "铜符落案后的唯一静态终点",
        "_codex_contract_repair_count": 4,
        "prompt_conflict_resolution": {
            "revision_round": 4,
            "policy": "最新近景合同替代旧中景约束",
        },
    }

    context = app.router._prompt_review_context("image", payload)

    assert context["shot_contract"]["景别"] == "近景"
    master = context["master_state_precedence"]
    assert master["revision_round"] == 4
    assert master["latest_shot_contract"]["焦段"] == "135mm"
    assert "作废" in master["policy"]


def test_non_shot_tasks_have_no_contract_to_repair(app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch, item_id="scene:废茶棚")
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc())) == ""


def test_completed_shot_is_registered_immediately_and_idempotently(
        app, tmp_path):
    project, _ = app.projects.get_or_create_project("逐张登记")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    uri = tmp_path / "shot_001.keyframe.png"
    uri.write_bytes(b"new-image")
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "script": {"scenes": [{"scene_no": 1, "location": "验牒书房"}]},
    }
    shot = {
        "shot_no": 1,
        "scene_no": 1,
        "characters": ["沈砚舟"],
        "end_state": {"沈砚舟": {
            "wardrobe": "深靛圆领袍",
            "headwear": "素黑网巾",
            "hair_makeup": "黑发低髻",
        }},
    }
    result = SimpleNamespace(
        uri=str(uri), provider="image_api", model="gpt-image-2",
        fallbacks=[], data={"image_quality": "high"},
        qc={"passed": True, "score": 1900})
    quality = {
        "level": "high", "recommended": "high",
        "source": "auto", "rule": "critical",
        "reasons": ["主角关键帧"],
    }

    app.director._register_completed_shot_result(
        ctx, shot, quality, result)
    app.director._register_completed_shot_result(
        ctx, shot, quality, result)

    rows = app.assets.history(
        project["id"], "image", "e001_shot001")
    assert len(rows) == 1
    assert rows[0]["uri"] == str(uri)
    meta = app.director._asset_meta(rows[0])
    assert meta["shot_no"] == 1
    assert meta["qc"]["passed"] is True
    assert result.data["_shot_asset_registered"] is True


def test_reconcile_replaces_stale_same_name_asset_metadata(app, tmp_path):
    project, _ = app.projects.get_or_create_project("断点补登当前镜头")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = tmp_path / "p001" / "e001"
    image_uri = out_root / "images" / "shot_001.keyframe.png"
    image_uri.parent.mkdir(parents=True)
    image_uri.write_bytes(b"current-shot")
    shot = {
        "shot_no": 1, "scene_no": 1, "characters": ["沈砚舟"],
        "description": "当前剧本的验牒镜头",
    }
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": out_root,
        "script": {"scenes": [{
            "scene_no": 1, "location": "临江县衙验牒书房",
        }]},
        "storyboard": {"shots": [shot]},
    }
    # 同一 project_id/name 下仍有上一个剧本留下的登记记录；文件路径
    # 已被当前通过图覆盖，但元数据哈希还是旧合同。
    app.assets.register(
        project["id"], "image", "e001_shot001",
        uri=str(image_uri),
        meta={"shot_content_hash": "old-script", "qc": {"passed": True}})
    app.director._plan_write(ctx, {"items": [{
        "id": "shot:1", "category": "shot_image",
        "status": "done", "output_uri": str(image_uri),
        "image_quality": "high",
        "qc": {"passed": True, "score": 1880},
    }]})

    report = app.director.reconcile_completed_shot_images(ctx)

    assert report["recovered"] == 1
    row = app.assets.latest(
        project["id"], "image", "e001_shot001")
    meta = app.director._asset_meta(row)
    assert meta["shot_content_hash"] == app.director._shot_content_hash(shot)
    assert meta["location"] == "临江县衙验牒书房"
    assert meta["qc"]["passed"] is True


def test_stage_images_seeds_stored_codex_repair_directly_into_three_draws(
        app, monkeypatch):
    project, _ = app.projects.get_or_create_project("断点自动三抽")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
    out_root.mkdir(parents=True, exist_ok=True)
    stored_qc = _escalation_qc(
        instruction="统一为135mm平视双人胸像")
    # 兼容旧版断点：有 triggered 与完整指令，但没有 status 字段。
    stored_qc["codex_escalation"].pop("status")
    stored_qc.update({"attempts": 2, "consecutive_failures": 2})
    stale_canonical = out_root / "images" / "shot_015.keyframe.png"
    stale_canonical.parent.mkdir(parents=True, exist_ok=True)
    stale_canonical.write_bytes(b"old-failed-image")
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": out_root,
        "script": {"scenes": [{"scene_no": 1, "location": "内书房"}]},
        "storyboard": {"shots": [{
            "shot_no": 15, "scene_no": 1, "characters": [],
        }]},
    }
    app.director._plan_write(ctx, {"items": [{
        "id": "shot:15", "category": "shot_image",
        "status": "awaiting_human", "qc": stored_qc,
        "output_uri": str(stale_canonical),
    }]})

    monkeypatch.setattr(app.director, "_plan_seed_shots", lambda _ctx: None)
    monkeypatch.setattr(app.director, "_distill_lessons", lambda _ctx: 0)
    monkeypatch.setattr(app.director, "_generation_preflight_issues",
                        lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        app.director, "reconcile_completed_shot_images",
        lambda _ctx: {
            "recovered": 0, "awaiting_human": 0,
            "autonomous_retry": 1, "stale_reset": 0,
            "awaiting_human_shots": [],
        })
    monkeypatch.setattr("aifos.director.write_relations",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app.director, "_shot_payload", lambda _ctx, shot: {
        "shot_no": shot["shot_no"],
        "prompt": "旧镜头提示词",
        "characters": [],
        "quality_decision": {
            "level": "medium", "recommended": "medium",
            "source": "test", "rule": "", "reasons": [],
        },
    })
    monkeypatch.setattr(app.director, "_shot_qc_spec",
                        lambda _ctx, _payload: {})
    monkeypatch.setattr(app.director, "_director_autonomy_enabled",
                        lambda: True)

    def apply(_ctx, task, result):
        assert result.qc == stored_qc
        task["payload"]["prompt"] = "已落实135mm平视双人胸像"
        return "合同已修复"

    captured = {}

    def run(_ctx, tasks, **_kwargs):
        captured["task"] = tasks[0]
        return {}, []

    monkeypatch.setattr(app.director, "_auto_apply_codex_escalation", apply)
    monkeypatch.setattr(app.director, "_run_parallel", run)

    app.director._stage_images(ctx)

    payload = captured["task"]["payload"]
    assert payload["_autonomous_repair_seeded"] is True
    assert payload["qc_consecutive_failures_base"] == 2
    assert "135mm" in payload["prompt"]
    assert payload["prompt"].startswith("【返工静态合同v1】")
    assert payload["feedback"] == ""


def test_codex_numbered_remove_changes_actual_prop_reference_but_not_identity(
        app, tmp_path):
    identity = tmp_path / "identity.png"
    prop = tmp_path / "prop.png"
    identity.write_bytes(b"identity")
    prop.write_bytes(b"prop")
    payload = {
        "prompt": "本镜",
        "identity_references": [{
            "character": "甲", "uri": str(identity), "asset_id": 1,
        }],
        "prop_refs": [str(prop)],
        "asset_matches": [{
            "asset_id": 2, "kind": "prop_identity", "name": "路引",
            "label": "核心道具:路引", "uri": str(prop),
            "reference_role": "prop",
        }],
    }
    app.director._attach_reference_manifest(payload)
    assert [item["role"] for item in payload["reference_manifest"]] == [
        "identity", "prop"]

    changes = app.director._apply_image_reference_adjustments(
        payload, {}, {
            "reference_diagnosis": {"status": "correct", "issues": []},
            "reference_adjustments": [],
        }, instruction="移除参考图2；不得读取原图2")

    assert str(prop) not in payload["prop_refs"]
    assert payload["identity_references"][0]["uri"] == str(identity)
    assert changes["applied"][0]["source"] == "codex_instruction"

    # Even an explicit numbered instruction cannot delete a locked identity.
    blocked = app.director._apply_image_reference_adjustments(
        payload, {}, {
            "reference_diagnosis": {"status": "conflicting", "issues": []},
            "reference_adjustments": [],
        }, instruction="删除参考图1")
    assert payload["identity_references"][0]["uri"] == str(identity)
    assert blocked["applied"] == []
    assert "锁定人物身份" in blocked["skipped"][0]["reason"]


def test_scene_replacement_without_asset_id_resolves_same_project_panorama(
        app, tmp_path):
    project, _ = app.projects.get_or_create_project("场景语义换图")
    main = tmp_path / "main.png"
    panorama = tmp_path / "panorama.png"
    main.write_bytes(b"main")
    panorama.write_bytes(b"panorama")
    main_row = app.assets.register(
        project["id"], "scene_art", "内书室::view:main",
        uri=str(main), meta={"base_location": "内书室", "view": "main"})
    pano_row = app.assets.register(
        project["id"], "scene_art", "内书室::view:panorama",
        uri=str(panorama), meta={
            "base_location": "内书室", "view": "panorama",
            "panorama": True,
        })
    payload = {
        "prompt": "本镜", "location": "内书室",
        "scene_ref": str(main),
        "asset_matches": [{
            "asset_id": main_row["id"], "kind": "scene_art",
            "name": main_row["name"], "label": main_row["name"],
            "uri": str(main), "reference_role": "scene",
        }],
    }
    app.director._attach_reference_manifest(payload)
    changes = app.director._apply_image_reference_adjustments(
        payload, {
            "_project_id": project["id"], "location": "内书室",
        }, {
            "reference_diagnosis": {
                "status": "needs_adjustment", "issues": ["当前图过窄"]},
            "reference_adjustments": [{
                "action": "replace", "target_index": 1, "role": "scene",
                "reason": "替换为清晰同场景广角全景基准图",
                "replacement_selector": {
                    "asset_id": None, "role": "scene", "character": "",
                },
            }],
        })

    assert payload["scene_ref"] == str(panorama)
    assert changes["applied"][0]["replacement_asset_id"] == pano_row["id"]
    assert all(
        item["project_id"] == project["id"]
        for item in [app.assets.get(
            changes["applied"][0]["replacement_asset_id"])])


def test_add_back_identity_detail_resolves_current_same_project_sheet(
        app, tmp_path):
    project, _ = app.projects.get_or_create_project("背面身份补充")
    identity = tmp_path / "identity.png"
    back = tmp_path / "back.png"
    identity.write_bytes(b"identity")
    back.write_bytes(b"back")
    identity_row = app.assets.register(
        project["id"], "character_art", "沈砚舟",
        uri=str(identity), meta={"candidate_asset_id": 700})
    back_row = app.assets.register(
        project["id"], "character_sheet", "沈砚舟:back",
        uri=str(back), meta={"source_candidate_asset_id": 700})
    payload = {
        "prompt": "过肩镜头",
        "identity_references": [{
            "character": "沈砚舟", "uri": str(identity),
            "asset_id": identity_row["id"],
        }],
        "asset_matches": [],
    }
    app.director._attach_reference_manifest(payload)
    changes = app.director._apply_image_reference_adjustments(
        payload, {"_project_id": project["id"]}, {
            "reference_diagnosis": {
                "status": "needs_adjustment", "issues": ["缺少背面轮廓"]},
            "reference_adjustments": [{
                "action": "add",
                # Codex links the supplement to the existing identity anchor.
                # This index must not be mistaken for a protected replacement.
                "target_index": 1,
                "role": "identity_back_silhouette",
                "character": "沈砚舟",
                "reason": "加入已登记背面图，锁定后脑与肩背轮廓",
                "replacement_selector": {
                    "asset_id": None,
                    "role": "identity_back_silhouette",
                    "character": "沈砚舟",
                },
            }],
        })

    assert changes["skipped"] == []
    assert changes["applied"][0]["replacement_asset_id"] == back_row["id"]
    assert str(back) in payload["reference_images"]
    supplement = next(
        item for item in payload["asset_matches"]
        if item["asset_id"] == back_row["id"])
    assert supplement["reference_role"] == "identity_detail"
    assert supplement["attach_to"] == "沈砚舟"


def test_persisted_reference_remove_survives_payload_rebuild(
        app, monkeypatch, tmp_path):
    project, _ = app.projects.get_or_create_project("参考图断点持久化")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    prop = tmp_path / "prop.png"
    prop.write_bytes(b"prop")
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "storyboard": {"shots": [{"shot_no": 7}]},
    }
    task = {"tag": 7, "payload": {}}
    changes = {"applied": [{
        "action": "remove", "target_uri": str(prop),
        "target_asset_id": 99, "role": "prop",
        "reason": "冲突道具参考",
    }]}
    assert app.director._persist_codex_reference_overrides(
        ctx, task, changes) == 1

    rebuilt = {
        "prompt": "断点重建", "location": "内书室",
        "prop_refs": [str(prop)],
        "asset_matches": [{
            "asset_id": 99, "kind": "prop_identity", "name": "路引",
            "label": "核心道具:路引", "uri": str(prop),
            "reference_role": "prop",
        }],
    }
    replay = app.director._apply_persisted_reference_overrides(
        ctx, rebuilt, ctx["storyboard"]["shots"][0])
    assert replay["applied"]
    assert rebuilt["prop_refs"] == []
    assert rebuilt["asset_matches"] == []


def test_spatial_rebind_reason_excludes_camera_even_with_generic_role(
        app, tmp_path):
    spatial = tmp_path / "blocking.png"
    spatial.write_bytes(b"blocking")
    payload = {
        "prompt": "本镜",
        "spatial_ref": str(spatial),
        "asset_matches": [{
            "asset_id": 88, "kind": "spatial_blocking",
            "name": "shot_021_space", "label": "本镜空间调度图",
            "uri": str(spatial), "reference_role": "spatial",
        }],
    }
    app.director._attach_reference_manifest(payload)
    changes = app.director._apply_image_reference_adjustments(
        payload, {}, {
            "reference_diagnosis": {
                "status": "needs_adjustment", "issues": []},
            "reference_adjustments": [{
                "action": "rebind", "target_index": 1, "role": "spatial",
                "reason": "保留站位遮挡，但明确排除图内135mm焦段",
            }],
        })
    assert changes["applied"]
    app.director._attach_reference_manifest(payload)
    item = payload["reference_manifest"][0]
    assert "blocking" in item["inherits"]
    assert "camera" not in item["inherits"]
    assert "focal_length" in item["excludes"]


# ---- 画面达标却被合同诊断一票否决(ep1 shot:8) ----

def _assess(verdict, spec=None):
    spec = spec or {"identity_required": False, "gender_required": False,
                    "wardrobe_required": False, "count_required": False,
                    "physical_logic_required": False}
    return Director._assess_image_qc(Director, spec, verdict, 1)


def test_clean_image_with_only_contract_complaints_now_passes():
    report = _assess({
        "pass": True, "visual_pass": True,
        "issues": ["[建议·不影响通过] 肩别左右相反"],
        "prompt_diagnosis": {"status": "conflicting"},
        "reference_diagnosis": {"status": "correct"},
    })
    assert report["passed"] is True
    assert report["contract_only_defect"] is True
    # 放行不等于无声吞掉:合同问题仍留在审计字段里
    assert report["contract_repair_required"] is True


def test_failed_image_still_hard_fails_on_contract_conflict():
    report = _assess({
        "pass": False, "visual_pass": False, "issues": ["人物身份漂移"],
        "prompt_diagnosis": {"status": "conflicting"},
        "reference_diagnosis": {"status": "correct"},
    })
    assert report["passed"] is False
    assert report["hard_failure"] is True
    assert report["contract_only_defect"] is False


def test_visual_hard_failure_is_untouched_by_the_relaxation():
    report = _assess(
        {"pass": True, "visual_pass": True, "count_checked": True,
         "count_match": False, "detected_count": 5,
         "prompt_diagnosis": {"status": "correct"},
         "reference_diagnosis": {"status": "correct"}},
        spec={"count_required": True, "count": 7})
    assert report["passed"] is False
    assert any("人数" in item for item in report["issues"])
