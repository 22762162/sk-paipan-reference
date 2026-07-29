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


def test_targeted_redraw_is_left_to_the_existing_qc_loop(app, monkeypatch):
    """定向重画质检循环里已经自动执行过,这里不能重复插一手。"""
    ctx, task = _ctx_and_task(app, monkeypatch)
    assert app.director._auto_apply_codex_escalation(
        ctx, task, _Result(_escalation_qc(action="targeted_redraw"))) == ""


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
