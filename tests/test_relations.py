"""十神与地支关系表测试。"""

import pytest

from paipan_ref.relations import branch_relations, pair_analysis, ten_god
from paipan_ref.validation import ContractInputError


def chart(pillars: tuple[tuple[str, str], ...]) -> dict[str, dict[str, str]]:
    return {
        name: {"stem": stem, "branch": branch}
        for name, (stem, branch) in zip(("year", "month", "day", "hour"), pillars)
    }


def relation_names(left: str, right: str) -> list[str]:
    return [item["relation"] for item in branch_relations(left, right)]


def test_ten_gods_from_jia_day_master_cover_all_ten_stems() -> None:
    assert [ten_god("甲", stem) for stem in "甲乙丙丁戊己庚辛壬癸"] == [
        "比肩", "劫财", "食神", "伤官", "偏财",
        "正财", "七杀", "正官", "偏印", "正印",
    ]


def test_ten_gods_from_yi_day_master_flip_polarity_roles() -> None:
    assert [ten_god("乙", stem) for stem in "甲乙丙丁戊己庚辛壬癸"] == [
        "劫财", "比肩", "伤官", "食神", "正财",
        "偏财", "正官", "七杀", "正印", "偏印",
    ]


@pytest.mark.parametrize(
    "left,right",
    (("子", "丑"), ("寅", "亥"), ("卯", "戌"), ("辰", "酉"), ("巳", "申"), ("午", "未")),
)
def test_all_liuhe_pairs_are_symmetric(left: str, right: str) -> None:
    assert "六合" in relation_names(left, right)
    assert "六合" in relation_names(right, left)


@pytest.mark.parametrize(
    "left,right",
    (("子", "午"), ("丑", "未"), ("寅", "申"), ("卯", "酉"), ("辰", "戌"), ("巳", "亥")),
)
def test_all_liuchong_pairs(left: str, right: str) -> None:
    assert "六冲" in relation_names(left, right)


@pytest.mark.parametrize(
    "left,right",
    (("子", "未"), ("丑", "午"), ("寅", "巳"), ("卯", "辰"), ("申", "亥"), ("酉", "戌")),
)
def test_all_liuhai_pairs(left: str, right: str) -> None:
    names = relation_names(left, right)
    assert "六害" in names


def test_punishments_include_group_pairwise_self_and_simultaneous_relations() -> None:
    assert branch_relations("子", "卯") == [{"relation": "刑", "subtype": "子卯刑"}]
    assert branch_relations("丑", "未") == [
        {"relation": "六冲"},
        {"relation": "刑", "subtype": "丑未戌三刑"},
    ]
    assert branch_relations("辰", "辰") == [{"relation": "刑", "subtype": "自刑"}]
    assert branch_relations("巳", "申") == [
        {"relation": "六合"},
        {"relation": "刑", "subtype": "寅巳申三刑"},
    ]
    assert branch_relations("子", "子") == []


def test_pair_analysis_builds_two_ten_god_views_and_full_4x4_matrix() -> None:
    a = chart((("甲", "子"), ("丙", "寅"), ("甲", "午"), ("乙", "卯")))
    b = chart((("庚", "午"), ("壬", "申"), ("庚", "子"), ("辛", "酉")))
    result = pair_analysis(a, b)
    assert [row["ten_god"] for row in result["ten_gods"]["a_observes_b"]] == [
        "七杀", "偏印", "七杀", "正官",
    ]
    assert len(result["branch_matrix"]) == 16
    year_year = result["branch_matrix"][0]
    assert year_year["a_branch"] == "子"
    assert year_year["b_branch"] == "午"
    assert year_year["relations"] == [{"relation": "六冲"}]


@pytest.mark.parametrize("bad", (None, True, 1, "木", [], {}))
def test_ten_god_and_branch_relations_reject_invalid_values(bad: object) -> None:
    with pytest.raises(ContractInputError):
        ten_god("甲", bad)  # type: ignore[arg-type]
    with pytest.raises(ContractInputError):
        branch_relations("子", bad)  # type: ignore[arg-type]
