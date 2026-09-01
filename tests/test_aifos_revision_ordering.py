"""逐项人工修订的时序裁决：后覆盖前，最新一轮永不被截断。

背景：旧实现是 ``f"{old}\\n{patch}"[:2400]``。累积几轮后两条人工修订
互斥，按治理条款 (c)「同级互斥且无显式裁决」只能熔断，单镜永久卡死；
而且截断保留的是最旧的几轮、丢掉最新一轮，与优先级正好相反。
"""

from aifos.rule_governance import (REVISION_FEEDBACK_BUDGET,
                                   prompt_adjudication_clause,
                                   stack_revision_feedback)


def test_first_round_carries_round_marker():
    out = stack_revision_feedback("", "把铃画小", 1)
    assert out.startswith("【第1轮修订")
    assert "把铃画小" in out


def test_later_rounds_stack_in_order():
    out = stack_revision_feedback("", "A", 1)
    out = stack_revision_feedback(out, "B", 2)
    out = stack_revision_feedback(out, "C", 3)
    assert out.index("第1轮") < out.index("第2轮") < out.index("第3轮")


def test_empty_patch_does_not_add_a_round():
    out = stack_revision_feedback("", "A", 1)
    assert stack_revision_feedback(out, "   ", 2) == out
    assert stack_revision_feedback(out, None, 2) == out


def test_overflow_drops_oldest_not_newest():
    """旧实现丢的是最新一轮——优先级最高的那条反而被截掉。"""
    out = ""
    for index in range(1, 40):
        out = stack_revision_feedback(out, f"第{index}条修订" * 20, index)
    assert len(out) <= REVISION_FEEDBACK_BUDGET
    assert "第39轮修订" in out          # 最新一轮必须在
    assert "第1轮修订" not in out       # 最旧的先被丢掉


def test_single_round_over_budget_keeps_its_marker():
    """单轮就超预算时截正文，但轮次标记要留住,否则无从判断谁在后。"""
    out = stack_revision_feedback("", "X" * (REVISION_FEEDBACK_BUDGET * 2), 7)
    assert out.startswith("【第7轮修订")
    assert len(out) <= REVISION_FEEDBACK_BUDGET


def test_round_number_is_normalised():
    assert stack_revision_feedback("", "A", 0).startswith("【第1轮修订")
    assert stack_revision_feedback("", "A", None).startswith("【第1轮修订")


def test_adjudication_clause_exempts_ordered_revisions_from_deadlock():
    policy = prompt_adjudication_clause()["policy"]
    assert "(b2)" in policy
    assert "按轮次取最后一条执行" in policy
    # 条款 (c) 的熔断口子必须显式排除掉可排序的修订
    assert "不属于 (b2) 的可排序修订" in policy


def test_next_round_is_globally_monotonic_across_paths():
    """轮次必须跨「人工重画」与「质检自动重画」全局单调。

    质检重试在单次调用内被 _qc_retries() 钳为 1 轮,如果它总是自称
    「第1轮」,(b2)「取轮次最大」会把跨次人工重画攒下的第3轮判成更新的,
    最新的质检意见反而失效。
    """
    from aifos.rule_governance import next_revision_round

    assert next_revision_round("") == 1
    assert next_revision_round(None) == 1
    marked = stack_revision_feedback("", "A", 1)
    assert next_revision_round(marked) == 2
    marked = stack_revision_feedback(marked, "B", 2)
    assert next_revision_round(marked) == 3
    # 多个来源取最大
    assert next_revision_round("【第5轮修订】x", "【第2轮修订】y") == 6
    # 非标记文本不影响
    assert next_revision_round("普通反馈,没有标记") == 1


def test_newest_human_supplement_survives_truncation():
    """真实病理:自动修订在前、人工补充在后,[:2400] 恰好砍掉人工补充。"""
    auto = "自动优化修订：" + "旧要求" * 500      # 远超预算
    human = "把铃铛画小,只有指节那么大"
    out = stack_revision_feedback(auto, human, 3)
    assert human in out, "最新的人工补充必须保住"
    assert len(out) <= REVISION_FEEDBACK_BUDGET
