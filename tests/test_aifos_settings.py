"""设置中心测试:Provider CLI/API 配置、能力路由、掩码与连通性测试。"""

import http.client
import json
import threading

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.config import Config
from aifos.errors import AifosError
from aifos.settings import test_provider as check_provider
from aifos.settings import (mask_key, set_routing, settings_payload,
                            update_provider)
from aifos.web.server import serve


def test_defaults_include_api_providers(tmp_path):
    app = App(tmp_path / "ws")
    try:
        providers = app.config.get("providers")
        for name, ptype in (("claude_api", "claude_api"),
                            ("image_api", "image_api"),
                            ("ark", "ark_video")):
            assert providers[name]["type"] == ptype
            assert name in app.router.providers
        assert app.config.get("providers", "claude_api", "model") == \
            "claude-opus-4-8"
        # 路由:CLI → API → mock
        assert app.config.get("routing", "script") == \
            ["claude", "claude_api", "mock"]
        assert app.config.get("routing", "video") == \
            ["jimeng", "ark", "api", "mock"]
        assert app.config.get("routing", "image") == \
            ["codex", "image_api", "api", "mock"]
    finally:
        app.close()


def test_config_load_migrates_only_builtin_legacy_say(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "providers": {
            "say": {
                "type": "cli", "enabled": True,
                "capabilities": ["voice"],
                "command": ["python3", "-m", "aifos.adapters.say_voice",
                            "--voice", "Tingting"],
                "cost_per_call": 0.0, "timeout": 120,
            },
        },
        "routing": {"voice": ["jimeng", "say", "api", "mock"]},
    }), encoding="utf-8")
    migrated = Config.load(config_path)
    assert "say" not in migrated.get("providers")
    assert migrated.get("routing", "voice") == ["doubao_tts", "api", "mock"]

    config_path.write_text(json.dumps({
        "providers": {
            "say": {
                "type": "cli", "enabled": True,
                "capabilities": ["voice"], "command": ["custom-say"],
            },
        },
        "routing": {"voice": ["say", "mock"]},
    }), encoding="utf-8")
    custom = Config.load(config_path)
    assert custom.get("providers", "say", "command") == ["custom-say"]
    assert custom.get("routing", "voice") == ["say", "mock"]


def test_settings_payload_masks_key(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {"claude_api": {"api_key": "sk-ant-api-0123456789"}}})
    try:
        view = settings_payload(app)
        row = next(p for p in view["providers"] if p["name"] == "claude_api")
        assert row["api_key_set"] is True
        assert row["api_key_masked"] == "****6789"
        assert "sk-ant" not in json.dumps(view)
        assert row["mode"] == "API"
        cli_row = next(p for p in view["providers"] if p["name"] == "codex")
        assert cli_row["mode"] == "CLI"
        assert "codex" in cli_row["command"]
    finally:
        app.close()
    assert mask_key("") == ""
    assert mask_key("short") == "****"


def test_update_provider_and_masked_key_kept(tmp_path):
    app = App(tmp_path / "ws")
    config_path = app.workspace.config_path
    app.close()
    written = update_provider(config_path, "claude_api", {
        "enabled": True, "api_key": "sk-live-8888", "model": "claude-opus-4-8",
        "endpoint": "https://api.anthropic.com"})
    assert written["enabled"] is True and written["api_key"] == "sk-live-8888"
    conf = Config.load(config_path)
    assert conf.get("providers", "claude_api", "enabled") is True
    assert conf.get("providers", "claude_api", "api_key") == "sk-live-8888"
    # 掩码回传 → 保留原 key;enabled 字符串也能解析
    update_provider(config_path, "claude_api",
                    {"api_key": "****8888", "enabled": "false"})
    conf = Config.load(config_path)
    assert conf.get("providers", "claude_api", "api_key") == "sk-live-8888"
    assert conf.get("providers", "claude_api", "enabled") is False
    # command 字符串 → 拆分列表
    update_provider(config_path, "claude", {
        "command": "python3 -m aifos.adapters.claude_script "
                   "--claude /usr/local/bin/claude"})
    conf = Config.load(config_path)
    assert conf.get("providers", "claude", "command")[-1] == \
        "/usr/local/bin/claude"
    update_provider(config_path, "jimeng", {"audio_in_video": "false"})
    conf = Config.load(config_path)
    assert conf.get("providers", "jimeng", "audio_in_video") is False
    with pytest.raises(AifosError):
        update_provider(config_path, "不存在", {"enabled": True})
    with pytest.raises(AifosError):
        update_provider(config_path, "claude_api", {"type": "mock"})


