"""年柱：YP-1、YP-2、YP-3 与第 3.3 节组合函数。"""

from .ganzhi import BRANCHES, STEMS, pillar_from_indexes
from .validation import require_plain_int


def resolve_bazi_year(civil_year: int, t_unix: int, lichun_unix: int) -> int:
    """YP-1：立春为闭下界，界前归上一八字年。"""

    civil_year = require_plain_int(civil_year, "civil_year")
    t_unix = require_plain_int(t_unix, "t_unix")
    lichun_unix = require_plain_int(lichun_unix, "lichun_unix")
    return civil_year - 1 if t_unix < lichun_unix else civil_year


def year_ganzhi(bazi_year: int) -> tuple[int, int]:
    """YP-2/YP-3：返回八字年的天干、地支零基序号。"""

    bazi_year = require_plain_int(bazi_year, "bazi_year")
    return (bazi_year - 4) % 10, (bazi_year - 4) % 12


def year_pillar(civil_year: int, t_unix: int, lichun_unix: int) -> dict[str, int | str]:
    """组合 YP-1/2/3，生成 ``year_pillar`` op 的 output。"""

    bazi_year = resolve_bazi_year(civil_year, t_unix, lichun_unix)
    stem, branch = year_ganzhi(bazi_year)
    output: dict[str, int | str] = {"bazi_year": bazi_year}
    output.update(pillar_from_indexes(stem, branch))
    return output


__all__ = [
    "BRANCHES",
    "STEMS",
    "resolve_bazi_year",
    "year_ganzhi",
    "year_pillar",
]
