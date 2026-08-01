"""图片视觉质检:核对剧本要求,不合格自动重画;镜头景别多样性。"""

import copy
import json
import threading
from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.production.base import ProviderResult


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    # 本文件验证旧严格内容QC、自动修订与单图返工语义。新默认选片模式
    # 会有意关闭这些内容判定，因此必须显式关闭选片模式，避免把产品
    # 默认变化误报成严格模式回归。
    instance.config.data.setdefault("defaults", {})[
        "selection_mode"] = False
    instance.config.data["defaults"]["image_content_qc"] = True
    yield instance
    instance.close()


def _preproduce(app, title="小鹿的一天Vlog", number=1):
    app.director.produce(title, number, pause_for_confirm=True)
    summary = app.director.produce(title, number, pause_for_confirm=True)
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


def test_qc_prompt_and_validation():
    from aifos.adapters.claude_script import (build_prompt,
                                              validate_image_qc)
    prompt = build_prompt("image_qc", {
        "image_uri": "/tmp/shot.png", "characters": ["小鹿", "石头"],
        "count": 2, "designs": "小鹿(发型:双丸子头)",
        "location": "夜市", "action": "追查线索",
        "camera": "全景", "forbid": ["字幕条"]})
    assert "/tmp/shot.png" in prompt
    assert "小鹿、石头" in prompt and "共 2 个" in prompt
    assert "名字不代表物种" in prompt
    assert "设定写明物种就按设定画" in prompt
    assert "允许与身份参考图不同" in prompt
    assert "悬挂的衣物" in prompt
    assert "全景" in prompt
    assert "正脸不可见本身不是错误" in prompt
    assert "不得另算成第三人" in prompt
    assert "物理/空间逻辑硬检查" in prompt
    assert "physical_logic_checked" in prompt
    ok = {"pass": True, "issues": []}
    assert validate_image_qc(ok) is None
    bad = {"pass": False, "issues": "镜头9画成了动物"}
    assert validate_image_qc(bad) is None
    assert bad["issues"] == ["镜头9画成了动物"]


def test_qc_blocks_conflicting_input_contract_even_when_image_looks_correct(app):
    from aifos.adapters.claude_script import validate_image_qc

    report = app.director._assess_image_qc({
        "identity_required": False,
        "gender_required": False,
        "count_required": True,
        "count": 1,
        "physical_logic_required": False,
    }, {
        "pass": False,
        "visual_pass": True,
        "input_contract_pass": False,
        "count_checked": True,
        "count_match": True,
        "issues": ["参考图编号与提交顺序冲突"],
        "prompt_diagnosis": {
            "status": "correct", "issues": [],
            "irrelevant_or_conflicting_sections": []},
        "reference_diagnosis": {
            "status": "conflicting", "issues": ["图2用途错位"],
            "missing_roles": []},
        "image_error": {
            "summary": "", "categories": [], "evidence": []},
        "targeted_prompt_patch": {
            "instructions": [], "preserve": [],
            "max_scope": "current_shot_only"},
        "reference_adjustments": [],
    }, attempts=1)

    assert report["image_passed"] is True
    assert report["input_contract_passed"] is False
    assert report["passed"] is False
    assert report["redraw_required"] is True
    assert report["contract_repair_required"] is True
    assert report["input_contract_advisory"] is True
    assert report["contract_hard_failure"] is True
    assert report["hard_failure"] is True
    assert report["qc_policy"] == "visible_major_defects_v2"
    assert validate_image_qc({"issues": []}) == "缺少 pass 字段"


def test_qc_keeps_wording_only_input_advice_non_blocking(app):
    report = app.director._assess_image_qc({
        "identity_required": False,
        "gender_required": False,
        "count_required": True,
        "count": 1,
        "physical_logic_required": False,
    }, {
        "pass": True,
        "visual_pass": True,
        "input_contract_pass": False,
        "count_checked": True,
        "count_match": True,
        "issues": ["提示词略长，可进一步压缩"],
        "prompt_diagnosis": {
            "status": "needs_patch",
            "issues": ["存在不影响执行的重复形容词"],
            "irrelevant_or_conflicting_sections": []},
        "reference_diagnosis": {
            "status": "correct", "issues": [], "missing_roles": []},
    }, attempts=1)

    assert report["image_passed"] is True
    assert report["input_contract_passed"] is False
    assert report["passed"] is True
    assert report["redraw_required"] is False
    assert report["contract_hard_failure"] is False
    assert report["hard_failure"] is False


def test_qc_treats_visible_wardrobe_drift_as_hard_failure(app):
    report = app.director._assess_image_qc({
        "identity_required": False,
        "gender_required": False,
        "wardrobe_required": True,
        "expected_wardrobe": {"沈砚": "青官袍、乌纱帽"},
        "count_required": True,
        "count": 1,
        "physical_logic_required": False,
    }, {
        "pass": True,
        "visual_pass": True,
        "input_contract_pass": True,
        "wardrobe_checked": True,
        "wardrobe_match": False,
        "count_checked": True,
        "count_match": True,
        "issues": ["沈砚从青官袍变成旧月白直裰"],
    }, attempts=1)

    assert report["passed"] is False
    assert report["hard_failure"] is True
    assert report["wardrobe_checked"] is True
    assert report["wardrobe_match"] is False
    assert report["redraw_required"] is True


def test_plan_read_drops_legacy_qc_only_from_initial_assets(app, tmp_path):
    ctx = {"out_root": tmp_path / "episode"}
    app.director._plan_write(ctx, {"items": [
        {
            "id": "sheet:孙九:profile",
            "category": "character_sheet",
            "status": "done",
            "qc": {"passed": False, "issues": ["旧版提示词重复"]},
        },
        {
            "id": "shot:1",
            "category": "shot_image",
            "status": "awaiting_human",
            "qc": {"passed": False, "issues": ["人物数量错误"]},
        },
    ]})

    by_id = {
        item["id"]: item
        for item in app.director._plan_read(ctx)["items"]
    }
    assert "qc" not in by_id["sheet:孙九:profile"]
    assert by_id["shot:1"]["qc"]["passed"] is False


def test_screen_text_rule_uses_only_explicit_whitelist_and_style():
    from aifos.adapters.codex_image import _screen_prop_rule
    prompt = "【镜头合同v2】【主体】电脑屏幕显示页面"
    blocked = _screen_prop_rule(prompt, {
        "carrier": "电脑屏幕", "whitelist": [],
    })
    assert "不得从" in blocked
    assert "镜头合同" in blocked
    assert "镜头合同v2" not in blocked
    locked = _screen_prop_rule(prompt, {
        "carrier": "电脑屏幕", "whitelist": ["东宫书房"],
        "layout": "左上标题栏", "style": "宋体黑字", "perspective": "贴合屏幕",
    })
    assert "东宫书房" in locked
    assert "左上标题栏" in locked
    assert "宋体黑字" in locked
    assert "镜头合同v2" not in locked


def test_readable_non_screen_prop_never_gets_computer_instructions():
    from aifos.adapters.codex_image import _screen_prop_rule

    rule = _screen_prop_rule(
        "铜鱼符特写，青铜錾刻阴文，边缘带包浆",
        {
            "required": True,
            "carrier": "铜鱼符",
            "whitelist": ["清河"],
            "style": "青铜錾刻阴文，边缘带包浆",
        },
    )

    assert rule == ""
    assert "电脑必须" not in rule
    assert "屏幕正对镜头" not in rule


def test_qc_prompt_audits_exact_current_prompt_and_reference_manifest():
    from aifos.adapters.claude_script import build_prompt

    payload = {
        "image_uri": "/tmp/shot-12.png",
        "shot_no": 12,
        "characters": ["李继周"],
        "count": 1,
        "generation_input": {
            "scope": {
                "item_id": "shot:12", "shot_no": 12,
                "frame_kind": "keyframe",
            },
            "prompt": "CURRENT_SHOT_ONLY_SENTINEL 李继周转身",
            "reference_manifest": [{
                "index": 1, "uri": "/tmp/li.png",
                "label": "李继周最终立绘", "role": "identity",
                "character": "李继周", "binding": "只锁脸",
            }],
        },
    }
    prompt = build_prompt("image_qc", payload)

    assert "CURRENT_SHOT_ONLY_SENTINEL" in prompt
    assert '"item_id":"shot:12"' in prompt
    assert '"role":"identity"' in prompt
    assert "prompt_diagnosis" in prompt
    assert "reference_diagnosis" in prompt
    assert "targeted_prompt_patch" in prompt
    assert "reference_adjustments" in prompt
    assert "禁止补写整集剧情或其他镜头" in prompt


def test_codex_qc_prompt_uses_same_structured_input_diagnosis_contract(
        tmp_path):
    from aifos.adapters.codex_image import build_instruction

    instruction, targets, _ = build_instruction("image_qc", {
        "image_uri": "/tmp/shot-7.png",
        "shot_no": 7,
        "characters": ["程沐"],
        "count": 1,
        "generation_input": {
            "scope": {"item_id": "shot:7", "shot_no": 7},
            "prompt": "CODEX_CURRENT_SHOT_SENTINEL",
            "reference_manifest": [{
                "index": 1, "uri": "/tmp/cheng.png",
                "role": "identity", "character": "程沐",
            }],
        },
    }, tmp_path)

    assert targets == []
    assert "CODEX_CURRENT_SHOT_SENTINEL" in instruction
    assert "prompt_diagnosis" in instruction
    assert "reference_diagnosis" in instruction
    assert "targeted_prompt_patch" in instruction
    assert "reference_adjustments" in instruction
    assert "同一人物不能同时穿两套互斥服装" in instruction
    assert "已死亡人物不能继续呼吸" in instruction
    assert "第二次生成" in instruction


