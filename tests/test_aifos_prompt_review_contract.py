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
    assert "事实源冲突本身不再是阻断理由" in instruction


# ---- 全局冲突裁决条款:宪法必须送到法官手里 ----
def test_adjudication_clause_carries_all_six_levels():
    from aifos.rule_governance import (
        RULE_PRECEDENCE, prompt_adjudication_clause)
    clause = prompt_adjudication_clause()
    for rule in RULE_PRECEDENCE:
        assert rule["label"] in clause["policy"]
    assert "不得再以「无优先级条款」为由阻断" in clause["policy"]
    # 字段层级对照必须覆盖审核上下文的关键字段
    fp = clause["field_precedence"]
    assert "identity_references" in fp["user_locked_fact"]
    assert "story_world" in fp["episode_fact_bible"]
    assert "action" in fp["shot_local_contract"]


def test_every_review_payload_carries_adjudication():
    """所有图片审核请求统一携带裁决条款与必留词。"""
    from aifos.production.router import ProviderRouter
    router = ProviderRouter.__new__(ProviderRouter)
    payload = {"prop_name": "旧靛青举人青袍", "characters": ["林川"]}
    review = router._build_review_payload(
        "核心道具「旧靛青举人青袍」,林川的青袍", {"style": "写实"}, payload)
    assert review["adjudication"]["schema"] == "aifos.prompt-adjudication/v1"
    assert "旧靛青举人青袍" in review["must_keep_verbatim"]
    assert "林川" in review["must_keep_verbatim"]


def test_reviewer_instruction_renders_adjudication_block():
    from aifos.rule_governance import prompt_adjudication_clause
    instruction, _t, _d = build_instruction(
        "prompt_review",
        {"review_prompt": "p", "review_context": {},
         "adjudication": prompt_adjudication_clause()}, "/tmp")
    assert "【冲突裁决规则】" in instruction
    assert "同级互斥" in instruction          # 只有同级才允许阻断
    assert "用户已锁定" in instruction        # 最高级在场


# ---- 人数校验:语义等价表述不算丢失,只有人名不算人数 ----
import pytest as _pytest


@_pytest.mark.parametrize("optimized, count, ok", [
    ("同一角色完整全身正面、严格90度侧面、完整180度背面", 1, True),
    ("单人全身正面自然站姿", 1, True),
    ("空镜:环境建立镜头,预留表演动线", 0, True),
    ("画面中不出现人物", 0, True),
    ("两名人物对峙于长街", 2, True),
    ("三位人物围坐", 3, True),
    ("严格共3人", 3, True),
    ("林川与阿砚在房内", 2, False),   # 只有人名 → 人数事实仍算丢失
])
def test_count_preservation_accepts_semantic_equivalents(
        optimized, count, ok):
    from aifos.production.router import ProviderRouter
    assert ProviderRouter._prompt_review_count_preserved(
        optimized, {"character_count": count}) is ok


def test_review_payload_carries_count_policy():
    from aifos.production.router import ProviderRouter
    router = ProviderRouter.__new__(ProviderRouter)
    review = router._build_review_payload("p", {}, {"character_count": 1})
    assert "人数" in review["count_policy"]
    assert "空镜" in review["count_policy"]


def test_contract_checked_markers_are_required_verbatim():
    """下游合同点名的结构标记(【质检同源合同】)必须整块保留。"""
    source = "【CANONICAL FRONT】全身正面…【质检同源合同】皮肤质感自然"
    tokens = required(source, {"characters": ["林川"]})
    assert "【质检同源合同】" in tokens
    assert "【质检同源合同】" not in required("普通场景图", {})


def test_back_profile_sheet_scopes_match_their_binding(tmp_path):
    """背面/侧面设定图 binding 说"补充服装/配饰/道具位置",作用域就
    不得再排除它们——同一条目自我互斥曾熔断关键帧(图6事故)。"""
    from aifos.director import Director
    director = Director.__new__(Director)
    img = tmp_path / "sheet_back.png"
    img.write_bytes(b"\x89PNG\r\n")
    payload = {
        "character_refs": [str(img)],
        "asset_matches": [{"uri": str(img), "kind": "character_sheet",
                           "name": "林川:back",
                           "label": "林川人物设定图·背面"}],
    }
    entries = director._reference_manifest(payload)
    back = next(e for e in entries if "背面" in e["label"])
    assert "wardrobe" in back["inherits"]
    assert "prop_position" in back["inherits"]
    assert "wardrobe" not in back["excludes"]
    assert "face_identity_override" in back["excludes"]


@_pytest.mark.parametrize("optimized, count, ok", [
    ("3名登记角色林川、赵百户、阿砚，加4名巡检弓兵", 7, True),
    ("三名登记角色与四名弓兵", 7, True),
    ("3名角色与3名弓兵", 7, False),          # 和不等于期望仍拒
    ("林川、赵百户、阿砚与巡检弓兵们", 7, False),   # 无数字仍拒
])
def test_count_preservation_accepts_component_sums(optimized, count, ok):
    """分组人数相加等于期望值也是明确的人数字面(7人群像真实事故)。"""
    from aifos.production.router import ProviderRouter
    assert ProviderRouter._prompt_review_count_preserved(
        optimized, {"character_count": count}) is ok
