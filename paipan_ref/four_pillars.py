"""月、日、时柱以及 ``four_pillars`` 组合实现。

本模块只执行 paipan-spec v0.2 的整数公式。节气、时区等历法事实均由
调用方注入；模块不读取网络、时钟、环境变量或仓外数据。
"""

from typing import Any

from .ganzhi import pillar_from_indexes
from .validation import (
    ContractInputError,
    require_exact_object,
    require_plain_int,
    require_string,
)
from .year_pillar import resolve_bazi_year, year_ganzhi

LOCAL_FIELDS = ("y", "m", "d", "hh", "mm", "ss")
MONTH_CONTEXT_FIELDS = ("jie_seq", "jie_unix", "next_jie_unix")
ZI_HOUR_MODES = ("split", "unified")


def is_gregorian_leap_year(year: int) -> bool:
    """返回格里历年份是否为闰年；LT-1 只允许正年份。"""

    year = require_plain_int(year, "year")
    if year < 1:
        raise ContractInputError("year must be at least 1")
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_gregorian_month(year: int, month: int) -> int:
    """返回正年份中指定格里历月份的天数。"""

    year = require_plain_int(year, "year")
    month = require_plain_int(month, "month")
    if year < 1:
        raise ContractInputError("year must be at least 1")
    if not 1 <= month <= 12:
        raise ContractInputError("month must be in 1..12")
    if month == 2:
        return 29 if is_gregorian_leap_year(year) else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def _validate_gregorian_date(year: int, month: int, day: int) -> None:
    if year < 1:
        raise ContractInputError("local.y must be at least 1")
    if not 1 <= month <= 12:
        raise ContractInputError("local.m must be in 1..12")
    maximum = days_in_gregorian_month(year, month)
    if not 1 <= day <= maximum:
        raise ContractInputError("local.d is not a valid day for local.y/local.m")


def validate_local(local: Any) -> dict[str, int]:
    """LT-1：校验完整、无额外字段的合法格里历本地民用时刻。"""

    obj = require_exact_object(local, LOCAL_FIELDS, "local")
    values = {
        key: require_plain_int(obj[key], f"local.{key}")
        for key in LOCAL_FIELDS
    }
    _validate_gregorian_date(values["y"], values["m"], values["d"])
    if not 0 <= values["hh"] < 24:
        raise ContractInputError("local.hh must be in 0..23")
    if not 0 <= values["mm"] < 60:
        raise ContractInputError("local.mm must be in 0..59")
    if not 0 <= values["ss"] < 60:
        raise ContractInputError("local.ss must be in 0..59")
    return values


def validate_month_context(t_unix: int, month_ctx: Any) -> dict[str, int]:
    """MP-1：校验节序号及包含 t 的闭开节气月窗口。"""

    t_unix = require_plain_int(t_unix, "t_unix")
    obj = require_exact_object(month_ctx, MONTH_CONTEXT_FIELDS, "month_ctx")
    values = {
        key: require_plain_int(obj[key], f"month_ctx.{key}")
        for key in MONTH_CONTEXT_FIELDS
    }
    if not 0 <= values["jie_seq"] <= 11:
        raise ContractInputError("month_ctx.jie_seq must be in 0..11")
    if not values["jie_unix"] <= t_unix < values["next_jie_unix"]:
        raise ContractInputError(
            "month_ctx must satisfy jie_unix <= t_unix < next_jie_unix"
        )
    return values


def validate_zi_hour_mode(zi_hour_mode: Any) -> str:
    """DP-4：只接受规格列出的两种日界流派。"""

    mode = require_string(zi_hour_mode, "zi_hour_mode")
    if mode not in ZI_HOUR_MODES:
        raise ContractInputError("zi_hour_mode must be 'split' or 'unified'")
    return mode


def month_ganzhi(year_stem: int, jie_seq: int) -> tuple[int, int]:
    """MP-1/MP-2：由年干与节序计算月干支序号。"""

    year_stem = require_plain_int(year_stem, "year_stem")
    jie_seq = require_plain_int(jie_seq, "jie_seq")
    if not 0 <= year_stem <= 9:
        raise ContractInputError("year_stem must be in 0..9")
    if not 0 <= jie_seq <= 11:
        raise ContractInputError("jie_seq must be in 0..11")
    stem = ((year_stem % 5) * 2 + 2 + jie_seq) % 10
    branch = (2 + jie_seq) % 12
    return stem, branch


def gregorian_jdn(year: int, month: int, day: int) -> int:
    """DP-2：以向下整除公式计算合法格里历日期的 JDN。"""

    year = require_plain_int(year, "year")
    month = require_plain_int(month, "month")
    day = require_plain_int(day, "day")
    _validate_gregorian_date(year, month, day)

    a = (14 - month) // 12
    shifted_year = year + 4800 - a
    shifted_month = month + 12 * a - 3
    return (
        day
        + (153 * shifted_month + 2) // 5
        + 365 * shifted_year
        + shifted_year // 4
        - shifted_year // 100
        + shifted_year // 400
        - 32045
    )