def test_codex_qc_checks_historical_prop_morphology(tmp_path):
    from aifos.adapters.codex_image import build_instruction

    instruction, _, _ = build_instruction("image_qc", {
        "image_uri": "/tmp/ming-lamp.png",
        "shot_no": 1,
        "characters": [],
        "count": 0,
        "physical_contract": {
            "rules": [
                "时代物件锁定—油灯：只画明代陶制或青铜开放式浅盏油灯，"
                "灯油与棉芯可见；绝不画玻璃灯罩或煤油灯筒",
            ],
        },
        "generation_input": {
            "scope": {"item_id": "shot:1", "shot_no": 1},
            "prompt": "明代驿馆病榻旁一盏油灯将尽",
            "reference_manifest": [],
        },
    }, tmp_path)

    assert "时代物件锁定—油灯" in instruction
    assert "玻璃灯罩" in instruction
    assert "煤油灯" in instruction
    assert "正确结构" in instruction


def test_legacy_qc_validation_remains_compatible_but_disables_blind_retry():
    from aifos.adapters.claude_script import validate_image_qc

    verdict = {
        "pass": False,
        "identity_checked": True,
        "identity_match": False,
        "issues": ["人物脸与最终立绘不一致"],
    }
    assert validate_image_qc(verdict) is None
    assert verdict["diagnosis_complete"] is False
    assert verdict["image_error"]["summary"] == "人物脸与最终立绘不一致"
    assert verdict["prompt_diagnosis"]["status"] == "unknown"
    assert verdict["reference_diagnosis"]["status"] == "unknown"


def test_screen_prop_rule_never_turns_contract_headings_into_screen_text():
    from aifos.adapters.codex_image import _screen_prop_rule

    prompt = (
        "【镜头合同v1】只执行下列事实。【主体】朱慈烺。"
        "【场景】东宫。【单一主动作】打开电脑。")
    explicit = _screen_prop_rule(prompt, {
        "required": True,
        "carrier": "电脑屏幕",
        "whitelist": ["明季北略", "崇祯"],
    })
    assert "明季北略、崇祯" in explicit
    assert "镜头合同v1" not in explicit
    assert "主体" not in explicit
    assert "单一主动作" not in explicit

    no_whitelist = _screen_prop_rule(prompt, {})
    assert "没有显式可读文字白名单" in no_whitelist
    assert "不得从【镜头合同】【主体】" in no_whitelist
    assert "明季北略" not in no_whitelist


def test_over_shoulder_qc_uses_face_for_front_and_silhouette_for_back(app):
    from aifos.adapters.claude_script import build_prompt

    composition = {
        "composition_type": "over_shoulder_dialogue",
        "expected_primary_count": 1,
        "expected_visible_figure_count": 2,
        "count_rule": (
            "前景半身背影是李继周本人，只计作该角色1人，不得另算"),
        "actors": [
            {
                "character": "朱慈烺", "role": "primary_subject",
                "expected_view": "front_or_three_quarter",
                "identity_basis": "face",
            },
            {
                "character": "李继周", "role": "foreground_counterpart",
                "expected_view": "back_or_over_shoulder",
                "identity_basis": "back_silhouette",
            },
        ],
    }
    prompt = build_prompt("image_qc", {
        "image_uri": "/tmp/shot.png",
        "characters": ["朱慈烺", "李继周"],
        "count": 2,
        "identity_references": [
            {
                "character": "朱慈烺", "reference_view": "closeup",
                "uri": "/tmp/zhu-front.png",
            },
            {
                "character": "李继周", "reference_view": "back",
                "uri": "/tmp/li-back.png",
            },
        ],
        "composition_contract": composition,
    })
    assert "朱慈烺=primary_subject/front_or_three_quarter/按face核验" in prompt
    assert "李继周=foreground_counterpart/back_or_over_shoulder/按back_silhouette核验" in prompt
    assert "李继周(back): /tmp/li-back.png" in prompt
    assert "实际可见人形=2" in prompt

    spec = {
        "identity_required": True,
        "identity_characters": ["朱慈烺", "李继周"],
        "gender_required": True,
        "count_required": True,
        "count": 2,
        "composition_contract": composition,
        "identity_references": [{}, {}],
    }
    report = app.director._assess_image_qc(spec, {
        "pass": True,
        # 兼容旧总字段故意返回 false：逐角色结构化结果应作为新事实源。
        "identity_checked": False,
        "identity_match": False,
        "identity_checks": [
            {
                "character": "朱慈烺",
                "view": "front_or_three_quarter",
                "basis": ["脸型", "五官", "年龄感"],
                "checked": True, "match": True,
            },
            {
                "character": "李继周",
                "view": "back_or_over_shoulder",
                "basis": ["发型轮廓", "服装", "体型", "道具", "站位"],
                "checked": True, "match": True,
            },
        ],
        "gender_checked": True,
        "gender_match": True,
        "count_checked": True,
        "count_match": True,
        "detected_count": 2,
        "issues": [],
    }, attempts=1)
    assert report["passed"] is True
    assert report["identity_match"] is True
    assert report["count_match"] is True


def test_qc_fail_triggers_auto_redraw(app, tmp_path):
    """严格QC首败经Codex定向修订后固定生成四候选。"""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    calls = {"image": [], "qc": []}

    class StubRouter:
        def review_image_prompt(self, capability, payload, out_dir,
                                cancel=None):
            return None

        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                calls["image"].append(dict(payload))
                return ProviderResult(provider="codex", cost=1.0,
                                      uri=str(image))
            if payload.get("required_provider") == "codex":
                return ProviderResult(
                    provider="codex", cost=0.5,
                    data=_targeted_codex_verdict(
                        "小鹿被画成了动物",
                        "小鹿是人类女性，不得生成动物或兽形"))
            calls["qc"].append(dict(payload))
            first = len(calls["qc"]) == 1
            return ProviderResult(
                provider="claude", cost=0.5,
                data={"pass": not first,
                      "issues": (["小鹿被画成了动物"] if first else []),
                      **({
                          "image_error": {
                              "summary": "小鹿被画成了动物",
                              "categories": ["species"],
                              "evidence": ["画面主体不是人类"],
                          },
                          "prompt_diagnosis": {
                              "status": "insufficient",
                              "issues": ["主体物种约束不够明确"],
                              "irrelevant_or_conflicting_sections": [],
                          },
                          "reference_diagnosis": {
                              "status": "correct", "issues": [],
                              "missing_roles": [],
                          },
                          "targeted_prompt_patch": {
                              "instructions": [
                                  "小鹿是人类女性，不得生成动物或兽形"],
                              "preserve": ["当前构图", "场景"],
                              "max_scope": "current_shot_only",
                          },
                          "reference_adjustments": [],
                      } if first else {})})

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {"prompt": "x", "shot_no": 1,
                  "_episode_id": "unit-test"}, tmp_path, None,
        {"characters": ["小鹿"], "count": 1, "designs": "",
         "location": "", "action": "", "forbid": []})
    assert len(calls["image"]) == 5          # 首画 + 同合同四候选
    for candidate in calls["image"][1:]:
        assert "小鹿是人类女性" in candidate["feedback"]
        assert "【Codex 通知 AIFOS】" in candidate["feedback"]
        assert "只修改当前镜头" in candidate["feedback"]
        assert "【质检原因】" not in candidate["feedback"]
    assert result.qc["passed"] is True
    assert result.qc["gacha"]["pulls"] == 4
    assert calls["qc"][0]["generation_input"]["scope"]["shot_no"] == 1
    assert calls["qc"][0]["generation_input"]["input_hash"]
    assert result.qc["first_failure"]["input_hash"]


def _escalation_verdict(*, escalated=False, action="repair_contract",
                        instructions=None):
    value = {
        "pass": False,
        "visual_pass": False,
        "input_contract_pass": False,
        "issues": ["双手动作与道具可见性互相冲突"],
        "image_error": {
            "summary": "静态画面无法同时完成两个手部动作",
            "categories": ["action", "prop"],
            "evidence": ["双手已被拱手动作占用"],
        },
        "prompt_diagnosis": {
            "status": "conflicting",
            "issues": ["既要求拱手又要求掌中道具清楚可见"],
            "irrelevant_or_conflicting_sections": ["手部动作冲突"],
        },
        "reference_diagnosis": {
            "status": "correct", "issues": [], "missing_roles": [],
        },
        "targeted_prompt_patch": {
            "instructions": ["定格拱手完成瞬间，道具允许被手掌遮挡"],
            "preserve": ["人物身份", "机位"],
            "max_scope": "current_shot_only",
        },
        "reference_adjustments": [],
    }
    if escalated:
        value["codex_escalation"] = {
            "aifos_action": action,
            "reason": "不是继续抽卡能解决的问题",
            "aifos_instructions": instructions if instructions is not None
            else ["把静态关键帧改为唯一拱手完成瞬间", "将核桃标为本帧允许遮挡"],
            "freeze_moment": "拱手完成",
            "visible_props": [],
            "hidden_props": ["核桃"],
        }
    return value


def _targeted_codex_verdict(issue, instruction,
                            reference_adjustments=None):
    """完整、可执行的Codex诊断；只有这种输入才允许一次定向返工。"""
    return {
        "pass": False,
        "visual_pass": False,
        "input_contract_pass": False,
        "issues": [issue],
        "identity_checked": True, "identity_match": True,
        "gender_checked": True, "gender_match": True,
        "count_checked": True, "count_match": False,
        "physical_logic_checked": True, "physical_logic_match": True,
        "spatial_logic_checked": True, "spatial_logic_match": True,
        "image_error": {
            "summary": issue, "categories": ["targeted_repair"],
            "evidence": [issue],
        },
        "prompt_diagnosis": {
            "status": "needs_patch", "issues": [issue],
            "irrelevant_or_conflicting_sections": [],
        },
        "reference_diagnosis": {
            "status": ("conflicting" if reference_adjustments else "correct"),
            "issues": ([issue] if reference_adjustments else []),
            "missing_roles": [],
        },
        "targeted_prompt_patch": {
            "instructions": [instruction],
            "preserve": ["未指出的身份、构图与空间关系"],
            "max_scope": "current_shot_only",
        },
        "reference_adjustments": list(reference_adjustments or []),
        "codex_escalation": {
            "aifos_action": "targeted_redraw", "reason": issue,
            "aifos_instructions": [instruction],
        },
    }


