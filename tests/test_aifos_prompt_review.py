from pathlib import Path

import pytest

from aifos.app import App
from aifos.config import Config
from aifos.errors import ProviderError
from aifos.production.base import ProviderResult
from aifos.production.router import ProviderRouter
from aifos.adapters.codex_image import build_instruction


class _Db:
    def query_one(self, *_args, **_kwargs):
        return None

    def execute(self, *_args, **_kwargs):
        return None


class _Log:
    def info(self, *_args, **_kwargs):
        return None

    def warn(self, *_args, **_kwargs):
        return None


class _Codex:
    name = "codex"
    quota_limit = 0
    reference_images = True
    conf = {"type": "cli"}

    def __init__(self, optimized):
        self.optimized = optimized
        self.calls = []

    def available(self, capability):
        return capability in {
            "prompt_review", "image", "frames", "cover",
        }, ""

    def generate(self, capability, payload, out_dir, cancel=None):
        self.calls.append((capability, dict(payload), Path(out_dir)))
        if capability == "prompt_review":
            source = payload["review_prompt"]
            optimized = (
                self.optimized(source)
                if callable(self.optimized) else self.optimized)
            return ProviderResult(
                provider="codex", cost=0.2,
                model="Codex 提示词审核优化",
                data={
                    "schema": "aifos.codex-prompt-review/v1",
                    "approved": True,
                    "optimized_prompt": optimized,
                    "issues_found": ["删除重复表达"],
                    "changes_made": ["压缩为单一可执行画面"],
                    "blocking_reason": "",
                })
        return ProviderResult(
            provider="codex", cost=1.0, uri=str(Path(out_dir) / "out.png"),
            model="gpt-image-2", data={})


def _router(optimized):
    config = Config({
        "defaults": {"parallel_images": 1},
        "providers": {},
        "routing": {
            "prompt_review": ["codex"],
            "image": ["codex"],
            "frames": ["codex"],
            "cover": ["codex"],
        },
    })
    router = ProviderRouter(config, _Db(), _Log())
    codex = _Codex(optimized)
    router.providers = {"codex": codex}
    return router, codex


def test_real_image_uses_codex_optimized_prompt_before_generation(tmp_path):
    source = (
        "【镜头合同v2.2】只执行事实。"
        "【主体】严格共1人：林川。"
        "【场景】县衙。"
        "【文字】无字幕。"
    )
    router, codex = _router(lambda value: value + "画面层级清晰。")
    payload = {
        "shot_no": 1,
        "prompt": source,
        "prompt_compact": source,
        "characters": ["林川"],
        "character_count": 1,
        "location": "县衙",
    }

    result = router.call("image", payload, tmp_path)

    assert [call[0] for call in codex.calls] == ["prompt_review", "image"]
    assert codex.calls[1][1]["prompt_compact"].endswith("画面层级清晰。")
    assert payload["prompt_aifos_original"] == source
    assert payload["prompt_review"]["approved"] is True
    assert result.data["prompt_review"]["approved"] is True
    assert result.cost == pytest.approx(1.2)


def test_optimized_prompt_cannot_delete_immutable_contract_facts(tmp_path):
    source = (
        "【镜头合同v2.2】只执行事实。"
        "【主体】严格共1人：林川。"
        "【场景】县衙。"
    )
    router, codex = _router("一个古装人物站在建筑内。")
    payload = {
        "shot_no": 1,
        "prompt": source,
        "characters": ["林川"],
        "character_count": 1,
        "location": "县衙",
    }

    with pytest.raises(ProviderError, match="删除了不可变事实"):
        router.call("image", payload, tmp_path)

    assert [call[0] for call in codex.calls] == ["prompt_review"]


