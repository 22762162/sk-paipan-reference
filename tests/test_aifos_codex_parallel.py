"""双 Codex 登录态分片：环境隔离、轮询分配与旧配置兼容。"""

import json

import pytest

from aifos.app import App
from aifos.errors import ProviderError
from aifos.production.external import CliProvider


def _profiles(tmp_path):
    home_a = tmp_path / "codex-a"
    home_b = tmp_path / "codex-b"
    home_a.mkdir()
    home_b.mkdir()
    return [
        {"id": "codex_a", "name": "Codex 账号 A", "enabled": True,
         "codex_home": str(home_a)},
        {"id": "codex_b", "name": "Codex 账号 B", "enabled": True,
         "codex_home": str(home_b)},
    ], home_a, home_b


def test_director_assigns_ready_profiles_round_robin(tmp_path):
    profiles, _home_a, _home_b = _profiles(tmp_path)
    app = App(tmp_path / "ws", config_overrides={
        "codex_profiles": profiles,
    })
    try:
        tasks = [
            {"capability": "image", "payload": {}, "item_id": "i1"},
            {"capability": "image", "payload": {}, "item_id": "i2"},
            {"capability": "frames", "payload": {}, "item_id": "i3"},
            {"capability": "video", "payload": {}, "item_id": "i4"},
        ]
        app.director._assign_codex_profiles(tasks)
        assert [task["payload"].get("_codex_profile") for task in tasks] == [
            "codex_a", "codex_b", "codex_a", None]
        assert [task.get("worker") for task in tasks[:3]] == [
            "codex:codex_a", "codex:codex_b", "codex:codex_a"]
    finally:
        app.close()


def test_director_skips_unavailable_profile_and_respects_strict_provider(
        tmp_path):
    profiles, _home_a, _home_b = _profiles(tmp_path)
    profiles[0]["enabled"] = False
    app = App(tmp_path / "ws", config_overrides={
        "codex_profiles": profiles,
    })
    try:
        tasks = [
            {"capability": "image", "payload": {
                "strict_provider": "image_api"}, "item_id": "api"},
            {"capability": "image", "payload": {}, "item_id": "codex"},
        ]
        app.director._assign_codex_profiles(tasks)
        assert "_codex_profile" not in tasks[0]["payload"]
        assert tasks[1]["payload"]["_codex_profile"] == "codex_b"
    finally:
        app.close()


def test_codex_cli_profile_sets_only_child_environment(monkeypatch, tmp_path):
    profiles, home_a, _home_b = _profiles(tmp_path)
    provider = CliProvider("codex", {
        "type": "cli", "enabled": True, "capabilities": ["image"],
        "command": ["codex"], "timeout": 30,
        "codex_profiles": profiles,
    })
    captured = {}

    def fake_run(name, command, input_text, timeout, cancel=None,
                 cwd=None, env=None):
        captured.update(name=name, command=command, input_text=input_text,
                        timeout=timeout, env=env)
        return 0, json.dumps({"ok": True, "data": {}, "uri": ""}), ""

    monkeypatch.setattr("aifos.production.external.run_interruptible",
                        fake_run)
    provider.generate("image", {"_codex_profile": "codex_a"}, tmp_path)
    assert captured["env"]["CODEX_HOME"] == str(home_a.resolve())
    assert captured["env"]["AIFOS_CODEX_PROFILE"] == "codex_a"
    assert captured["env"] is not __import__("os").environ


def test_codex_cli_profile_missing_home_fails_closed(tmp_path):
    provider = CliProvider("codex", {
        "type": "cli", "enabled": True, "capabilities": ["image"],
        "command": ["codex"], "timeout": 30,
        "codex_profiles": [{"id": "codex_a", "enabled": True,
                            "codex_home": str(tmp_path / "missing")}],
    })
    with pytest.raises(ProviderError, match="CODEX_HOME 不存在"):
        provider.generate("image", {"_codex_profile": "codex_a"}, tmp_path)