class _EscalationRouter:
    """出图/质检双桩：质检恒判不合格，required_provider=codex 时回升级结论。"""

    def __init__(self, image_uri, action):
        self.image_uri = image_uri
        self.action = action
        self.calls = {"image": 0, "qc": 0}
        self.image_payloads = []
        self.codex_payloads = []

    # 真实 Router 在每次出图前都会过一次提示词审核；桩必须提供同名方法，
    # 否则被测代码在调用生图前就 AttributeError，测不到本来要测的东西。
    def review_image_prompt(self, capability, payload, out_dir, cancel=None):
        return None

    def call(self, capability, payload, out_dir, cancel=None):
        if capability == "image":
            self.calls["image"] += 1
            self.image_payloads.append(copy.deepcopy(payload))
            return ProviderResult(
                provider="seedream", cost=0.2, uri=str(self.image_uri))
        self.calls["qc"] += 1
        if payload.get("required_provider") == "codex":
            self.codex_payloads.append(copy.deepcopy(payload))
            return ProviderResult(
                provider="codex", cost=0.0, model="Codex 视觉质检",
                data=_escalation_verdict(
                    escalated=True, action=self.action))
        return ProviderResult(
            provider="claude", cost=0.1, data=_escalation_verdict())


def _qc_spec():
    return {"characters": ["赵德昌"], "count": 1,
            "location": "县衙", "action": "拱手", "forbid": []}


def test_first_failure_escalates_then_redraws_with_codex_prompt(
        app, tmp_path):
    """第1张不合格:Codex 改提示词，第二轮固定生成4张并全量选优。"""
    image = tmp_path / "first-failed.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 24)
    router = _EscalationRouter(image, "targeted_redraw")
    app.director.router = router

    result = app.director._generate_image_with_qc(
        "image", {"prompt": "赵德昌拱手", "shot_no": 8,
                  "_episode_id": "unit-test"},
        tmp_path, None, _qc_spec())

    # 第 1 次失败就升级(不再等到第 2 次)，且带的是第 1 次的失败计数。
    assert router.codex_payloads[0]["required_provider"] == "codex"
    assert router.codex_payloads[0]["codex_escalation_context"][
        "consecutive_failures"] == 1
    # 初版1张 + 同一份 Codex 修订合同候选4张，候选不得提前停止。
    assert router.calls["image"] == 5
    # 初检1 + 首败升级1 + 4张候选逐张判分 + 全败升级1。
    assert router.calls["qc"] == 7
    for candidate in router.image_payloads[1:]:
        feedback = candidate.get("feedback") or ""
        assert "把静态关键帧改为唯一拱手完成瞬间" in feedback
        assert candidate["revision_mode"] == "targeted_qc_fix"
        assert candidate["qc_revision"]["source"] == "codex_escalation"
    # 四张仍不合格：保留最高分候选和 Codex 下一轮指令，不转人工确认。
    assert result.qc["consecutive_failures"] == 2
    assert result.qc["codex_escalation"]["stage"] == "final_analysis"
    assert result.qc["codex_escalation"]["executable"] is False
    assert result.qc["retry_blocked"] is True
    assert result.qc["gacha"]["pulls"] == 4
    assert result.qc["gacha"]["select_after_all"] is True


def test_contract_repair_auto_applies_then_stops_on_second_failure(
        app, tmp_path):
    """repair_contract 不再是死路:首失败把 Codex 修合同指令自动落到提示词
    基底，第二轮用同一份新合同固定生成4张并自动择优。

    旧契约(首失败即停)的死结:Codex 下达了修合同指令,但全仓库没有代码
    执行它——escalation_redraw_block 等着「合同真的改了就放行」,而没有人
    去改,合同类失败只能人工介入。
    """
    image = tmp_path / "contract-failed.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 24)
    router = _EscalationRouter(image, "repair_contract")
    app.director.router = router

    result = app.director._generate_image_with_qc(
        "image", {"prompt": "赵德昌拱手", "shot_no": 8,
                  "_episode_id": "unit-test"},
        tmp_path, None, _qc_spec())

    assert router.calls["image"] == 5
    for candidate in router.image_payloads[1:]:
        prompt = str(candidate.get("prompt") or "")
        assert "【Codex合同修订·必须执行】" in prompt
        assert "把静态关键帧改为唯一拱手完成瞬间" in prompt
    assert result.qc["consecutive_failures"] == 2
    assert result.qc["codex_escalation"]["stage"] == "final_analysis"
    assert result.qc["codex_escalation"]["executable"] is False
    assert result.qc["codex_escalation"]["aifos_action"] == "repair_contract"
    assert result.qc["contract_repair_required"] is True
    assert result.qc["retry_blocked"] is True
    # 升级结论记住了当时的生成输入指纹，供后续重画闸门比对。
    assert result.qc["codex_escalation"]["contract_input_hash"]


def _escalation_qc(action="targeted_redraw",
                   instruction="【Codex 通知 AIFOS】定格拱手完成瞬间",
                   input_hash="hash-old"):
    return {
        "passed": False,
        "hard_failure": True,
        "issues": ["双手动作与道具可见性互相冲突"],
        "consecutive_failures": 1,
        "attempts": 1,
        "redraw_required": action == "targeted_redraw",
        "contract_repair_required": action in ("repair_contract",
                                               "split_shot"),
        "revision_feedback": instruction,
        "codex_escalation": {
            "schema": "aifos.codex-qc-escalation/v1",
            "triggered": True,
            "status": "completed",
            "stage": "first_failure_autofix",
            "executable": action == "targeted_redraw",
            "aifos_action": action,
            "reason": "一张静帧承担不了两个先后动作",
            "freeze_moment": "拱手完成",
            "instruction_to_aifos": instruction,
            "consecutive_failures": 1,
            "contract_input_hash": input_hash,
        },
    }


def test_escalation_gate_blocks_non_redraw_until_contract_changes(app):
    """Codex 判非重画类处理时熔断；合同真变了或人工放行才通过。"""
    block = app.director.escalation_redraw_block
    context = app.director.escalation_context

    same_input = {"input_hash": "hash-old"}
    repair = context(_escalation_qc(action="repair_contract"))
    assert repair["aifos_action"] == "repair_contract"
    assert repair["contract_input_hash"] == "hash-old"

    reason = block(repair, same_input)
    assert "修复本镜生成合同" in reason
    assert "熔断" in reason
    # 诊断与建议冻结瞬间要带进熔断说明，用户才知道该改什么。
    assert "一张静帧承担不了两个先后动作" in reason
    assert "拱手完成" in reason

    # 合同真的改了(生成输入哈希变化)→ 自动放行，不需要人工点确认。
    assert block(repair, {"input_hash": "hash-new"}) == ""
    # 人工确认已按指令修好 → 放行。
    assert block({**repair, "override": True}, same_input) == ""
    # 判定就是定向重画 / 根本没升级过 → 不拦。
    assert block(context(_escalation_qc()), same_input) == ""
    assert block({}, same_input) == ""
    assert context({}) == {}

    for action in ("split_shot", "accept_current", "manual_review"):
        blocked = block(context(_escalation_qc(action=action)), same_input)
        assert blocked, f"{action} 应当熔断"


def test_plan_run_blocks_image_call_when_codex_requires_contract_repair(
        app, tmp_path):
    """闸门落在 _plan_run:判「改合同」时根本不会调到生图 API。"""
    calls = {"image": 0}

    class StubRouter:
        def review_image_prompt(self, capability, payload, out_dir,
                                cancel=None):
            return None

        def call(self, capability, payload, out_dir, cancel=None):
            calls["image"] += 1
            return ProviderResult(provider="seedream", cost=0.2, uri="")

    app.director.router = StubRouter()
    marks = []
    app.director._plan_mark = (
        lambda ctx, item_id, status, **kw: marks.append((item_id, status)))
    ctx = {"out_root": tmp_path, "episode": {"id": 1}}
    payload = {
        "prompt": "赵德昌拱手",
        "reference_manifest": [],
        "qc_escalation": app.director.escalation_context(
            _escalation_qc(action="repair_contract", input_hash="")),
    }
    # 哈希留空表示"拿不到旧指纹"，此时按最保守处理:照样熔断。
    with pytest.raises(AifosError) as excinfo:
        app.director._plan_run(
            ctx, "scene:县衙", lambda *a, **k: None, payload=payload)

    assert "修复本镜生成合同" in str(excinfo.value)
    assert calls["image"] == 0, "熔断必须发生在调用生图 API 之前"
    assert ("scene:县衙", "awaiting_human") in marks


def _inject_escalation_qc(app, project, item_category="shot_image",
                          **kwargs):
    """把一条 Codex 升级结论写进 render_plan 的目标条目。

    返回 (item_id, shot_no)——镜头图条目的 id 形如 ``shot:3``。
    """
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    target = next(item for item in plan["items"]
                  if item["category"] == item_category)
    target["qc"] = _escalation_qc(**kwargs)
    target["status"] = "awaiting_human"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")
    return target["id"], int(str(target["id"]).split(":")[1])


