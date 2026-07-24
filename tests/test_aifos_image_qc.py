"""图片视觉质检:核对剧本要求,不合格自动重画;镜头景别多样性。"""

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
    ok = {"pass": True, "issues": []}
    assert validate_image_qc(ok) is None
    bad = {"pass": False, "issues": "镜头9画成了动物"}
    assert validate_image_qc(bad) is None
    assert bad["issues"] == ["镜头9画成了动物"]
    assert validate_image_qc({"issues": []}) == "缺少 pass 字段"


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
    """质检不过 → 自动带意见重画 → 通过;意见进入重画载荷。"""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    calls = {"image": [], "qc": []}

    class StubRouter:
        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                calls["image"].append(dict(payload))
                return ProviderResult(provider="codex", cost=1.0,
                                      uri=str(image))
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
        "image", {"prompt": "x", "shot_no": 1}, tmp_path, None,
        {"characters": ["小鹿"], "count": 1, "designs": "",
         "location": "", "action": "", "forbid": []})
    assert len(calls["image"]) == 2          # 首画 + 质检重画
    assert "小鹿是人类女性" in calls["image"][1]["feedback"]
    assert "【本镜定向修正】" in calls["image"][1]["feedback"]
    assert "只修改当前镜头" in calls["image"][1]["feedback"]
    assert "【质检原因】" not in calls["image"][1]["feedback"]
    assert result.qc["passed"] is True
    assert result.qc["attempts"] == 2
    assert result.cost == 3.0        # 两次出图(2.0)+两次质检(1.0)
    assert calls["qc"][0]["generation_input"]["scope"]["shot_no"] == 1
    assert calls["qc"][0]["generation_input"]["input_hash"]
    assert len(result.qc["attempt_history"]) == 2
    assert result.qc["attempt_history"][0][
        "input_hash"] != result.qc["attempt_history"][1]["input_hash"]


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
            "reference_images": [str(wrong)],
            "asset_matches": [{
                "uri": str(wrong), "label": "用户错误参考图",
                "reference_role": "manual",
            }],
        }, tmp_path, None, {
            "characters": [], "count": 0, "count_required": True,
        })

    assert result.qc["passed"] is True
    assert len(calls["image"]) == 2
    assert str(wrong) not in calls["image"][1].get("reference_images", [])
    assert calls["image"][1].get("reference_manifest") == []
    assert result.qc["attempt_history"][0][
        "reference_hash"] != result.qc["attempt_history"][1][
            "reference_hash"]


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


def test_count_mismatch_auto_revises_bad_image_with_locked_references(
        app, tmp_path):
    """人数错误必须把失败图作为待修改基底重画，并与最终立绘一起复检。"""
    image = tmp_path / "shot.png"
    identity = tmp_path / "identity.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    identity.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    calls = {"image": [], "qc": []}

    class StubRouter:
        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                calls["image"].append(dict(payload))
                return ProviderResult(
                    provider="seedream5_lite", cost=0.2, uri=str(image))
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
    assert len(calls["image"]) == 2
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
    monkeypatch.setattr(app.director, "_claim_dispatch_task",
                        lambda _ctx, task: task)
    monkeypatch.setattr(app.director, "_finish_dispatch_task",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app.director, "_attach_reference_manifest",
                        lambda _payload: None)
    monkeypatch.setattr(app.director, "_generate_image_with_qc",
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
    assert output_by_shot[2].exists(), "失败图必须留给人工查看和定向修改"

    plan = app.director._plan_read(ctx)
    by_id = {item["id"]: item for item in plan["items"]}
    assert by_id["shot:2"]["status"] == "awaiting_human"
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

    assert result == {"recovered": 1, "awaiting_human": 2}
    formal = app.assets.active_list(project["id"], kind="image")
    assert [(row["name"], row["uri"]) for row in formal] == [
        ("e001_shot001", str(passed_uri))]
    plan = app.director._plan_read(ctx)
    by_id = {item["id"]: item for item in plan["items"]}
    assert by_id["shot:1"]["output_uri"] == str(passed_uri)
    assert by_id["shot:2"]["status"] == "awaiting_human"
    assert by_id["shot:2"]["output_uri"] == str(failed_uri)
    assert by_id["shot:2"]["qc"]["auto_repair_exhausted"] is True
    assert "output_uri" not in by_id["shot:3"]
    assert by_id["shot:4"]["status"] == "awaiting_human"
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
    for cat in ("character_candidate", "scene_art"):
        drawn = [i for i in plan["items"]
                 if i["category"] == cat
                 and i["status"] in ("done", "reused")]
        assert drawn, f"{cat} 无生成条目"
        assert all("qc" not in i for i in drawn), f"{cat} 不应做初始视觉质检"
    # 四视图等母资产会被后续所有镜头当参考图:生成后即做身份质检
    sheets = [i for i in plan["items"]
              if i["category"] == "character_sheet"
              and i["status"] in ("done", "reused")]
    assert sheets, "character_sheet 无生成条目"
    assert all(i.get("qc", {}).get("passed") for i in sheets), \
        "character_sheet 缺母资产身份质检结果"
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


def test_batch_redo_dispatches_different_scenes_in_parallel(app, monkeypatch):
    """批量关键帧返工按 Codex 槽位并行,同场仍由场景锁保护。"""
    project = _preproduce(app, title="并行返工调度")
    plan_path = (app.workspace.artifacts_dir
                 / f"p{project['id']:03d}" / "e001" / "render_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    shots = [item for item in plan["items"]
             if item["category"] == "shot_image"]
    assert len(shots) >= 2
    # 选不同场景,否则连续性锁会按设计串行。
    storyboard, _ = app.projects.latest_document(
        app.db.query_one("SELECT id FROM episodes WHERE project_id=?",
                         (project["id"],))["id"], "storyboard")
    scene_by_shot = {int(shot["shot_no"]): shot.get("scene_no")
                     for shot in storyboard["shots"]}
    chosen = []
    scenes = set()
    for item in shots:
        scene = scene_by_shot.get(int(item["shot_no"]))
        if scene not in scenes:
            chosen.append(item)
            scenes.add(scene)
        if len(chosen) == 2:
            break
    assert len(chosen) == 2
    ids = {item["id"] for item in chosen}
    for item in plan["items"]:
        if item["id"] in ids:
            item["status"] = "done"
            item["qc"] = {"passed": False, "issues": ["测试标记未过"]}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    barrier = threading.Barrier(2)
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_regen(self, *args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
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
                      "issues": [] if calls["n"] != 2 else ["尾帧人物不符"]})
        return real_call(capability, payload, out_dir, cancel=cancel)

    app.director.router.call = qc_router
    report = app.director.qc_item("首尾帧质检", 1, "frames:2")
    assert calls["n"] == 2                 # 首帧 + 尾帧都检了
    assert report["passed"] is False
    assert any("尾帧" in x for x in report["issues"])
