"""YP-1/YP-2/YP-3；时间戳均为合成边界值。"""

import pytest

from paipan_ref.validation import ContractInputError
from paipan_ref.year_pillar import resolve_bazi_year, year_ganzhi, year_pillar


def test_yp1_lichun_boundary_triplet() -> None:
    lichun = 1_000_000
    assert resolve_bazi_year(2000, lichun - 1, lichun) == 1999
    assert resolve_bazi_year(2000, lichun, lichun) == 2000
    assert resolve_bazi_year(2000, lichun + 1, lichun) == 2000


def test_yp1_boundary_triplet_with_negative_unix_seconds() -> None:
    lichun = -628_000_000
    assert resolve_bazi_year(1950, lichun - 1, lichun) == 1949
    assert resolve_bazi_year(1950, lichun, lichun) == 1950
    assert resolve_bazi_year(1950, lichun + 1, lichun) == 1950


def test_yp2_euclidean_modulo_for_years_below_four() -> None:
    assert year_ganzhi(4) == (0, 0)
    assert year_ganzhi(3) == (9, 11)
    assert year_pillar(3, 0, 0) == {
        "bazi_year": 3,
        "stem": "癸",
        "branch": "亥",
        "ganzhi": "癸亥",
    }


def test_yp2_sixty_year_cycle() -> None:
    for year in (-10_000, -1, 0, 3, 4, 1984, 2000, 10_000):
        assert year_ganzhi(year + 60) == year_ganzhi(year)


def test_yp3_anchor_1984_is_jiazi() -> None:
    assert year_ganzhi(1984) == (0, 0)
    assert year_pillar(1984, 1, 0) == {
        "bazi_year": 1984,
        "stem": "甲",
        "branch": "子",
        "ganzhi": "甲子",
    }


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", None])
def test_public_year_functions_reject_non_integer_types(bad: object) -> None:
    with pytest.raises(ContractInputError):
        resolve_bazi_year(bad, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(ContractInputError):
        year_ganzhi(bad)  # type: ignore[arg-type]