def test_regen_image_applies_codex_instruction(app, monkeypatch):
    """重画消费侧:Codex 指令进 feedback，升级上下文随 payload 下传。"""
    project = _preproduce(app, title="升级指令重画")
    instruction = "【Codex 通知 AIFOS】定格拱手完成瞬间，核桃允许被手掌遮挡"
    _item_id, shot_no = _inject_escalation_qc(
        app, project, instruction=instruction)

    captured = {}

    class _Stop(Exception):
        pass

    def fake_plan_run(self, ctx, item_id, fn, prompt=None, payload=None,
                      revision_source="manual", capability="image"):
        captured["item_id"] = item_id
        captured["payload"] = payload or {}
        captured["revision_source"] = revision_source
        raise _Stop()

    from aifos.director import Director
    monkeypatch.setattr(Director, "_plan_run", fake_plan_run)

    with pytest.raises(_Stop):
        app.director.regen_image(
            "升级指令重画", 1,
            {"kind": "shot", "shot_no": shot_no})

    # Codex 指令压过本地规则编译器，直接成为本次重画的修改意见。
    assert instruction in captured["payload"]["feedback"]
    # 升级上下文必须随 payload 下传，_plan_run 的闸门才有依据。
    escalation = captured["payload"]["qc_escalation"]
    assert escalation["aifos_action"] == "targeted_redraw"
    assert escalation["instruction_to_aifos"] == instruction
    assert escalation.get("override") is None
    assert captured["payload"]["qc_consecutive_failures_base"] == 1


def test_regen_image_passes_human_override_into_escalation(app, monkeypatch):
    """人工确认合同已修好时，override 要真的传到闸门上。"""
    project = _preproduce(app, title="升级指令放行")
    _item_id, shot_no = _inject_escalation_qc(
        app, project, action="repair_contract")

    captured = {}

    class _Stop(Exception):
        pass

    def fake_plan_run(self, ctx, item_id, fn, prompt=None, payload=None,
                      revision_source="manual", capability="image"):
        captured["payload"] = payload or {}
        raise _Stop()

    from aifos.director import Director
    monkeypatch.setattr(Director, "_plan_run", fake_plan_run)

    with pytest.raises(_Stop):
        app.director.regen_image(
            "升级指令放行", 1,
            {"kind": "shot", "shot_no": shot_no},
            escalation_override=True)

    assert captured["payload"]["qc_escalation"]["override"] is True


def test_redo_items_uses_codex_instruction_as_feedback(app, monkeypatch):
    """批量重画失败项:走 codex_escalation 来源，而不是本地 batch_qc 文案。"""
    project = _preproduce(app, title="批量升级指令")
    instruction = "【Codex 通知 AIFOS】只保留两名已登记角色"
    _item_id, shot_no = _inject_escalation_qc(
        app, project, instruction=instruction)

    calls = []

    from aifos.director import Director

    def fake_regen(self, project_title, episode_number, target, **kwargs):
        calls.append({"target": target, **kwargs})
        return {}

    monkeypatch.setattr(Director, "regen_image", fake_regen)

    summary = app.director.redo_items("批量升级指令", 1, only_failed=True)

    assert summary["status"] != "blocked"
    mine = [call for call in calls
            if int(call["target"].get("shot_no") or 0) == shot_no]
    assert mine, "被升级的失败项应当进入批量重画目标"
    assert mine[0]["feedback"] == instruction
    assert mine[0]["revision_source"] == "codex_escalation"
    assert mine[0]["escalation_override"] is False


def test_existing_image_recheck_keeps_failure_count_and_escalates(
        app, tmp_path, monkeypatch):
    project, _ = app.projects.get_or_create_project("复检失败计数")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    image = tmp_path / "existing-failed.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"f" * 24)
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": tmp_path,
    }
    item = {
        "id": "shot:3", "category": "shot_image", "shot_no": 3,
        "status": "awaiting_human",
        "qc": {
            "passed": False, "attempts": 1,
            "consecutive_failures": 1, "signature": "old",
            "issues": ["第一次未过"],
        },
    }
    spec = {
        "identity_required": False,
        "gender_required": False,
        "count_required": False,
        "physical_logic_required": False,
    }
    generation_input = {
        "scope": {"item_id": "shot:3", "shot_no": 3},
        "prompt": "唯一动作",
        "reference_manifest": [],
        "input_hash": "new-input",
    }
    marked = {}

    def verdict(*, escalation=False):
        data = {
            "pass": False, "visual_pass": False,
            "input_contract_pass": True,
            "issues": ["第二次仍未过"],
            "image_error": {
                "summary": "动作峰值错误",
                "categories": ["action"], "evidence": ["手势不成立"]},
            "prompt_diagnosis": {
                "status": "correct", "issues": [],
                "irrelevant_or_conflicting_sections": []},
            "reference_diagnosis": {
                "status": "correct", "issues": [], "missing_roles": []},
            "targeted_prompt_patch": {
                "instructions": ["只修正手势"], "preserve": ["其他画面"],
                "max_scope": "current_shot_only"},
            "reference_adjustments": [],
        }
        if escalation:
            data["codex_escalation"] = {
                "aifos_action": "targeted_redraw",
                "reason": "画面手势明确错误",
                "aifos_instructions": ["保持其他内容，只重画手势"],
            }
        return data

    class StubRouter:
        def __init__(self):
            self.calls = []

        def call(self, capability, payload, out_dir, cancel=None):
            self.calls.append(dict(payload))
            if payload.get("required_provider") == "codex":
                return ProviderResult(
                    provider="codex", cost=0,
                    data=verdict(escalation=True))
            return ProviderResult(
                provider="claude", cost=0, data=verdict())

    router = StubRouter()
    app.director.router = router
    monkeypatch.setattr(
        app.director, "_plan_item_asset",
        lambda *_args, **_kwargs: (str(image), spec))
    monkeypatch.setattr(
        app.director, "_plan_generation_input",
        lambda *_args, **_kwargs: generation_input)
    monkeypatch.setattr(
        app.director, "_qc_signature",
        lambda *_args, **_kwargs: "new-signature")
    monkeypatch.setattr(
        app.director, "_plan_mark",
        lambda _ctx, _item_id, _status, **kwargs:
        marked.update(kwargs.get("extra") or {}))

    report = app.director._qc_one(
        dict(project), dict(episode), ctx, item)

    assert report["consecutive_failures"] == 2
    assert len(router.calls) == 2
    assert router.calls[1]["required_provider"] == "codex"
    assert report["codex_escalation"]["status"] == "completed"
    assert report["codex_escalation"]["aifos_action"] == "targeted_redraw"
    assert marked["qc"]["consecutive_failures"] == 2


def test_incomplete_legacy_diagnosis_blocks_blind_second_generation(
        app, tmp_path):
    image = tmp_path / "legacy-failure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    image_calls = []

    class StubRouter:
        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                image_calls.append(dict(payload))
                return ProviderResult(
                    provider="seedream", cost=0.2, uri=str(image))
            return ProviderResult(provider="legacy-qc", cost=0.1, data={
                "pass": False, "issues": ["人物身份不一致"],
                "identity_checked": True, "identity_match": False,
                "gender_checked": True, "gender_match": True,
                "count_checked": True, "count_match": True,
            })

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {"prompt": "本镜人物转身", "shot_no": 4},
        tmp_path, None, {
            "characters": ["甲"], "count": 1,
            "identity_required": True, "gender_required": True,
            "count_required": True, "identity_references": [{}],
        })

    assert len(image_calls) == 1
    assert result.qc["passed"] is False
    assert result.qc["diagnosis_complete"] is False
    assert result.qc["retry_blocked"] is True
    assert "禁止盲目原样重试" in result.qc["retry_blocked_reason"]


def test_reference_diagnosis_removes_wrong_manual_ref_before_retry(
        app, tmp_path):
    output = tmp_path / "shot.png"
    wrong = tmp_path / "wrong-reference.png"
    output.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    wrong.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    calls = {"image": [], "qc": []}

    class StubRouter:
        def review_image_prompt(self, capability, payload, out_dir,
                                cancel=None):
            return None

        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                calls["image"].append(copy := dict(payload))
                # Keep nested values stable for assertions after director
                # mutates the next attempt.
                copy["reference_manifest"] = [
                    dict(item) for item in (
                        payload.get("reference_manifest") or [])]
                return ProviderResult(
                    provider="seedream", cost=0.2, uri=str(output))
            if payload.get("required_provider") == "codex":
                return ProviderResult(
                    provider="codex", cost=0.0,
                    data=_targeted_codex_verdict(
                        "错误参考图造成现代服装",
                        "移除错误服装参考后重新生成",
                        reference_adjustments=[{
                            "action": "remove", "target_index": 1,
                            "role": "manual", "character": "",
                            "reason": "移除错误服装参考",
                        }]))
            calls["qc"].append(dict(payload))
            first = len(calls["qc"]) == 1
            return ProviderResult(provider="vision", cost=0.1, data={
                "pass": not first,
                "issues": ["错误参考图造成现代服装"] if first else [],
                "identity_checked": True, "identity_match": True,
                "gender_checked": True, "gender_match": True,
                "count_checked": True, "count_match": True,
                **({
                    "image_error": {
                        "summary": "服装年代错误",
                        "categories": ["wardrobe"],
                        "evidence": ["画面是现代西装"],
                    },
                    "prompt_diagnosis": {
                        "status": "correct", "issues": [],
                        "irrelevant_or_conflicting_sections": [],
                    },
                    "reference_diagnosis": {
                        "status": "conflicting",
                        "issues": ["图1属于其他项目且服装错误"],
                        "missing_roles": [],
                    },
                    "targeted_prompt_patch": {
                        "instructions": [], "preserve": [],
                        "max_scope": "current_shot_only",
                    },
                    "reference_adjustments": [{
                        "action": "remove", "target_index": 1,
                        "role": "manual", "character": "",
                        "reason": "移除错误服装参考",
                    }],
                } if first else {}),
            })

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {
            "prompt": "空镜中的衣架", "shot_no": 8,
            "_episode_id": "unit-test",
            "reference_images": [str(wrong)],
            "asset_matches": [{
                "uri": str(wrong), "label": "用户错误参考图",
                "reference_role": "manual",
            }],
        }, tmp_path, None, {
            "characters": [], "count": 0, "count_required": True,
        })

    assert result.qc["passed"] is True
    assert len(calls["image"]) == 5
    assert all(str(wrong) not in payload.get("reference_images", [])
               for payload in calls["image"][1:])
    assert all(payload.get("reference_manifest") == []
               for payload in calls["image"][1:])


