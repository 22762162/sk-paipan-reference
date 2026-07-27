"""Codex 通道速度统计:落盘路径推导、聚合口径与 CliProvider 计时挂钩。"""

import json
import stat
import sys
import time
from pathlib import Path

import pytest

from aifos.channel_stats import (
    FILENAME, format_table, record, stats_path_for, summarize)
from aifos.errors import ProviderError
from aifos.production.external import CliProvider


def _workspace(tmp_path):
    out_dir = tmp_path / "workspace" / "artifacts" / "p001" / "e001" / "images"
    out_dir.mkdir(parents=True)
    return out_dir, tmp_path / "workspace" / "logs" / FILENAME


def test_stats_path_derived_from_artifacts_anchor(tmp_path):
    out_dir, stats_file = _workspace(tmp_path)
    assert stats_path_for(out_dir) == stats_file
    # 没有 artifacts 锚点的目录 → 放弃记录
    assert stats_path_for(tmp_path / "elsewhere") is None


def test_record_and_summarize_split_ok_and_fail(tmp_path):
    out_dir, stats_file = _workspace(tmp_path)
    record(out_dir, "codex", "codex_b", "image", 120.0, True)
    record(out_dir, "codex", "codex_b", "image", 60.0, True)
    record(out_dir, "codex", "codex_b", "image", 900.0, False,
           error="codex 调用超时(900s)")
    record(out_dir, "codex", "codex_c", "frames", 30.0, True)
    # 非图片能力与其他 Provider 默认不进"出图速度"口径
    record(out_dir, "codex", "codex_b", "image_qc", 10.0, True)
    record(out_dir, "seedream5_lite", "default", "image", 5.0, True)

    summary = summarize(stats_file.parent)
    channels = summary["channels"]
    assert channels["codex_b"]["completed"] == 2
    assert channels["codex_b"]["failed"] == 1
    assert channels["codex_b"]["avg_seconds"] == 90.0   # 失败不摊进平均
    assert channels["codex_b"]["per_minute"] == pytest.approx(0.67, abs=0.01)
    assert channels["codex_c"]["completed"] == 1
    # capabilities=None 时 image_qc 也计入
    all_caps = summarize(stats_file.parent, capabilities=None)
    assert all_caps["channels"]["codex_b"]["completed"] == 3
    assert "codex_b" in format_table(summary)


def test_summarize_hours_cutoff(tmp_path):
    out_dir, stats_file = _workspace(tmp_path)
    record(out_dir, "codex", "codex_b", "image", 50.0, True)
    old = json.loads(stats_file.read_text().splitlines()[0])
    old["ts"] = time.time() - 48 * 3600
    stats_file.write_text(
        json.dumps(old) + "\n" + stats_file.read_text().splitlines()[0] + "\n")
    summary = summarize(stats_file.parent, hours=24)
    assert summary["channels"]["codex_b"]["completed"] == 1


FAKE_BRIDGE_OK = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"ok": True, "data": {}, "uri": ""}))
'''

FAKE_BRIDGE_DOWN = '''#!/usr/bin/env python3
import sys
sys.stdin.read()
sys.exit(3)
'''


def _make_bridge(tmp_path, name, content):
    script = tmp_path / "bin" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(content, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _codex_provider(tmp_path, bridge, profile_home):
    return CliProvider("codex", {
        "type": "cli", "enabled": True,
        "capabilities": ["image", "frames", "cover"],
        "command": [sys.executable, str(bridge)],
        "codex_profiles": [
            {"id": "codex_b", "codex_home": str(profile_home)}],
        "timeout": 30,
    })


def test_cli_provider_records_codex_channel_timing(tmp_path):
    out_dir, stats_file = _workspace(tmp_path)
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    bridge = _make_bridge(tmp_path, "bridge_ok.py", FAKE_BRIDGE_OK)
    provider = _codex_provider(tmp_path, bridge, home_b)
    result = provider.generate(
        "image", {"_codex_profile": "codex_b"}, out_dir)
    assert result.provider == "codex"
    entry = json.loads(stats_file.read_text().splitlines()[-1])
    assert entry["provider"] == "codex"
    assert entry["profile"] == "codex_b"
    assert entry["capability"] == "image"
    assert entry["ok"] is True
    assert entry["seconds"] >= 0


def test_cli_provider_records_failure(tmp_path):
    out_dir, stats_file = _workspace(tmp_path)
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    bridge = _make_bridge(tmp_path, "bridge_down.py", FAKE_BRIDGE_DOWN)
    provider = _codex_provider(tmp_path, bridge, home_b)
    with pytest.raises(ProviderError):
        provider.generate("image", {"_codex_profile": "codex_b"}, out_dir)
    entry = json.loads(stats_file.read_text().splitlines()[-1])
    assert entry["ok"] is False
    assert "退出码" in entry["error"]


def test_non_codex_provider_not_recorded(tmp_path):
    out_dir, stats_file = _workspace(tmp_path)
    bridge = _make_bridge(tmp_path, "bridge_ok2.py", FAKE_BRIDGE_OK)
    provider = CliProvider("jimeng", {
        "type": "cli", "enabled": True, "capabilities": ["image"],
        "command": [sys.executable, str(bridge)], "timeout": 30})
    provider.generate("image", {}, out_dir)
    assert not stats_file.exists()
