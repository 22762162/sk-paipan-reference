from pathlib import Path

import pytest

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


def test_candidate_review_uses_explicit_single_look_context(tmp_path):
    source = (
        "【任务】林川单人定角候选。"
        "【本张唯一造型】灰褐麻布短褐，携湿旧蓝布包袱。"
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
            "schema": "aifos.character-candidate-review/v2",
            "characters": ["林川"],
            "character_count": 1,
            "current_candidate_variant": {
                "wardrobe": "灰褐麻布短褐",
                "accessories_and_props": "湿旧蓝布包袱",
            },
        },
    }

    router.call("image", payload, tmp_path)

    review_context = codex.calls[0][1]["review_context"]
    assert review_context["capability"] == "image"
    assert review_context["current_candidate_variant"]["wardrobe"] \
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