def test_gender_mismatch_is_a_hard_identity_gate(app, tmp_path):
    """即使视觉模型声称 pass，性别与最终立绘不符也必须拦截。"""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)

    class StubRouter:
        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                return ProviderResult(provider="codex", cost=1.0,
                                      uri=str(image))
            return ProviderResult(
                provider="codex", cost=0.1,
                data={"pass": True, "identity_checked": True,
                      "gender_checked": True, "gender_match": False,
                      "issues": []})

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {"prompt": "x", "shot_no": 1}, tmp_path, None,
        {"characters": ["程沐"], "count": 1,
         "identity_required": True, "gender_required": True,
         "identity_references": [{"character": "程沐",
                                    "uri": "/tmp/chengmu.png"}],
         "designs": "", "location": "", "action": "", "forbid": []})
    assert result.qc["passed"] is False
    assert result.qc["gender_checked"] is True
    assert result.qc["gender_match"] is False
    assert any("性别" in issue for issue in result.qc["issues"])


def test_qc_cannot_pass_when_gender_check_fields_are_omitted(app):
    """视觉模型漏答性别字段时不得按“未发现问题”放行。"""
    spec = {
        "identity_required": True,
        "gender_required": True,
        "count_required": True,
        "count": 1,
    }
    report = app.director._assess_image_qc(spec, {
        "pass": True,
        "identity_checked": True,
        "identity_match": True,
        "count_checked": True,
        "count_match": True,
        "issues": [],
    }, attempts=1)
    assert report["passed"] is False
    assert report["gender_checked"] is False
    assert report["gender_match"] is False
    assert any("未单独核对人物性别" in issue
               for issue in report["issues"])


def test_physical_and_spatial_logic_are_hard_gates(app):
    spec = {
        "identity_required": False,
        "gender_required": False,
        "count_required": True,
        "count": 1,
        "physical_logic_required": True,
        "physical_contract": {
            "schema": "aifos.physical-space/v1",
            "rules": ["使用者与电脑屏幕正面必须在同一侧"],
        },
    }
    report = app.director._assess_image_qc(spec, {
        "pass": True,
        "count_checked": True,
        "count_match": True,
        # 模型漏答或无法证明物理关系时，不能按“没发现问题”放行。
        "issues": [],
    }, attempts=1)
    assert report["passed"] is False
    assert report["physical_logic_checked"] is False
    assert report["spatial_logic_checked"] is False
    assert any("物理" in issue for issue in report["issues"])

    report = app.director._assess_image_qc(spec, {
        "pass": True,
        "count_checked": True,
        "count_match": True,
        "physical_logic_checked": True,
        "physical_logic_match": False,
        "spatial_logic_checked": True,
        "spatial_logic_match": False,
        "issues": ["人物坐在笔记本屏幕后方却看到屏幕正面"],
    }, attempts=1)
    assert report["passed"] is False
    assert report["hard_failure"] is True
    assert any("空间" in issue or "物理" in issue
               for issue in report["issues"])


def test_count_mismatch_auto_revises_bad_image_with_locked_references(
        app, tmp_path):
    """人数错误必须把失败图作为待修改基底重画，并与最终立绘一起复检。"""
    image = tmp_path / "shot.png"
    identity = tmp_path / "identity.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    identity.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    calls = {"image": [], "qc": []}

    class StubRouter:
        def review_image_prompt(self, capability, payload, out_dir,
                                cancel=None):
            return None

        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                calls["image"].append(dict(payload))
                return ProviderResult(
                    provider="seedream5_lite", cost=0.2, uri=str(image))
            if payload.get("required_provider") == "codex":
                return ProviderResult(
                    provider="codex", cost=0.0,
                    data=_targeted_codex_verdict(
                        "画面多出一名人物",
                        "画面严格只保留甲、乙两人"))
            calls["qc"].append(dict(payload))
            first = len(calls["qc"]) == 1
            return ProviderResult(provider="vision", cost=0.1, data={
                "pass": not first,
                "identity_checked": True, "identity_match": True,
                "gender_checked": True, "gender_match": True,
                "count_checked": True, "count_match": not first,
                "detected_count": 3 if first else 2,
                "issues": ["多出一名人物"] if first else [],
                **({
                    "image_error": {
                        "summary": "画面多出一名人物",
                        "categories": ["count"],
                        "evidence": ["检测到3人，要求2人"],
                    },
                    "prompt_diagnosis": {
                        "status": "needs_patch",
                        "issues": ["人数边界需要强化"],
                        "irrelevant_or_conflicting_sections": [],
                    },
                    "reference_diagnosis": {
                        "status": "correct", "issues": [],
                        "missing_roles": [],
                    },
                    "targeted_prompt_patch": {
                        "instructions": ["画面严格只保留甲、乙两人"],
                        "preserve": ["甲乙身份", "原机位"],
                        "max_scope": "current_shot_only",
                    },
                    "reference_adjustments": [],
                } if first else {}),
            })

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {
            "prompt": "两人对话", "characters": ["甲", "乙"],
            "shot_no": 1, "_episode_id": "unit-test",
            "identity_references": [
                {"character": "甲", "uri": str(identity)}],
        }, tmp_path, None, {
            "characters": ["甲", "乙"], "count": 2,
            "identity_required": True, "gender_required": True,
            "count_required": True,
            "identity_references": [
                {"character": "甲", "uri": str(identity)}],
        })
    assert result.qc["passed"] is True
    assert len(calls["image"]) == 5
    revised = calls["image"][1]
    assert revised["revision_mode"] == "targeted_qc_fix"
    # 人数/身份错误稿不能反过来成为第二次生成的参考图，否则多余人物
    # 容易被继续复制；只保留锁定人物参考并用短提示词定向重生。
    assert str(image) not in revised.get("reference_images", [])
    assert not any(
        item["label"] == "质检未过的待修改基底"
        for item in revised["reference_manifest"])
    assert revised["reference_manifest"][0]["uri"] == str(identity)


@pytest.mark.parametrize("worker_count", [1, 2])
def test_stage_images_collects_qc_failure_and_finishes_later_shots(
        app, monkeypatch, worker_count):
    """单镜二次 QC 失败不能阻断后续镜头，也不能污染正式图片资产。"""
    project, _ = app.projects.get_or_create_project(
        f"关键帧失败不中断-{worker_count}")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
                / "e001")
    out_root.mkdir(parents=True, exist_ok=True)
    shots = [
        {"shot_no": shot_no, "scene_no": 1, "characters": []}
        for shot_no in range(1, 5)
    ]
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": out_root,
        "script": {
            "scenes": [{"scene_no": 1, "location": "会议室"}],
        },
        "storyboard": {"shots": shots},
    }
    app.director._plan_write(ctx, {"items": [{
        "id": f"shot:{shot['shot_no']}",
        "category": "shot_image",
        "label": f"镜头 {shot['shot_no']:02d}",
        "status": "pending",
        "error": "",
    } for shot in shots]})

    generated = []
    output_by_shot = {}

    def fake_generate(_capability, payload, out_dir, _cancel, _qc_spec):
        shot_no = int(payload["shot_no"])
        generated.append(shot_no)
        output = out_dir / f"shot-{shot_no}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([shot_no]) * 16)
        output_by_shot[shot_no] = output
        result = ProviderResult(
            provider="stub-image", cost=0.1, uri=str(output))
        failed = shot_no == 2
        result.qc = {
            "passed": not failed,
            "attempts": 2 if failed else 1,
            "issues": ["镜头2人物多出一人"] if failed else [],
            "hard_failure": failed,
        }
        return result

    monkeypatch.setattr(app.director, "_plan_seed_shots",
                        lambda _ctx: None)
    monkeypatch.setattr("aifos.director.write_relations",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app.director, "_shot_payload", lambda _ctx, shot: {
        "shot_no": shot["shot_no"],
        "prompt": f"镜头 {shot['shot_no']}",
        "characters": [],
        "character_count": 0,
        "quality_decision": {
            "level": "medium", "recommended": "medium",
            "source": "test", "rule": "", "reasons": [],
        },
    })
    monkeypatch.setattr(app.director, "_qc_spec",
                        lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        app.director, "_parallel_workers", lambda: worker_count)
    monkeypatch.setattr(app.director, "_prepare_dispatch_contracts",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app.director, "_generation_preflight_issues",
                        lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app.director, "_claim_dispatch_task",
                        lambda _ctx, task: task)
    monkeypatch.setattr(app.director, "_finish_dispatch_task",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app.director, "_attach_reference_manifest",
                        lambda _payload: None)
    monkeypatch.setattr(app.director, "_generate_shot_candidate_group",
                        fake_generate)
    app.director._task_cost = 0.0
    app.director._task_providers = set()

    with pytest.raises(AifosError) as caught:
        app.director._stage_images(ctx)

    assert sorted(generated) == [1, 2, 3, 4], \
        "二次 QC 失败后仍应派发并完成本集剩余关键帧"
    assert "问题镜头: 2" in str(caught.value)
    assert [item["shot_no"] for item in ctx["images"]] == [1, 3, 4]

    formal = app.assets.active_list(project["id"], kind="image")
    assert {row["name"] for row in formal} == {
        "e001_shot001", "e001_shot003", "e001_shot004"}
    assert not any(row["uri"] == str(output_by_shot[2]) for row in formal)
    assert output_by_shot[2].exists(), "失败图必须保留给下一轮自动分析"

    plan = app.director._plan_read(ctx)
    by_id = {item["id"]: item for item in plan["items"]}
    assert by_id["shot:2"]["status"] == "failed"
    assert by_id["shot:2"]["output_uri"] == str(output_by_shot[2])
    assert by_id["shot:2"]["qc"]["passed"] is False
    assert by_id["shot:2"]["qc"]["attempts"] == 2
    assert by_id["shot:2"]["qc"]["issues"] == ["镜头2人物多出一人"]
    assert all(by_id[f"shot:{n}"]["status"] == "done"
               for n in (1, 3, 4))


