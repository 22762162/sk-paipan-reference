"""提示词审核与下游派发合同必须用同一套标准。

审核会重写提示词。若下游合同逐字校验某个词、而审核不知道它不可
改写,就会出现「审核通过 → 合同拒绝」的死锁:《雨夜凶杀》道具母版
被优化成「青袍」后卡在"提示词没有明确写出对象「旧靛青举人青袍」"。
"""

from aifos.adapters.codex_image import build_instruction
from aifos.production.router import ProviderRouter

required = ProviderRouter._prompt_review_required_tokens


def test_prop_name_is_required_verbatim():
    """下游合同校验 prop_name,必留词表就必须包含它。"""
    source = "【任务】核心道具「旧靛青举人青袍」四选一候选。材质棉布。"
    tokens = required(source, {"prop_name": "旧靛青举人青袍"})
    assert "旧靛青举人青袍" in tokens


def test_internal_filenames_are_not_forced_into_prompt():
    """未出现在原稿里的内部文件名不得被要求保留。"""
    source = "【任务】核心道具「旧靛青举人青袍」四选一候选。"
    tokens = required(source, {
        "prop_name": "旧靛青举人青袍",
        "art_name": "旧靛青举人青袍_candidate_01"})
    assert "旧靛青举人青袍_candidate_01" not in tokens


def test_optimized_prompt_dropping_prop_name_is_detectable():
    """审核把全名改写成简称时必须能被检出(下游据此拒绝)。"""
    source = "核心道具「旧靛青举人青袍」，靛青棉布右衽长袍。"
    tokens = required(source, {"prop_name": "旧靛青举人青袍"})
    optimized = "单件举人青袍居中展示，青袍采用植物染靛青棉布。"
    assert [t for t in tokens if t not in optimized] == ["旧靛青举人青袍"]


def test_reviewer_is_told_which_tokens_are_verbatim():
    """必留词与规则必须真正出现在给 Codex 的指令里。"""
    instruction, _targets, _data = build_instruction(
        "prompt_review",
        {"review_prompt": "核心道具「旧靛青举人青袍」",
         "review_context": {"style": "写实"},
         "must_keep_verbatim": ["旧靛青举人青袍"]},
        "/tmp")
    assert "【必须逐字保留的词】" in instruction
    assert "旧靛青举人青袍" in instruction.split(
        "【必须逐字保留的词】")[1][:120]
    assert "不得改写" in instruction


def test_reviewer_may_follow_explicit_precedence_instead_of_blocking():
    """上下文给出显式优先级裁决时,审核不得再以"冲突"为由阻断。"""
    instruction, _targets, _data = build_instruction(
        "prompt_review",
        {"review_prompt": "母版", "review_context": {}}, "/tmp")
    assert "master_state_precedence" in instruction
    assert "不算需要猜测的冲突" in instruction
