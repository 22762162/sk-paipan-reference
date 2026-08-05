"""Deterministic, content-neutral technical QC for generated images.

This module answers only whether an image asset is a real, locally decoded
file with usable geometry and the requested aspect ratio.  It deliberately
does not score composition, identity, continuity, style, or aesthetics.

Remote URLs are never accepted by their spelling.  Callers must first
download them into managed storage and then probe the local file.
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse


PROBE_SCHEMA = "aifos.image-media-probe/v1"
DEFAULT_ASPECT_TOLERANCE = 0.03
_MAX_STDLIB_DECODE_BYTES = 256 * 1024 * 1024

try:  # Pillow is optional; PNG still has a strict stdlib decoder fallback.
    from PIL import Image as _PIL_IMAGE
except (ImportError, OSError):  # pragma: no cover - environment dependent
    _PIL_IMAGE = None


def _is_remote(source: Any) -> bool:
    parsed = urlparse(str(source or ""))
    return parsed.scheme.lower() in {"http", "https"}


def _issue(code: str, message: str, *, expected: Any = None,
           actual: Any = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "check": "image_technical",
        "code": code,
        "severity": "error",
        "rerunnable": True,
        "message": message,
    }
    if expected is not None:
        item["expected"] = expected
    if actual is not None:
        item["actual"] = actual
    return item


def _warning(code: str, message: str, *, actual: Any = None
             ) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "check": "image_technical",
        "code": code,
        "severity": "warning",
        "message": message,
    }
    if actual is not None:
        item["actual"] = actual
    return item


def _base_result(source: Any) -> Dict[str, Any]:
    return {
        "schema": PROBE_SCHEMA,
        "source": str(source or ""),
        "is_remote": _is_remote(source),
        "status": "failed",
        "needs_download": False,
        "probed": False,
        "probe_ok": False,
        "ok": False,
        "file_exists": False,
        "nonempty": False,
        "size_bytes": None,
        "magic_format": "",
        "decoded_format": "",
        "decode_backend": "",
        "decoded": False,
        "width": None,
        "height": None,
        "aspect_ratio": None,
        "expected_aspect": None,
        "aspect_matches": None,
        "checks": [],
        "issues": [],
        "warnings": [],
        "error_code": "",
        "error": "",
    }


def _fail(result: Dict[str, Any], code: str, message: str, *,
          expected: Any = None, actual: Any = None) -> Dict[str, Any]:
    result["issues"].append(
        _issue(code, message, expected=expected, actual=actual))
    result["error_code"] = code
    result["error"] = message
    result["status"] = "failed"
    result["probe_ok"] = False
    result["ok"] = False
    return result


def _magic_format(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if (len(data) >= 12 and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"):
        return "webp"
    return ""


def _pillow_decode(data: bytes) -> Tuple[str, int, int]:
    if _PIL_IMAGE is None:
        raise RuntimeError("pillow_unavailable")
    with _PIL_IMAGE.open(io.BytesIO(data)) as image:
        decoded_format = str(image.format or "").strip().lower()
        # load(), unlike inspecting the header, forces pixel decompression.
        image.load()
        width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("decoded image has invalid geometry")
    return decoded_format, int(width), int(height)


def _png_stdlib_decode(data: bytes) -> Tuple[int, int]:
    """Fully inflate and validate a non-interlaced PNG using stdlib only."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG signature missing")
    offset = 8
    chunks: List[Tuple[bytes, bytes]] = []
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk payload")
        payload = data[offset + 8:offset + 8 + length]
        stored_crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(payload, actual_crc) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            raise ValueError("PNG chunk CRC mismatch")
        chunks.append((chunk_type, payload))
        offset = end
        if chunk_type == b"IEND":
            saw_iend = True
            break
    if not saw_iend or offset != len(data):
        raise ValueError("PNG IEND missing or trailing data present")
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise ValueError("PNG IHDR missing or invalid")

    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", chunks[0][1]))
    if width <= 0 or height <= 0:
        raise ValueError("PNG geometry is invalid")
    valid_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if bit_depth not in valid_depths.get(color_type, set()):
        raise ValueError("PNG bit depth/color type combination is invalid")
    if compression != 0 or filtering != 0:
        raise ValueError("PNG uses an unsupported compression/filter method")
    if interlace != 0:
        raise RuntimeError("stdlib_png_interlace_unavailable")
    idat = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    if not idat:
        raise ValueError("PNG IDAT missing")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    row_bytes = (width * channels * bit_depth + 7) // 8
    expected_size = height * (row_bytes + 1)
    if expected_size > _MAX_STDLIB_DECODE_BYTES:
        raise ValueError("PNG decoded size exceeds safe probe limit")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(idat, expected_size + 1)
    raw += decoder.flush()
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or len(raw) != expected_size):
        raise ValueError("PNG pixel stream is truncated or oversized")
    stride = row_bytes + 1
    if any(raw[row * stride] > 4 for row in range(height)):
        raise ValueError("PNG scanline filter is invalid")
    return int(width), int(height)