def test_codex_may_remove_contract_headings_but_not_visual_facts(tmp_path):
    source = (
        "【镜头合同v2.2】只执行事实。"
        "【主体】严格共1人：林川，穿青色圆领官袍。"
        "【场景】县衙。"
        "【镜头】中近景。"
    )
    optimized = (
        "县衙内严格只有1人：林川，身穿青色圆领官袍；"
        "中近景固定画面。"
    )
    router, codex = _router(optimized)
    payload = {
        "shot_no": 1,
        "prompt": source,
        "characters": ["林川"],
        "character_count": 1,
        "location": "县衙",
        "start_state": {"林川": {"wardrobe": "青色圆领官袍"}},
        "end_state": {"林川": {"wardrobe": "青色圆领官袍"}},
    }

    router.call("image", payload, tmp_path)

    assert payload["prompt"] == optimized
    assert [call[0] for call in codex.calls] == ["prompt_review", "image"]


def test_prompt_review_instruction_forbids_image_generation(tmp_path):
    instruction, targets, _ = build_instruction(
        "prompt_review",
        {
            "review_schema": "aifos.codex-prompt-review/v1",
            "review_prompt": "【任务】角色正面母资产：林川。",
            "review_context": {"characters": ["林川"]},
        },
        tmp_path,
    )

    assert targets == []
    assert "禁止调用imagegen" in instruction
    assert "optimized_prompt" in instruction
    assert "【任务】角色正面母资产：林川。" in instruction


def test_candidate_review_uses_explicit_initial_state_context(tmp_path):
    source = (
        "【任务】林川单人初始状态定角候选。"
        "【共同初始造型】灰褐麻布短褐，携旧蓝布包袱。"
    )
    router, codex = _router(source)
    payload = {
        "portrait_candidate": True,
        "art_name": "林川_candidate_01",
        "prompt": source,
        "characters": ["林川"],
        "character_count": 1,
        "character_background": {
            "costume": "旧靛青举人袍",
            "signature_props": "吏部札付",
        },
        "prompt_review_context": {
            "schema": "aifos.character-candidate-review/v3-initial-state",
            "characters": ["林川"],
            "character_count": 1,
            "initial_character_state": {
                "wardrobe": "灰褐麻布短褐",
                "accessories_and_props": "旧蓝布包袱",
            },
        },
    }

    router.call("image", payload, tmp_path)

    review_context = codex.calls[0][1]["review_context"]
    assert review_context["capability"] == "image"
    assert review_context["initial_character_state"]["wardrobe"] \
        == "灰褐麻布短褐"
    assert "character_background" not in review_context
    assert "旧靛青举人袍" not in str(review_context)
    assert "吏部札付" not in str(review_context)
    # 技术文件名不属于画面事实，不应强迫 Codex 把它写入优化稿。
    assert "林川_candidate_01" not in payload["prompt"]


def test_regular_shot_review_keeps_full_character_background():
    context = ProviderRouter._prompt_review_context("image", {
        "characters": ["林川"],
        "character_background": {
            "林川": {"costume": "青色圆领官袍"},
        },
    })

    assert context["character_background"]["林川"]["costume"] \
        == "青色圆领官袍"


def test_complete_shot_review_context_excludes_raw_audit_fields():
    context = ProviderRouter._prompt_review_context("image", {
        "prompt_contract_complete": True,
        "shot_no": 1,
        "frame_kind": "keyframe",
        "characters": ["虞寻歌"],
        "character_count": 1,
        "location": "现代卧室",
        "action": "旧字段把半垂纱幕塞回画面",
        "start_state": {"虞寻歌": {"direction": "视线越过纱幕"}},
        "shot_contract": {"画面内容描述": "书案香炉"},
        "character_background": {"虞寻歌": {"identity": "整集背景"}},
        "prompt_contract": {
            "schema": "aifos.shot-prompt/v2.2",
            "scene": "现代卧室",
            "action": "虞寻歌坐在现代沙发上",
            "frame_target": {"phase": "freeze", "state": "坐在沙发上"},
        },
    })

    packed = str(context)
    assert "坐在沙发上" in packed
    assert "虞寻歌坐在现代沙发上" not in packed
    assert "action" not in context["prompt_contract"]
    assert "start" not in context["prompt_contract"]
    assert "end" not in context["prompt_contract"]
    for leaked in ("半垂纱幕", "视线越过纱幕", "书案香炉", "整集背景"):
        assert leaked not in packed


