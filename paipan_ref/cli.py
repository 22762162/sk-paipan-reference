"""对拍 CLI：stdin JSONL → stdout JSONL（spec 附录 A）。

单行错误 → 该行 ok:false，继续处理后续行，进程不崩溃。
"""

import json
import sys

from .year_pillar import year_pillar


def _require_int(inp: dict, key: str) -> int:
    """取字段并校验为 JSON 整数；bool 是 int 子类型，须显式排除（协议：类型不符 → ok:false）。"""
    value = inp.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field {key!r} must be a JSON integer, got {type(value).__name__}")
    return value


def handle(line: str) -> dict:
    try:
        case = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"case_id": None, "ok": False, "error": f"bad case line: {exc}"}
    if not isinstance(case, dict):
        return {"case_id": None, "ok": False, "error": "bad case line: not a JSON object"}
    case_id = case.get("case_id")
    op = case.get("op")
    if op != "year_pillar":
        return {"case_id": case_id, "ok": False, "error": f"unknown op: {op}"}
    try:
        inp = case["input"]
        if not isinstance(inp, dict):
            raise ValueError("input must be a JSON object")
        out = year_pillar(
            _require_int(inp, "civil_year"),
            _require_int(inp, "t_unix"),
            _require_int(inp, "lichun_unix"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return {"case_id": case_id, "ok": False, "error": f"bad input: {exc!r}"}
    return {"case_id": case_id, "ok": True, "output": out}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        print(json.dumps(handle(line), ensure_ascii=False))


if __name__ == "__main__":
    main()