def _jpeg_dimensions(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None, None
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0x01, *range(0xD0, 0xDA)):
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return int(width), int(height)
        offset += length
    return None, None


def _decode_jpeg_with_ffmpeg(path: Path, data: bytes
                             ) -> Tuple[str, int, int]:
    """Fully decode a JPEG when Pillow is absent.

    AIFOS already bundles/uses ffmpeg for video and image normalization.  The
    previous probe nevertheless treated every JPEG as undecodable whenever
    Pillow was not installed, even after the provider had successfully
    written a real 1080x1920 frame.  Decode one complete still frame into the
    null muxer and use the JPEG SOF geometry only after that decode succeeds.
    This keeps the no-header-only safety contract without adding a mandatory
    Python dependency.
    """
    executable = shutil.which("ffmpeg")
    if not executable:
        bundled = Path.home() / ".local" / "bin" / "ffmpeg"
        executable = str(bundled) if bundled.is_file() else ""
    if not executable:
        raise RuntimeError("ffmpeg_unavailable")
    width, height = _jpeg_dimensions(data)
    if not width or not height:
        raise ValueError("JPEG SOF geometry is missing or invalid")
    try:
        completed = subprocess.run([
            executable, "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
        ], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("ffmpeg_unavailable") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"")[-500:]
        raise ValueError(
            "ffmpeg JPEG pixel decode failed: "
            + detail.decode("utf-8", errors="replace"))
    return "jpeg", int(width), int(height)


def _parse_expected_aspect(value: Any) -> Tuple[str, float]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        left, right = value
        label = f"{left}:{right}"
    else:
        text = str(value or "").strip()
        if ":" not in text:
            raise ValueError("期望画幅必须使用 W:H 格式，例如 9:16")
        left, right = text.split(":", 1)
        label = text
    try:
        width_ratio, height_ratio = float(left), float(right)
    except (TypeError, ValueError) as exc:
        raise ValueError("期望画幅必须是正数 W:H") from exc
    if (not math.isfinite(width_ratio) or not math.isfinite(height_ratio)
            or width_ratio <= 0 or height_ratio <= 0):
        raise ValueError("期望画幅必须是正数 W:H")
    return label, width_ratio / height_ratio


