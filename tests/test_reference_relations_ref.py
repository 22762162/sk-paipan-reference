from reference.relations_ref import (
    branch_relations, find_branch_relations, relation_names, ten_god,
)


def test_all_six_pairs_and_half_combinations():
    assert "六合" in relation_names("子", "丑")
    assert "六冲" in relation_names("子", "午")
    assert "六害" in relation_names("子", "未")
    assert any(item["relation"] == "半合" for item in branch_relations("子", "辰"))
    assert not any(item["relation"] == "半合" for item in branch_relations("申", "辰"))
    assert branch_relations("辰", "辰") == [{"relation": "三刑", "subtype": "自刑"}]
    assert any(item.get("subtype") == "寅巳申三刑" for item in branch_relations("巳", "申"))


def test_full_trine_and_full_punishment_are_distinct_from_pairs():
    result = find_branch_relations(["申", "子", "辰", "午"])
    assert result["sanhe"] == [{
        "relation": "三合", "group": "申子辰", "element": "水", "indices": [0, 1, 2]
    }]
    assert find_branch_relations(["寅", "巳", "申"])["sanxing"][0]["subtype"] == "寅巳申三刑"


def test_ten_gods_cover_the_standard_jia_day_master_order():
    assert [ten_god("甲", stem) for stem in "甲乙丙丁戊己庚辛壬癸"] == [
        "比肩", "劫财", "食神", "伤官", "偏财", "正财", "七杀", "正官", "偏印", "正印"
    ]
