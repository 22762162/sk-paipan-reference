from reference.shensha_ref import (
    GUCHEN_GUASU, TIANYI, WENCHANG_XU, WENCHANG_ZI, shensha, void_branches,
)


def _chart(day=("甲", "子"), hour=("乙", "丑")):
    return {
        "year": {"stem": "甲", "branch": "子"},
        "month": {"stem": "丙", "branch": "寅"},
        "day": {"stem": day[0], "branch": day[1]},
        "hour": {"stem": hour[0], "branch": hour[1]},
    }


def test_classic_stem_tables_and_jiazi_void():
    assert TIANYI["甲"] == ("丑", "未")
    assert WENCHANG_ZI["辛"] == ("子",)
    assert WENCHANG_XU["辛"] == ("戌",)
    assert void_branches("甲子") == ("戌", "亥")
    assert set().union(*GUCHEN_GUASU) == set("子丑寅卯辰巳午未申酉戌亥")


def test_all_thirteen_stars_are_reported_and_variant_is_explicit():
    result = shensha(_chart(day=("辛", "酉"), hour=("戊", "子")), "zi")
    names = [item["name"] for item in result["stars"]]
    assert len(names) == 13
    assert result["rules"]["文昌"][0]["targets"] == ["子"]
    # 本实现把“旬空”作为任务所要求的名称；同时确认命中柱可对拍。
    # 年支、时支都为子，二者都落入辛酉日所属甲寅旬的旬空，均应报告。
    assert result["rules"]["旬空"][0]["hit_pillars"] == ["year", "hour"]
    xu = shensha(_chart(day=("辛", "酉"), hour=("戊", "子")), "xu")
    assert xu["rules"]["文昌"][0]["targets"] == ["戌"]
