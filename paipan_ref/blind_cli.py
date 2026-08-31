"""非契约盲写扩展 JSONL CLI。

正式 paipan-spec CLI 仍是 ``paipan_ref.cli``；本入口刻意隔离，避免未签署的
大运/神煞/合盘口径伪装成钉版契约能力。
"""

import json
import sys
from typing import Any, Callable

from .extended_validation import validate_chart, validate_pillar
from .luck import dayun
from .relations import pair_analysis
from .shensha import shensha
from .validation import (
    ContractInputError,
    require_exact_object,
    require_plain_int,
    require_string,
)

REQUEST_FIELDS = ("case_id", "op", "input")
DAYUN_FIELDS = (
    "year_stem", "sex", "month_pillar", "birth_unix",
    "previous_jie_unix", "next_jie_unix", "count",
)
SHENSHA_FIELDS = ("chart", "wenchang_variant")
PAIR_FIELDS = ("chart_a", "chart_b")


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _error(case_id: str | None, message: str) -> dict[str, Any]:
    return {"case_id": case_id, "ok": False, "error": message}


def _handle_dayun(inp: Any) -> dict[str, Any]:
    obj = require_exact_object(inp, DAYUN_FIELDS, "input")
    return dayun(
        require_string(obj["year_stem"], "input.year_stem"),
        require_string(obj["sex"], "input.sex"),
        validate_pillar(obj["month_pillar"], "input.month_pillar"),
        require_plain_int(obj["birth_unix"], "input.birth_unix"),
        require_plain_int(obj["previous_jie_unix"], "input.previous_jie_unix"),
        require_plain_int(obj["next_jie_unix"], "input.next_jie_unix"),
        require_plain_int(obj["count"], "input.count"),
    )


def _handle_shensha(inp: Any) -> dict[str, Any]:
    obj = require_exact_object(inp, SHENSHA_FIELDS, "input")
    return shensha(
        validate_chart(obj["chart"], "input.chart"),
        require_string(obj["wenchang_variant"], "input.wenchang_variant"),
    )


def _handle_pair(inp: Any) -> dict[str, Any]:
    obj = require_exact_object(inp, PAIR_FIELDS, "input")
    return pair_analysis(
        validate_chart(obj["chart_a"], "input.chart_a"),
        validate_chart(obj["chart_b"], "input.chart_b"),
    )


HANDLERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "dayun": _handle_dayun,
    "shensha": _handle_shensha,
    "pair_relations": _handle_pair,
}


def handle(line: str) -> dict[str, Any]:
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
        if op not in HANDLERS:
            return _error(case_id, f"unknown op: {op}")
        output = HANDLERS[op](request["input"])
    except Exception as exc:
        return _error(case_id, f"bad input: {exc}")
    return {"case_id": case_id, "ok": True, "output": output}


def _configure_utf8(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def main() -> None:
    _configure_utf8(sys.stdin)
    _configure_utf8(sys.stdout)
    for line in sys.stdin:
        print(json.dumps(handle(line), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