def probe_image(source: Any, *, expected_aspect: Optional[Any] = None,
                aspect_tolerance: float = DEFAULT_ASPECT_TOLERANCE
                ) -> Dict[str, Any]:
    """Probe a local image and return structured technical evidence.

    ``aspect_tolerance`` is the maximum relative ratio error.  For example,
    ``0.03`` accepts an actual width/height ratio within 3% of the expected
    ratio.  A successful result means bytes were genuinely decompressed, not
    merely that the filename or first bytes looked like an image.
    """
    result = _base_result(source)
    source_text = str(source or "").strip()
    if not source_text:
        return _fail(result, "source_missing", "图片地址为空")
    if result["is_remote"]:
        message = "远程图片必须先下载到受管本地文件后再做技术探测"
        result.update({
            "status": "needs_download",
            "needs_download": True,
            "error_code": "remote_download_required",
            "error": message,
        })
        result["issues"].append(_issue("remote_download_required", message))
        result["checks"].append({
            "name": "local_file", "passed": False,
            "actual": "remote_url", "required_action": "download_then_probe",
        })
        return result

    path = Path(source_text).expanduser()
    if not path.is_file():
        result["checks"].append({
            "name": "local_file", "passed": False, "actual": str(path)})
        return _fail(result, "source_missing", f"图片文件不存在: {path}")
    result["file_exists"] = True
    result["checks"].append({
        "name": "local_file", "passed": True, "actual": str(path)})
    try:
        size = path.stat().st_size
        data = path.read_bytes()
    except OSError as exc:
        return _fail(result, "source_unreadable", f"图片文件无法读取: {exc}")
    result["size_bytes"] = int(size)
    result["nonempty"] = bool(size and data)
    result["checks"].append({
        "name": "nonempty", "passed": result["nonempty"],
        "actual": int(size),
    })
    if not result["nonempty"]:
        return _fail(result, "image_empty", "图片文件为空")

    result["probed"] = True
    magic = _magic_format(data)
    result["magic_format"] = magic
    result["checks"].append({
        "name": "format_magic", "passed": bool(magic),
        "actual": magic or "unknown",
    })
    if not magic:
        return _fail(
            result, "image_format_unknown",
            "文件魔数不是受支持的 PNG、JPEG、GIF 或 WebP 图片")

    decoded_format = ""
    width: Optional[int] = None
    height: Optional[int] = None
    try:
        if _PIL_IMAGE is not None:
            decoded_format, width, height = _pillow_decode(data)
            result["decode_backend"] = "pillow"
        elif magic == "png":
            width, height = _png_stdlib_decode(data)
            decoded_format = "png"
            result["decode_backend"] = "stdlib_png"
        elif magic == "jpeg":
            decoded_format, width, height = _decode_jpeg_with_ffmpeg(
                path, data)
            result["decode_backend"] = "ffmpeg"
        else:
            result.update({"width": width, "height": height})
            message = (
                f"当前环境没有可用解码器，不能对 {magic.upper()} 执行真实像素解码；"
                "不得仅凭文件头放行")
            result["checks"].append({
                "name": "pixel_decode", "passed": False,
                "actual": "decoder_unavailable",
            })
            return _fail(result, "image_decoder_unavailable", message)
    except RuntimeError as exc:
        code = str(exc)
        message = (
            "当前环境无法安全解码该图片；不得仅凭文件头放行"
            if code != "pillow_unavailable" else
            "当前环境没有可用图片解码器；不得仅凭文件头放行")
        result["checks"].append({
            "name": "pixel_decode", "passed": False, "actual": code})
        return _fail(result, "image_decoder_unavailable", message)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        result["checks"].append({
            "name": "pixel_decode", "passed": False,
            "actual": str(exc)[:300],
        })
        return _fail(
            result, "image_decode_failed",
            f"图片真实解码失败: {str(exc)[:300]}")
    except Exception as exc:  # Defensive boundary for optional decoder plugins.
        result["checks"].append({
            "name": "pixel_decode", "passed": False,
            "actual": str(exc)[:300],
        })
        return _fail(
            result, "image_decode_failed",
            f"图片真实解码失败: {str(exc)[:300]}")

    result.update({
        "decoded": True,
        "decoded_format": decoded_format,
        "width": int(width or 0) or None,
        "height": int(height or 0) or None,
    })
    magic_matches_decode = (
        decoded_format == magic
        or {decoded_format, magic} <= {"jpg", "jpeg"})
    result["checks"].append({
        "name": "pixel_decode", "passed": True,
        "actual": {"backend": result["decode_backend"],
                   "format": decoded_format},
    })
    result["checks"].append({
        "name": "magic_matches_decode", "passed": magic_matches_decode,
        "expected": magic, "actual": decoded_format,
    })
    if not magic_matches_decode:
        return _fail(
            result, "image_format_mismatch",
            "文件魔数与真实解码格式不一致",
            expected=magic, actual=decoded_format)

    valid_geometry = bool(
        isinstance(result["width"], int) and result["width"] > 0
        and isinstance(result["height"], int) and result["height"] > 0)
    result["checks"].append({
        "name": "geometry", "passed": valid_geometry,
        "actual": {"width": result["width"], "height": result["height"]},
    })
    if not valid_geometry:
        return _fail(result, "image_geometry_missing", "无法取得有效图片宽高")
    result["aspect_ratio"] = result["width"] / result["height"]

    suffix = path.suffix.lower().lstrip(".")
    normalized_suffix = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    if normalized_suffix and normalized_suffix != magic:
        result["warnings"].append(_warning(
            "extension_magic_mismatch",
            "扩展名与真实图片格式不同；以魔数和解码结果为准",
            actual={"extension": suffix, "magic_format": magic}))

    if expected_aspect is not None:
        try:
            tolerance = float(aspect_tolerance)
        except (TypeError, ValueError):
            return _fail(
                result, "aspect_tolerance_invalid",
                "画幅容差必须是非负有限数", actual=aspect_tolerance)
        if not math.isfinite(tolerance) or tolerance < 0:
            return _fail(
                result, "aspect_tolerance_invalid",
                "画幅容差必须是非负有限数", actual=aspect_tolerance)
        try:
            label, target_ratio = _parse_expected_aspect(expected_aspect)
        except ValueError as exc:
            return _fail(
                result, "expected_aspect_invalid", str(exc),
                actual=expected_aspect)
        relative_error = abs(result["aspect_ratio"] - target_ratio) / target_ratio
        aspect_matches = relative_error <= tolerance + 1e-12
        result["expected_aspect"] = {
            "label": label,
            "ratio": target_ratio,
            "relative_tolerance": tolerance,
        }
        result["aspect_matches"] = aspect_matches
        result["checks"].append({
            "name": "aspect_ratio", "passed": aspect_matches,
            "expected": result["expected_aspect"],
            "actual": {
                "ratio": result["aspect_ratio"],
                "width": result["width"],
                "height": result["height"],
                "relative_error": relative_error,
            },
        })
        if not aspect_matches:
            return _fail(
                result, "aspect_ratio_mismatch",
                f"实际画幅 {result['width']}:{result['height']} 与期望 "
                f"{label} 的相对误差超过 {tolerance:.1%}",
                expected=result["expected_aspect"],
                actual={"ratio": result["aspect_ratio"],
                        "relative_error": relative_error})

    result.update({
        "status": "passed",
        "probe_ok": True,
        "ok": True,
        "error_code": "",
        "error": "",
    })
    return result


def image_is_technically_usable(result: Mapping[str, Any]) -> bool:
    """Conservative convenience predicate for a completed probe result."""
    return bool(
        result
        and result.get("probed") is True
        and result.get("probe_ok") is True
        and result.get("decoded") is True
        and result.get("status") == "passed")
