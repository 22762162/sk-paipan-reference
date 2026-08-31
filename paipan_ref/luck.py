"""大运顺逆、起运折算与十年运柱的独立盲写实现。

本模块是业主明确要求的非契约对拍扩展，不属于当前钉版 paipan-spec
v0.2 的正式 op。节气时刻必须由调用方注入；运行时不读取历法数据。
"""

from typing import Any

from .extended_validation import ganzhi_cycle_index, validate_pillar
from .ganzhi import BRANCHES, STEMS, pillar_from_indexes
from .validation import ContractInputError, require_plain_int, require_string

SECONDS_PER_DAY = 86_400
NOMINAL_DAYS_PER_MONTH = 30
NOMINAL_MONTHS_PER_YEAR = 12
NOMINAL_DAYS_PER_YEAR = NOMINAL_DAYS_PER_MONTH * NOMINAL_MONTHS_PER_YEAR
TRADITIONAL_AGE_SCALE = 120  # 三日折一岁 => 实际一秒折名义年龄 120 秒
SEXES = ("male", "female")


def dayun_direction(year_stem: str, sex: str) -> int:
    """阳男阴女顺行返回 1，阴男阳女逆行返回 -1。"""

    year_stem = require_string(year_stem, "year_stem")
    sex = require_string(sex, "sex")
    if year_stem not in STEMS:
        raise ContractInputError("year_stem is not a heavenly stem")
    if sex not in SEXES:
        raise ContractInputError("sex must be 'male' or 'female'")
    stem_is_yang = STEMS.index(year_stem) % 2 == 0
    return 1 if stem_is_yang == (sex == "male") else -1


def _nominal_age_parts(nominal_seconds: int) -> dict[str, int]:
    """按一年 360 日、一月 30 日拆分传统折算后的名义年龄。"""

    seconds_per_year = NOMINAL_DAYS_PER_YEAR * SECONDS_PER_DAY
    seconds_per_month = NOMINAL_DAYS_PER_MONTH * SECONDS_PER_DAY
    years, remainder = divmod(nominal_seconds, seconds_per_year)
    months, remainder = divmod(remainder, seconds_per_month)
    days, remainder = divmod(remainder, SECONDS_PER_DAY)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return {
        "years": years,
        "months": months,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
    }


def start_age(
    birth_unix: int,
    previous_jie_unix: int,
    next_jie_unix: int,
    direction: int,
) -> dict[str, Any]:
    """以出生到顺/逆向相邻节的时距按三日一岁精确折算。

    返回原始时距、不可约前的精确年分数，以及 360 日年制的名义年龄拆分，
    从而不因实现方提前采用四舍五入而丢失对拍信息。
    """

    birth_unix = require_plain_int(birth_unix, "birth_unix")
    previous_jie_unix = require_plain_int(previous_jie_unix, "previous_jie_unix")
    next_jie_unix = require_plain_int(next_jie_unix, "next_jie_unix")
    direction = require_plain_int(direction, "direction")
    if direction not in (-1, 1):
        raise ContractInputError("direction must be -1 or 1")
    if not previous_jie_unix <= birth_unix <= next_jie_unix:
        raise ContractInputError(
            "jie bounds must satisfy previous_jie_unix <= birth_unix <= next_jie_unix"
        )

    boundary = next_jie_unix if direction == 1 else previous_jie_unix
    distance_seconds = abs(boundary - birth_unix)
    nominal_seconds = distance_seconds * TRADITIONAL_AGE_SCALE
    return {
        "boundary": "next_jie" if direction == 1 else "previous_jie",
        "distance_seconds": distance_seconds,
        "years_fraction": {
            "numerator": distance_seconds,
            "denominator": 3 * SECONDS_PER_DAY,
        },
        "nominal_age": _nominal_age_parts(nominal_seconds),
        "nominal_total_days_floor": nominal_seconds // SECONDS_PER_DAY,
        "nominal_total_months_floor": nominal_seconds
        // (NOMINAL_DAYS_PER_MONTH * SECONDS_PER_DAY),
    }


def _pillar_for_cycle_index(cycle_index: int) -> dict[str, str]:
    return pillar_from_indexes(cycle_index % 10, cycle_index % 12)


def dayun(
    year_stem: str,
    sex: str,
    month_pillar: Any,
    birth_unix: int,
    previous_jie_unix: int,
    next_jie_unix: int,
    count: int = 8,
) -> dict[str, Any]:
    """计算起运年龄及从月柱后第一柱开始的连续十年大运。"""

    direction = dayun_direction(year_stem, sex)
    month = validate_pillar(month_pillar, "month_pillar")
    month_index = ganzhi_cycle_index(
        month["stem"], month["branch"], "month_pillar"
    )
    count = require_plain_int(count, "count")
    if not 1 <= count <= 20:
        raise ContractInputError("count must be in 1..20")

    age = start_age(
        birth_unix,
        previous_jie_unix,
        next_jie_unix,
        direction,
    )
    first_nominal_seconds = age["distance_seconds"] * TRADITIONAL_AGE_SCALE
    periods = []
    for offset in range(count):
        cycle_index = (month_index + direction * (offset + 1)) % 60
        period_age = _nominal_age_parts(
            first_nominal_seconds
            + offset * 10 * NOMINAL_DAYS_PER_YEAR * SECONDS_PER_DAY
        )
        periods.append(
            {
                "order": offset + 1,
                "pillar": _pillar_for_cycle_index(cycle_index),
                "start_age": period_age,
            }
        )

    return {
        "direction": "forward" if direction == 1 else "reverse",
        "direction_step": direction,
        "start_age": age,
        "periods": periods,
    }


__all__ = [
    "NOMINAL_DAYS_PER_MONTH",
    "NOMINAL_DAYS_PER_YEAR",
    "NOMINAL_MONTHS_PER_YEAR",
    "SECONDS_PER_DAY",
    "SEXES",
    "TRADITIONAL_AGE_SCALE",
    "dayun",
    "dayun_direction",
    "start_age",
]
