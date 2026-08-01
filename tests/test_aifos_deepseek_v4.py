"""DeepSeek V4 Flash 配置迁移、请求合同与设置页离线测试。"""

import json
from pathlib import Path

import pytest

from aifos.app import App
from aifos.config import Config, is_official_deepseek_endpoint
from aifos.errors import AifosError
from aifos.production import api_providers
from aifos.production.api_providers import OpenAIChatProvider
from aifos.settings import settings_payload, update_provider


def _write_config(path, provider):
    path.write_text(json.dumps({"providers": {"deepseek": provider}}),
                    encoding="utf-8")


def test_defaults_register_flash_as_disabled_fallback(tmp_path):
    config = Config.load(tmp_path / "missing.json")
    provider = config.get("providers", "deepseek")
    assert provider["type"] == "openai_chat"
    assert provider["enabled"] is False
    assert provider["model"] == "deepseek-v4-flash"
    assert provider["thinking_mode"] == "disabled"
    assert provider["max_tokens"] == 32768
    assert config.get("routing", "script") == [
        "claude", "claude_api", "deepseek", "mock"]
    assert config.get("routing", "storyboard") == [
        "claude", "claude_api", "deepseek", "mock"]


@pytest.mark.parametrize(("legacy", "mode"), (
    ("deepseek-chat", "disabled"),
    ("deepseek-reasoner", "enabled"),
))
def test_saved_official_aliases_migrate_with_semantics(
        tmp_path, legacy, mode):
    path = tmp_path / "config.json"
    _write_config(path, {
        "type": "openai_chat",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "model": legacy,
        "max_tokens": 8192,
    })
    config = Config.load(path)
    assert config.get("providers", "deepseek", "model") == \
        "deepseek-v4-flash"
    assert config.get("providers", "deepseek", "thinking_mode") == mode
    assert config.get("providers", "deepseek", "max_tokens") == 32768


def test_custom_endpoint_and_explicit_overrides_are_not_migrated(tmp_path):
    path = tmp_path / "config.json"
    _write_config(path, {
        "type": "openai_chat", "endpoint": "https://gateway.example/v1",
        "model": "deepseek-chat", "max_tokens": 7000,
    })
    custom = Config.load(path)
    assert custom.get("providers", "deepseek", "model") == "deepseek-chat"
    assert custom.get("providers", "deepseek", "max_tokens") == 7000

    _write_config(path, {
        "type": "openai_chat", "endpoint": "https://api.deepseek.com",
        "model": "deepseek-chat", "max_tokens": 8192,
    })
    overridden = Config.load(path, overrides={"providers": {"deepseek": {
        "model": "deepseek-v4-pro", "thinking_mode": "enabled",
        "max_tokens": 12000,
    }}})
    assert overridden.get("providers", "deepseek", "model") == \
        "deepseek-v4-pro"
    assert overridden.get("providers", "deepseek", "thinking_mode") == \
        "enabled"
    assert overridden.get("providers", "deepseek", "max_tokens") == 12000


