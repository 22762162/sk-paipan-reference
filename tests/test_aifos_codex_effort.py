"""Codex 出图链推理强度:账号全局 xhigh 不得拖慢机械执行环节。

12星座 ep1 实测:出图链占全部墙钟 97%,而账号 config.toml 默认
model_reasoning_effort="xhigh"——此前只有 prompt_review 显式压到
low,出图与视觉复检每次调用都在最大思考强度下白烧几分钟。
"""

from aifos.adapters.codex_image import (
    DEFAULT_EXEC_ARGS,
    IMAGE_QC_EXEC_ARGS,
    PROMPT_REVIEW_EXEC_ARGS,
    _exec_args_for,
)


def _effort(args):
    for index, value in enumerate(args):
        if value == "-c" and index + 1 < len(args):
            override = args[index + 1]
            if override.startswith("model_reasoning_effort="):
                return override.split("=", 1)[1].strip('"')
    return None


def test_every_capability_overrides_account_xhigh():
    """账号 config.toml 是 xhigh;出图链每个能力都必须显式覆盖。"""
    for capability in ("image", "frames", "cover", "image_qc",
                       "prompt_review"):
        args = _exec_args_for(capability)
        assert _effort(args) is not None, capability
        assert "--skip-git-repo-check" in args


def test_mechanical_generation_runs_at_low_effort():
    assert _effort(_exec_args_for("image")) == "low"
    assert _effort(_exec_args_for("frames")) == "low"
    assert _effort(_exec_args_for("prompt_review")) == "low"


def test_visual_qc_keeps_medium_reasoning():
    """视觉复检要逐张比对参考图与画面事实,保留 medium 判断力。"""
    assert _effort(_exec_args_for("image_qc")) == "medium"
    assert "--sandbox" in IMAGE_QC_EXEC_ARGS
    assert "workspace-write" in IMAGE_QC_EXEC_ARGS


def test_plain_mode_stays_empty_for_legacy_retry():
    assert _exec_args_for("image", plain=True) == []
    assert _exec_args_for("image_qc", plain=True) == []
    # 既有默认集合不被破坏
    assert "--sandbox" in DEFAULT_EXEC_ARGS
    assert "--ephemeral" in PROMPT_REVIEW_EXEC_ARGS
