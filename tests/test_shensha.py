"""神煞映射、异文配置及命中位置测试。"""

from paipan_ref.shensha import (
    GUCHEN_GUASU,
    LUSHEN,
    TIANYI,
    TRINE_TABLES,
    WENCHANG_XU,
    WENCHANG_ZI,
    YANGREN,
    shensha,
)


def make_chart(
    year: tuple[str, str] = ("甲", "子"),
    month: tuple[str, str] = ("丙", "寅"),
    day: tuple[str, str] = ("甲", "子"),
    hour: tuple[str, str] = ("乙", "丑"),
) -> dict[str, dict[str, str]]:
    return {
        "year": {"stem": year[0], "branch": year[1]},
        "month": {"stem": month[0], "branch": month[1]},
        "day": {"stem": day[0], "branch": day[1]},
        "hour": {"stem": hour[0], "branch": hour[1]},
    }


def rule(result: dict[str, object], name: str) -> list[dict[str, object]]:
    stars = result["stars"]
    assert isinstance(stars, list)
    return next(item["rules"] for item in stars if item["name"] == name)


def test_trine_tables_cover_every_branch_once_and_expected_targets() -> None:
    members = [branch for table in TRINE_TABLES.values() for branch in table["members"]]
    assert sorted(members) == sorted("子丑寅卯辰巳午未申酉戌亥")
    assert TRINE_TABLES["申子辰"] == {
        "members": frozenset("申子辰"),
        "桃花": "酉", "驿马": "寅", "华盖": "辰", "将星": "子",
    }
    assert TRINE_TABLES["寅午戌"]["桃花"] == "卯"
    assert TRINE_TABLES["亥卯未"]["驿马"] == "巳"
    assert TRINE_TABLES["巳酉丑"]["华盖"] == "丑"


def test_stem_based_tables_are_complete_and_known_anchors_match() -> None:
    stems = set("甲乙丙丁戊己庚辛壬癸")
    assert set(TIANYI) == set(WENCHANG_ZI) == set(YANGREN) == set(LUSHEN) == stems
    assert TIANYI["甲"] == ("丑", "未")
    assert TIANYI["辛"] == ("午", "寅")
    assert YANGREN["甲"] == ("卯",)
    assert LUSHEN["癸"] == ("子",)


def test_wenchang_xin_variant_is_explicit_and_changes_hits() -> None:
    assert WENCHANG_ZI["辛"] == ("子",)
    assert WENCHANG_XU["辛"] == ("戌",)
    chart = make_chart(day=("辛", "酉"), hour=("戊", "子"))
    zi_result = shensha(chart, "zi")
    xu_result = shensha(chart, "xu")
    assert rule(zi_result, "文昌")[0]["hit_pillars"] == ["year", "hour"]
    assert rule(xu_result, "文昌")[0]["hit_pillars"] == []


def test_year_and_day_branch_bases_are_kept_separate() -> None:
    result = shensha(make_chart(day=("甲", "午")))
    peach = rule(result, "桃花")
    assert peach[0]["basis"] == {"type": "year_branch", "value": "子"}
    assert peach[0]["targets"] == ["酉"]
    assert peach[1]["basis"] == {"type": "day_branch", "value": "午"}
    assert peach[1]["targets"] == ["卯"]


def test_red_luan_tian_xi_guchen_guasu_and_jiazi_void() -> None:
    result = shensha(make_chart(hour=("乙", "亥")))
    assert rule(result, "红鸾")[0]["targets"] == ["卯"]
    assert rule(result, "天喜")[0]["targets"] == ["酉"]
    assert rule(result, "孤辰")[0]["targets"] == ["寅"]
    assert rule(result, "寡宿")[0]["targets"] == ["戌"]
    void = rule(result, "空亡")[0]
    assert void["basis"] == {"type": "day_pillar", "value": "甲子"}
    assert void["targets"] == ["戌", "亥"]
    assert void["hit_pillars"] == ["hour"]


def test_guchen_guasu_groups_cover_all_year_branches() -> None:
    all_members = set().union(*GUCHEN_GUASU)
    assert all_members == set("子丑寅卯辰巳午未申酉戌亥")
