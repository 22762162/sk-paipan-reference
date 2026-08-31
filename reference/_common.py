"""七个盲写模块共用的静态干支、八卦和严格校验表。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

STEMS = tuple("甲乙丙丁戊己庚辛壬癸")
BRANCHES = tuple("子丑寅卯辰巳午未申酉戌亥")
STEM_INDEX = {value: index for index, value in enumerate(STEMS)}
BRANCH_INDEX = {value: index for index, value in enumerate(BRANCHES)}

STEM_ELEMENTS = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
    "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水",
}
BRANCH_ELEMENTS = {
    "子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土",
    "巳": "火", "午": "火", "未": "土", "申": "金", "酉": "金",
    "戌": "土", "亥": "水",
}
ELEMENT_GENERATES = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
ELEMENT_CONTROLS = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}


def plain_int(value: Any, name: str) -> int:
    """接受真正的整数，拒绝 bool（bool 是 int 的子类）。"""

    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def stem(value: Any, name: str = "stem") -> str:
    if type(value) is not str or value not in STEM_INDEX:
        raise ValueError(f"{name} must be one of the ten heavenly stems")
    return value


def branch(value: Any, name: str = "branch") -> str:
    if type(value) is not str or value not in BRANCH_INDEX:
        raise ValueError(f"{name} must be one of the twelve earthly branches")
    return value


def cycle_index(stem_value: Any, branch_value: Any) -> int:
    """返回合法干支在六十甲子中的 0 起序号。"""

    stem_value = stem(stem_value, "stem")
    branch_value = branch(branch_value, "branch")
    for index in range(60):
        if index % 10 == STEM_INDEX[stem_value] and index % 12 == BRANCH_INDEX[branch_value]:
            return index
    raise ValueError(f"{stem_value}{branch_value} is not a valid sexagenary pair")


def pillar(value: Any, name: str = "pillar") -> dict[str, str]:
    """标准化 ``甲子``、``(甲, 子)`` 或 ``{'stem','branch'}`` 柱。"""

    if type(value) is str:
        if len(value) != 2:
            raise ValueError(f"{name} must contain one stem and one branch")
        stem_value, branch_value = value[0], value[1]
    elif isinstance(value, Mapping):
        if "stem" not in value or "branch" not in value:
            raise ValueError(f"{name} must contain stem and branch")
        stem_value, branch_value = value["stem"], value["branch"]
    elif type(value) in (tuple, list) and len(value) == 2:
        stem_value, branch_value = value
    else:
        raise ValueError(f"{name} must be a ganzhi string, pair, or object")
    stem_value = stem(stem_value, f"{name}.stem")
    branch_value = branch(branch_value, f"{name}.branch")
    cycle_index(stem_value, branch_value)
    if isinstance(value, Mapping) and "ganzhi" in value:
        if type(value["ganzhi"]) is not str or value["ganzhi"] != stem_value + branch_value:
            raise ValueError(f"{name}.ganzhi must equal stem + branch")
    return {"stem": stem_value, "branch": branch_value, "ganzhi": stem_value + branch_value}


CHART_PILLARS = ("year", "month", "day", "hour")


def chart(value: Any, name: str = "chart") -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    missing = [key for key in CHART_PILLARS if key not in value]
    if missing:
        raise ValueError(f"{name} missing {missing[0]}")
    return {
        key: pillar(value[key], f"{name}.{key}")
        for key in CHART_PILLARS
    }


# 先天数：一乾、二兑、三离、四震、五巽、六坎、七艮、八坤。
TRIGRAM_BY_NUMBER = {
    1: "乾", 2: "兑", 3: "离", 4: "震",
    5: "巽", 6: "坎", 7: "艮", 8: "坤",
}
TRIGRAM_LINES = {
    # 各 tuple 皆为初爻至上爻，1 阳、0 阴。
    "乾": (1, 1, 1), "兑": (1, 1, 0), "离": (1, 0, 1), "震": (1, 0, 0),
    "巽": (0, 1, 1), "坎": (0, 1, 0), "艮": (0, 0, 1), "坤": (0, 0, 0),
}
TRIGRAM_FROM_LINES = {lines: name for name, lines in TRIGRAM_LINES.items()}
TRIGRAM_ELEMENTS = {"乾": "金", "兑": "金", "离": "火", "震": "木",
                    "巽": "木", "坎": "水", "艮": "土", "坤": "土"}

# 京房八宫次序：本宫、初变至五变、游魂、归魂。次序表本身也是
# 《京氏易传》/《卜筮正宗》装世应时最常用的八宫口诀化表达。
PALACE_ORDER = {
    "乾": ("乾为天", "天风姤", "天山遁", "天地否", "风地观", "山地剥", "火地晋", "火天大有"),
    "坎": ("坎为水", "水泽节", "水雷屯", "水火既济", "泽火革", "雷火丰", "地火明夷", "地水师"),
    "艮": ("艮为山", "山火贲", "山天大畜", "山泽损", "火泽睽", "天泽履", "风泽中孚", "风山渐"),
    "震": ("震为雷", "雷地豫", "雷水解", "雷风恒", "地风升", "水风井", "泽风大过", "泽雷随"),
    "巽": ("巽为风", "风天小畜", "风火家人", "风雷益", "天雷无妄", "火雷噬嗑", "山雷颐", "山风蛊"),
    "离": ("离为火", "火山旅", "火风鼎", "火水未济", "山水蒙", "风水涣", "天水讼", "天火同人"),
    "坤": ("坤为地", "地雷复", "地泽临", "地天泰", "雷天大壮", "泽天夬", "水天需", "水地比"),
    "兑": ("兑为泽", "泽水困", "泽地萃", "泽山咸", "水山蹇", "地山谦", "雷山小过", "雷泽归妹"),
}
PALACE_ELEMENTS = {"乾": "金", "兑": "金", "艮": "土", "坤": "土",
                   "震": "木", "巽": "木", "坎": "水", "离": "火"}


def _flip(lines: tuple[int, ...], indexes: Sequence[int]) -> tuple[int, ...]:
    result = list(lines)
    for index in indexes:
        result[index] = 1 - result[index]
    return tuple(result)


def _palace_lines(base_trigram: str) -> tuple[tuple[int, ...], ...]:
    base = TRIGRAM_LINES[base_trigram] * 2
    return (
        base,
        _flip(base, (0,)),
        _flip(base, (0, 1)),
        _flip(base, (0, 1, 2)),
        _flip(base, (0, 1, 2, 3)),
        _flip(base, (0, 1, 2, 3, 4)),
        _flip(_flip(base, (0, 1, 2, 3, 4)), (3,)),
        _flip(_flip(_flip(base, (0, 1, 2, 3, 4)), (3,)), (0, 1, 2)),
    )


HEXAGRAMS: dict[tuple[int, ...], dict[str, Any]] = {}
for palace_name, names in PALACE_ORDER.items():
    for sequence, (name, lines) in enumerate(zip(names, _palace_lines(palace_name))):
        lower = TRIGRAM_FROM_LINES[lines[:3]]
        upper = TRIGRAM_FROM_LINES[lines[3:]]
        HEXAGRAMS[lines] = {
            "name": name,
            "palace": palace_name,
            "palace_element": PALACE_ELEMENTS[palace_name],
            "sequence": sequence,
            "sequence_name": ("本宫", "一世", "二世", "三世", "四世", "五世", "游魂", "归魂")[sequence],
            "lower": lower,
            "upper": upper,
        }
if len(HEXAGRAMS) != 64:  # pragma: no cover - protects the static table itself.
    raise AssertionError("eight palace construction must contain 64 hexagrams")


def hexagram_info(lines: Sequence[int]) -> dict[str, Any]:
    key = tuple(lines)
    if key not in HEXAGRAMS:
        raise ValueError("hexagram must contain six binary lines")
    return dict(HEXAGRAMS[key])


def parse_yao_values(value: Any) -> tuple[int, ...]:
    if type(value) not in (tuple, list) or len(value) != 6:
        raise ValueError("yao values must be six integers from 6, 7, 8, 9")
    values = tuple(plain_int(item, "yao") for item in value)
    if any(item not in (6, 7, 8, 9) for item in values):
        raise ValueError("yao values must be six integers from 6, 7, 8, 9")
    return values


def lines_from_yao_values(values: Sequence[int]) -> tuple[int, ...]:
    return tuple(1 if value in (7, 9) else 0 for value in values)


def changed_lines_from_yao_values(values: Sequence[int]) -> tuple[int, ...]:
    return tuple(1 - line if value in (6, 9) else line
                 for value, line in zip(values, lines_from_yao_values(values)))


def mutual_lines(lines: Sequence[int]) -> tuple[int, ...]:
    # 互卦取二、三、四爻为下卦，三、四、五爻为上卦。
    return (lines[1], lines[2], lines[3], lines[2], lines[3], lines[4])


__all__ = [
    "BRANCHES", "BRANCH_ELEMENTS", "BRANCH_INDEX", "CHART_PILLARS",
    "ELEMENT_CONTROLS", "ELEMENT_GENERATES", "HEXAGRAMS", "PALACE_ELEMENTS",
    "PALACE_ORDER", "STEMS", "STEM_ELEMENTS", "STEM_INDEX", "TRIGRAM_BY_NUMBER",
    "TRIGRAM_ELEMENTS", "TRIGRAM_FROM_LINES", "TRIGRAM_LINES", "branch", "chart",
    "changed_lines_from_yao_values", "cycle_index", "hexagram_info", "lines_from_yao_values",
    "mutual_lines", "parse_yao_values", "pillar", "plain_int", "stem",
]
