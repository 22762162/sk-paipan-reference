"""非契约盲写扩展 JSONL 协议测试。"""

import json
from pathlib import Path
import subprocess
import sys

from paipan_ref.blind_cli import handle

REPO_ROOT = Path(__file__).resolve().parents[1]


def chart_a() -> dict[str, dict[str, str]]:
    return {
        "year": {"stem": "甲", "branch": "子", "ganzhi": "甲子"},
        "month": {"stem": "丙", "branch": "寅", "ganzhi": "丙寅"},
        "day": {"stem": "甲", "branch": "午", "ganzhi": "甲午"},
        "hour": {"stem": "乙", "branch": "卯", "ganzhi": "乙卯"},
    }


def chart_b() -> dict[str, dict[str, str]]:
    return {
        "year": {"stem": "庚", "branch": "午"},
        "month": {"stem": "壬", "branch": "申"},
        "day": {"stem": "庚", "branch": "子"},
        "hour": {"stem": "辛", "branch": "酉"},
    }


def case(op: str, inp: object, case_id: str = "x") -> str:
    return json.dumps({"case_id": case_id, "op": op, "input": inp}, ensure_ascii=False)


def test_all_three_ops_succeed() -> None:
    dayun = handle(case("dayun", {
        "year_stem": "甲", "sex": "male",
        "month_pillar": {"stem": "丙", "branch": "寅", "ganzhi": "丙寅"},
        "birth_unix": 0, "previous_jie_unix": -86_400,
        "next_jie_unix": 259_200, "count": 2,
    }, "d"))
    assert dayun["ok"] is True
    assert dayun["output"]["periods"][0]["pillar"]["ganzhi"] == "丁卯"

    stars = handle(case("shensha", {"chart": chart_a(), "wenchang_variant": "zi"}, "s"))
    assert stars["ok"] is True
    assert "桃花" in stars["output"]["matched_stars"]

    pair = handle(case("pair_relations", {"chart_a": chart_a(), "chart_b": chart_b()}, "p"))
    assert pair["ok"] is True
    assert len(pair["output"]["branch_matrix"]) == 16


def test_unknown_op_bad_types_missing_and_extra_fields_are_line_errors() -> None:
    assert handle(case("unknown", {}))["ok"] is False
    malformed_inputs = [
        {"case_id": "x", "op": "dayun"},
        {"case_id": "x", "op": "shensha", "input": [], "extra": 1},
        {"case_id": 1, "op": "pair_relations", "input": {}},
    ]
    for value in malformed_inputs:
        assert handle(json.dumps(value))["ok"] is False


def test_dayun_rejects_boolean_integer_and_extra_input() -> None:
    inp = {
        "year_stem": "甲", "sex": "male",
        "month_pillar": {"stem": "丙", "branch": "寅"},
        "birth_unix": 0, "previous_jie_unix": -1,
        "next_jie_unix": 1, "count": True,
    }
    assert handle(case("dayun", inp))["ok"] is False
    inp["count"] = 8
    inp["timezone"] = "UTC"
    assert handle(case("dayun", inp))["ok"] is False


def test_process_survives_bad_lines_and_preserves_order() -> None:
    lines = [
        case("shensha", {"chart": chart_a(), "wenchang_variant": "zi"}, "one"),
        "{bad-json",
        case("unknown", {}, "two"),
        case("pair_relations", {"chart_a": chart_a(), "chart_b": chart_b()}, "three"),
    ]
    process = subprocess.run(
        [sys.executable, "-m", "paipan_ref.blind_cli"],
        cwd=REPO_ROOT,
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    output = [json.loads(line) for line in process.stdout.splitlines()]
    assert [item["case_id"] for item in output] == ["one", None, "two", "three"]
    assert [item["ok"] for item in output] == [True, False, False, True]
    assert process.stderr == ""