def test_update_provider_key_auto_enables(tmp_path):
    """只填 Key 不用再点启用:保存 Key 即自动 enabled=true。"""
    app = App(tmp_path / "ws")
    config_path = app.workspace.config_path
    app.close()
    written = update_provider(config_path, "image_api",
                              {"api_key": "sk-only-key"})
    assert written["enabled"] is True
    conf = Config.load(config_path)
    assert conf.get("providers", "image_api", "enabled") is True
    # 显式给了 enabled=False 则尊重用户选择,不强行启用
    update_provider(config_path, "image_api",
                    {"api_key": "sk-other", "enabled": False})
    conf = Config.load(config_path)
    assert conf.get("providers", "image_api", "enabled") is False


def test_set_routing_validation(tmp_path):
    app = App(tmp_path / "ws")
    config_path = app.workspace.config_path
    app.close()
    set_routing(config_path, "video", ["ark", "jimeng", "mock"])
    assert Config.load(config_path).get("routing", "video") == \
        ["ark", "jimeng", "mock"]
    with pytest.raises(AifosError):
        set_routing(config_path, "video", ["没有这个"])
    with pytest.raises(AifosError):
        set_routing(config_path, "没有这个能力", ["mock"])
    with pytest.raises(AifosError):
        set_routing(config_path, "video", [])


def test_check_provider(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {"ark": {"enabled": True}}})
    try:
        report = check_provider(app, "mock")
        assert report["ok"] is True
        # ark 启用但没配 key → 明确报出原因
        report = check_provider(app, "ark")
        assert report["ok"] is False
        assert "api_key" in report["results"][0]["reason"]
        with pytest.raises(AifosError):
            check_provider(app, "不存在")
    finally:
        app.close()


def test_check_provider_credit_failure_is_connection_failure(tmp_path):
    dreamina = tmp_path / "dreamina"
    dreamina.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    dreamina.chmod(0o755)
    app = App(tmp_path / "ws", config_overrides={
        "providers": {"jimeng": {
            "enabled": True, "command": [str(dreamina)],
        }},
    })
    try:
        report = check_provider(app, "jimeng")
        assert report["ok"] is False
        assert report["results"][0]["ok"] is True
        assert "余额查询失败" in report["extra"]
    finally:
        app.close()


def test_cli_config_commands(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "init"]) == 0
    capsys.readouterr()
    # set:启用 + key
    assert main(["--workspace", ws, "config", "set",
                 "--provider", "claude_api", "--enable",
                 "--api-key", "sk-cli-4321"]) == 0
    out = capsys.readouterr().out
    assert "claude_api" in out and "sk-cli-4321" not in out
    # list:掩码显示
    assert main(["--workspace", ws, "config", "list"]) == 0
    out = capsys.readouterr().out
    assert "****4321" in out and "sk-cli-4321" not in out
    assert "claude_api" in out and "script" in out
    # route
    assert main(["--workspace", ws, "config", "route",
                 "--capability", "video", "--chain", "ark,jimeng,mock"]) == 0
    capsys.readouterr()
    assert main(["--workspace", ws, "config", "route",
                 "--capability", "video", "--chain", "nope"]) == 1
    capsys.readouterr()
    # test:mock 无需外联 → 通过;claude_api 假 Key 真实 ping 必失败
    assert main(["--workspace", ws, "config", "test",
                 "--provider", "mock"]) == 0
    assert "✓" in capsys.readouterr().out
    assert main(["--workspace", ws, "config", "test",
                 "--provider", "claude_api"]) == 1
    assert "✗" in capsys.readouterr().out
    assert main(["--workspace", ws, "config", "set",
                 "--provider", "jimeng", "--enable", "--command",
                 str(tmp_path / "missing-dreamina")]) == 0
    capsys.readouterr()
    assert main(["--workspace", ws, "config", "test",
                 "--provider", "jimeng"]) == 1
    assert "✗" in capsys.readouterr().out


