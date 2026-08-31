"""大运顺逆、起运折算与运柱序列的独立测试。"""

import pytest

from paipan_ref.luck import dayun, dayun_direction, start_age
from paipan_ref.validation import ContractInputError


@pytest.mark.parametrize("stem", ("甲", "丙", "戊", "庚", "壬"))
def test_yang_male_and_yang_female_directions(stem: str) -> None:
    assert dayun_direction(stem, "male") == 1
    assert dayun_direction(stem, "female") == -1


@pytest.mark.parametrize("stem", ("乙", "丁", "己", "辛", "癸"))
def test_yin_male_and_yin_female_directions(stem: str) -> None:
    assert dayun_direction(stem, "male") == -1
    assert dayun_direction(stem, "female") == 1


def test_three_days_one_year_one_day_four_months_two_hours_ten_days() -> None:
    three_days = start_age(0, -1, 3 * 86_400, 1)
    assert three_days["nominal_age"] == {
        "years": 1, "months": 0, "days": 0,
        "hours": 0, "minutes": 0, "seconds": 0,
    }

    one_day = start_age(0, -1, 86_400, 1)
    assert one_day["nominal_age"]["years"] == 0
    assert one_day["nominal_age"]["months"] == 4

    two_hours = start_age(0, -1, 7_200, 1)
    assert two_hours["nominal_age"]["days"] == 10


def test_next_jie_closed_boundary_triplet() -> None:
    before = start_age(99, 0, 100, 1)
    equal = start_age(100, 0, 100, 1)
    assert before["distance_seconds"] == 1
    assert equal["distance_seconds"] == 0
    with pytest.raises(ContractInputError):
        start_age(101, 0, 100, 1)


def test_previous_jie_closed_boundary_triplet() -> None:
    with pytest.raises(ContractInputError):
        start_age(99, 100, 200, -1)
    equal = start_age(100, 100, 200, -1)
    after = start_age(101, 100, 200, -1)
    assert equal["distance_seconds"] == 0
    assert after["distance_seconds"] == 1


def test_forward_and_reverse_dayun_start_after_month_pillar() -> None:
    forward = dayun(
        "甲", "male", {"stem": "丙", "branch": "寅", "ganzhi": "丙寅"},
        0, -86_400, 3 * 86_400, 3,
    )
    assert forward["direction"] == "forward"
    assert [period["pillar"]["ganzhi"] for period in forward["periods"]] == [
        "丁卯", "戊辰", "己巳",
    ]
    assert [period["start_age"]["years"] for period in forward["periods"]] == [
        1, 11, 21,
    ]

    reverse = dayun(
        "乙", "male", {"stem": "戊", "branch": "寅"},
        0, -3 * 86_400, 86_400, 3,
    )
    assert reverse["direction"] == "reverse"
    assert [period["pillar"]["ganzhi"] for period in reverse["periods"]] == [
        "丁丑", "丙子", "乙亥",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("木", "male"),
        ("甲", "unknown"),
        ("甲", True),
    ],
)
def test_direction_rejects_invalid_types_and_values(args: tuple[object, object]) -> None:
    with pytest.raises(ContractInputError):
        dayun_direction(*args)  # type: ignore[arg-type]


def test_dayun_rejects_invalid_count_and_non_cycle_month_pillar() -> None:
    common = ("甲", "male", {"stem": "丙", "branch": "寅"}, 0, -1, 1)
    for bad_count in (0, 21, True, 1.0):
        with pytest.raises(ContractInputError):
            dayun(*common, bad_count)  # type: ignore[arg-type]
    with pytest.raises(ContractInputError):
        dayun("甲", "male", {"stem": "甲", "branch": "丑"}, 0, -1, 1, 8)
    with pytest.raises(ContractInputError):
        dayun(
            "甲", "male",
            {"stem": "丙", "branch": "寅", "ganzhi": "丙卯"},
            0, -1, 1, 8,
        )