def test_custom_endpoint_does_not_inherit_deepseek_thinking(
        tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    _write_config(path, {
        "type": "openai_chat", "endpoint": "https://gateway.example/v1",
        "api_key": "secret", "model": "deepseek-chat",
    })
    config = Config.load(path)
    conf = config.get("providers", "deepseek")
    assert "thinking_mode" not in conf
    captured = {}

    def fake_request(name, url, headers, body=None, timeout=None, **kwargs):
        captured.update(url=url, body=body)
        return {"choices": [{"message": {"content": "pong"},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    provider = OpenAIChatProvider("deepseek", conf)
    assert provider._chat([{"role": "user", "content": "ping"}]) == "pong"
    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-chat"
    assert "thinking" not in captured["body"]


@pytest.mark.parametrize("endpoint", (
    "https://api.deepseek.com",
    "https://api.deepseek.com/",
    "https://api.deepseek.com/v1",
    "https://api.deepseek.com/chat/completions",
    "https://api.deepseek.com/v1/chat/completions",
))
def test_official_endpoint_variants(endpoint):
    assert is_official_deepseek_endpoint(endpoint)
    assert not is_official_deepseek_endpoint("https://gateway.example/v1")


def test_chat_request_maps_legacy_alias_and_preserves_non_thinking(
        monkeypatch):
    captured = {}

    def fake_request(name, url, headers, body=None, timeout=None, **kwargs):
        captured.update(url=url, body=body, headers=headers)
        return {"choices": [{"message": {"content": "pong"},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    provider = OpenAIChatProvider("deepseek", {
        "enabled": True, "capabilities": ["script"],
        "endpoint": "https://api.deepseek.com", "api_key": "secret",
        "model": "deepseek-chat", "temperature": 0.7,
    })
    assert provider._chat([{"role": "user", "content": "ping"}]) == "pong"
    assert captured["url"] == \
        "https://api.deepseek.com/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["temperature"] == pytest.approx(0.7)
    assert captured["body"]["max_tokens"] == 32768


def test_thinking_request_uses_full_url_and_omits_temperature(monkeypatch):
    captured = {}

    def fake_request(name, url, headers, body=None, timeout=None, **kwargs):
        captured.update(url=url, body=body)
        return {"choices": [{"message": {"content": "done"},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    provider = OpenAIChatProvider("deepseek", {
        "enabled": True, "capabilities": ["script"],
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "secret", "model": "deepseek-reasoner",
        "temperature": 0.9,
    })
    assert provider._chat([{"role": "user", "content": "solve"}]) == "done"
    assert captured["url"] == \
        "https://api.deepseek.com/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert "temperature" not in captured["body"]


def test_invalid_thinking_mode_fails_closed():
    provider = OpenAIChatProvider("deepseek", {
        "enabled": True, "capabilities": ["script"],
        "endpoint": "https://api.deepseek.com", "api_key": "secret",
        "model": "deepseek-v4-flash", "thinking_mode": "sometimes",
    })
    ok, reason = provider.available("script")
    assert not ok
    assert "thinking_mode" in reason


def test_settings_expose_api_without_leaking_key(tmp_path):
    app = App(tmp_path / "ws")
    config_path = app.workspace.config_path
    app.close()
    update_provider(config_path, "deepseek", {
        "api_key": "sk-deepseek-0123456789", "enabled": True,
        "model": "deepseek-v4-flash", "thinking_mode": "disabled",
    })
    app = App(tmp_path / "ws")
    try:
        payload = settings_payload(app)
    finally:
        app.close()
    row = next(item for item in payload["providers"]
               if item["name"] == "deepseek")
    assert row["label"] == "DeepSeek API · 编剧/分镜"
    assert row["mode"] == "API"
    assert row["model"] == "deepseek-v4-flash"
    assert row["thinking_mode"] == "disabled"
    assert row["api_key_set"] is True
    assert row["api_key_masked"] == "****6789"
    assert "sk-deepseek" not in json.dumps(payload)

    update_provider(config_path, "deepseek", {
        "api_key": "****6789", "thinking_mode": "enabled",
    })
    config = Config.load(config_path)
    assert config.get("providers", "deepseek", "api_key") == \
        "sk-deepseek-0123456789"
    assert config.get("providers", "deepseek", "thinking_mode") == "enabled"
    with pytest.raises(AifosError, match="thinking_mode"):
        update_provider(config_path, "deepseek", {
            "thinking_mode": "automatic"})


def test_web_card_recognizes_openai_chat_as_editable_api():
    source = Path("aifos/web/static/app.js").read_text(encoding="utf-8")
    assert '"openai_chat"].includes(p.type)' in source
    assert 'type="password"' in source
    assert 'data-field="model"' in source
    assert 'data-field="thinking_mode"' in source
