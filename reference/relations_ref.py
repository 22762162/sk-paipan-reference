"""地支关系与十神的盲写参考实现。

口诀出处：六合、六冲、六害、三刑、三合沿用《三命通会》及传统地支歌诀；
十神依《三命通会》卷五关于“我生、我克、生我、克我、同我”及阴阳同异
定正偏的法则。这里只给结构化关系，不附加吉凶断语。
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from ._common import (
    BRANCHES, ELEMENT_CONTROLS, ELEMENT_GENERATES, STEM_ELEMENTS, STEM_INDEX,
    branch, stem,
)

LIUHE = (frozenset(pair) for pair in
         (("子", "丑"), ("寅", "亥"), ("卯", "戌"), ("辰", "酉"),
          ("巳", "申"), ("午", "未")))
LIUHE = tuple(LIUHE)
LIUCHONG = tuple(frozenset(pair) for pair in
                 (("子", "午"), ("丑", "未"), ("寅", "申"), ("卯", "酉"),
                  ("辰", "戌"), ("巳", "亥")))
LIUHAI = tuple(frozenset(pair) for pair in
               (("子", "未"), ("丑", "午"), ("寅", "巳"), ("卯", "辰"),
                ("申", "亥"), ("酉", "戌")))

SANHE = {
    "申子辰": {"members": frozenset(("申", "子", "辰")), "element": "水"},
    "亥卯未": {"members": frozenset(("亥", "卯", "未")), "element": "木"},
    "寅午戌": {"members": frozenset(("寅", "午", "戌")), "element": "火"},
    "巳酉丑": {"members": frozenset(("巳", "酉", "丑")), "element": "金"},
}
SANHE_WANG = {"申子辰": "子", "亥卯未": "卯", "寅午戌": "午", "巳酉丑": "酉"}
SANXING = (
    (frozenset(("寅", "巳", "申")), "寅巳申三刑"),
    (frozenset(("丑", "未", "戌")), "丑未戌三刑"),
)
ZI_MAO_XING = frozenset(("子", "卯"))
SELF_XING = frozenset(("辰", "午", "酉", "亥"))


def ten_god(day_stem: Any, other_stem: Any) -> str:
    """以日干为“我”给任意显干定十神。"""

    day_stem = stem(day_stem, "day_stem")
    other_stem = stem(other_stem, "other_stem")
    mine = STEM_ELEMENTS[day_stem]
    theirs = STEM_ELEMENTS[other_stem]
    same_yin_yang = STEM_INDEX[day_stem] % 2 == STEM_INDEX[other_stem] % 2
    if theirs == mine:
        return "比肩" if same_yin_yang else "劫财"
    if ELEMENT_GENERATES[mine] == theirs:
        return "食神" if same_yin_yang else "伤官"
    if ELEMENT_CONTROLS[mine] == theirs:
        return "偏财" if same_yin_yang else "正财"
    if ELEMENT_CONTROLS[theirs] == mine:
        return "七杀" if same_yin_yang else "正官"
    if ELEMENT_GENERATES[theirs] == mine:
        return "偏印" if same_yin_yang else "正印"
    raise AssertionError("all five element relations are exhaustive")


def _detail(relation: str, **extra: str) -> dict[str, str]:
    result = {"relation": relation}
    result.update(extra)
    return result


def branch_relations(left: Any, right: Any) -> list[dict[str, str]]:
    """返回一对地支的全部二元关系；巳申等可同时命中多项。

    含四正中神的两支按传统“半合”报告（如申子、子辰）；三支齐备的完整三合请用
    :func:`find_branch_relations`，它会另列 ``三合`` 项。
    """

    left = branch(left, "left")
    right = branch(right, "right")
    pair = frozenset((left, right))
    result: list[dict[str, str]] = []
    if len(pair) == 2 and pair in LIUHE:
        result.append(_detail("六合"))
    if len(pair) == 2 and pair in LIUCHONG:
        result.append(_detail("六冲"))
    if len(pair) == 2 and pair in LIUHAI:
        result.append(_detail("六害"))

    if left == right and left in SELF_XING:
        result.append(_detail("三刑", subtype="自刑"))
    elif pair == ZI_MAO_XING:
        result.append(_detail("三刑", subtype="子卯刑"))
    elif len(pair) == 2:
        for group_name, data in SANXING:
            if pair <= group_name:
                result.append(_detail("三刑", subtype=data))
                break

    if len(pair) == 2:
        for group_name, data in SANHE.items():
            if pair <= data["members"] and SANHE_WANG[group_name] in pair:
                result.append(_detail("半合", group=group_name, element=data["element"]))
                break
    return result


def relation_names(left: Any, right: Any) -> list[str]:
    """只取二元关系名称的便捷函数。"""

    return [item["relation"] for item in branch_relations(left, right)]


def find_branch_relations(branches: Any) -> dict[str, list[dict[str, Any]]]:
    """扫描一组地支，识别二元关系、完整三合和完整三刑成员。

    输入通常是四柱地支的 4 元序列；结果保留输入位置，重复地支不会被误并。
    """

    if type(branches) not in (tuple, list):
        raise ValueError("branches must be a list or tuple")
    values = [branch(item, "branches[]") for item in branches]
    pairs: list[dict[str, Any]] = []
    for left_index, right_index in combinations(range(len(values)), 2):
        details = branch_relations(values[left_index], values[right_index])
        for detail in details:
            pairs.append({
                "left_index": left_index, "right_index": right_index,
                "left": values[left_index], "right": values[right_index],
                **detail,
            })

    groups: list[dict[str, Any]] = []
    for group_name, data in SANHE.items():
        positions = [index for index, value in enumerate(values) if value in data["members"]]
        if all(member in values for member in data["members"]):
            groups.append({"relation": "三合", "group": group_name,
                           "element": data["element"], "indices": positions})

    punishments: list[dict[str, Any]] = []
    for group, name in SANXING:
        positions = [index for index, value in enumerate(values) if value in group]
        if all(member in values for member in group):
            punishments.append({"relation": "三刑", "subtype": name, "indices": positions})
    return {"pair_relations": pairs, "sanhe": groups, "sanxing": punishments}


def ten_god_table(day_stem: Any, other_stems: Any) -> list[dict[str, str]]:
    """按给定顺序计算多个显干的十神。"""

    if type(other_stems) not in (tuple, list):
        raise ValueError("other_stems must be a list or tuple")
    return [{"stem": stem_value, "ten_god": ten_god(day_stem, stem_value)}
            for stem_value in other_stems]


__all__ = [
    "LIUHAI", "LIUHE", "LIUCHONG", "SANHE", "SANHE_WANG", "SANXING", "SELF_XING",
    "ZI_MAO_XING", "branch_relations", "find_branch_relations", "relation_names",
    "ten_god", "ten_god_table",
]
