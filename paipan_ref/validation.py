"""JSON 协议与公开计算函数共用的严格输入校验。"""

from collections.abc import Iterable
from typing import Any


class ContractInputError(ValueError):
    """输入不满足 paipan 契约。"""


def require_plain_int(value: Any, path: str) -> int:
    """只接受 JSON 整数对应的 Python ``int``，显式排除 ``bool``。"""

    if type(value) is not int:
        raise ContractInputError(f"{path} must be an integer")
    return value


def require_string(value: Any, path: str) -> str:
    if type(value) is not str:
        raise ContractInputError(f"{path} must be a string")
    return value


def require_exact_object(
    value: Any,
    required_keys: Iterable[str],
    path: str,
) -> dict[str, Any]:
    """校验对象类型、必需字段及 schema 的 ``additionalProperties:false``。"""

    if type(value) is not dict:
        raise ContractInputError(f"{path} must be an object")

    keys = tuple(required_keys)
    missing = [key for key in keys if key not in value]
    if missing:
        raise ContractInputError(f"{path} missing field: {missing[0]}")

    allowed = set(keys)
    extra = sorted(key for key in value if key not in allowed)
    if extra:
        raise ContractInputError(f"{path} has unknown field: {extra[0]}")
    return value
