"""年柱：八字年判定与年柱干支。

契约：docs/paipan-spec.md 第 3 节（条款 YP-1 / YP-2 / YP-3）。
确定性纯计算：不访问网络与时钟，不含历法常量；立春时刻由调用方注入。
"""

STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"


def resolve_bazi_year(civil_year: int, t_unix: int, lichun_unix: int) -> int:
    """YP-1：t 早于当年立春归上一年；恰好等于立春归当年（闭下界，spec 1.3）。"""
    return civil_year - 1 if t_unix < lichun_unix else civil_year


def year_ganzhi(bazi_year: int) -> tuple[int, int]:
    """YP-2：s = (Y−4) mod 10，b = (Y−4) mod 12。

    Python 的 % 即欧几里得取模（结果非负），Y < 4 亦成立。
    锚点 YP-3：1984 → (0, 0) 甲子。
    """
    return (bazi_year - 4) % 10, (bazi_year - 4) % 12


def year_pillar(civil_year: int, t_unix: int, lichun_unix: int) -> dict:
    """spec 3.3 组合函数；返回结构即对拍协议的 output 字段。"""
    y = resolve_bazi_year(civil_year, t_unix, lichun_unix)
    s, b = year_ganzhi(y)
    return {
        "bazi_year": y,
        "stem": STEMS[s],
        "branch": BRANCHES[b],
        "ganzhi": STEMS[s] + BRANCHES[b],
    }
