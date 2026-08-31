"""十神与跨命盘地支六合、六冲、六害、刑关系表。"""

from typing import Any

from .extended_validation import CHART_FIELDS, validate_chart
from .ganzhi import BRANCHES, STEMS
from .validation import ContractInputError, require_string

ELEMENTS = ("wood", "fire", "earth", "metal", "water")
STEM_ELEMENTS = {
    "甲": "wood", "乙": "wood", "丙": "fire", "丁": "fire", "戊": "earth",
    "己": "earth", "庚": "metal", "辛": "metal", "壬": "water", "癸": "water",
}
GENERATES = {
    "wood": "fire", "fire": "earth", "earth": "metal",
    "metal": "water", "water": "wood",
}
CONTROLS = {
    "wood": "earth", "earth": "water", "water": "fire",
    "fire": "metal", "metal": "wood",
}

LIUHE = {
    frozenset(pair) for pair in (("子", "丑"), ("寅", "亥"), ("卯", "戌"),
                                 ("辰", "酉"), ("巳", "申"), ("午", "未"))
}
LIUCHONG = {
    frozenset(pair) for pair in (("子", "午"), ("丑", "未"), ("寅", "申"),
                                 ("卯", "酉"), ("辰", "戌"), ("巳", "亥"))
}
LIUHAI = {
    frozenset(pair) for pair in (("子", "未"), ("丑", "午"), ("寅", "巳"),
                                 ("卯", "辰"), ("申", "亥"), ("酉", "戌"))
}
THREE_PUNISHMENT_GROUPS = (
    (frozenset(("寅", "巳", "申")), "寅巳申三刑"),
    (frozenset(("丑", "未", "戌")), "丑未戌三刑"),
)
ZI_MAO_PUNISHMENT = frozenset(("子", "卯"))
SELF_PUNISHMENT = frozenset(("辰", "午", "酉", "亥"))


def _stem(stem: str, path: str) -> tuple[str, bool]:
    stem = require_string(stem, path)
    if stem not in STEMS:
        raise ContractInputError(f"{path} is not a heavenly stem")
    return STEM_ELEMENTS[stem], STEMS.index(stem) % 2 == 0


def ten_god(day_stem: str, other_stem: str) -> str:
    """按日主与另一显干的五行生克、阴阳同异返回十神。"""

    day_element, day_is_yang = _stem(day_stem, "day_stem")
    other_element, other_is_yang = _stem(other_stem, "other_stem")
    same_polarity = day_is_yang == other_is_yang

    if other_element == day_element:
        return "比肩" if same_polarity else "劫财"
    if GENERATES[day_element] == other_element:
        return "食神" if same_polarity else "伤官"
    if CONTROLS[day_element] == other_element:
        return "偏财" if same_polarity else "正财"
    if CONTROLS[other_element] == day_element:
        return "七杀" if same_polarity else "正官"
    if GENERATES[other_element] == day_element:
        return "偏印" if same_polarity else "正印"
    raise AssertionError("unreachable five-element relationship")


def branch_relations(left: str, right: str) -> list[dict[str, str]]:
    """返回一对地支同时成立的全部关系；巳申可同时为六合与刑。"""

    left = require_string(left, "left_branch")
    right = require_string(right, "right_branch")
    if left not in BRANCHES or right not in BRANCHES:
        raise ContractInputError("branch relation inputs must be earthly branches")

    pair = frozenset((left, right))
    result: list[dict[str, str]] = []
    if len(pair) == 2 and pair in LIUHE:
        result.append({"relation": "六合"})
    if len(pair) == 2 and pair in LIUCHONG:
        result.append({"relation": "六冲"})
    if len(pair) == 2 and pair in LIUHAI:
        result.append({"relation": "六害"})

    if left == right and left in SELF_PUNISHMENT:
        result.append({"relation": "刑", "subtype": "自刑"})
    elif pair == ZI_MAO_PUNISHMENT:
        result.append({"relation": "刑", "subtype": "子卯刑"})
    elif len(pair) == 2:
        for group, subtype in THREE_PUNISHMENT_GROUPS:
            if pair <= group:
                result.append({"relation": "刑", "subtype": subtype})
                break
    return result


def _ten_god_table(
    observer: dict[str, dict[str, str]],
    target: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    day_stem = observer["day"]["stem"]
    return [
        {
            "target_pillar": pillar,
            "target_stem": target[pillar]["stem"],
            "ten_god": ten_god(day_stem, target[pillar]["stem"]),
        }
        for pillar in CHART_FIELDS
    ]


def pair_analysis(chart_a: Any, chart_b: Any) -> dict[str, Any]:
    """生成双方日主视角十神表及全部 4×4 跨盘地支关系单元。"""

    a = validate_chart(chart_a, "chart_a")
    b = validate_chart(chart_b, "chart_b")
    matrix = []
    for a_pillar in CHART_FIELDS:
        for b_pillar in CHART_FIELDS:
            matrix.append(
                {
                    "a_pillar": a_pillar,
                    "a_branch": a[a_pillar]["branch"],
                    "b_pillar": b_pillar,
                    "b_branch": b[b_pillar]["branch"],
                    "relations": branch_relations(
                        a[a_pillar]["branch"], b[b_pillar]["branch"]
                    ),
                }
            )
    return {
        "ten_gods": {
            "a_observes_b": _ten_god_table(a, b),
            "b_observes_a": _ten_god_table(b, a),
        },
        "branch_matrix": matrix,
    }


__all__ = [
    "CONTROLS",
    "GENERATES",
    "LIUCHONG",
    "LIUHAI",
    "LIUHE",
    "SELF_PUNISHMENT",
    "STEM_ELEMENTS",
    "branch_relations",
    "pair_analysis",
    "ten_god",
]
