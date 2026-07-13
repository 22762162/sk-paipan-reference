"""LT/MP/DP/HP 与 four_pillars 组合条款测试。"""

from copy import deepcopy

import pytest

from paipan_ref.four_pillars import (
    day_ganzhi,
    effective_day_jdn,
    four_pillars,
    gregorian_jdn,
    hour_branch,
    hour_ganzhi,
    is_gregorian_leap_year,
    month_ganzhi,
    validate_local,
    validate_month_context,
)
from paipan_ref.validation import ContractInputError


def make_local(
    y: int = 2000,
    m: int = 1,
    d: int = 1,
    hh: int = 0,
    mm: int = 0,
    ss: int = 0,
) -> dict[str, int]:
    return {"y": y, "m": m, "d": d, "hh": hh, "mm": mm, "ss": ss}


def test_lt1_accepts_valid_leap_day_and_unbounded_positive_year() -> None:
    assert validate_local(make_local(2000, 2, 29, 23, 59, 59))["d"] == 29
    assert validate_local(make_local(10_000, 12, 31))["y"] == 10_000


@pytest.mark.parametrize(
    "local",
    [
        make_local(0, 1, 1),
        make_local(2000, 0, 1),
        make_local(2000, 13, 1),
        make_local(2000, 2, 30),
        make_local(1900, 2, 29),
        make_local(2000, 1, 1, 24),
        make_local(2000, 1, 1, 0, 60),
        make_local(2000, 1, 1, 0, 0, 60),
    ],
)
def test_lt1_rejects_invalid_local_values(local: dict[str, int]) -> None:
    with pytest.raises(ContractInputError):
        validate_local(local)


def test_lt1_rejects_missing_extra_float_and_boolean_fields() -> None:
    missing = make_local()
    del missing["ss"]
    extra = {**make_local(), "timezone": "UTC"}
    floating = {**make_local(), "d": 1.0}
    boolean = {**make_local(), "hh": False}
    for local in (missing, extra, floating, boolean):
        with pytest.raises(ContractInputError):
            validate_local(local)


def test_mp1_context_lower_boundary_triplet() -> None:
    context = {"jie_seq": 0, "jie_unix": 100, "next_jie_unix": 200}
    with pytest.raises(ContractInputError):
        validate_month_context(99, context)
    assert validate_month_context(100, context) == context
    assert validate_month_context(101, context) == context


def test_mp1_context_upper_boundary_triplet() -> None:
    context = {"jie_seq": 0, "jie_unix": 100, "next_jie_unix": 200}
    assert validate_month_context(199, context) == context
    with pytest.raises(ContractInputError):
        validate_month_context(200, context)
    with pytest.raises(ContractInputError):
        validate_month_context(201, context)


@pytest.mark.parametrize("jie_seq", range(12))
def test_mp1_each_jie_transition_has_before_equal_after_cases(jie_seq: int) -> None:
    boundary = 10_000 + jie_seq * 1_000
    previous_seq = (jie_seq - 1) % 12
    previous_context = {
        "jie_seq": previous_seq,
        "jie_unix": boundary - 100,
        "next_jie_unix": boundary,
    }
    current_context = {
        "jie_seq": jie_seq,
        "jie_unix": boundary,
        "next_jie_unix": boundary + 100,
    }

    before = validate_month_context(boundary - 1, previous_context)
    equal = validate_month_context(boundary, current_context)
    after = validate_month_context(boundary + 1, current_context)
    assert month_ganzhi(0, before["jie_seq"])[1] == (2 + previous_seq) % 12
    assert month_ganzhi(0, equal["jie_seq"])[1] == (2 + jie_seq) % 12
    assert month_ganzhi(0, after["jie_seq"])[1] == (2 + jie_seq) % 12


@pytest.mark.parametrize("jie_seq", [-1, 12])
def test_mp1_rejects_jie_seq_outside_closed_range(jie_seq: int) -> None:
    context = {"jie_seq": jie_seq, "jie_unix": 100, "next_jie_unix": 200}
    with pytest.raises(ContractInputError):
        validate_month_context(150, context)


def test_mp2_wuhu_formula_for_all_year_stems_and_months() -> None:
    for year_stem in range(10):
        for jie_seq in range(12):
            assert month_ganzhi(year_stem, jie_seq) == (
                ((year_stem % 5) * 2 + 2 + jie_seq) % 10,
                (2 + jie_seq) % 12,
            )
    assert month_ganzhi(0, 0) == (2, 2)  # 甲年立春月：丙寅
    assert month_ganzhi(4, 0) == (0, 2)  # 戊年立春月：甲寅


def test_dp1_dp2_dp3_anchor_and_sixty_day_cycle() -> None:
    anchor = gregorian_jdn(2000, 1, 1)
    assert anchor == 2451545
    assert day_ganzhi(anchor) == (4, 6)  # 戊午，六十甲子序 54
    assert day_ganzhi(anchor + 60) == (4, 6)
    assert day_ganzhi(anchor - 60) == (4, 6)


