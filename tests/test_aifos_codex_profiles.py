"""Codex 双配置、安全设置与纯函数调度测试。"""

import json

import pytest

from aifos.app import App
from aifos.config import Config, assign_codex_task, codex_parallel_status
from aifos.errors import AifosError
from aifos.settings import set_codex_profiles, settings_payload


def _profiles():
    command = ["python3", "-m", "aifos.adapters.codex_image",
               "--codex", "codex"]
    return [
        {
            "id": "codex_a",
            "name": "codex-main",
            "codex_home": "~/.codex-main",
            "command": command,
            "enabled": True,
        },
        {
            "id": "codex_b",
            "name": "codex-alt",
            "codex_home": "~/.codex-alt",
            "command": command,
            "enabled": True,
        },
    ]


def test_legacy_single_codex_provider_populates_primary_profile(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "providers": {
            "codex": {
                "enabled": True,
                "codex_home": "~/.codex-legacy",
                "command": ["codex-wrapper", "--codex", "codex"],
            },
        },
    }), encoding="utf-8")

    config = Config.load(config_path)
    assert config.codex_profiles() == [{
        "id": "codex_a",
        "name": "Codex A",
        "codex_home": "~/.codex-legacy",
        "command": ["codex-wrapper", "--codex", "codex"],
        "enabled": True,
    }, {
        "id": "codex_b",
        "name": "Codex B",
        "codex_home": "~/.codex-account-b",
        "command": ["python3", "-m", "aifos.adapters.codex_image",
                    "--codex", "codex"],
        "enabled": False,
    }]


def test_set_two_profiles_persists_safe_fields_and_mirrors_primary(tmp_path):
    config_path = tmp_path / "config.json"
    written = set_codex_profiles(config_path, _profiles())

    assert [profile["name"] for profile in written] == [
        "codex-main", "codex-alt"]
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["codex_parallel"]["max_parallel"] == 2
    assert raw["codex_parallel"]["profiles"] == _profiles()
    assert raw["providers"]["codex"]["enabled"] is True
    assert raw["providers"]["codex"]["codex_home"] == "~/.codex-main"
    assert raw["providers"]["codex"]["command"] == _profiles()[0]["command"]
    assert Config.load(config_path).codex_profiles() == _profiles()


def test_profile_writer_rejects_auth_and_secret_command_args(tmp_path):
    config_path = tmp_path / "config.json"
    with pytest.raises(AifosError, match="认证字段"):
        set_codex_profiles(config_path, [{
            **_profiles()[0],
            "auth": {"token": "never-save-me"},
        }])
    with pytest.raises(AifosError, match="token/auth"):
        set_codex_profiles(config_path, [{
            **_profiles()[0],
            "command": ["codex", "--token", "never-save-me"],
        }])
    with pytest.raises(AifosError, match="认证文件"):
        set_codex_profiles(config_path, [{
            **_profiles()[0],
            "codex_home": "/tmp/codex/auth.json",
        }])
    assert not config_path.exists()


def test_empty_command_uses_safe_default_and_ids_remain_stable(tmp_path):
    config_path = tmp_path / "config.json"
    profiles = _profiles()
    profiles[1]["command"] = ""
    written = set_codex_profiles(config_path, profiles)
    assert written[1]["id"] == "codex_b"
    loaded = Config.load(config_path).codex_profiles()
    assert loaded[1]["command"] == [
        "python3", "-m", "aifos.adapters.codex_image",
        "--codex", "codex"]


def test_settings_payload_exposes_paths_but_never_reads_codex_auth(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(
        '{"token":"super-secret-auth-value"}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / "config.json"
    profiles = _profiles()
    profiles[0]["codex_home"] = str(home)
    set_codex_profiles(config_path, profiles)

    app = App(workspace)
    try:
        payload = settings_payload(app)
    finally:
        app.close()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["codex_profiles"][0]["codex_home"] == str(home)
    assert payload["codex_profiles"][0]["id"] == "codex_a"
    assert payload["codex_profiles"][0]["command"].startswith("python3 -m")
    assert payload["codex_profiles"][0]["status"] in {"ready", "missing"}
    assert payload["codex_profiles"][0]["assigned"] is False
    assert payload["codex_profiles"][0]["active_jobs"] == []
    assert payload["codex_parallel"]["configured_count"] == 2
    assert payload["codex_parallel"]["available_count"] == 2
    assert "super-secret-auth-value" not in serialized
    assert "auth.json" not in serialized


def test_assignment_is_idempotent_and_uses_each_profile_once():
    assignments = {}
    first = assign_codex_task(_profiles(), assignments, "task-1")
    assert first["profile_id"] == "codex_a"
    assert first["profile_name"] == "codex-main"
    assert assignments == {}

    second = assign_codex_task(
        _profiles(), first["assignments"], "task-2")
    assert second["profile_id"] == "codex_b"
    assert second["profile_name"] == "codex-alt"
    third = assign_codex_task(
        _profiles(), second["assignments"], "task-3")
    assert third["assigned"] is False
    assert third["reason"] == "all_busy"

    repeated = assign_codex_task(
        _profiles(), second["assignments"], "task-1")
    assert repeated["assigned"] is True
    assert repeated["reason"] == "already_assigned"
    assert repeated["profile_id"] == "codex_a"
    assert repeated["profile_name"] == "codex-main"

    status = codex_parallel_status(
        _profiles(), second["assignments"])
    assert status["active_count"] == 2
    assert status["available_count"] == 0
    assert [profile["state"] for profile in status["profiles"]] == [
        "busy", "busy"]


def test_assignment_skips_disabled_profile():
    profiles = _profiles()
    profiles[0]["enabled"] = False
    result = assign_codex_task(profiles, {}, "task-1")
    assert result["profile_id"] == "codex_b"
    assert result["profile_name"] == "codex-alt"
    status = codex_parallel_status(profiles, result["assignments"])
    assert [profile["state"] for profile in status["profiles"]] == [
        "disabled", "busy"]
