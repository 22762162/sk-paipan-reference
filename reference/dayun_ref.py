"""大运盲写参考实现。

口诀出处：顺逆取《渊海子平·论起大运法》“阳男阴女顺行，阴男阳女逆行”；
起运以出生时刻到顺向下一节、逆向上一节的距离，依“三日折一岁、一日折四月”
换算。这里的节时必须由调用方传入，模块不内置历法时刻。
"""

from __future__ import annotations

from typing import Any

from ._common import BRANCHES, STEMS, STEM_INDEX, cycle_index, pillar, plain_int, stem

SECONDS_PER_DAY = 86_400
MONTHS_PER_DAY = 4
MONTHS_PER_YEAR = 12
DEFAULT_STEPS = 8


def _gender_is_male(gender: Any) -> bool:
    if gender in ("male", "man", "男"):
        return True
    if gender in ("female", "woman", "女"):
        return False
    raise ValueError("gender must be male/female or 男/女")


def dayun_direction(year_stem: Any, gender: Any) -> int:
    """阳男、阴女为 +1 顺行；阴男、阳女为 -1 逆行。"""

    year_stem = stem(year_stem, "year_stem")
    male = _gender_is_male(gender)
    yang = STEM_INDEX[year_stem] % 2 == 0
    return 1 if yang == male else -1


def _age_months(distance_seconds: int) -> tuple[int, int]:
    # 3日=12月，所以每一整月的折算长度是 21600 秒；取整月，精确到月。
    total_months, remainder = divmod(distance_seconds * MONTHS_PER_DAY, SECONDS_PER_DAY)
    return total_months, remainder


def start_age(
    birth_time: Any,
    previous_jie: Any,
    next_jie: Any,
    direction: int,
) -> dict[str, Any]:
    """返回起运方向、节距及精确到月的名义起运年龄。"""

    birth_time = plain_int(birth_time, "birth_time")
    previous_jie = plain_int(previous_jie, "previous_jie")
    next_jie = plain_int(next_jie, "next_jie")
    direction = plain_int(direction, "direction")
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    if previous_jie > birth_time or birth_time > next_jie:
        raise ValueError("previous_jie <= birth_time <= next_jie is required")
    boundary = next_jie if direction == 1 else previous_jie
    distance = boundary - birth_time if direction == 1 else birth_time - boundary
    total_months, remainder = _age_months(distance)
    return {
        "boundary": "next_jie" if direction == 1 else "previous_jie",
        "distance_seconds": distance,
        "total_months": total_months,
        "remainder_seconds_after_month": remainder,
        "age": {"years": total_months // MONTHS_PER_YEAR,
                "months": total_months % MONTHS_PER_YEAR},
    }


def _step_pillar(month: dict[str, str], step: int) -> dict[str, str]:
    index = (cycle_index(month["stem"], month["branch"]) + step) % 60
    result_stem = STEMS[index % 10]
    result_branch = BRANCHES[index % 12]
    return {"stem": result_stem, "branch": result_branch, "ganzhi": result_stem + result_branch}


def dayun_sequence(
    month_pillar: Any,
    year_stem: Any,
    gender: Any,
    steps: Any = DEFAULT_STEPS,
) -> list[dict[str, str]]:
    """从月柱的下一柱（顺）或上一柱（逆）排出大运柱。"""

    # 同时容纳 ``(月柱, 年干, 性别)`` 与常见的
    # ``(年干, 性别, 月柱)`` 调用顺序；返回结构不受调用顺序影响。
    if (type(month_pillar) is str and month_pillar in STEM_INDEX
            and type(year_stem) is str and year_stem in ("male", "man", "男", "female", "woman", "女")):
        month_pillar, year_stem, gender = gender, month_pillar, year_stem
    month = pillar(month_pillar, "month_pillar")
    direction = dayun_direction(year_stem, gender)
    steps = plain_int(steps, "steps")
    if not 1 <= steps <= 60:
        raise ValueError("steps must be in 1..60")
    return [_step_pillar(month, direction * (offset + 1)) for offset in range(steps)]


def ganzhi_sequence(
    month_pillar: Any,
    year_stem: Any,
    gender: Any,
    steps: Any = DEFAULT_STEPS,
) -> list[str]:
    """只返回八步大运干支名称的便捷函数。"""

    return [item["ganzhi"] for item in dayun_sequence(month_pillar, year_stem, gender, steps)]


def calculate_dayun(
    year_stem: Any,
    gender: Any,
    month_pillar: Any,
    birth_time: Any | None = None,
    previous_jie: Any | None = None,
    next_jie: Any | None = None,
    steps: Any = DEFAULT_STEPS,
) -> dict[str, Any]:
    """计算顺逆、起运月数和大运序列。

    三个时间参数要么全部省略（只求大运柱），要么全部给出；这样序列函数和
    含起运年龄的对拍请求都能使用同一套干支逻辑。
    """

    direction = dayun_direction(year_stem, gender)
    sequence = dayun_sequence(month_pillar, year_stem, gender, steps)
    result: dict[str, Any] = {
        "direction": "顺行" if direction == 1 else "逆行",
        "direction_step": direction,
        "pillars": sequence,
        "sequence": [item["ganzhi"] for item in sequence],
    }
    supplied = (birth_time is not None, previous_jie is not None, next_jie is not None)
    if any(supplied):
        if not all(supplied):
            raise ValueError("birth_time, previous_jie, next_jie must be supplied together")
        age = start_age(birth_time, previous_jie, next_jie, direction)
        result["start_age"] = age
        first_month = age["total_months"]
        result["periods"] = [
            {
                "order": index + 1,
                "pillar": sequence[index],
                "start_age": {
                    "years": (first_month + index * 120) // 12,
                    "months": (first_month + index * 120) % 12,
                },
            }
            for index in range(len(sequence))
        ]
    return result


# 便于对拍脚本采用任务名直接调用。
dayun = calculate_dayun

__all__ = [
    "DEFAULT_STEPS", "MONTHS_PER_DAY", "MONTHS_PER_YEAR", "SECONDS_PER_DAY",
    "calculate_dayun", "dayun", "dayun_direction", "ganzhi_sequence", "start_age",
    "dayun_sequence",
]
