"""常用十三种神煞的盲写查表实现。

口诀出处：天乙“甲戊庚牛羊，乙己鼠猴乡，丙丁猪鸡位，壬癸蛇兔藏，
六辛逢马虎”；文昌“甲巳乙午报君知，丙戊申宫丁己鸡，庚猪辛鼠壬逢虎，
癸人兔上好追随”；禄神、羊刃以及桃花驿马华盖将星、红鸾天喜、孤辰寡宿
均按《三命通会》卷二、卷三及传统表诀查取。辛干文昌的子/戌异文用参数显式选择。
"""

from __future__ import annotations

from typing import Any

from ._common import BRANCHES, BRANCH_INDEX, cycle_index, chart, stem

TIANYI = {
    "甲": ("丑", "未"), "戊": ("丑", "未"), "庚": ("丑", "未"),
    "乙": ("子", "申"), "己": ("子", "申"),
    "丙": ("亥", "酉"), "丁": ("亥", "酉"),
    "壬": ("卯", "巳"), "癸": ("卯", "巳"), "辛": ("午", "寅"),
}
WENCHANG_ZI = {
    "甲": ("巳",), "乙": ("午",), "丙": ("申",), "丁": ("酉",),
    "戊": ("申",), "己": ("酉",), "庚": ("亥",), "辛": ("子",),
    "壬": ("寅",), "癸": ("卯",),
}
WENCHANG_XU = {**WENCHANG_ZI, "辛": ("戌",)}
YANGREN = {
    "甲": ("卯",), "乙": ("寅",), "丙": ("午",), "丁": ("巳",),
    "戊": ("午",), "己": ("巳",), "庚": ("酉",), "辛": ("申",),
    "壬": ("子",), "癸": ("亥",),
}
LUSHEN = {
    "甲": ("寅",), "乙": ("卯",), "丙": ("巳",), "丁": ("午",),
    "戊": ("巳",), "己": ("午",), "庚": ("申",), "辛": ("酉",),
    "壬": ("亥",), "癸": ("子",),
}

TRINE_STARS = {
    "申子辰": {"members": frozenset(("申", "子", "辰")), "桃花": "酉", "驿马": "寅", "华盖": "辰", "将星": "子"},
    "寅午戌": {"members": frozenset(("寅", "午", "戌")), "桃花": "卯", "驿马": "申", "华盖": "戌", "将星": "午"},
    "亥卯未": {"members": frozenset(("亥", "卯", "未")), "桃花": "子", "驿马": "巳", "华盖": "未", "将星": "卯"},
    "巳酉丑": {"members": frozenset(("巳", "酉", "丑")), "桃花": "午", "驿马": "亥", "华盖": "丑", "将星": "酉"},
}
GUCHEN_GUASU = {
    frozenset(("亥", "子", "丑")): {"孤辰": "寅", "寡宿": "戌"},
    frozenset(("寅", "卯", "辰")): {"孤辰": "巳", "寡宿": "丑"},
    frozenset(("巳", "午", "未")): {"孤辰": "申", "寡宿": "辰"},
    frozenset(("申", "酉", "戌")): {"孤辰": "亥", "寡宿": "未"},
}
STAR_ORDER = (
    "天乙贵人", "文昌", "羊刃", "禄神", "桃花", "驿马", "华盖", "将星",
    "红鸾", "天喜", "孤辰", "寡宿", "旬空",
)


def _hit_pillars(pillars: dict[str, dict[str, str]], targets: tuple[str, ...]) -> list[str]:
    return [name for name in ("year", "month", "day", "hour")
            if pillars[name]["branch"] in targets]


def _rule(
    pillars: dict[str, dict[str, str]], name: str, basis: str, value: str,
    targets: tuple[str, ...],
) -> dict[str, Any]:
    return {"name": name, "basis": {"type": basis, "value": value},
            "targets": list(targets), "hit_pillars": _hit_pillars(pillars, targets)}


