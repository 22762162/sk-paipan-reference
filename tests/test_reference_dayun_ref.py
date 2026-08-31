import pytest

from reference.dayun_ref import calculate_dayun, dayun_direction, start_age


def test_direction_rule_and_eight_pillar_sequence():
    assert dayun_direction("甲", "男") == 1
    assert dayun_direction("乙", "男") == -1
    assert dayun_direction("乙", "女") == 1
    assert calculate_dayun("甲", "male", "丙寅", 0, -86400, 259200)["sequence"] == [
        "丁卯", "戊辰", "己巳", "庚午", "辛未", "壬申", "癸酉", "甲戌"
    ]


def test_three_days_and_one_day_fold_to_exact_months():
    assert start_age(0, -1, 3 * 86400, 1)["age"] == {"years": 1, "months": 0}
    assert start_age(0, -1, 86400, 1)["age"] == {"years": 0, "months": 4}


def test_forward_boundary_triplet():
    assert start_age(99, 0, 100, 1)["distance_seconds"] == 1
    assert start_age(100, 0, 100, 1)["distance_seconds"] == 0
    with pytest.raises(ValueError):
        start_age(101, 0, 100, 1)


def test_reverse_boundary_triplet():
    with pytest.raises(ValueError):
        start_age(99, 100, 200, -1)
    assert start_age(100, 100, 200, -1)["distance_seconds"] == 0
    assert start_age(101, 100, 200, -1)["distance_seconds"] == 1
