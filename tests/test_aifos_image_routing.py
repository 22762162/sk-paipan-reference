"""Seedream 5.0 Lite 参考图接入与图片成本分层回归。"""

import base64
import struct

import pytest

from aifos.app import App
from aifos.config import Config
from aifos.errors import ProviderError, ProviderUnavailable
from aifos.production import api_providers
from aifos.production.api_providers import (OpenAIImageProvider,
                                            SeedreamImageProvider)
from aifos.production.base import ProviderResult
from aifos.production.router import ProviderRouter


def _png(path, marker=b"0"):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker * 16)
    return str(path)


def _png_header(path, width, height):
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height))
    return str(path)


def _jpeg_header(path, width, height):
    path.write_bytes(
        b"\xff\xd8\xff\xc0"
        + struct.pack(">HBHHB", 8, 8, height, width, 3))
    return str(path)


def test_defaults_register_seedream_and_explicit_cost_routing(tmp_path):
    config = Config.load(tmp_path / "missing.json")
    seedream = config.get("providers", "seedream5_lite")
    assert seedream["type"] == "seedream_image"
    assert seedream["model"] == "doubao-seedream-5-0-lite-260128"
    assert seedream["cost_per_call"] == 0.22
    assert seedream["reference_images"] is True
    assert config.get("image_routing") == {
        "batch": ["seedream5_lite", "image_api"],
        "important": ["codex", "image_api"],
        "final": ["codex", "image_api"],
        "complex_text": ["codex", "image_api"],
    }
    app = App(tmp_path / "ws")
    try:
        assert isinstance(app.router.providers["seedream5_lite"],
                          SeedreamImageProvider)
    finally:
        app.close()


def test_router_class_policy_keeps_batch_cost_first_with_codex_emergency(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        router = app.router
        assert router._routing_chain("image", {
            "image_task_class": "batch"}) == [
                "seedream5_lite", "image_api", "codex", "mock"]
        for task_class in ("important", "final", "complex_text"):
            chain = router._routing_chain(
                "image", {"image_task_class": task_class})
            assert chain[:2] == ["codex", "image_api"]
        # 旧 payload 不强行改链，兼容已有工作区。
        assert router._routing_chain("image", {}) == \
            ["codex", "image_api", "api", "mock"]
        with pytest.raises(ProviderUnavailable, match="image_task_class"):
            router._routing_chain(
                "image", {"image_task_class": "expensive"})
    finally:
        app.close()


def test_openai_image_provider_rejects_two_to_one_panorama_before_network():
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-test",
    })
    with pytest.raises(ProviderError, match="不支持 2:1"):
        provider._size({"aspect": "2:1"})


def test_seedream_two_to_one_panorama_meets_real_api_minimum_pixels():
    provider = SeedreamImageProvider("seedream5_lite", {
        "type": "seedream_image", "enabled": True,
        "capabilities": ["image"], "api_key": "ark-test",
    })
    assert provider._size({"aspect": "2:1"}) == "2880x1440"


def test_router_rejects_square_panorama_and_accepts_strict_two_to_one(
        tmp_path):
    square = ProviderResult(
        provider="image_api", cost=1,
        uri=_png_header(tmp_path / "square.png", 1024, 1024))
    with pytest.raises(ProviderError, match="不是严格 2:1"):
        ProviderRouter._validate_generated_image_shape(
            "image", {"aspect": "2:1"}, square)

    panorama = ProviderResult(
        provider="seedream5_lite", cost=1,
        uri=_png_header(tmp_path / "panorama.png", 2560, 1280))
    ProviderRouter._validate_generated_image_shape(
        "image", {"aspect": "2:1"}, panorama)

    jpeg_in_png_path = ProviderResult(
        provider="seedream5_lite", cost=1,
        uri=_jpeg_header(tmp_path / "panorama_from_api.png", 2880, 1440))
    ProviderRouter._validate_generated_image_shape(
        "image", {"aspect": "2:1"}, jpeg_in_png_path)


