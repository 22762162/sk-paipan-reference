"""常用神煞查表的独立盲写实现。

本模块只报告表格命中，不输出吉凶解释。取法基准与文昌异文均显式写入输出，
避免把流派选择藏在实现细节里。
"""

from typing import Any

from .extended_validation import CHART_FIELDS, ganzhi_cycle_index, validate_chart
from .ganzhi import BRANCHES, STEMS
from .validation import ContractInputError, require_string

TRINE_TABLES = {
    "申子辰": {"members": frozenset("申子辰"), "桃花": "酉", "驿马": "寅", "华盖": "辰", "将星": "子"},
    "寅午戌": {"members": frozenset("寅午戌"), "桃花": "卯", "驿马": "申", "华盖": "戌", "将星": "午"},
    "亥卯未": {"members": frozenset("亥卯未"), "桃花": "子", "驿马": "巳", "华盖": "未", "将星": "卯"},
    "巳酉丑": {"members": frozenset("巳酉丑"), "桃花": "午", "驿马": "亥", "华盖": "丑", "将星": "酉"},
}
TIANYI = {
    "甲": ("丑", "未"), "戊": ("丑", "未"), "庚": ("丑", "未"),
    "乙": ("子", "申"), "己": ("子", "申"),
    "丙": ("亥", "酉"), "丁": ("亥", "酉"),
    "壬": ("卯", "巳"), "癸": ("卯", "巳"), "辛": ("午", "寅"),
}
WENCHANG_ZI = {
    "甲": ("巳",), "乙": ("午",), "丙": ("申",), "丁": ("酉",), "戊": ("申",),
    "己": ("酉",), "庚": ("亥",), "辛": ("子",), "壬": ("寅",), "癸": ("卯",),
}
WENCHANG_XU = {**WENCHANG_ZI, "辛": ("戌",)}
YANGREN = {
    "甲": ("卯",), "乙": ("寅",), "丙": ("午",), "丁": ("巳",), "戊": ("午",),
    "己": ("巳",), "庚": ("酉",), "辛": ("申",), "壬": ("子",), "癸": ("亥",),
}
LUSHEN = {
    "甲": ("寅",), "乙": ("卯",), "丙": ("巳",), "丁": ("午",), "戊": ("巳",),
    "己": ("午",), "庚": ("申",), "辛": ("酉",), "壬": ("亥",), "癸": ("子",),
}
GUCHEN_GUASU = {
    frozenset("亥子丑"): {"孤辰": "寅", "寡宿": "戌"},
    frozenset("寅卯辰"): {"孤辰": "巳", "寡宿": "丑"},
    frozenset("巳午未"): {"孤辰": "申", "寡宿": "辰"},
    frozenset("申酉戌"): {"孤辰": "亥", "寡宿": "未"},
}
STAR_ORDER = (
    "桃花", "驿马", "华盖", "将星", "天乙贵人", "文昌", "羊刃", "禄神",
    "红鸾", "天喜", "孤辰", "寡宿", "空亡",
)


def _trine_targets(branch: str) -> dict[str, str]:
    for table in TRINE_TABLES.values():
        if branch in table["members"]:
            return {name: table[name] for name in ("桃花", "驿马", "华盖", "将星")}
    raise AssertionError("every earthly branch belongs to one trine")


def _hit_pillars(chart: dict[str, dict[str, str]], targets: tuple[str, ...]) -> list[str]:
    return [pillar for pillar in CHART_FIELDS if chart[pillar]["branch"] in targets]


def _add_entry(
    entries: dict[str, list[dict[str, Any]]],
    chart: dict[str, dict[str, str]],
    name: str,
    basis_type: str,
    basis_value: str,
    targets: tuple[str, ...],
) -> None:
    entries[name].append(
        {
            "basis": {"type": basis_type, "value": basis_value},
            "targets": list(targets),
            "hit_pillars": _hit_pillars(chart, targets),
        }
    )


def shensha(chart: Any, wenchang_variant: str = "zi") -> dict[str, Any]:
    """按年支、日支、日干和日柱旬空输出十三种神煞的目标与命中柱。"""

    pillars = validate_chart(chart, "chart")
    variant = require_string(wenchang_variant, "wenchang_variant")
    if variant not in ("zi", "xu"):
        raise ContractInputError("wenchang_variant must be 'zi' or 'xu'")

    entries: dict[str, list[dict[str, Any]]] = {name: [] for name in STAR_ORDER}
    for basis_name, basis_branch in (
        ("year_branch", pillars["year"]["branch"]),
        ("day_branch", pillars["day"]["branch"]),
    ):
        for name, target in _trine_targets(basis_branch).items():
            _add_entry(entries, pillars, name, basis_name, basis_branch, (target,))

    day_stem = pillars["day"]["stem"]
    _add_entry(entries, pillars, "天乙贵人", "day_stem", day_stem, TIANYI[day_stem])
    wenchang = WENCHANG_ZI if variant == "zi" else WENCHANG_XU
    _add_entry(entries, pillars, "文昌", "day_stem", day_stem, wenchang[day_stem])
    _add_entry(entries, pillars, "羊刃", "day_stem", day_stem, YANGREN[day_stem])
    _add_entry(entries, pillars, "禄神", "day_stem", day_stem, LUSHEN[day_stem])

    year_branch = pillars["year"]["branch"]
    red_luan_index = (3 - BRANCHES.index(year_branch)) % 12
    red_luan = BRANCHES[red_luan_index]
    tian_xi = BRANCHES[(red_luan_index + 6) % 12]
    _add_entry(entries, pillars, "红鸾", "year_branch", year_branch, (red_luan,))
    _add_entry(entries, pillars, "天喜", "year_branch", year_branch, (tian_xi,))

    for group, targets in GUCHEN_GUASU.items():
        if year_branch in group:
            for name in ("孤辰", "寡宿"):
                _add_entry(
                    entries, pillars, name, "year_branch", year_branch, (targets[name],)
                )
            break

    day = pillars["day"]
    day_index = ganzhi_cycle_index(day["stem"], day["branch"], "chart.day")
    xun_start_branch = (day_index // 10 * 10) % 12
    void_targets = (
        BRANCHES[(xun_start_branch + 10) % 12],
        BRANCHES[(xun_start_branch + 11) % 12],
    )
    _add_entry(
        entries, pillars, "空亡", "day_pillar", day["stem"] + day["branch"], void_targets
    )

    matched = [name for name in STAR_ORDER if any(item["hit_pillars"] for item in entries[name])]
    return {
        "wenchang_variant": variant,
        "stars": [{"name": name, "rules": entries[name]} for name in STAR_ORDER],
        "matched_stars": matched,
    }


__all__ = [
    "GUCHEN_GUASU",
    "LUSHEN",
    "STAR_ORDER",
    "TIANYI",
    "TRINE_TABLES",
    "WENCHANG_XU",
    "WENCHANG_ZI",
    "YANGREN",
    "shensha",
]