def test_complete_review_persists_canonical_prompt_and_is_idempotent(
        tmp_path):
    source = (
        "【镜头合同v2.2】只执行事实。"
        "【主体】严格共1人：虞寻歌。"
        "【场景】现代卧室。")
    optimized = (
        "【镜头合同v2.2】只执行事实。\n\n"
        "【主体】严格共1人：虞寻歌。  \n"
        "【场景】现代卧室。")
    router, codex = _router(optimized)
    payload = {
        "shot_no": 1,
        "frame_kind": "keyframe",
        "prompt": source,
        "prompt_compact": source,
        "prompt_contract_complete": True,
        "characters": ["虞寻歌"],
        "character_count": 1,
        "location": "现代卧室",
        "active_realm_id": "modern",
        "prompt_contract": {
            "schema": "aifos.shot-prompt/v2.2",
            "scene": "现代卧室",
            "frame_target": {"phase": "freeze", "state": "坐在沙发"},
        },
    }

    router.call("image", payload, tmp_path)
    canonical = payload["prompt_compact"]
    assert "\n" not in canonical
    assert payload["prompt_review"]["optimized_prompt"] == canonical

    router.call("image", payload, tmp_path)

    assert [call[0] for call in codex.calls] == [
        "prompt_review", "image", "image"]


def test_frozen_candidate_uses_one_approved_prompt_without_re_review(
        tmp_path):
    source = (
        "【镜头合同v2.2】只执行事实。"
        "【主体】严格共1人：虞寻歌。"
        "【场景】现代卧室。")
    router, codex = _router(source)
    payload = {
        "shot_no": 1,
        "prompt": source,
        "prompt_compact": source,
        "prompt_contract_complete": True,
        "characters": ["虞寻歌"],
        "character_count": 1,
        "location": "现代卧室",
        "_candidate_prompt_review_locked": True,
        "_prompt_review_frozen_input_hash": "frozen-director-input",
        "prompt_review": {
            "schema": "aifos.codex-prompt-review/v1",
            "approved": True,
            "status": "approved",
            "optimized_prompt": source,
            "optimized_hash": router._stable_hash(source),
        },
    }

    router.call("image", payload, tmp_path)

    assert [call[0] for call in codex.calls] == ["image"]


def test_prompt_review_scene_guard_falls_back_instead_of_changing_set(
        tmp_path):
    source = (
        "【镜头合同v2.2】只执行事实。"
        "【主体】严格共1人：虞寻歌。"
        "【场景】现代卧室。")
    router, codex = _router(
        lambda value: value + "左缘新增半垂纱幕作为虚化前景。")
    payload = {
        "shot_no": 1,
        "frame_kind": "keyframe",
        "prompt": source,
        "prompt_compact": source,
        "prompt_contract_complete": True,
        "characters": ["虞寻歌"],
        "character_count": 1,
        "location": "现代卧室",
        "active_realm_id": "modern",
        "prompt_contract": {
            "schema": "aifos.shot-prompt/v2.2",
            "scene": "现代卧室",
            "action": "虞寻歌坐在沙发上",
        },
    }

    router.call("image", payload, tmp_path)

    assert payload["prompt"] == source
    assert payload["prompt_review"]["status"] \
        == "approved_scene_guard_fallback"
    assert "纱幕" not in payload["prompt"]
    assert [call[0] for call in codex.calls] == ["prompt_review", "image"]


def test_director_autonomy_dispatches_clean_scene_projection(tmp_path):
    source = (
        "【镜头合同v2.2】现代卧室内严格共1人：虞寻歌。"
        "手机厚度明显小于书案上的线装册。"
        "左缘有半垂纱幕和铜香炉，沉香烟雾形成丁达尔光。")
    router, codex = _router(source)
    payload = {
        "shot_no": 1,
        "prompt": source,
        "prompt_compact": source,
        "prompt_contract_complete": True,
        "director_autonomy_mode": True,
        "characters": ["虞寻歌"],
        "character_count": 1,
        "location": "现代卧室",
        "active_realm_id": "modern",
        "prompt_contract": {
            "schema": "aifos.shot-prompt/v2.2",
            "scene": "现代卧室",
        },
    }

    router.call("image", payload, tmp_path)

    assert [call[0] for call in codex.calls] == ["image"]
    dispatched = codex.calls[0][1]["prompt_compact"]
    for forbidden in ("书案", "线装册", "纱幕", "香炉", "沉香烟雾"):
        assert forbidden not in dispatched
    assert payload["prompt_review"]["status"] \
        == "not_applicable_director_autonomy"