def test_batch_fallback_is_seedream_then_gpt_then_codex_emergency(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "seedream5_lite": {"enabled": True, "api_key": "ark-test"},
            "image_api": {"enabled": True, "api_key": "openai-test"},
            "codex": {"enabled": True},
        },
    })
    calls = []
    try:
        seedream = app.router.providers["seedream5_lite"]
        image_api = app.router.providers["image_api"]
        codex = app.router.providers["codex"]
        seedream.available = image_api.available = codex.available = \
            lambda capability: (True, "")

        def seedream_fail(*args, **kwargs):
            calls.append("seedream5_lite")
            raise ProviderError("test failure")

        def image_fail(*args, **kwargs):
            calls.append("image_api")
            raise ProviderError("test failure")

        def codex_emergency(*args, **kwargs):
            calls.append("codex")
            return ProviderResult(provider="codex", cost=0, data={})

        seedream.generate = seedream_fail
        image_api.generate = image_fail
        codex.generate = codex_emergency
        result = app.router.call("image", {
            "shot_no": 1,
            "image_task_class": "batch",
            "image_quality": "high",
            "reference_images": [str(tmp_path / "identity.png")],
        }, app.workspace.artifacts_dir)
        assert calls == ["seedream5_lite", "image_api", "codex"]
        assert result.provider == "codex"
    finally:
        app.close()


def test_text_only_batch_skips_paid_image_apis_and_uses_codex(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "seedream5_lite": {"enabled": True, "api_key": "ark-test"},
            "image_api": {"enabled": True, "api_key": "openai-test"},
            "codex": {"enabled": True},
        },
    })
    calls = []
    try:
        for name in ("seedream5_lite", "image_api", "codex"):
            app.router.providers[name].available = lambda capability: (True, "")
        app.router.providers["seedream5_lite"].generate = \
            lambda *a, **k: calls.append("seedream5_lite")
        app.router.providers["image_api"].generate = \
            lambda *a, **k: calls.append("image_api")

        def codex(*args, **kwargs):
            calls.append("codex")
            return ProviderResult(provider="codex", cost=0, data={})

        app.router.providers["codex"].generate = codex
        result = app.router.call("image", {
            "shot_no": 1,
            "image_task_class": "batch",
            "image_quality": "medium",
        }, app.workspace.artifacts_dir)
        assert result.provider == "codex"
        assert calls == ["codex"]
        assert [item["provider"] for item in result.fallbacks[:2]] == [
            "seedream5_lite", "image_api"]
    finally:
        app.close()


def test_explicit_cold_start_can_fall_back_to_paid_text_to_image(tmp_path):
    """Fresh mother assets must not deadlock when Codex has no image tool."""
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "image_api": {"enabled": True, "api_key": "openai-test"},
            "codex": {"enabled": True},
            "mock": {"enabled": False},
        },
        "routing": {"image": ["codex", "image_api"]},
    })
    calls = []
    try:
        codex = app.router.providers["codex"]
        image_api = app.router.providers["image_api"]
        codex.available = image_api.available = \
            lambda capability: (True, "")

        def codex_fail(*args, **kwargs):
            calls.append("codex")
            raise ProviderError("nested Codex has no image_gen")

        def image_generate(*args, **kwargs):
            calls.append("image_api")
            return ProviderResult(provider="image_api", cost=1.12, data={})

        app.router._generate_via_codex_slot = codex_fail
        image_api.generate = image_generate
        result = app.router.call("image", {
            "portrait_candidate": True,
            "allow_text_to_image_bootstrap": True,
            "art_name": "cold_start",
            "prompt": "single character mother asset",
            "prompt_review_exempt": True,
            "image_task_class": "important",
            "image_quality": "high",
        }, app.workspace.artifacts_dir)
        assert result.provider == "image_api"
        assert calls == ["codex", "image_api"]
    finally:
        app.close()


