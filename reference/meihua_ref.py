"""梅花易数报数起卦的盲写参考实现。

口诀出处：《梅花易数》卷一、卷二的先天数与体用章：一乾、二兑、三离、
四震、五巽、六坎、七艮、八坤；两数分别取上下卦，合数除六取动爻；
“凡上下卦无动爻者为体，有动爻者为用”，互卦取中四爻，变卦取动爻之变。
时辰序按子一、丑二至亥十二计。
"""

from __future__ import annotations

from typing import Any

from ._common import (
    BRANCHES, ELEMENT_CONTROLS, ELEMENT_GENERATES, TRIGRAM_BY_NUMBER, TRIGRAM_ELEMENTS,
    TRIGRAM_LINES, branch, hexagram_info, mutual_lines, plain_int,
)

TIME_BRANCH_NUMBER = {value: index + 1 for index, value in enumerate(BRANCHES)}


def _mod_eight(value: int) -> int:
    remainder = value % 8
    return 8 if remainder == 0 else remainder


def _mod_six(value: int) -> int:
    remainder = value % 6
    return 6 if remainder == 0 else remainder


def _trigram_from_number(value: int) -> str:
    return TRIGRAM_BY_NUMBER[_mod_eight(value)]


def _relation(body: str, use: str) -> str:
    body_element = TRIGRAM_ELEMENTS[body]
    use_element = TRIGRAM_ELEMENTS[use]
    if body_element == use_element:
        return "比和"
    if ELEMENT_GENERATES[body_element] == use_element:
        return "体生用"
    if ELEMENT_CONTROLS[body_element] == use_element:
        return "体克用"
    if ELEMENT_CONTROLS[use_element] == body_element:
        return "用克体"
    if ELEMENT_GENERATES[use_element] == body_element:
        return "用生体"
    raise AssertionError("five element relation is exhaustive")


def _describe(lines: tuple[int, ...]) -> dict[str, Any]:
    info = hexagram_info(lines)
    return {
        "name": info["name"], "upper": info["upper"], "lower": info["lower"],
    }


def meihua(n1: Any, n2: Any, time_branch: Any) -> dict[str, Any]:
    """两正整数加时辰地支起梅花卦，返回本、互、变及体用生克。"""

    n1 = plain_int(n1, "n1")
    n2 = plain_int(n2, "n2")
    if n1 <= 0 or n2 <= 0:
        raise ValueError("n1 and n2 must be positive integers")
    if type(time_branch) is int:
        if not 1 <= time_branch <= 12:
            raise ValueError("time branch number must be in 1..12")
        time_number = time_branch
        time_name = BRANCHES[time_number - 1]
    else:
        time_name = branch(time_branch, "time_branch")
        time_number = TIME_BRANCH_NUMBER[time_name]

    upper = _trigram_from_number(n1)
    lower = _trigram_from_number(n2)
    moving_position = _mod_six(n1 + n2 + time_number)  # 1=初爻 ... 6=上爻
    lines = TRIGRAM_LINES[lower] + TRIGRAM_LINES[upper]
    changed_list = list(lines)
    changed_list[moving_position - 1] = 1 - changed_list[moving_position - 1]
    changed = tuple(changed_list)
    mutual = mutual_lines(lines)

    if moving_position <= 3:
        body, use = upper, lower
    else:
        body, use = lower, upper
    body_element = TRIGRAM_ELEMENTS[body]
    use_element = TRIGRAM_ELEMENTS[use]
    relation = _relation(body, use)
    return {
        "n1": n1,
        "n2": n2,
        "time_branch": time_name,
        "upper_number": _mod_eight(n1),
        "lower_number": _mod_eight(n2),
        "moving_line": moving_position,
        "本卦": _describe(lines),
        "互卦": _describe(mutual),
        "变卦": _describe(changed),
        "original": _describe(lines),
        "mutual": _describe(mutual),
        "changed": _describe(changed),
        "body": {"trigram": body, "element": body_element},
        "use": {"trigram": use, "element": use_element},
        "body_use_relation": relation,
        "体用": {"体": body, "用": use, "关系": relation},
    }


calculate_meihua = meihua
plum_blossom = meihua

__all__ = ["TIME_BRANCH_NUMBER", "calculate_meihua", "meihua", "plum_blossom"]
