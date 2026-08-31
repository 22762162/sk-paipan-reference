"""六爻装卦的盲写参考实现。

口诀出处：纳甲依《京氏易传》传统《纳甲歌》“乾金甲子外壬午，坎水戊寅外
戊申；震木庚子外庚午，艮土丙辰外丙戌；巽木辛丑外辛未，离火己卯外己酉；
坤土乙未外癸丑，兑金丁巳外丁亥”；世应依八宫“本宫上爻、初至五世、
游魂四爻、归魂三爻”，六神依《卜筮正宗》“甲乙青龙、丙丁朱雀、戊勾陈、
己螣蛇、庚辛白虎、壬癸玄武，自初爻向上排”。
"""

from __future__ import annotations

from typing import Any

from ._common import (
    BRANCH_ELEMENTS, BRANCHES, ELEMENT_CONTROLS, ELEMENT_GENERATES, PALACE_ELEMENTS,
    STEM_INDEX, changed_lines_from_yao_values, cycle_index, hexagram_info,
    lines_from_yao_values, mutual_lines, parse_yao_values, stem,
)
from .shensha_ref import void_branches

NAJIA_STEMS = {
    "乾": ("甲", "甲", "甲", "壬", "壬", "壬"),
    "坤": ("乙", "乙", "乙", "癸", "癸", "癸"),
    "震": ("庚", "庚", "庚", "庚", "庚", "庚"),
    "巽": ("辛", "辛", "辛", "辛", "辛", "辛"),
    "坎": ("戊", "戊", "戊", "戊", "戊", "戊"),
    "离": ("己", "己", "己", "己", "己", "己"),
    "艮": ("丙", "丙", "丙", "丙", "丙", "丙"),
    "兑": ("丁", "丁", "丁", "丁", "丁", "丁"),
}
NAJIA_BRANCHES = {
    "乾": ("子", "寅", "辰", "午", "申", "戌"),
    "坤": ("未", "巳", "卯", "丑", "亥", "酉"),
    "震": ("子", "寅", "辰", "午", "申", "戌"),
    "巽": ("丑", "亥", "酉", "未", "巳", "卯"),
    "坎": ("寅", "辰", "午", "申", "戌", "子"),
    "离": ("卯", "丑", "亥", "酉", "未", "巳"),
    "艮": ("辰", "午", "申", "戌", "子", "寅"),
    "兑": ("巳", "卯", "丑", "亥", "酉", "未"),
}
SIX_RELATIVES = ("兄弟", "子孙", "妻财", "官鬼", "父母")
SIX_SPIRITS = ("青龙", "朱雀", "勾陈", "螣蛇", "白虎", "玄武")
SPIRIT_START = {
    "甲": "青龙", "乙": "青龙", "丙": "朱雀", "丁": "朱雀",
    "戊": "勾陈", "己": "螣蛇", "庚": "白虎", "辛": "白虎",
    "壬": "玄武", "癸": "玄武",
}
SHI_POSITIONS = (6, 1, 2, 3, 4, 5, 4, 3)


def _six_relative(palace_element: str, branch_value: str) -> str:
    branch_element = BRANCH_ELEMENTS[branch_value]
    if branch_element == palace_element:
        return "兄弟"
    if ELEMENT_GENERATES[palace_element] == branch_element:
        return "子孙"
    if ELEMENT_CONTROLS[palace_element] == branch_element:
        return "妻财"
    if ELEMENT_CONTROLS[branch_element] == palace_element:
        return "官鬼"
    if ELEMENT_GENERATES[branch_element] == palace_element:
        return "父母"
    raise AssertionError("five element relation is exhaustive")


def _spirits(day_stem: str) -> tuple[str, ...]:
    start = SIX_SPIRITS.index(SPIRIT_START[day_stem])
    return tuple(SIX_SPIRITS[(start + index) % 6] for index in range(6))


def _day_pillar(day_stem_or_pillar: Any, day_branch: Any | None) -> tuple[str, str]:
    if day_branch is None:
        if type(day_stem_or_pillar) is not str or len(day_stem_or_pillar) != 2:
            raise ValueError("day pillar must be '甲子' when day_branch is omitted")
        return stem(day_stem_or_pillar[0], "day_stem"), day_stem_or_pillar[1]
    return stem(day_stem_or_pillar, "day_stem"), day_branch


def liuyao(
    yao_values: Any,
    day_stem_or_pillar: Any,
    day_branch: Any | None = None,
) -> dict[str, Any]:
    """输入初爻至上爻的 6/7/8/9，返回完整装卦结果。"""

    values = parse_yao_values(yao_values)
    day_stem, day_branch = _day_pillar(day_stem_or_pillar, day_branch)
    # cycle_index 同时完成日支的合法性和甲子旬分组校验。
    cycle_index(day_stem, day_branch)
    lines = lines_from_yao_values(values)
    changed = changed_lines_from_yao_values(values)
    original = hexagram_info(lines)
    transformed = hexagram_info(changed)
    mutual = hexagram_info(mutual_lines(lines))
    shi = SHI_POSITIONS[original["sequence"]]
    ying = (shi + 3 - 1) % 6 + 1
    branches = NAJIA_BRANCHES[original["lower"]][:3] + NAJIA_BRANCHES[original["upper"]][3:]
    stems = NAJIA_STEMS[original["lower"]][:3] + NAJIA_STEMS[original["upper"]][3:]
    spirits = _spirits(day_stem)
    void = void_branches(day_stem + day_branch)

    yao: list[dict[str, Any]] = []
    for index, (value, branch_value, stem_value) in enumerate(zip(values, branches, stems)):
        position = index + 1
        yao.append({
            "position": position,
            "value": value,
            "changing": value in (6, 9),
            "na_jia_stem": stem_value,
            "na_jia_branch": branch_value,
            "six_relative": _six_relative(original["palace_element"], branch_value),
            "six_spirit": spirits[index],
            "void": branch_value in void,
            "is_shi": position == shi,
            "is_ying": position == ying,
        })

    def public_info(info: dict[str, Any]) -> dict[str, Any]:
        return {"name": info["name"], "upper": info["upper"], "lower": info["lower"]}

    return {
        "yao_values": list(values),
        "lines": list(lines),
        "original": public_info(original),
        "changed": public_info(transformed),
        "mutual": public_info(mutual),
        "original_name": original["name"],
        "changed_name": transformed["name"],
        "本卦名": original["name"],
        "变卦名": transformed["name"],
        "palace": original["palace"],
        "八宫": original["palace"],
        "palace_element": original["palace_element"],
        "palace_sequence": original["sequence_name"],
        "shi": shi,
        "ying": ying,
        "世应": {"世": shi, "应": ying},
        "世爻": shi,
        "应爻": ying,
        "day_pillar": day_stem + day_branch,
        "void_branches": list(void),
        "旬空": list(void),
        "na_jia_branches": list(branches),
        "纳甲": list(branches),
        "six_relatives": [item["six_relative"] for item in yao],
        "six_spirits": list(spirits),
        "yao": yao,
    }


install_hexagram = liuyao
calculate_liuyao = liuyao

__all__ = [
    "NAJIA_BRANCHES", "NAJIA_STEMS", "SIX_RELATIVES", "SIX_SPIRITS", "calculate_liuyao",
    "install_hexagram", "liuyao",
]