def test_deterministic_codex_image_unavailable_is_circuit_broken(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "image_api": {"enabled": True, "api_key": "openai-test"},
            "codex": {"enabled": True},
            "mock": {"enabled": False},
        },
        "routing": {"image": ["codex", "image_api"]},
    })
    calls = []
    try:
        codex = app.router.providers["codex"]
        image_api = app.router.providers["image_api"]
        codex.available = image_api.available = \
            lambda capability: (True, "")

        def codex_fail(*args, **kwargs):
            calls.append("codex")
            raise ProviderError(
                "当前会话未提供可调用的内置 `image_gen` 图像生成能力")

        def image_generate(*args, **kwargs):
            calls.append("image_api")
            return ProviderResult(provider="image_api", cost=1.12, data={})

        app.router._generate_via_codex_slot = codex_fail
        image_api.generate = image_generate
        payload = {
            "portrait_candidate": True,
            "allow_text_to_image_bootstrap": True,
            "art_name": "cold_start",
            "prompt": "single character mother asset",
            "prompt_review_exempt": True,
            "image_task_class": "important",
            "image_quality": "high",
        }
        first = app.router.call(
            "image", dict(payload), app.workspace.artifacts_dir)
        second = app.router.call(
            "image", dict(payload), app.workspace.artifacts_dir)

        assert first.provider == second.provider == "image_api"
        assert calls == ["codex", "image_api", "image_api"]
        assert ("codex", "image") in app.router._capability_blocked
        assert second.fallbacks[0] == {
            "provider": "codex",
            "reason": "本服务进程已确认该产线缺少此生成能力",
        }
    finally:
        app.close()


@pytest.mark.parametrize(("task_class", "requested", "sent", "cost"), [
    ("batch", "high", "medium", 0.28),
    ("important", "high", "high", 1.12),
    ("final", "high", "high", 1.12),
    ("complex_text", "high", "high", 1.12),
    ("batch", "lite", "low", 0.07),
])
def test_gpt_quality_gate_allows_high_for_classified_important_assets(
        tmp_path, monkeypatch, task_class, requested, sent, cost):
    captured = {}

    def fake_request(name, url, headers, body=None, timeout=300, method=None):
        captured.update(body)
        return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-test",
        "model": "gpt-image-2", "cost_per_call": 0.28,
        "cost_by_quality": {"lite": 0.07, "medium": 0.28,
                            "high": 1.12},
    })
    result = provider.generate("image", {
        "portrait": True, "art_name": task_class, "prompt": "portrait",
        "image_task_class": task_class, "image_quality": requested,
    }, tmp_path / task_class)
    assert captured["quality"] == sent
    assert result.cost == cost
    assert result.data["model"] == "gpt-image-2"
    assert result.data["image_task_class"] == task_class
    assert result.data["image_quality"] == sent
    assert result.data["unit_cost"] == cost


def test_gpt_reference_edit_keeps_classified_important_high(tmp_path,
                                                             monkeypatch):
    ref = _png(tmp_path / "identity.png")
    captured = {}

    def fake_multipart(name, url, headers, fields, files, timeout):
        captured.update(fields)
        return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}

    monkeypatch.setattr(api_providers, "_multipart_post", fake_multipart)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-test",
    })
    provider.generate("image", {
        "portrait": True, "art_name": "important", "prompt": "portrait",
        "image_task_class": "important", "image_quality": "high",
        "character_refs": [ref], "require_reference_images": True,
    }, tmp_path / "edit")
    assert captured["quality"] == "high"