def test_web_settings_endpoints(tmp_path):
    ws = tmp_path / "ws"
    App(ws).close()
    httpd = serve(ws, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=30)

        def post(path, body):
            conn.request("POST", path, body=json.dumps(body),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))

        conn.request("GET", "/api/settings")
        resp = conn.getresponse()
        view = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        names = [p["name"] for p in view["providers"]]
        assert {"claude", "claude_api", "image_api", "ark",
                "jimeng", "mock"} <= set(names)
        assert view["routing"]["script"] == ["claude", "claude_api", "mock"]

        status, fresh = post("/api/settings", {
            "provider": "claude_api",
            "fields": {"enabled": True, "api_key": "sk-web-9999"}})
        assert status == 200
        row = next(p for p in fresh["providers"]
                   if p["name"] == "claude_api")
        assert row["enabled"] and row["api_key_masked"] == "****9999"
        assert "sk-web-9999" not in json.dumps(fresh)

        status, fresh = post("/api/settings", {
            "capability": "video", "chain": "ark, jimeng, mock"})
        assert status == 200
        assert fresh["routing"]["video"] == ["ark", "jimeng", "mock"]

        # 假 Key 会发真实 ping → 必须明确报失败(而不是显示配置正常)
        status, report = post("/api/settings/test",
                              {"provider": "claude_api"})
        assert status == 200 and report["ok"] is False
        assert report["extra"] and "✗" in report["extra"]
        status, _ = post("/api/settings", {"provider": "不存在",
                                           "fields": {"enabled": True}})
        assert status == 400
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_config_load_migrates_oldest_say_type(tmp_path):
    """最早一代内置 say(type="say")也要迁移:否则 router 每次请求
    都告警"未知 provider 类型: say"刷爆产线日志。"""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "providers": {
            "say": {"type": "say", "enabled": True,
                    "capabilities": ["voice"]},
        },
        "routing": {"voice": ["say", "doubao_tts", "mock"],
                    "edit": ["say"]},
    }), encoding="utf-8")
    migrated = Config.load(config_path)
    assert "say" not in migrated.get("providers")
    assert migrated.get("routing", "voice") == ["doubao_tts", "mock"]
    # 链清空时落回默认,产线不断链
    assert migrated.get("routing", "edit") == ["jianying", "mock"]


def _fake_bin(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_test_provider_diagnoses_disabled_cli(tmp_path, monkeypatch):
    """未启用的 CLI 也要能诊断:命令就绪则报通过并标记 disabled,
    不改配置文件——前端据此顺手打开「启用」,而不是卡在“未启用”。"""
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    claude = _fake_bin(tmp_path / "bin", "claude")
    app = App(tmp_path / "ws")
    try:
        update_provider(app.workspace.config_path, "claude", {
            "command": ["python3", "-m", "aifos.adapters.claude_script",
                        "--claude", str(claude)]})
        app2 = App(tmp_path / "ws")
        try:
            report = check_provider(app2, "claude")
            assert report["ok"] and report["disabled"]
            assert all(r["ok"] for r in report["results"])
            # 诊断只在内存里临时启用,不落盘
            conf = Config.load(app2.workspace.config_path)
            assert conf.get("providers", "claude", "enabled") in (False, None)
        finally:
            app2.close()
    finally:
        app.close()


def test_test_provider_reports_missing_bridge_binary(tmp_path, monkeypatch):
    """桥接命令(python3 -m …claude_script --claude X)诊断要核验真正的 X,
    而不是只查到启动器 python3 就误报就绪。"""
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    _fake_bin(tmp_path / "bin", "python3")   # 只有启动器,没有 claude
    app = App(tmp_path / "ws")
    try:
        update_provider(app.workspace.config_path, "claude", {
            "command": ["python3", "-m", "aifos.adapters.claude_script",
                        "--claude", "/no/such/claude"]})
        app2 = App(tmp_path / "ws")
        try:
            report = check_provider(app2, "claude")
            assert report["ok"] is False and report["disabled"]
            assert any("/no/such/claude" in r["reason"]
                       for r in report["results"])
        finally:
            app2.close()
    finally:
        app.close()