def test_dp2_gregorian_leap_and_century_boundaries() -> None:
    # 2000 是闰年：2 月 28 日、29 日、3 月 1 日逐日递增。
    leap_triplet = [
        gregorian_jdn(2000, 2, 28),
        gregorian_jdn(2000, 2, 29),
        gregorian_jdn(2000, 3, 1),
    ]
    assert leap_triplet[1] == leap_triplet[0] + 1
    assert leap_triplet[2] == leap_triplet[1] + 1
    assert is_gregorian_leap_year(2000) is True

    # 1900 是平年，2 月 28 日后直接进入 3 月 1 日。
    assert gregorian_jdn(1900, 3, 1) == gregorian_jdn(1900, 2, 28) + 1
    assert is_gregorian_leap_year(1900) is False
    with pytest.raises(ContractInputError):
        gregorian_jdn(1900, 2, 29)


def test_dp4_split_midnight_boundary_triplet() -> None:
    before = make_local(2000, 1, 1, 23, 59, 59)
    equal = make_local(2000, 1, 2, 0, 0, 0)
    after = make_local(2000, 1, 2, 0, 0, 1)
    assert effective_day_jdn(before, "split") == 2451545
    assert effective_day_jdn(equal, "split") == 2451546
    assert effective_day_jdn(after, "split") == 2451546


def test_dp4_unified_23_boundary_triplet_and_split_contrast() -> None:
    before = make_local(2000, 1, 1, 22, 59, 59)
    equal = make_local(2000, 1, 1, 23, 0, 0)
    after = make_local(2000, 1, 1, 23, 0, 1)
    assert effective_day_jdn(before, "unified") == 2451545
    assert effective_day_jdn(equal, "unified") == 2451546
    assert effective_day_jdn(after, "unified") == 2451546

    assert effective_day_jdn(before, "split") == 2451545
    assert effective_day_jdn(equal, "split") == 2451545
    assert effective_day_jdn(after, "split") == 2451545
    assert effective_day_jdn(make_local(2000, 1, 1, 23, 59, 59), "split") == 2451545
    assert effective_day_jdn(make_local(2000, 1, 1, 23, 59, 59), "unified") == 2451546


@pytest.mark.parametrize("boundary_hour", range(1, 24, 2))
def test_hp1_all_twelve_hour_branch_boundary_triplets(boundary_hour: int) -> None:
    expected_after = ((boundary_hour + 1) // 2) % 12
    assert hour_branch(boundary_hour - 1, 59, 59) == (expected_after - 1) % 12
    assert hour_branch(boundary_hour, 0, 0) == expected_after
    assert hour_branch(boundary_hour, 0, 1) == expected_after


def test_hp2_split_late_zi_uses_next_day_stem_boundary_triplet() -> None:
    current_day_stem, _ = day_ganzhi(2451545)
    next_day_stem, _ = day_ganzhi(2451546)

    before_branch = hour_branch(22, 59, 59)
    equal_branch = hour_branch(23, 0, 0)
    after_branch = hour_branch(23, 0, 1)
    assert hour_ganzhi(current_day_stem, before_branch) == (9, 11)  # 癸亥
    assert hour_ganzhi(next_day_stem, equal_branch) == (0, 0)  # 甲子
    assert hour_ganzhi(next_day_stem, after_branch) == (0, 0)

    common = {
        "t_unix": 100,
        "lichun_unix": 50,
        "month_ctx": {"jie_seq": 0, "jie_unix": 50, "next_jie_unix": 150},
    }
    split = four_pillars(local=make_local(2000, 1, 1, 23), zi_hour_mode="split", **common)
    unified = four_pillars(local=make_local(2000, 1, 1, 23), zi_hour_mode="unified", **common)
    assert split["day"]["ganzhi"] == "戊午"
    assert split["hour"]["ganzhi"] == "甲子"
    assert unified["day"]["ganzhi"] == "己未"
    assert unified["hour"]["ganzhi"] == "甲子"


def test_four_pillars_combines_all_v02_formulas() -> None:
    result = four_pillars(
        t_unix=100,
        lichun_unix=50,
        local=make_local(2000, 6, 1, 8, 30, 0),
        month_ctx={"jie_seq": 3, "jie_unix": 90, "next_jie_unix": 200},
        zi_hour_mode="split",
    )
    assert result == {
        "bazi_year": 2000,
        "year": {"stem": "庚", "branch": "辰", "ganzhi": "庚辰"},
        "month": {"stem": "辛", "branch": "巳", "ganzhi": "辛巳"},
        "day": {"stem": "庚", "branch": "寅", "ganzhi": "庚寅"},
        "hour": {"stem": "庚", "branch": "辰", "ganzhi": "庚辰"},
    }


def test_four_pillars_year_boundary_triplet() -> None:
    common = {
        "local": make_local(2000, 2, 1, 12),
        "month_ctx": {"jie_seq": 0, "jie_unix": 900, "next_jie_unix": 1100},
        "zi_hour_mode": "split",
    }
    before = four_pillars(t_unix=999, lichun_unix=1000, **common)
    equal = four_pillars(t_unix=1000, lichun_unix=1000, **common)
    after = four_pillars(t_unix=1001, lichun_unix=1000, **common)
    assert before["bazi_year"] == 1999
    assert equal["bazi_year"] == 2000
    assert after["bazi_year"] == 2000


def test_four_pillars_rejects_bad_mode_and_bad_context_without_mutation() -> None:
    local = make_local()
    context = {"jie_seq": 0, "jie_unix": 0, "next_jie_unix": 10}
    original_local = deepcopy(local)
    original_context = deepcopy(context)
    with pytest.raises(ContractInputError):
        four_pillars(5, 0, local, context, "midnight")
    assert local == original_local
    assert context == original_context