def void_branches(day_pillar: Any) -> tuple[str, str]:
    """返回日柱所在旬的旬空二支；甲子旬的黄金值为戌亥。"""

    if type(day_pillar) is str:
        if len(day_pillar) != 2:
            raise ValueError("day_pillar must be a two-character ganzhi")
        stem_value, branch_value = day_pillar
    elif isinstance(day_pillar, dict):
        stem_value, branch_value = day_pillar.get("stem"), day_pillar.get("branch")
    else:
        raise ValueError("day_pillar must be a ganzhi string or object")
    index = cycle_index(stem_value, branch_value)
    first_branch = (index // 10 * 10) % 12
    return BRANCHES[(first_branch + 10) % 12], BRANCHES[(first_branch + 11) % 12]


def _trine_table(branch_value: str) -> dict[str, str]:
    for table in TRINE_STARS.values():
        if branch_value in table["members"]:
            return table
    raise AssertionError("all earthly branches belong to a trine table")


def shensha(chart_value: Any, wenchang_variant: str = "zi") -> dict[str, Any]:
    """按年支/日支、日干与日柱旬空列出目标支及命中柱。"""

    pillars = chart(chart_value)
    if type(wenchang_variant) is not str or wenchang_variant not in ("zi", "xu"):
        raise ValueError("wenchang_variant must be 'zi' or 'xu'")
    day_stem = stem(pillars["day"]["stem"], "day_stem")
    rules: dict[str, list[dict[str, Any]]] = {name: [] for name in STAR_ORDER}

    for name, targets in (("天乙贵人", TIANYI[day_stem]),
                          ("文昌", (WENCHANG_ZI if wenchang_variant == "zi" else WENCHANG_XU)[day_stem]),
                          ("羊刃", YANGREN[day_stem]), ("禄神", LUSHEN[day_stem])):
        rules[name].append(_rule(pillars, name, "day_stem", day_stem, targets))

    for basis_name in ("year", "day"):
        basis_branch = pillars[basis_name]["branch"]
        table = _trine_table(basis_branch)
        for name in ("桃花", "驿马", "华盖", "将星"):
            rules[name].append(_rule(pillars, name, basis_name + "_branch", basis_branch,
                                     (table[name],)))

    year_branch = pillars["year"]["branch"]
    red_luan_index = (3 - BRANCH_INDEX[year_branch]) % 12
    red_luan = BRANCHES[red_luan_index]
    tian_xi = BRANCHES[(red_luan_index + 6) % 12]
    rules["红鸾"].append(_rule(pillars, "红鸾", "year_branch", year_branch, (red_luan,)))
    rules["天喜"].append(_rule(pillars, "天喜", "year_branch", year_branch, (tian_xi,)))

    for group, targets in GUCHEN_GUASU.items():
        if year_branch in group:
            for name in ("孤辰", "寡宿"):
                rules[name].append(_rule(pillars, name, "year_branch", year_branch,
                                         (targets[name],)))
            break

    day_pillar = pillars["day"]["ganzhi"]
    rules["旬空"].append(_rule(pillars, "旬空", "day_pillar", day_pillar,
                                void_branches(day_pillar)))
    rules["空亡"] = rules["旬空"]
    matches = {
        name: list(dict.fromkeys(hit for item in values for hit in item["hit_pillars"]))
        for name, values in rules.items()
    }
    # “旬空”与“空亡”是同一星煞的两种常见称呼，主序列只保留一个项目，
    # 但在结果字典中保留别名，方便不同对拍器按古籍用语取值。
    matches["空亡"] = matches["旬空"]
    return {
        "wenchang_variant": wenchang_variant,
        "rules": rules,
        "stars": [{"name": name, "rules": rules[name]} for name in STAR_ORDER],
        "matches": matches,
        "matched_stars": [name for name in STAR_ORDER if matches[name]],
        "空亡": rules["旬空"],
    }


calculate_shensha = shensha

__all__ = [
    "GUCHEN_GUASU", "LUSHEN", "STAR_ORDER", "TIANYI", "TRINE_STARS", "WENCHANG_XU",
    "WENCHANG_ZI", "YANGREN", "calculate_shensha", "shensha", "void_branches",
]
