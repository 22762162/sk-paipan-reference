"""附录 A JSONL 协议与 schema 错误路径。"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from paipan_ref.cli import handle

REPO_ROOT = Path(__file__).resolve().parents[1]


def valid_year_case() -> dict[str, object]:
    return {
        "case_id": "yp",
        "op": "year_pillar",
        "input": {"civil_year": 1984, "t_unix": 1, "lichun_unix": 0},
    }


def valid_four_case() -> dict[str, object]:
    return {
        "case_id": "fp",
        "op": "four_pillars",
        "input": {
            "t_unix": 100,
            "lichun_unix": 50,
            "local": {"y": 2000, "m": 6, "d": 1, "hh": 8, "mm": 30, "ss": 0},
            "month_ctx": {"jie_seq": 3, "jie_unix": 90, "next_jie_unix": 200},
            "zi_hour_mode": "split",
        },
    }


def encode(case: object) -> str:
    return json.dumps(case, ensure_ascii=False)


def test_cli_success_outputs_match_both_op_contracts() -> None:
    year = handle(encode(valid_year_case()))
    assert year == {
        "case_id": "yp",
        "ok": True,
        "output": {
            "bazi_year": 1984,
            "stem": "甲",
            "branch": "子",
            "ganzhi": "甲子",
        },
    }

    four = handle(encode(valid_four_case()))
    assert four["case_id"] == "fp"
    assert four["ok"] is True
    assert four["output"]["month"]["ganzhi"] == "辛巳"
    assert four["output"]["day"]["ganzhi"] == "庚寅"


@pytest.mark.parametrize("case", [[], 42, "text", None, True])
def test_cli_rejects_non_object_case_lines(case: object) -> None:
    response = handle(encode(case))
    assert response["case_id"] is None
    assert response["ok"] is False


@pytest.mark.parametrize("line", ["", "{", "NaN", "Infinity", "-Infinity"])
def test_cli_rejects_invalid_or_nonstandard_json(line: str) -> None:
    response = handle(line)
    assert response["case_id"] is None
    assert response["ok"] is False


def test_cli_rejects_unknown_op_missing_fields_and_wrong_request_types() -> None:
    unknown = valid_year_case()
    unknown["op"] = "no_such_op"
    response = handle(encode(unknown))
    assert response["ok"] is False
    assert response["error"] == "unknown op: no_such_op"

    bad_cases = []
    for missing in ("case_id", "op", "input"):
        case = valid_year_case()
        del case[missing]
        bad_cases.append(case)
    bad_cases.extend(
        [
            {**valid_year_case(), "case_id": 1},
            {**valid_year_case(), "op": False},
            {**valid_year_case(), "input": []},
            {**valid_year_case(), "extra": 1},
        ]
    )
    for case in bad_cases:
        assert handle(encode(case))["ok"] is False


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", None, [], {}])
def test_cli_year_op_rejects_non_integer_fields(bad: object) -> None:
    for key in ("civil_year", "t_unix", "lichun_unix"):
        case = valid_year_case()
        case["input"][key] = bad  # type: ignore[index]
        assert handle(encode(case))["ok"] is False


def test_cli_year_op_rejects_missing_and_extra_input_fields() -> None:
    missing = valid_year_case()
    del missing["input"]["t_unix"]  # type: ignore[index]
    extra = valid_year_case()
    extra["input"]["timezone"] = "UTC"  # type: ignore[index]
    assert handle(encode(missing))["ok"] is False
    assert handle(encode(extra))["ok"] is False


INTEGER_PATHS = (
    ("t_unix",),
    ("lichun_unix",),
    ("local", "y"),
    ("local", "m"),
    ("local", "d"),
    ("local", "hh"),
    ("local", "mm"),
    ("local", "ss"),
    ("month_ctx", "jie_seq"),
    ("month_ctx", "jie_unix"),
    ("month_ctx", "next_jie_unix"),
)


@pytest.mark.parametrize("path", INTEGER_PATHS)
@pytest.mark.parametrize("bad", [True, 1.0])
def test_cli_four_op_rejects_boolean_and_float_in_every_integer_field(
    path: tuple[str, ...], bad: object
) -> None:
    case = valid_four_case()
    target = case["input"]
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = bad  # type: ignore[index]
    assert handle(encode(case))["ok"] is False


def test_cli_four_op_rejects_nested_shape_and_value_errors() -> None:
    cases = []

    missing_outer = valid_four_case()
    del missing_outer["input"]["local"]  # type: ignore[index]
    cases.append(missing_outer)

    extra_outer = valid_four_case()
    extra_outer["input"]["civil_year"] = 2000  # type: ignore[index]
    cases.append(extra_outer)

    extra_local = valid_four_case()
    extra_local["input"]["local"]["tz"] = "UTC"  # type: ignore[index]
    cases.append(extra_local)

    extra_context = valid_four_case()
    extra_context["input"]["month_ctx"]["name"] = "synthetic"  # type: ignore[index]
    cases.append(extra_context)

    invalid_date = valid_four_case()
    invalid_date["input"]["local"]["d"] = 31  # type: ignore[index]
    invalid_date["input"]["local"]["m"] = 6  # type: ignore[index]
    cases.append(invalid_date)

    invalid_hour = valid_four_case()
    invalid_hour["input"]["local"]["hh"] = 24  # type: ignore[index]
    cases.append(invalid_hour)

    invalid_context = valid_four_case()
    invalid_context["input"]["month_ctx"]["next_jie_unix"] = 100  # type: ignore[index]
    cases.append(invalid_context)

    invalid_mode = valid_four_case()
    invalid_mode["input"]["zi_hour_mode"] = "other"  # type: ignore[index]
    cases.append(invalid_mode)

    for case in cases:
        assert handle(encode(case))["ok"] is False


def test_cli_does_not_mutate_input_objects() -> None:
    case = valid_four_case()
    original = deepcopy(case)
    assert handle(encode(case))["ok"] is True
    assert case == original


def test_jsonl_process_survives_bad_lines_and_preserves_line_order() -> None:
    lines = [
        encode(valid_year_case()),
        "{bad-json",
        encode({"case_id": "unknown", "op": "xxx", "input": {}}),
        encode(valid_four_case()),
        encode({"case_id": "missing", "op": "year_pillar", "input": {}}),
    ]
    process = subprocess.run(
        [sys.executable, "-m", "paipan_ref.cli"],
        cwd=REPO_ROOT,
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    output = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(output) == len(lines)
    assert [item["case_id"] for item in output] == ["yp", None, "unknown", "fp", "missing"]
    assert [item["ok"] for item in output] == [True, False, False, True, False]
    assert process.stderr == ""
