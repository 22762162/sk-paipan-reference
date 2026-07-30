"""Codex 给了修改意见就自动执行,不再推给人工。

《雨夜凶杀》第1集实测:21 个关键帧里 15 个锁死在待人工,其中 6 个的失败
原因是「Codex 已完成升级分析并通知 AIFOS 执行 repair_contract」——修复
指令一字不差写好了,却没有任何环节去执行它。另有 1 个(shot:8)问题全是
[建议·不影响通过]、结构化核验逐项达标,仍被合同诊断一票否决锁死。
"""

import pytest

from aifos.app import App
from aifos.director import Director


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
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


def test_repair_is_capped_so_a_bad_shot_cannot_burn_quota_forever(
        app, monkeypatch):
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
    for _ in range(Director.CODEX_CONTRACT_REPAIR_LIMIT):
        assert app.director._auto_apply_codex_escalation(
            ctx, task, _Result(_escalation_qc()))
    # 超过上限后转人工,不再无限改
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc())) == ""


def test_repair_that_does_not_change_the_input_falls_back_to_human(
        app, monkeypatch):
    """合同没真的变时再画一次撞的是同一份坏数据,必须转人工。"""
    ctx, task = _ctx_and_task(app, monkeypatch)
    monkeypatch.setattr(Director, "_shot_payload",
                        lambda _s, _c, _shot: dict(task["payload"]))  # 合同没变
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc())) == ""


def test_non_shot_tasks_have_no_contract_to_repair(app, monkeypatch):
    ctx, task = _ctx_and_task(app, monkeypatch, item_id="scene:废茶棚")
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc())) == ""


def test_stage_images_seeds_stored_codex_repair_directly_into_three_draws(
        app, monkeypatch):
    project, _ = app.projects.get_or_create_project("断点自动三抽")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
    out_root.mkdir(parents=True, exist_ok=True)
    stored_qc = _escalation_qc(
        instruction="统一为135mm平视双人胸像")
    stored_qc.update({"attempts": 2, "consecutive_failures": 2})
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
    }]})

    monkeypatch.setattr(app.director, "_plan_seed_shots", lambda _ctx: None)
    monkeypatch.setattr(app.director, "_distill_lessons", lambda _ctx: 0)
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
