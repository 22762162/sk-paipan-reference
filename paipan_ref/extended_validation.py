"""业主要求的非契约盲写扩展所用严格输入校验。"""

from typing import Any

from .ganzhi import BRANCHES, STEMS
from .validation import ContractInputError, require_exact_object, require_string

PILLAR_FIELDS = ("stem", "branch")
PILLAR_WITH_NAME_FIELDS = ("stem", "branch", "ganzhi")
CHART_FIELDS = ("year", "month", "day", "hour")


def ganzhi_cycle_index(stem: str, branch: str, path: str = "pillar") -> int:
    """返回合法六十甲子序号；阴阳不配的干支组合会被拒绝。"""

    stem = require_string(stem, f"{path}.stem")
    branch = require_string(branch, f"{path}.branch")
    if stem not in STEMS:
        raise ContractInputError(f"{path}.stem is not a heavenly stem")
    if branch not in BRANCHES:
        raise ContractInputError(f"{path}.branch is not an earthly branch")
    stem_index = STEMS.index(stem)
    branch_index = BRANCHES.index(branch)
    for cycle_index in range(60):
        if cycle_index % 10 == stem_index and cycle_index % 12 == branch_index:
            return cycle_index
    raise ContractInputError(f"{path} is not a valid sexagenary pair")


def validate_pillar(value: Any, path: str) -> dict[str, str]:
    """校验合法干支柱；兼容正式四柱输出携带的冗余 ``ganzhi``。"""

    if type(value) is not dict:
        raise ContractInputError(f"{path} must be an object")
    fields = PILLAR_WITH_NAME_FIELDS if "ganzhi" in value else PILLAR_FIELDS
    obj = require_exact_object(value, fields, path)
    stem = require_string(obj["stem"], f"{path}.stem")
    branch = require_string(obj["branch"], f"{path}.branch")
    ganzhi_cycle_index(stem, branch, path)
    if "ganzhi" in obj:
        ganzhi = require_string(obj["ganzhi"], f"{path}.ganzhi")
        if ganzhi != stem + branch:
            raise ContractInputError(f"{path}.ganzhi must equal stem + branch")
    return {"stem": stem, "branch": branch}


def validate_chart(value: Any, path: str = "chart") -> dict[str, dict[str, str]]:
    """校验年、月、日、时四柱对象，不接受额外字段。"""

    obj = require_exact_object(value, CHART_FIELDS, path)
    return {name: validate_pillar(obj[name], f"{path}.{name}") for name in CHART_FIELDS}


__all__ = [
    "CHART_FIELDS",
    "PILLAR_FIELDS",
    "PILLAR_WITH_NAME_FIELDS",
    "ganzhi_cycle_index",
    "validate_chart",
    "validate_pillar",
]
