import struct
import zlib

import pytest

import aifos.image_media_qc as image_media_qc
from aifos.image_media_qc import (
    PROBE_SCHEMA,
    image_is_technically_usable,
    probe_image,
)


def _chunk(kind, payload):
    crc = zlib.crc32(kind)
    crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png(width, height, rgb=(20, 40, 60)):
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    pixels = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(pixels))
        + _chunk(b"IEND", b"")
    )


def test_valid_local_png_is_really_decoded_and_reports_geometry(tmp_path):
    path = tmp_path / "portrait.png"
    path.write_bytes(_png(90, 160))

    result = probe_image(path, expected_aspect="9:16")

    assert result["schema"] == PROBE_SCHEMA
    assert result["status"] == "passed"
    assert result["probed"] is True
    assert result["probe_ok"] is True
    assert result["decoded"] is True
    assert result["magic_format"] == "png"
    assert result["decoded_format"] == "png"
    assert result["width"] == 90
    assert result["height"] == 160
    assert result["aspect_matches"] is True
    assert image_is_technically_usable(result) is True


def test_empty_file_is_rejected_before_decode(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")

    result = probe_image(path)

    assert result["file_exists"] is True
    assert result["nonempty"] is False
    assert result["probe_ok"] is False
    assert result["error_code"] == "image_empty"


def test_signature_without_decodable_pixels_is_not_accepted(tmp_path):
    path = tmp_path / "fake.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not-an-image")

    result = probe_image(path)

    assert result["magic_format"] == "png"
    assert result["decoded"] is False
    assert result["error_code"] == "image_decode_failed"
    assert image_is_technically_usable(result) is False


def test_crc_corruption_fails_stdlib_real_decode_without_pillow(
        tmp_path, monkeypatch):
    path = tmp_path / "corrupt.png"
    payload = bytearray(_png(9, 16))
    payload[-8] ^= 0x01  # Corrupt IEND chunk type while retaining PNG magic.
    path.write_bytes(payload)
    monkeypatch.setattr(image_media_qc, "_PIL_IMAGE", None)

    result = probe_image(path)

    assert result["decode_backend"] == ""
    assert result["error_code"] == "image_decode_failed"
    assert "CRC" in result["error"]


def test_stdlib_png_fallback_decodes_pixels_and_checks_aspect(
        tmp_path, monkeypatch):
    path = tmp_path / "fallback.png"
    path.write_bytes(_png(90, 160))
    monkeypatch.setattr(image_media_qc, "_PIL_IMAGE", None)

    result = probe_image(path, expected_aspect="9:16")

    assert result["decode_backend"] == "stdlib_png"
    assert result["decoded"] is True
    assert result["aspect_matches"] is True
    assert result["probe_ok"] is True


def test_aspect_tolerance_is_relative_and_mismatch_is_structured(tmp_path):
    within = tmp_path / "within.png"
    outside = tmp_path / "outside.png"
    within.write_bytes(_png(92, 160))  # 2.22% wider than 9:16.
    outside.write_bytes(_png(96, 160))  # 6.67% wider than 9:16.

    accepted = probe_image(
        within, expected_aspect="9:16", aspect_tolerance=0.03)
    rejected = probe_image(
        outside, expected_aspect="9:16", aspect_tolerance=0.03)

    assert accepted["aspect_matches"] is True
    assert accepted["probe_ok"] is True
    assert rejected["aspect_matches"] is False
    assert rejected["probe_ok"] is False
    assert rejected["error_code"] == "aspect_ratio_mismatch"
    assert rejected["issues"][0]["expected"]["label"] == "9:16"
    assert rejected["issues"][0]["actual"]["relative_error"] == pytest.approx(
        1 / 15)


def test_wrong_extension_is_reported_but_magic_and_decode_are_authoritative(
        tmp_path):
    path = tmp_path / "provider-returned.jpg"
    path.write_bytes(_png(16, 9))

    result = probe_image(path, expected_aspect="16:9")

    assert result["probe_ok"] is True
    assert result["magic_format"] == "png"
    assert result["warnings"][0]["code"] == "extension_magic_mismatch"


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/frame.png",
    "http://cdn.example.com/frame.jpg",
])
def test_remote_url_requires_download_and_is_never_shape_accepted(url):
    result = probe_image(url, expected_aspect="9:16")

    assert result["status"] == "needs_download"
    assert result["needs_download"] is True
    assert result["probed"] is False
    assert result["probe_ok"] is False
    assert result["error_code"] == "remote_download_required"
    assert result["checks"][0]["required_action"] == "download_then_probe"


def test_missing_local_file_and_invalid_expected_aspect_are_explicit(tmp_path):
    missing = probe_image(tmp_path / "missing.png")
    assert missing["error_code"] == "source_missing"

    path = tmp_path / "valid.png"
    path.write_bytes(_png(9, 16))
    invalid = probe_image(path, expected_aspect="portrait")
    assert invalid["decoded"] is True
    assert invalid["error_code"] == "expected_aspect_invalid"


def test_without_pillow_non_png_header_is_not_enough_to_pass(
        tmp_path, monkeypatch):
    path = tmp_path / "header-only.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 20)
    monkeypatch.setattr(image_media_qc, "_PIL_IMAGE", None)
    monkeypatch.setattr(
        image_media_qc, "_decode_jpeg_with_ffmpeg",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("ffmpeg_unavailable")))

    result = probe_image(path)

    assert result["magic_format"] == "jpeg"
    assert result["decoded"] is False
    assert result["probe_ok"] is False
    assert result["error_code"] == "image_decoder_unavailable"


def test_without_pillow_valid_jpeg_uses_full_ffmpeg_decode(
        tmp_path, monkeypatch):
    path = tmp_path / "provider-output.png"
    # The provider historically wrote JPEG bytes under a .png extension.
    path.write_bytes(b"\xff\xd8\xff\xe0provider-jpeg-fixture")
    monkeypatch.setattr(image_media_qc, "_PIL_IMAGE", None)
    monkeypatch.setattr(
        image_media_qc, "_decode_jpeg_with_ffmpeg",
        lambda candidate, data: (
            "jpeg", 1080, 1920)
        if candidate == path and data.startswith(b"\xff\xd8\xff")
        else (_ for _ in ()).throw(ValueError("wrong fixture")))

    result = probe_image(path, expected_aspect="9:16")

    assert result["probe_ok"] is True
    assert result["decoded"] is True
    assert result["decode_backend"] == "ffmpeg"
    assert result["decoded_format"] == "jpeg"
    assert (result["width"], result["height"]) == (1080, 1920)