def day_ganzhi(jdn: int) -> tuple[int, int]:
    """DP-1/DP-3：由 JDN 返回日干、日支序号。"""

    jdn = require_plain_int(jdn, "jdn")
    cycle_index = (54 + (jdn - 2451545)) % 60
    return cycle_index % 10, cycle_index % 12


def effective_day_jdn(local: Any, zi_hour_mode: str = "split") -> int:
    """DP-4：返回日柱使用的有效 JDN。"""

    values = validate_local(local)
    mode = validate_zi_hour_mode(zi_hour_mode)
    base = gregorian_jdn(values["y"], values["m"], values["d"])
    if mode == "unified" and values["hh"] >= 23:
        return base + 1
    return base


def hour_branch(hh: int, mm: int, ss: int) -> int:
    """HP-1：按闭下界的两小时区间计算时支序号。"""

    hh = require_plain_int(hh, "hh")
    mm = require_plain_int(mm, "mm")
    ss = require_plain_int(ss, "ss")
    if not 0 <= hh < 24:
        raise ContractInputError("hh must be in 0..23")
    if not 0 <= mm < 60:
        raise ContractInputError("mm must be in 0..59")
    if not 0 <= ss < 60:
        raise ContractInputError("ss must be in 0..59")
    seconds = hh * 3600 + mm * 60 + ss
    return ((seconds + 3600) // 7200) % 12


def hour_ganzhi(effective_day_stem: int, branch: int) -> tuple[int, int]:
    """HP-2：以有效日干和 HP-1 时支执行五鼠遁。"""

    effective_day_stem = require_plain_int(effective_day_stem, "effective_day_stem")
    branch = require_plain_int(branch, "hour_branch")
    if not 0 <= effective_day_stem <= 9:
        raise ContractInputError("effective_day_stem must be in 0..9")
    if not 0 <= branch <= 11:
        raise ContractInputError("hour_branch must be in 0..11")
    stem = ((effective_day_stem % 5) * 2 + branch) % 10
    return stem, branch


def _hour_effective_jdn(local: dict[str, int], zi_hour_mode: str) -> int:
    """返回 HP-2 起遁用 JDN；晚子时在两种模式下均使用次日干。"""

    validate_zi_hour_mode(zi_hour_mode)
    base = gregorian_jdn(local["y"], local["m"], local["d"])
    return base + 1 if local["hh"] >= 23 else base


def four_pillars(
    t_unix: int,
    lichun_unix: int,
    local: Any,
    month_ctx: Any,
    zi_hour_mode: str = "split",
) -> dict[str, Any]:
    """组合 LT/YP/MP/DP/HP 条款，生成 ``four_pillars`` output。"""

    t_unix = require_plain_int(t_unix, "t_unix")
    lichun_unix = require_plain_int(lichun_unix, "lichun_unix")
    local_values = validate_local(local)
    month_values = validate_month_context(t_unix, month_ctx)
    mode = validate_zi_hour_mode(zi_hour_mode)

    bazi_year = resolve_bazi_year(local_values["y"], t_unix, lichun_unix)
    year_stem, year_branch = year_ganzhi(bazi_year)
    month_stem, month_branch = month_ganzhi(
        year_stem, month_values["jie_seq"]
    )

    base_jdn = gregorian_jdn(
        local_values["y"], local_values["m"], local_values["d"]
    )
    day_jdn = base_jdn + (
        1 if mode == "unified" and local_values["hh"] >= 23 else 0
    )
    day_stem, day_branch = day_ganzhi(day_jdn)

    hour_branch_index = hour_branch(
        local_values["hh"], local_values["mm"], local_values["ss"]
    )
    hour_day_stem, _ = day_ganzhi(
        _hour_effective_jdn(local_values, mode)
    )
    hour_stem, hour_branch_index = hour_ganzhi(
        hour_day_stem, hour_branch_index
    )

    return {
        "bazi_year": bazi_year,
        "year": pillar_from_indexes(year_stem, year_branch),
        "month": pillar_from_indexes(month_stem, month_branch),
        "day": pillar_from_indexes(day_stem, day_branch),
        "hour": pillar_from_indexes(hour_stem, hour_branch_index),
    }


__all__ = [
    "LOCAL_FIELDS",
    "MONTH_CONTEXT_FIELDS",
    "ZI_HOUR_MODES",
    "day_ganzhi",
    "days_in_gregorian_month",
    "effective_day_jdn",
    "four_pillars",
    "gregorian_jdn",
    "hour_branch",
    "hour_ganzhi",
    "is_gregorian_leap_year",
    "month_ganzhi",
    "validate_local",
    "validate_month_context",
    "validate_zi_hour_mode",
]