def test_reconcile_completed_shot_images_recovers_only_qc_passed_files(app):
    """旧批次中断后，只补登记已通过 QC 的落盘图；失败稿继续隔离。"""
    project, _ = app.projects.get_or_create_project("旧批次关键帧对账")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    out_root = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
                / "e001")
    images_dir = out_root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    passed_uri = images_dir / "shot_001.keyframe.png"
    failed_uri = images_dir / "shot_002.keyframe.png"
    legacy_failed_uri = images_dir / "shot_004.keyframe.png"
    unchecked_uri = images_dir / "shot_005.keyframe.png"
    passed_uri.write_bytes(b"\x89PNG\r\n\x1a\n" + b"p" * 24)
    failed_uri.write_bytes(b"\x89PNG\r\n\x1a\n" + b"f" * 24)
    legacy_failed_uri.write_bytes(b"\x89PNG\r\n\x1a\n" + b"l" * 24)
    unchecked_uri.write_bytes(b"\x89PNG\r\n\x1a\n" + b"u" * 24)
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": out_root,
        "script": {
            "scenes": [{"scene_no": 1, "location": "东宫"}],
        },
        "storyboard": {"shots": [
            {"shot_no": 1, "scene_no": 1, "characters": ["朱慈烺"]},
            {"shot_no": 2, "scene_no": 1, "characters": ["朱慈烺"]},
            {"shot_no": 3, "scene_no": 1, "characters": []},
            {"shot_no": 4, "scene_no": 1, "characters": ["朱慈烺"]},
            {"shot_no": 5, "scene_no": 1, "characters": ["朱慈烺"]},
        ]},
    }
    app.director._plan_write(ctx, {"items": [
        {
            "id": "shot:1", "category": "shot_image",
            "shot_no": 1, "status": "done",
            "image_quality": "high",
            "qc": {"passed": True, "attempts": 2, "issues": []},
        },
        {
            "id": "shot:2", "category": "shot_image",
            "shot_no": 2, "status": "failed",
            "image_quality": "high",
            "qc": {
                "passed": False, "attempts": 2,
                "issues": ["人物年龄感漂移"],
            },
        },
        {
            "id": "shot:3", "category": "shot_image",
            "shot_no": 3, "status": "done",
            "image_quality": "medium",
            "qc": {"passed": True, "attempts": 1, "issues": []},
        },
        {
            "id": "shot:4", "category": "shot_image",
            "shot_no": 4, "status": "done",
            "image_quality": "high",
            "qc": {
                "passed": False, "attempts": 1,
                "issues": ["旧逻辑误标完成"],
            },
        },
        {
            "id": "shot:5", "category": "shot_image",
            "shot_no": 5, "status": "done",
            "image_quality": "medium",
        },
    ]})

    result = app.director.reconcile_completed_shot_images(ctx)

    assert result["recovered"] == 1
    assert result["awaiting_human"] == 0
    assert result["autonomous_retry"] == 2
    # 旧待人工状态迁移为自动重试，不再生成用户确认门。
    assert result["stale_reset"] == 0
    assert result["awaiting_human_shots"] == []
    formal = app.assets.active_list(project["id"], kind="image")
    assert [(row["name"], row["uri"]) for row in formal] == [
        ("e001_shot001", str(passed_uri))]
    plan = app.director._plan_read(ctx)
    by_id = {item["id"]: item for item in plan["items"]}
    assert by_id["shot:1"]["output_uri"] == str(passed_uri)
    assert by_id["shot:2"]["status"] == "failed"
    assert by_id["shot:2"]["output_uri"] == str(failed_uri)
    assert by_id["shot:2"]["qc"]["auto_repair_exhausted"] is True
    assert "output_uri" not in by_id["shot:3"]
    assert by_id["shot:4"]["status"] == "failed"
    assert by_id["shot:4"]["output_uri"] == str(legacy_failed_uri)
    assert by_id["shot:5"]["output_uri"] == str(unchecked_uri)
    assert app.assets.latest(
        project["id"], "image", "e001_shot005") is None


def test_qc_report_lands_in_plan(app):
    """初始母资产不空耗视觉 QC；正式镜头图必须带通过结果。"""
    project = _preproduce(app)
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    for cat in ("character_candidate", "character_sheet", "scene_art"):
        drawn = [i for i in plan["items"]
                 if i["category"] == cat
                 and i["status"] in ("done", "reused")]
        assert drawn, f"{cat} 无生成条目"
        assert all("qc" not in i for i in drawn), f"{cat} 不应做初始视觉质检"
    # 关键帧在生成阶段自动质检；首尾帧由整集批量质检同时核对
    # 首、尾两张，避免把帧容器条目误当成单张生成结果。
    drawn = [i for i in plan["items"]
             if i["category"] == "shot_image"
             and i["status"] in ("done", "reused")]
    assert drawn, "shot_image 无生成条目"
    assert all(i.get("qc", {}).get("passed") for i in drawn), \
        "shot_image 缺质检结果"

    report = app.director.qc_all(project["title"], 1)
    assert report["failed"] == 0
    plan = json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    for cat in ("shot_image", "frames"):
        drawn = [i for i in plan["items"]
                 if i["category"] == cat
                 and i["status"] in ("done", "reused")]
        assert drawn, f"{cat} 无生成条目"
        assert all(i.get("qc", {}).get("passed") for i in drawn), \
            f"{cat} 缺质检结果"