def test_seedream_uploads_ordered_identity_refs_and_forces_one_image(
        tmp_path, monkeypatch):
    lin = _png(tmp_path / "lin.png", b"1")
    ye = _png(tmp_path / "ye.png", b"2")
    captured = {}

    def fake_request(name, url, headers, body=None, timeout=300, method=None):
        captured.update({"name": name, "url": url, "headers": headers,
                         "body": body})
        return {"data": [{"b64_json": base64.b64encode(b"seedream").decode()}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    provider = SeedreamImageProvider("seedream5_lite", {
        "type": "seedream_image", "enabled": True,
        "capabilities": ["image"], "api_key": "ark-test",
        "model": "doubao-seedream-5-0-lite-260128",
        "reference_images": True, "cost_per_call": 0.22,
        "image_size": "2K", "watermark": False,
    })
    result = provider.generate("image", {
        "shot_no": 8, "characters": ["林昭", "叶晚"],
        "prompt": "FULL_EPISODE_PLOT_SENTINEL_整集剧情",
        "prompt_compact": "CURRENT_SHOT_SENTINEL_两人在办公室交谈",
        "prompt_contract_complete": True,
        "character_background": {
            "林昭": {"backstory": "FULL_BIO_SENTINEL_人物身世"}},
        "aspect": "9:16",
        "image_task_class": "batch", "image_quality": "medium",
        "identity_references": [
            {"actor_id": "P01", "character": "林昭", "uri": lin},
            {"actor_id": "P02", "character": "叶晚", "uri": ye},
        ],
        # 这里故意重复：最终立绘必须只上传一次。
        "character_refs": [lin, ye],
        "require_reference_images": True,
    }, tmp_path / "out")
    body = captured["body"]
    assert captured["url"].endswith("/api/v3/images/generations")
    assert captured["headers"]["Authorization"] == "Bearer ark-test"
    assert body["model"] == "doubao-seedream-5-0-lite-260128"
    assert body["size"] == "1440x2560"
    assert body["sequential_image_generation"] == "disabled"
    assert "sequential_image_generation_options" not in body
    assert body["response_format"] == "b64_json"
    assert body["watermark"] is False
    assert len(body["image"]) == 2
    assert all(value.startswith("data:image/png;base64,")
               for value in body["image"])
    assert "图1=P01/林昭最终立绘" in body["prompt"]
    assert "图2=P02/叶晚最终立绘" in body["prompt"]
    assert "最终成片不得画出" in body["prompt"]
    assert "CURRENT_SHOT_SENTINEL" in body["prompt"]
    assert "FULL_EPISODE_PLOT_SENTINEL" not in body["prompt"]
    assert "FULL_BIO_SENTINEL" not in body["prompt"]
    assert result.cost == 0.22
    assert result.model == "doubao-seedream-5-0-lite-260128"
    assert result.data["model"] == "doubao-seedream-5-0-lite-260128"
    assert result.data["image_task_class"] == "batch"
    assert result.data["image_quality"] == "medium"
    assert result.data["unit_cost"] == 0.22
    assert (tmp_path / "out" / "shot_008.keyframe.png").read_bytes() == \
        b"seedream"


def test_seedream_required_reference_never_degrades_to_text(tmp_path,
                                                             monkeypatch):
    called = False

    def fake_request(*args, **kwargs):
        nonlocal called
        called = True
        return {"data": [{"b64_json": ""}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    provider = SeedreamImageProvider("seedream5_lite", {
        "type": "seedream_image", "enabled": True,
        "capabilities": ["image"], "api_key": "ark-test",
        "reference_images": True,
    })
    with pytest.raises(ProviderError, match="禁止退化为纯文字"):
        provider.generate("image", {
            "portrait": True, "art_name": "missing", "prompt": "portrait",
            "image_task_class": "important", "image_quality": "high",
            "identity_references": [{
                "actor_id": "P01", "character": "林昭",
                "uri": str(tmp_path / "missing.png"),
            }],
            "require_reference_images": True,
        }, tmp_path / "out")
    assert called is False


def test_router_filters_provider_without_real_reference_support(tmp_path):
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "api": {"enabled": True, "endpoint": "http://unused"},
            "mock": {"enabled": False},
        },
        "routing": {"image": ["api"]},
    })
    ref = _png(tmp_path / "identity.png")
    called = False
    try:
        generic = app.router.providers["api"]
        generic.available = lambda capability: (True, "")

        def must_not_generate(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("reference_images=false 的 Provider 不得被调用")

        generic.generate = must_not_generate
        with pytest.raises(ProviderUnavailable):
            app.router.call("image", {
                "portrait": True, "art_name": "identity",
                "image_task_class": "important", "image_quality": "high",
                "character_refs": [ref],
                "require_reference_images": True,
            }, app.workspace.artifacts_dir)
        assert called is False
    finally:
        app.close()
