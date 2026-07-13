"""附录 A JSONL CLI：stdin 逐行输入，stdout 同序逐行输出。"""

import json
import sys
from typing import Any

from .four_pillars import four_pillars
from .validation import (
    ContractInputError,
    require_exact_object,
    require_plain_int,
    require_string,
)
from .year_pillar import year_pillar

REQUEST_FIELDS = ("case_id", "op", "input")
YEAR_PILLAR_FIELDS = ("civil_year", "t_unix", "lichun_unix")
FOUR_PILLARS_FIELDS = (
    "t_unix",
    "lichun_unix",
    "local",
    "month_ctx",
    "zi_hour_mode",
)


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _error(case_id: str | None, message: str) -> dict[str, Any]:
    return {"case_id": case_id, "ok": False, "error": message}


def _handle_year_pillar(inp: Any) -> dict[str, int | str]:
    obj = require_exact_object(inp, YEAR_PILLAR_FIELDS, "input")
    return year_pillar(
        require_plain_int(obj["civil_year"], "input.civil_year"),
        require_plain_int(obj["t_unix"], "input.t_unix"),
        require_plain_int(obj["lichun_unix"], "input.lichun_unix"),
    )


def _handle_four_pillars(inp: Any) -> dict[str, Any]:
    obj = require_exact_object(inp, FOUR_PILLARS_FIELDS, "input")
    return four_pillars(
        require_plain_int(obj["t_unix"], "input.t_unix"),
        require_plain_int(obj["lichun_unix"], "input.lichun_unix"),
        obj["local"],
        obj["month_ctx"],
        obj["zi_hour_mode"],
    )


def handle(line: str) -> dict[str, Any]:
    """处理一行；所有普通解析/校验/计算异常都转为该行 ``ok:false``。"""

    try:
        case = json.loads(line, parse_constant=_reject_nonstandard_constant)
    except Exception as exc:
        return _error(None, f"bad case line: {exc}")

    if type(case) is not dict:
        return _error(None, "bad case line: not a JSON object")

    raw_case_id = case.get("case_id")
    case_id = raw_case_id if type(raw_case_id) is str else None
    try:
        request = require_exact_object(case, REQUEST_FIELDS, "request")
        case_id = require_string(request["case_id"], "request.case_id")
        op = require_string(request["op"], "request.op")
        inp = request["input"]
        if type(inp) is not dict:
            raise ContractInputError("request.input must be an object")

        if op == "year_pillar":
            output = _handle_year_pillar(inp)
        elif op == "four_pillars":
            output = _handle_four_pillars(inp)
        else:
            return _error(case_id, f"unknown op: {op}")
    except Exception as exc:
        return _error(case_id, f"bad input: {exc}")

    return {"case_id": case_id, "ok": True, "output": output}


def _configure_utf8(stream: Any) -> None:
    """标准流支持重配置时，显式固定附录 A 要求的 UTF-8。"""

    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def main() -> None:
    _configure_utf8(sys.stdin)
    _configure_utf8(sys.stdout)
    for line in sys.stdin:
        response = handle(line)
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
