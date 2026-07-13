"""年柱测试。期望值仅来自 spec 数学定义；立春时刻为合成占位数值。"""

import json
import random
import subprocess
import sys
from pathlib import Path

from paipan_ref.year_pillar import resolve_bazi_year, year_ganzhi, year_pillar

PKG_ROOT = Path(__file__).resolve().parents[1]


def test_anchor_1984_is_jiazi():
    assert year_ganzhi(1984) == (0, 0)
    assert year_pillar(1984, 1, 0)["ganzhi"] == "甲子"


def test_lichun_boundary_triplet():
    lichun = 1_000_000
    assert resolve_bazi_year(2000, lichun - 1, lichun) == 1999
    assert resolve_bazi_year(2000, lichun, lichun) == 2000
    assert resolve_bazi_year(2000, lichun + 1, lichun) == 2000


def test_negative_timestamps():
    lichun = -628_000_000
    assert resolve_bazi_year(1950, lichun - 1, lichun) == 1949
    assert resolve_bazi_year(1950, lichun, lichun) == 1950


def test_cycle_properties():
    rng = random.Random(20260712)  # 固定种子（testing-standards：禁止无种子随机）
    for _ in range(2000):
        y = rng.randint(-10_000, 10_000)
        assert year_ganzhi(y) == year_ganzhi(y + 60)
        s0, b0 = year_ganzhi(y)
        s1, b1 = year_ganzhi(y + 1)
        assert s1 == (s0 + 1) % 10
        assert b1 == (b0 + 1) % 12
        assert 0 <= s0 < 10 and 0 <= b0 < 12


def test_cli_rejects_non_object_case_line():
    # 合法 JSON 但非对象：协议要求 ok:false 且不崩溃（Codex 评审 P2-1）
    from paipan_ref.cli import handle

    for line in ("[]", "42", '"str"', "null"):
        result = handle(line)
        assert result["ok"] is False
        assert result["case_id"] is None


def test_cli_rejects_non_integer_field_types():
    # 类型不符须拒绝而非强转，保持与 Rust 侧行为一致（Codex 评审 P2-2）
    from paipan_ref.cli import handle

    def case(**inp):
        return json.dumps({"case_id": "x", "op": "year_pillar", "input": inp})

    bad_inputs = [
        {"civil_year": "2000", "t_unix": 0, "lichun_unix": 0},   # 字符串
        {"civil_year": 2000, "t_unix": 0.9, "lichun_unix": 0},   # 浮点
        {"civil_year": True, "t_unix": 0, "lichun_unix": 0},     # 布尔
        {"civil_year": 2000, "t_unix": 0, "lichun_unix": None},  # null
        {"civil_year": 2000, "t_unix": 0},                       # 缺字段
    ]
    for inp in bad_inputs:
        assert handle(case(**inp))["ok"] is False

    assert handle('{"case_id": "y", "op": "year_pillar", "input": []}')["ok"] is False


def test_cli_protocol_roundtrip():
    lines = [
        {"case_id": "a", "op": "year_pillar",
         "input": {"civil_year": 1984, "t_unix": 1, "lichun_unix": 0}},
        {"case_id": "b", "op": "no_such_op", "input": {}},
        {"case_id": "c", "op": "year_pillar", "input": {"civil_year": 2000}},
    ]
    payload = "\n".join(json.dumps(x) for x in lines) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "paipan_ref.cli"],
        cwd=PKG_ROOT, input=payload, capture_output=True, text=True, check=True,
    )
    out = [json.loads(l) for l in proc.stdout.splitlines()]
    assert [o["case_id"] for o in out] == ["a", "b", "c"]
    assert out[0]["ok"] and out[0]["output"]["ganzhi"] == "甲子"
    assert not out[1]["ok"] and "unknown op" in out[1]["error"]
    assert not out[2]["ok"]