def test_camera_scales_are_varied(app):
    """镜头景别不再近景为主:每场开场全/远景,相邻景别变化。"""
    project = _preproduce(app, title="景别测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    shots = storyboard["shots"]
    scales = [((s.get("five_dimensions") or {})
               .get("camera_design") or {}).get("shot_scale", "")
              for s in shots]
    scales = [x for x in scales if x]
    assert len(set(scales)) >= 3, f"景别过于单一: {scales}"
    close = sum(1 for x in scales if x in ("近景", "特写", "大特写"))
    assert close < len(scales), "全部是近景/特写"
    wide = sum(1 for x in scales if x in ("全景", "远景"))
    assert wide >= 1, f"没有任何全景/远景定场: {scales}"


def test_species_flows_into_prompts(app):
    """形态字段进入设定与提示词:默认人类,动物角色按设定执行。"""
    project = _preproduce(app, title="形态测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    design = app.director._character_design(project["id"], name)
    assert design.get("species") == "人类"
    import json as _json
    plan = _json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    portrait = next(i for i in plan["items"] if i["id"] == f"char:{name}")
    assert "形态:人类" in portrait["prompt"]


def test_frames_are_chained_within_scene(app):
    """帧链:同场景内 上一镜尾帧 = 下一镜首帧;拼接处画面连贯。"""
    project = _preproduce(app, title="帧链测试")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    by_scene = {}
    for shot in storyboard["shots"]:
        by_scene.setdefault(shot["scene_no"], []).append(shot)
    chained = 0
    for scene_shots in by_scene.values():
        for prev, cur in zip(scene_shots, scene_shots[1:]):
            prev_last = app.assets.latest(
                project["id"], "last_frame",
                f"e001_shot{prev['shot_no']:03d}")
            cur_first = app.assets.latest(
                project["id"], "first_frame",
                f"e001_shot{cur['shot_no']:03d}")
            assert Path(prev_last["uri"]).read_bytes() ==                 Path(cur_first["uri"]).read_bytes(),                 f"镜头{prev['shot_no']}尾帧 != 镜头{cur['shot_no']}首帧"
            chained += 1
    assert chained >= 2, "没有可验证的帧链对"


def test_redo_placeholders_only_redraws_mock_items(app):
    """一键补真:只重画清单里的占位条目,其余不动。"""
    import json as _json
    project = _preproduce(app, title="补真测试")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    target_item = next(i for i in plan["items"]
                       if i["category"] == "scene_art")
    for item in plan["items"]:
        item["real"] = item["id"] == target_item["id"] and False or True
    target_item["real"] = False
    plan_path.write_text(_json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")
    name = target_item["name"]
    before = app.assets.latest(project["id"], "scene_art", name)
    others_before = app.assets.latest(
        project["id"], "character_sheet",
        next(i["name"] for i in plan["items"]
             if i["category"] == "character_sheet") + ":turnaround")
    summary = app.director.redo_placeholders("补真测试", 1)
    assert summary["status"] == "done" and summary["redone"] == 1
    after = app.assets.latest(project["id"], "scene_art", name)
    assert after["version"] == before["version"] + 1
    others_after = app.assets.latest(
        project["id"], "character_sheet", others_before["name"])
    assert others_after["version"] == others_before["version"]


def test_regen_frames_only(app):
    """只重做首尾帧:沿用关键图与帧链,作废旧视频。"""
    project = _preproduce(app, title="帧重做测试")
    name = "e001_shot002"
    img_before = app.assets.latest(project["id"], "image", name)
    frame_before = app.assets.latest(project["id"], "first_frame", name)
    app.director.regen_image("帧重做测试", 1,
                             {"kind": "frames", "shot_no": 2})
    assert app.assets.latest(project["id"], "image",
                             name)["version"] == img_before["version"]
    assert app.assets.latest(
        project["id"], "first_frame",
        name)["version"] == frame_before["version"] + 1
    # 帧链仍连贯:首帧 = 上一镜(同场)尾帧
    prev_last = app.assets.latest(project["id"], "last_frame",
                                  "e001_shot001")
    cur_first = app.assets.latest(project["id"], "first_frame", name)
    from pathlib import Path as _P
    assert _P(prev_last["uri"]).read_bytes() == \
        _P(cur_first["uri"]).read_bytes()


def test_regen_frames_with_prompt_override(app):
    """首尾帧可改提示词单独重画:清单标记已改词,版本递增。"""
    import json as _json
    project = _preproduce(app, title="帧改词测试")
    name = "e001_shot002"
    before = app.assets.latest(project["id"], "first_frame", name)
    app.director.regen_image(
        "帧改词测试", 1, {"kind": "frames", "shot_no": 2},
        prompt_override="尾帧定格在角色回头的瞬间,逆光剪影")
    after = app.assets.latest(project["id"], "first_frame", name)
    assert after["version"] == before["version"] + 1
    plan = _json.loads(
        (app.workspace.artifacts_dir / f"p{project['id']:03d}" / "e001"
         / "render_plan.json").read_text(encoding="utf-8"))
    item = next(i for i in plan["items"] if i["id"] == "frames:2")
    assert item["custom_prompt"] is True
    assert "逆光剪影" in item["prompt"]


def test_openai_api_uses_reference_images(tmp_path, monkeypatch):
    """API 出图有参考图时走 images/edits 多图输入,与 CLI 用参考图一致。"""
    from aifos.production import api_providers
    from aifos.production.api_providers import OpenAIImageProvider

    anchor = tmp_path / "anchor.png"
    anchor.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    portrait = tmp_path / "portrait.png"
    portrait.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 8)

    captured = {}

    def fake_multipart(name, url, headers, fields, files, timeout):
        captured["url"] = url
        captured["files"] = [f[1].name for f in files]
        captured["prompt"] = fields["prompt"]
        return {"data": [{"b64_json": "aGk="}]}

    def fake_json(*a, **k):
        captured["fell_back_to_generations"] = True
        return {"data": [{"b64_json": "aGk="}]}

    monkeypatch.setattr(api_providers, "_multipart_post", fake_multipart)
    monkeypatch.setattr(api_providers, "_request_json", fake_json)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-x",
        "model": "gpt-image-2"})
    result = provider.generate("image", {
        "character_sheet": "makeup", "art_name": "小鹿",
        "prompt": "妆容设定", "aspect": "9:16",
        "character_refs": [str(portrait)],
        "style_ref": str(anchor)}, tmp_path)
    assert "images/edits" in captured["url"]
    # 人工锁定人物图优先于风格图，不能因接口上限被挤掉
    assert captured["files"][0] == "portrait.png"
    assert "anchor.png" in captured["files"]
    assert "禁止漂移" in captured["prompt"]
    assert "fell_back_to_generations" not in captured
    assert result.provider == "image_api"


