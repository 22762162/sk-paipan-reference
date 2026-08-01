"""Provider output normalization: paid native images become formal 9:16."""

import base64
import struct
from pathlib import Path

import pytest

from aifos.errors import ProviderError
from aifos.production import api_providers
from aifos.production.api_providers import OpenAIImageProvider


def _png_header(width, height):
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"test-fixture")


def _fake_conversion(source, target, crop_box, target_size):
    assert Path(source).is_file()
    Path(target).write_bytes(_png_header(*target_size))
    return "test-converter"


def test_native_two_by_three_is_cropped_once_and_only_nine_by_sixteen_returns(
        tmp_path, monkeypatch):
    source = tmp_path / "shot_001.keyframe.png"
    source.write_bytes(_png_header(1024, 1536))
    monkeypatch.setattr(
        api_providers, "_run_image_conversion", _fake_conversion)

    metadata = api_providers._normalize_generated_image(
        source, {"aspect": "9:16"})

    assert api_providers._image_dimensions(source) == (1080, 1920)
    assert metadata["source_dimensions"] == {"width": 1024, "height": 1536}
    assert metadata["crop_box"] == {
        "x": 80, "y": 0, "width": 864, "height": 1536,
    }
    assert metadata["safe_area"]["retained_width_fraction"] == 0.84375
    assert metadata["safe_area"]["discard_left_fraction"] == 0.078125
    assert metadata["safe_area"]["discard_right_fraction"] == 0.078125
    original = Path(metadata["original_uri"])
    assert original.parent.name == ".provider-originals"
    assert api_providers._image_dimensions(original) == (1024, 1536)
    assert metadata["formal_uri"] == str(source)
    assert metadata["converter"] == "test-converter"


def test_provider_normalizes_locally_without_retrying_paid_api(
        tmp_path, monkeypatch):
    calls = []

    def fake_request(*_args, **_kwargs):
        calls.append("generation")
        return {"data": [{"b64_json": base64.b64encode(
            _png_header(1024, 1536)).decode()}]}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    monkeypatch.setattr(
        api_providers, "_run_image_conversion", _fake_conversion)
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-test",
        "model": "gpt-image-2",
    })

    result = provider.generate("image", {
        "shot_no": 1, "characters": ["甲"], "prompt": "甲站在室内",
        "aspect": "9:16",
    }, tmp_path / "out")

    assert calls == ["generation"]
    assert api_providers._image_dimensions(result.uri) == (1080, 1920)
    normalization = result.data["image_normalization"]
    assert normalization["applied"] is True
    assert Path(normalization["original_uri"]).is_file()


def test_openai_native_prompt_reserves_the_crop_safe_area():
    provider = OpenAIImageProvider("image_api", {"enabled": True})
    prompt = provider._semantic_prompt(
        "人物拿着关键道具", {"aspect": "9:16"}, [])

    assert "中央84.4%宽度内" in prompt
    assert "左右各约7.8%仅为可丢弃背景" in prompt
    assert "人物脸、手、关键道具和可读文字" in prompt


def test_local_normalization_failure_keeps_audit_source_and_no_formal_file(
        tmp_path, monkeypatch):
    source = tmp_path / "shot_002.keyframe.png"
    source.write_bytes(_png_header(1024, 1536))
    monkeypatch.setattr(api_providers, "_image_converter", lambda: ("", ""))

    with pytest.raises(ProviderError, match="禁止用同一提示词重复付费抽图"):
        api_providers._normalize_generated_image(
            source, {"aspect": "9:16"})

    assert not source.exists()
    originals = list((tmp_path / ".provider-originals").iterdir())
    assert len(originals) == 1
    assert api_providers._image_dimensions(originals[0]) == (1024, 1536)


def test_missing_converter_stops_before_paid_provider_call(
        tmp_path, monkeypatch):
    calls = []

    def fake_request(*_args, **_kwargs):
        calls.append("paid")
        return {"data": []}

    monkeypatch.setattr(api_providers, "_request_json", fake_request)
    monkeypatch.setattr(api_providers, "_image_converter", lambda: ("", ""))
    provider = OpenAIImageProvider("image_api", {
        "type": "image_api", "enabled": True,
        "capabilities": ["image"], "api_key": "sk-test",
    })

    with pytest.raises(ProviderError, match="调用图片 API 前停止"):
        provider.generate("image", {
            "shot_no": 4, "characters": [], "prompt": "空镜",
            "aspect": "9:16",
        }, tmp_path)

    assert calls == []
