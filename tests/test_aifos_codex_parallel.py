"""Codex 多登录态分片：环境隔离、轮询分配与旧配置兼容。"""

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


def _three_profiles(tmp_path):
    profiles, home_a, home_b = _profiles(tmp_path)
    home_c = tmp_path / "codex-c"
    home_c.mkdir()
    profiles.append({
        "id": "codex_c", "name": "Codex 账号 C", "enabled": True,
        "codex_home": str(home_c),
    })
    return profiles, home_a, home_b, home_c


def test_director_assigns_ready_profiles_round_robin(tmp_path):
    profiles, _home_a, _home_b = _profiles(tmp_path)
    app = App(tmp_path / "ws", config_overrides={
        "codex_parallel": {"profiles": profiles},
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


def test_dual_codex_channels_each_contribute_eight_workers(tmp_path):
    profiles, _home_a, _home_b = _profiles(tmp_path)
    app = App(tmp_path / "ws", config_overrides={
        "defaults": {"parallel_images": 8},
        "codex_parallel": {"profiles": profiles},
    })
    try:
        assert app.director._parallel_workers() == 8
        assert app.director._total_image_workers() == 16
        status = app.config.codex_parallel_status()
        assert status["parallel_per_channel"] == 8
        assert status["total_parallel"] == 16
        assert app.router._codex_parallel_per_channel == 8
        assert set(app.router._codex_profile_slots) == {
            "codex_a", "codex_b"}
    finally:
        app.close()


def test_three_codex_channels_contribute_twenty_four_workers(tmp_path):
    profiles, _home_a, _home_b, _home_c = _three_profiles(tmp_path)
    app = App(tmp_path / "ws", config_overrides={
        "defaults": {"parallel_images": 8},
        "codex_parallel": {"max_parallel": 3, "profiles": profiles},
    })
    try:
        assert app.director._parallel_workers() == 8
        assert app.director._total_image_workers() == 24
        status = app.config.codex_parallel_status()
        assert status["enabled_count"] == 3
        assert status["total_parallel"] == 24
        assert set(app.router._codex_profile_slots) == {
            "codex_a", "codex_b", "codex_c"}
    finally:
        app.close()


def test_director_skips_unavailable_profile_and_respects_strict_provider(
        tmp_path):
    profiles, _home_a, _home_b = _profiles(tmp_path)
    profiles[0]["enabled"] = False
    app = App(tmp_path / "ws", config_overrides={
        "codex_parallel": {"profiles": profiles},
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


# ---- 通道溢出调度:B、C 先行,占满才用 A ----
def _overflow_app(tmp_path, parallel=1):
    profiles, _a, _b, _c = _three_profiles(tmp_path)
    by_id = {p["id"]: p for p in profiles}
    ordered = [by_id["codex_b"], by_id["codex_c"], by_id["codex_a"]]
    return App(tmp_path / "ws", config_overrides={
        "defaults": {"parallel_images": parallel},
        "codex_parallel": {"profiles": ordered},
    })


def test_router_slot_order_prefers_b_c_then_overflows_to_a(tmp_path):
    app = _overflow_app(tmp_path, parallel=1)
    try:
        router = app.router
        assert router._codex_profile_order == [
            "codex_b", "codex_c", "codex_a"]
        p1, s1 = router._acquire_codex_slot()
        p2, s2 = router._acquire_codex_slot()
        p3, s3 = router._acquire_codex_slot()
        assert [p1, p2, p3] == ["codex_b", "codex_c", "codex_a"]
        # C 释放后,新任务优先回填 C(B 仍满),而不是继续用 A
        s2.release()
        p4, s4 = router._acquire_codex_slot()
        assert p4 == "codex_c"
        for slot in (s1, s3, s4):
            slot.release()
    finally:
        app.close()


def test_router_slot_acquire_cancel_while_full(tmp_path):
    app = _overflow_app(tmp_path, parallel=1)
    try:
        router = app.router
        held = [router._acquire_codex_slot() for _ in range(3)]
        from aifos.errors import ProduceCancelled
        with pytest.raises(ProduceCancelled):
            router._acquire_codex_slot(cancel=lambda: True)
        for _pid, slot in held:
            slot.release()
    finally:
        app.close()


def test_router_generate_via_slot_rewrites_payload_profile(tmp_path):
    app = _overflow_app(tmp_path, parallel=1)
    try:
        router = app.router
        seen = []

        class _FakeProvider:
            def generate(self, capability, payload, out_dir, cancel=None):
                seen.append(payload.get("_codex_profile"))
                return "ok"

        # 静态分配写的是 codex_a,执行时应被改写为优先通道 codex_b
        payload = {"_codex_profile": "codex_a"}
        assert router._generate_via_codex_slot(
            _FakeProvider(), "image", payload, tmp_path) == "ok"
        assert seen == ["codex_b"]
        assert payload["_codex_profile"] == "codex_b"
        # 槽已释放:再次取用仍从 codex_b 开始
        pid, slot = router._acquire_codex_slot()
        assert pid == "codex_b"
        slot.release()
    finally:
        app.close()


def test_disabled_profile_excluded_from_slot_order(tmp_path):
    profiles, _a, _b, _c = _three_profiles(tmp_path)
    by_id = {p["id"]: p for p in profiles}
    by_id["codex_a"]["enabled"] = False
    ordered = [by_id["codex_b"], by_id["codex_c"], by_id["codex_a"]]
    app = App(tmp_path / "ws", config_overrides={
        "defaults": {"parallel_images": 1},
        "codex_parallel": {"profiles": ordered},
    })
    try:
        assert app.router._codex_profile_order == ["codex_b", "codex_c"]
    finally:
        app.close()
