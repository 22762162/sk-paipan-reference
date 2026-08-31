"""五虎遁流月盲写参考实现。

口诀出处：《渊海子平·起月例》：“甲己之年丙作首，乙庚之岁戊为头，
丙辛岁首寻庚起，丁壬壬寅顺流，若问戊癸何方发，甲寅之上好追求。”
正月固定为寅，之后干支各顺行一位。
"""

from __future__ import annotations

from typing import Any

from ._common import BRANCHES, STEMS, STEM_INDEX, plain_int, stem


def first_month_stem(year_stem: Any) -> str:
    """返回该年寅月月干。"""

    year_stem = stem(year_stem, "year_stem")
    # 甲己丙、乙庚戊、丙辛庚、丁壬壬、戊癸甲。
    return STEMS[((STEM_INDEX[year_stem] % 5) * 2 + 2) % 10]


def flowing_months(year_stem: Any) -> list[dict[str, Any]]:
    """返回正月至十二月的流月干支对象。"""

    first = STEM_INDEX[first_month_stem(year_stem)]
    result = []
    for month in range(12):
        stem_value = STEMS[(first + month) % 10]
        branch_value = BRANCHES[(2 + month) % 12]
        result.append({
            "month": month + 1,
            "stem": stem_value,
            "branch": branch_value,
            "ganzhi": stem_value + branch_value,
        })
    return result


def wuhudun(year_stem: Any) -> list[str]:
    """以最简形式返回十二个流月干支名。"""

    return [month["ganzhi"] for month in flowing_months(year_stem)]


def wuhu_dun(year_stem: Any) -> list[str]:
    """英文分词别名。"""

    return wuhudun(year_stem)


__all__ = ["first_month_stem", "flowing_months", "wuhu_dun", "wuhudun"]