def test_openai_api_no_reference_falls_back_to_generations(tmp_path,
                                                           monkeypatch):
    """没有可用参考图时回退纯文本 generations,不崩。"""
    from aifos.production import api_providers
    from aifos.production.api_providers import OpenAIImageProvider

    used = {}

    def fake_gen(*a, **k):
        used["gen"] = True
        return {"data": [{"b64_json": "aGk="}]}

    def fake_edit(*a, **k):
        used["edit"] = True
        return {"data": [{"b64_json": "aGk="}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_gen)
    monkeypatch.setattr(api_providers, "_multipart_post", fake_edit)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-x"})
    provider.generate("image", {
        "portrait": True, "art_name": "小鹿", "prompt": "立绘",
        "aspect": "9:16"}, tmp_path)
    assert used.get("gen") and "edit" not in used


def test_single_and_batch_qc_and_redo(app):
    """单张质检 / 批量质检 / 批量重画未过 三件套。"""
    import json as _json
    project = _preproduce(app, title="质检三件套")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan_before = _json.loads(plan_path.read_text(encoding="utf-8"))["items"]
    initial = next(i for i in plan_before if i["category"] == "scene_art")
    with pytest.raises(AifosError, match="初始人物/场景母资产不做视觉质检"):
        app.director.qc_item("质检三件套", 1, initial["id"])
    # 正式镜头单张质检:mock 默认通过
    item = next(i for i in plan_before
                if i["category"] == "shot_image")
    report = app.director.qc_item("质检三件套", 1, item["id"])
    assert report["passed"] is True
    after = next(i for i in _json.loads(
        plan_path.read_text(encoding="utf-8"))["items"]
        if i["id"] == item["id"])
    assert after["qc"]["passed"] is True
    # 批量质检:全部核对
    summary = app.director.qc_all("质检三件套", 1)
    assert summary["status"] == "done" and summary["checked"] > 0
    # 人为标记两张未过 → 批量重画未过
    plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    fail_ids = [i["id"] for i in plan["items"]
                if i["category"] == "shot_image"][:2]
    vers = {}
    for i in plan["items"]:
        if i["id"] in fail_ids:
            i["qc"] = {"passed": False, "issues": ["测试标记未过"],
                       "attempts": 1}
            if i["id"] == fail_ids[0]:
                i["status"] = "awaiting_human"
            t = app.director._plan_item_target(i["id"])
            vers[i["id"]] = t
    plan_path.write_text(_json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")
    updates = []
    redo = app.director.redo_items(
        "质检三件套", 1, only_failed=True,
        progress=lambda **fields: updates.append(fields))
    assert redo["status"] == "done" and redo["redone"] == 2
    assert redo["checked"] == 2
    assert {u["phase"] for u in updates} >= {
        "queued", "redrawing", "checking", "done"}
    final_plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    redrawn = [i for i in final_plan["items"] if i["id"] in fail_ids]
    assert all(i["revision"]["source"] == "batch_qc" for i in redrawn)
    assert all(i["revision"]["prompt_modified"] for i in redrawn)
    assert all("测试标记未过" in i["prompt"] for i in redrawn)
    assert all(i["reference_inputs"]["attached"] for i in redrawn)
    assert all(i["reference_inputs"]["count"] >= 1 for i in redrawn)


def test_current_contract_recheck_includes_pending_existing_keyframes(app):
    """分镜换版后计划虽 pending，磁盘旧图仍必须按当前合同重新质检。"""
    import json as _json
    project = _preproduce(app, title="当前合同重检")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    target = next(
        item for item in plan["items"]
        if item["category"] == "shot_image")
    target["status"] = "pending"
    target["qc"] = None
    target["invalidated_previous_output"] = True
    plan_path.write_text(
        _json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    summary = app.director.qc_all(
        "当前合同重检", 1, include_existing=True,
        auto_repair=False, parallel=False, categories=["shot_image"])
    assert summary["checked"] > 0
    refreshed = next(
        item for item in _json.loads(
            plan_path.read_text(encoding="utf-8"))["items"]
        if item["id"] == target["id"])
    assert refreshed["status"] == "done"
    assert refreshed["qc"]["passed"] is True
    assert refreshed["contract_recheck"] is True


def test_batch_redo_dispatches_same_scene_keyframes_in_parallel(app, monkeypatch):
    """同场关键帧彼此独立，批量返工也会真正占用两个 Codex 槽位。"""
    project = _preproduce(app, title="并行返工调度")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    shots = [item for item in plan["items"]
             if item["category"] == "shot_image"]
    assert len(shots) >= 2
    # 选同一场的两个关键帧；只有首尾帧链需要按场串行。
    storyboard, _ = app.projects.latest_document(
        app.db.query_one("SELECT id FROM episodes WHERE project_id=?",
                         (project["id"],))["id"], "storyboard")
    scene_by_shot = {int(shot["shot_no"]): shot.get("scene_no")
                     for shot in storyboard["shots"]}
    by_scene = {}
    for item in shots:
        scene = scene_by_shot.get(int(item["shot_no"]))
        by_scene.setdefault(scene, []).append(item)
    chosen = next(
        values[:2] for values in by_scene.values() if len(values) >= 2)
    assert len(chosen) == 2
    ids = {item["id"] for item in chosen}
    for item in plan["items"]:
        if item["id"] in ids:
            item["status"] = "done"
            item["custom_prompt"] = True
            item["prompt"] = "旧版整段提示词不得再次进入当前镜头"
            item["qc"] = {"passed": False, "issues": ["测试标记未过"]}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    barrier = threading.Barrier(2)
    active = 0
    max_active = 0
    regen_kwargs = []
    state_lock = threading.Lock()

    def fake_regen(self, *args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            regen_kwargs.append(dict(kwargs))
        try:
            barrier.wait(timeout=2)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr("aifos.director.Director.regen_image", fake_regen)
    monkeypatch.setattr(
        "aifos.director.Director._qc_one",
        lambda self, project, episode, ctx, item: {"passed": True,
                                                    "issues": []})
    monkeypatch.setattr(app.director, "_codex_parallel_profiles", lambda: [
        {"id": "codex_a"}, {"id": "codex_b"}])

    result = app.director.redo_items(
        project["title"], 1, item_ids=list(ids))
    assert result["status"] == "done"
    assert result["redone"] == 2 and result["checked"] == 2
    assert max_active == 2
    assert all(call["prompt_override"] == "" for call in regen_kwargs)
    assert all(call["revision_source"] == "batch_current_contract"
               for call in regen_kwargs)


def test_manual_qc_pass_promotes_failed_draft_and_keeps_audit_reason(app,
                                                                      tmp_path):
    """轻微问题可人工放行;失败稿进入正式资产但原问题不丢。"""
    project = _preproduce(app, title="人工放行质检")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    item = next(i for i in plan["items"] if i["category"] == "shot_image")
    asset_name = f"e001_shot{int(item['shot_no']):03d}"
    failed = tmp_path / "manual-pass-failed.png"
    failed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"failed" * 8)
    app.assets.soft_delete(project["id"], "image", asset_name)
    item.update({
        "status": "awaiting_human",
        "output_uri": str(failed),
        "qc": {"passed": False, "attempts": 2,
               "issues": ["人物表情轻微偏差"]},
    })
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    result = app.director.manual_qc_pass(
        "人工放行质检", 1, item_ids=[item["id"]],
        note="人工确认：表情偏差不影响剧情理解")
    assert result["passed"] == 1 and result["skipped"] == 0
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    final = next(i for i in final_plan["items"] if i["id"] == item["id"])
    assert final["status"] == "done"
    assert final["qc"]["passed"] is True
    assert final["qc"]["manual_override"] is True
    assert final["qc"]["manual_original_issues"] == ["人物表情轻微偏差"]
    latest = app.assets.latest(project["id"], "image", asset_name)
    assert latest["uri"] == str(failed)
    assert json.loads(latest["meta"])["manual_qc_override"] is True


def test_manual_qc_pass_cannot_override_identity_or_count_failure(app,
                                                                  tmp_path):
    project = _preproduce(app, title="硬错误禁止放行")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    item = next(i for i in plan["items"] if i["category"] == "shot_image")
    failed = tmp_path / "identity-count-failed.png"
    failed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"failed" * 8)
    item.update({
        "status": "awaiting_human",
        "output_uri": str(failed),
        "qc": {
            "passed": False,
            "hard_failure": True,
            "identity_checked": True,
            "identity_match": False,
            "count_checked": True,
            "count_match": False,
            "issues": ["人物身份不一致", "画面人数多一人"],
        },
    })
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    result = app.director.manual_qc_pass(
        "硬错误禁止放行", 1, item_ids=[item["id"]])
    assert result["passed"] == 0
    assert result["skipped"] == 1
    assert "不能人工强行放行" in result["skipped_items"][0]["reason"]


def test_manual_qc_pass_can_override_prompt_contract_and_minor_camera_issue(
        app, tmp_path):
    """画面硬门均通过时，提示词冗余和轻微机位偏差可由人工放行。"""
    project = _preproduce(app, title="人工放行输入合同")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    item = next(i for i in plan["items"] if i["category"] == "shot_image")
    failed = tmp_path / "minor-camera-failed.png"
    failed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"failed" * 8)
    item.update({
        "status": "awaiting_human",
        "output_uri": str(failed),
        "qc": {
            "passed": False,
            "hard_failure": False,
            "identity_checked": True,
            "identity_match": True,
            "gender_checked": True,
            "gender_match": True,
            "count_checked": True,
            "count_match": True,
            "physical_logic_checked": True,
            "physical_logic_match": True,
            "spatial_logic_checked": True,
            "spatial_logic_match": True,
            "input_contract_passed": False,
            "issues": [
                "荷兰角不够明显",
                "其余人数、身份、服装、动作和空间关系成立",
                "生成提示词存在服装描述冲突",
            ],
        },
    })
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    result = app.director.manual_qc_pass(
        "人工放行输入合同", 1, item_ids=[item["id"]])

    assert result["passed"] == 1
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    final = next(i for i in final_plan["items"] if i["id"] == item["id"])
    assert final["status"] == "done"
    assert final["qc"]["manual_override"] is True


def test_codex_qc_instruction_and_parse(tmp_path, monkeypatch):
    """Codex 图像质检:构造读图指令,从 stdout 解析判定 JSON。"""
    from aifos.adapters import codex_image
    instruction, targets, data = codex_image.build_instruction(
        "image_qc", {"image_uri": "/tmp/f.png",
                     "characters": ["小鹿"], "count": 1,
                     "designs": "小鹿(发型:银白短发)",
                     "location": "地铁站", "forbid": ["悬挂衣物"]}, tmp_path)
    assert "/tmp/f.png" in instruction and "小鹿" in instruction
    assert "允许与身份参考图不同" in instruction
    assert '"pass"' in instruction
    assert targets == [] and data["qc"] is True

    escalation_instruction, _, _ = codex_image.build_instruction(
        "image_qc", {
            "image_uri": "/tmp/f.png",
            "characters": ["小鹿"],
            "codex_escalation_context": {"consecutive_failures": 2},
        }, tmp_path)
    assert "质检失败后的 Codex 升级分析" in escalation_instruction
    assert "藏入袖内" in escalation_instruction
    assert "split_shot" in escalation_instruction
    assert "aifos_instructions" in escalation_instruction
    # 候选组三抽仍失败后，Codex 继续给唯一自动执行指令，不转人工。
    assert "修订候选组仍未通过" in escalation_instruction
    assert "AIFOS 会自动应用" in escalation_instruction
    assert "不会停在人工确认点" in escalation_instruction

    # 第 1 次失败要的是"可直接拼进下一次提示词"的可执行表述,不是建议。
    first_failure_instruction, _, _ = codex_image.build_instruction(
        "image_qc", {
            "image_uri": "/tmp/f.png",
            "characters": ["小鹿"],
            "codex_escalation_context": {"consecutive_failures": 1},
        }, tmp_path)
    assert "此前已失败 1 次" in first_failure_instruction
    assert "按你给出的新提示词" in first_failure_instruction
    assert "targeted_redraw" in first_failure_instruction
    assert "最终裁决" not in first_failure_instruction

    # 预授权模式(随首检下发,此前失败=0):判定必须保持中立,
    # 不得因为带了升级上下文就预设本图已失败。
    prearmed_instruction, _, _ = codex_image.build_instruction(
        "image_qc", {
            "image_uri": "/tmp/f.png",
            "characters": ["小鹿"],
            "codex_escalation_context": {"consecutive_failures": 0},
        }, tmp_path)
    assert "不预设结论" in prearmed_instruction
    assert "判定通过则" in prearmed_instruction
    assert "codex_escalation" in prearmed_instruction
    assert "这是质检失败后的 Codex 升级分析" not in prearmed_instruction
    assert "最终裁决" not in prearmed_instruction

    stdout = '思考中…\n{"pass": false, "issues": ["尾帧换了个人"]}\n完成'

    class FakePopen:
        def __init__(self, *a, **k):
            self.args = a[0] if a else []
            self.returncode = 0

        def communicate(self, timeout=None):
            return stdout, ""

    monkeypatch.setattr(codex_image.shutil, "which",
                        lambda _c: "/usr/bin/codex")
    monkeypatch.setattr(codex_image.subprocess, "Popen", FakePopen)
    reply = codex_image.run({
        "capability": "image_qc",
        "payload": {"image_uri": "/tmp/f.png", "characters": ["小鹿"]},
        "out_dir": str(tmp_path)}, "codex", 30, [])
    assert reply["ok"] is True
    assert reply["data"]["pass"] is False
    assert reply["data"]["issues"] == ["尾帧换了个人"]


def test_codex_qc_unparseable_output_fails_closed(tmp_path, monkeypatch):
    """质检没有可靠 JSON 时不得伪造 checked=true 后放行。"""
    from aifos.adapters import codex_image

    class FakePopen:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.args = args[0] if args else []

        def communicate(self, timeout=None):
            return "我看过了，应该没问题。", ""

    monkeypatch.setattr(codex_image.shutil, "which",
                        lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(codex_image.subprocess, "Popen", FakePopen)
    reply = codex_image.run({
        "capability": "image_qc",
        "payload": {"image_uri": "/tmp/f.png", "characters": ["程沐"]},
        "out_dir": str(tmp_path)}, "codex", 30, [])
    assert reply["ok"] is True
    assert reply["data"]["pass"] is False
    assert reply["data"]["identity_checked"] is False
    assert reply["data"]["gender_checked"] is False
    assert reply["data"]["count_checked"] is False
    assert "未返回可解析" in reply["data"]["issues"][0]


def test_frames_qc_checks_both_frames(app, monkeypatch):
    """首尾帧质检:首帧或尾帧任一不符即整组不合格。"""
    project = _preproduce(app, title="首尾帧质检")
    calls = {"n": 0}
    real_call = app.director.router.call

    def qc_router(capability, payload, out_dir, cancel=None):
        if capability == "image_qc":
            calls["n"] += 1
            # 第 2 次(尾帧)判不合格
            from aifos.production.base import ProviderResult
            return ProviderResult(
                provider="codex", cost=0.1,
                data={"pass": calls["n"] != 2,
                      "identity_checked": True,
                      "identity_match": True,
                      "gender_checked": True,
                      "gender_match": True,
                      "count_checked": True,
                      "count_match": True,
                      "physical_logic_checked": True,
                      "physical_logic_match": True,
                      "spatial_logic_checked": True,
                      "spatial_logic_match": True,
                      "issues": [] if calls["n"] != 2 else ["尾帧人物不符"]})
        return real_call(capability, payload, out_dir, cancel=cancel)

    app.director.router.call = qc_router
    report = app.director.qc_item("首尾帧质检", 1, "frames:2")
    # 首帧、尾帧及两帧联合连续性各检查一次。
    assert calls["n"] == 3
    assert report["passed"] is False
    assert any("尾帧" in x for x in report["issues"])