def test_same_prompt_candidate_group_reuses_one_codex_optimized_prompt(
        tmp_path, monkeypatch):
    app = App(tmp_path / "ws")

    class ReviewRouter:
        calls = 0

        @staticmethod
        def _prompt_review_context(capability, payload):
            return {
                **payload["prompt_review_context"],
                "capability": capability,
            }

        def review_image_prompt(
                self, capability, payload, out_dir, cancel=None):
            self.calls += 1
            source = payload["prompt"]
            payload["prompt_aifos_original"] = source
            payload["prompt"] = "Codex统一优化后的初始人物提示词"
            payload["prompt_review"] = {
                "approved": True,
                "status": "approved",
                "reviewed_input_hash": "shared",
            }
            payload["prompt_review_schema"] = (
                "aifos.codex-prompt-review/v1")
            return None

    router = ReviewRouter()
    app.director.router = router
    monkeypatch.setattr(
        app.director, "_plan_mark", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app.director, "_plan_read_status",
        lambda *_args, **_kwargs: "pending")
    tasks = []
    for index in range(1, 5):
        tasks.append({
            "item_id": f"candidate:林川:{index}",
            "capability": "image",
            "sub_dir": "cast/candidates",
            "payload": {
                "art_name": f"林川_candidate_{index:02d}",
                "prompt": "AIFOS完全相同的初始人物提示词",
                "prompt_review_group_key": "character-initial:1:林川:v3",
                "prompt_review_context": {
                    "schema":
                        "aifos.character-candidate-review/v3-initial-state",
                    "characters": ["林川"],
                    "character_count": 1,
                    "initial_character_state": {
                        "wardrobe": "灰褐粗麻短褐",
                    },
                },
            },
        })

    try:
        app.director._review_image_tasks(
            {"out_root": tmp_path}, tasks)
    finally:
        app.close()

    assert router.calls == 1
    assert {task["payload"]["prompt"] for task in tasks} == {
        "Codex统一优化后的初始人物提示词"}
    assert all(task["payload"]["prompt_review"]["approved"]
               for task in tasks)


def test_review_strips_audit_sentence_that_quotes_obsolete_camera_clause(
        tmp_path):
    source = (
        "严格共2人：沈砚舟、顾明昭。景别锁定为中近景。"
        "不得同时执行被作废的旧条款：【Codex 通知 AIFOS】"
        "删除‘景别锁定为近景’，全合同只保留中近景。")
    optimized = source + "固定机位，平视侧面。"
    router, codex = _router(optimized)
    payload = {
        "shot_no": 18,
        "prompt": source,
        "characters": ["沈砚舟", "顾明昭"],
        "character_count": 2,
    }

    router.call("image", payload, tmp_path)

    submitted = codex.calls[1][1]["prompt"]
    assert "景别锁定为中近景" in submitted
    assert "景别锁定为近景" not in submitted
    assert "被作废的旧条款" not in submitted


def test_review_blocks_multiple_executable_scale_locks_before_image_api(
        tmp_path):
    source = "严格共2人：沈砚舟、顾明昭。景别锁定为中近景。"
    optimized = source + "另一执行条款：景别锁定为近景。"
    router, codex = _router(optimized)
    payload = {
        "shot_no": 18,
        "prompt": source,
        "characters": ["沈砚舟", "顾明昭"],
        "character_count": 2,
    }

    with pytest.raises(ProviderError, match="同时锁定多个景别"):
        router.call("image", payload, tmp_path)

    assert [call[0] for call in codex.calls] == ["prompt_review"]
