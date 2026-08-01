"""Local-only technical video QC and deterministic frame sampling.

This module deliberately has no dependency on the asset registry or an AI
provider.  Extracted frames are review evidence only: every returned sample is
marked ineligible for the generation reference chain and asset registration.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse


PROBE_SCHEMA = "aifos.video-media-probe/v1"
TECHNICAL_QC_SCHEMA = "aifos.video-technical-qc/v1"
FRAME_QC_SCHEMA = "aifos.video-frame-qc-evidence/v1"
SAMPLE_RATIOS = (0.0, 0.25, 0.5, 0.75, 1.0)
QC_CACHE_DIRECTORY = "video_qc_frames"

_RESOLUTION_SHORT_EDGE = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "2160p": 2160,
    "4k": 2160,
}


def _is_remote(source: Any) -> bool:
    parsed = urlparse(str(source or ""))
    return parsed.scheme.lower() in {"http", "https"}


def _resolve_binary(command: str, binary_finder: Optional[Callable] = None):
    candidate = Path(str(command)).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate) if candidate.is_file() else ""
    finder = binary_finder or shutil.which
    return str(finder(str(command)) or "")


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> Optional[float]:
    number = _safe_float(value)
    return number if number is not None and number > 0 else None


def _rational(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text or text in {"0", "0/0", "N/A"}:
        return None
    if "/" not in text:
        return _positive_float(text)
    numerator, denominator = text.split("/", 1)
    top = _safe_float(numerator)
    bottom = _safe_float(denominator)
    if top is None or bottom in (None, 0):
        return None
    return _positive_float(top / bottom)


def _rotation(stream: Mapping[str, Any]) -> int:
    candidates = [(stream.get("tags") or {}).get("rotate")]
    candidates.extend(
        item.get("rotation")
        for item in (stream.get("side_data_list") or [])
        if isinstance(item, Mapping)
    )
    for value in candidates:
        number = _safe_float(value)
        if number is not None:
            return int(round(number)) % 360
    return 0


def _stream_duration(stream: Mapping[str, Any]) -> Optional[float]:
    duration = _positive_float(stream.get("duration"))
    if duration is not None:
        return duration
    tags = stream.get("tags") or {}
    return _positive_float(tags.get("DURATION"))


def _probe_failure(source: Any, code: str, message: str, *,
                   is_remote: Optional[bool] = None) -> Dict[str, Any]:
    return {
        "schema": PROBE_SCHEMA,
        "source": str(source or ""),
        "is_remote": _is_remote(source) if is_remote is None else is_remote,
        "probed": False,
        "probe_ok": False,
        "ok": False,
        "error_code": code,
        "error": message,
        "has_video": False,
        "has_audio": False,
        "video_streams": [],
        "audio_streams": [],
        "width": None,
        "height": None,
        "fps": None,
        "duration": None,
    }


def probe_video(source: Any, *, ffprobe: str = "ffprobe",
                runner: Optional[Callable] = None,
                binary_finder: Optional[Callable] = None,
                timeout: float = 30.0) -> Dict[str, Any]:
    """Probe a local path or remote URL with ffprobe.

    Remote URLs are never accepted on URL shape alone.  They must complete the
    same ffprobe command and yield a real video stream before ``probe_ok`` can
    be true.
    """
    source_text = str(source or "").strip()
    remote = _is_remote(source_text)
    if not source_text:
        return _probe_failure(source_text, "source_missing", "视频地址为空")
    if not remote:
        path = Path(source_text).expanduser()
        if not path.is_file():
            return _probe_failure(
                source_text, "source_missing", f"视频文件不存在: {path}")

    executable = _resolve_binary(ffprobe, binary_finder)
    if not executable:
        return _probe_failure(
            source_text, "ffprobe_unavailable",
            "ffprobe 不可用，无法实测视频流、分辨率、时长和音轨",
            is_remote=remote)

    run = runner or subprocess.run
    command = [
        executable,
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        source_text,
    ]
    try:
        completed = run(
            command, capture_output=True, text=True, timeout=timeout,
            check=False)
    except subprocess.TimeoutExpired:
        return _probe_failure(
            source_text, "ffprobe_timeout",
            f"ffprobe 超过 {timeout:g} 秒未完成", is_remote=remote)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return _probe_failure(
            source_text, "ffprobe_unavailable",
            f"ffprobe 无法执行: {exc}", is_remote=remote)

    if completed.returncode != 0:
        detail = str(completed.stderr or completed.stdout or "").strip()
        return _probe_failure(
            source_text, "ffprobe_failed",
            "ffprobe 探测失败" + (f": {detail[:500]}" if detail else ""),
            is_remote=remote)
    try:
        payload = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError) as exc:
        return _probe_failure(
            source_text, "ffprobe_invalid_json",
            f"ffprobe 未返回有效 JSON: {exc}", is_remote=remote)

    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        streams = []
    raw_videos = [
        item for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == "video"]
    raw_audio = [
        item for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == "audio"]

    video_streams = []
    for stream in raw_videos:
        coded_width = int(_safe_float(stream.get("width")) or 0)
        coded_height = int(_safe_float(stream.get("height")) or 0)
        rotation = _rotation(stream)
        display_width, display_height = coded_width, coded_height
        if rotation in {90, 270}:
            display_width, display_height = coded_height, coded_width
        video_streams.append({
            "index": stream.get("index"),
            "codec": str(stream.get("codec_name") or ""),
            "coded_width": coded_width or None,
            "coded_height": coded_height or None,
            "width": display_width or None,
            "height": display_height or None,
            "rotation": rotation,
            "fps": (_rational(stream.get("avg_frame_rate"))
                    or _rational(stream.get("r_frame_rate"))),
            "duration": _stream_duration(stream),
            "pix_fmt": str(stream.get("pix_fmt") or ""),
        })
    audio_streams = [{
        "index": stream.get("index"),
        "codec": str(stream.get("codec_name") or ""),
        "channels": int(_safe_float(stream.get("channels")) or 0) or None,
        "sample_rate": int(_safe_float(stream.get("sample_rate")) or 0)
                       or None,
        "duration": _stream_duration(stream),
    } for stream in raw_audio]

    primary = video_streams[0] if video_streams else {}
    format_data = payload.get("format") or {}
    duration = _positive_float(format_data.get("duration"))
    if duration is None:
        duration = primary.get("duration")
    has_video = bool(video_streams)
    result = {
        "schema": PROBE_SCHEMA,
        "source": source_text,
        "is_remote": remote,
        "probed": True,
        "probe_ok": has_video,
        "ok": has_video,
        "error_code": "" if has_video else "video_stream_missing",
        "error": "" if has_video else "ffprobe 结果中没有视频流",
        "has_video": has_video,
        "has_audio": bool(audio_streams),
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "width": primary.get("width"),
        "height": primary.get("height"),
        "fps": primary.get("fps"),
        "duration": duration,
        "format_name": str(format_data.get("format_name") or ""),
        "format_size": int(_safe_float(format_data.get("size")) or 0) or None,
        "bit_rate": int(_safe_float(format_data.get("bit_rate")) or 0)
                    or None,
    }
    return result


def build_sample_points(duration: Any, fps: Any = None,
                        ratios: Sequence[float] = SAMPLE_RATIOS
                        ) -> List[Dict[str, Any]]:
    """Return EOF-safe, frame-aligned sampling points.

    Very short clips can map several requested ratios onto the same decodable
    frame.  Those ratios are merged instead of extracting duplicate images.
    """
    duration_value = _positive_float(duration)
    if duration_value is None:
        raise ValueError("duration must be a positive finite number")
    fps_value = _positive_float(fps)
    frame_step = (1.0 / fps_value) if fps_value else min(0.04, duration_value)
    last_decodable = max(0.0, duration_value - frame_step)
    points: List[Dict[str, Any]] = []
    for raw_ratio in ratios:
        ratio = _safe_float(raw_ratio)
        if ratio is None or ratio < 0 or ratio > 1:
            raise ValueError("sample ratios must be finite numbers from 0 to 1")
        timestamp = min(duration_value * ratio, last_decodable)
        if fps_value:
            frame_index = math.floor((timestamp + 1e-9) * fps_value)
            timestamp = frame_index / fps_value
        timestamp = round(max(0.0, timestamp), 6)
        existing = next((
            item for item in points
            if math.isclose(item["timestamp"], timestamp, abs_tol=1e-6)
        ), None)
        percent = int(round(ratio * 100))
        if existing is not None:
            existing["ratios"].append(ratio)
            existing["percentages"].append(percent)
            existing["label"] = "/".join(
                f"{value}%" for value in existing["percentages"])
            continue
        points.append({
            "ratio": ratio,
            "ratios": [ratio],
            "percentage": percent,
            "percentages": [percent],
            "label": f"{percent}%",
            "timestamp": timestamp,
        })
    return points


def sample_timestamps(duration: Any, fps: Any = None) -> List[float]:
    """Convenience form of :func:`build_sample_points`."""
    return [item["timestamp"] for item in build_sample_points(duration, fps)]


def video_media_signature(source: Any,
                          probe: Optional[Mapping[str, Any]] = None) -> str:
    """Return a deterministic cache signature for the exact media input."""
    source_text = str(source or "").strip()
    if not source_text:
        raise ValueError("video source is required")
    digest = hashlib.sha256()
    if _is_remote(source_text):
        if not probe or probe.get("probed") is not True:
            raise ValueError("remote video must be probed before it can be signed")
        normalized = {
            "kind": "remote",
            "source": source_text,
            "width": probe.get("width"),
            "height": probe.get("height"),
            "fps": probe.get("fps"),
            "duration": probe.get("duration"),
            "video_streams": probe.get("video_streams") or [],
            "audio_streams": probe.get("audio_streams") or [],
        }
        digest.update(json.dumps(
            normalized, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8"))
        return digest.hexdigest()

    path = Path(source_text).expanduser()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    # A content digest stays stable if a provider result is copied into the
    # managed workspace, unlike path/mtime-based cache keys.
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_dimensions(aspect: str = "9:16",
                        resolution: str = "720p") -> Optional[Dict[str, int]]:
    """Resolve the display dimensions implied by aspect and short-edge tier."""
    short_edge = _RESOLUTION_SHORT_EDGE.get(str(resolution).strip().lower())
    try:
        width_ratio, height_ratio = (
            float(value) for value in str(aspect).strip().split(":", 1))
    except (TypeError, ValueError):
        return None
    if not short_edge or width_ratio <= 0 or height_ratio <= 0:
        return None
    if width_ratio <= height_ratio:
        width = short_edge
        height = round(short_edge * height_ratio / width_ratio)
    else:
        height = short_edge
        width = round(short_edge * width_ratio / height_ratio)
    # Codec dimensions should be even.  Standard ratios such as 9:16 remain
    # exact (720x1280); unusual ratios are rounded to the nearest even pixel.
    width = int(2 * round(width / 2))
    height = int(2 * round(height / 2))
    return {"width": width, "height": height}


def _technical_issue(code: str, message: str, *, expected: Any = None,
                     actual: Any = None) -> Dict[str, Any]:
    issue = {
        "check": "video_technical",
        "code": code,
        "severity": "error",
        "rerunnable": True,
        "message": message,
    }
    if expected is not None:
        issue["expected"] = expected
    if actual is not None:
        issue["actual"] = actual
    return issue


def evaluate_video_technical(
        probe: Mapping[str, Any], *, expected_aspect: str = "9:16",
        expected_resolution: str = "720p",
        expected_duration: Optional[float] = None,
        duration_tolerance: Optional[float] = None,
        require_audio: bool = False) -> Dict[str, Any]:
    """Evaluate hard delivery facts from a real ffprobe result."""
    probe = dict(probe or {})
    issues: List[Dict[str, Any]] = []
    checks: List[Dict[str, Any]] = []

    probe_passed = bool(probe.get("probed") is True
                        and probe.get("probe_ok") is True)
    checks.append({
        "name": "ffprobe", "passed": probe_passed,
        "actual": probe.get("error") or "已完成",
    })
    if not probe_passed:
        code = str(probe.get("error_code") or "probe_required")
        issues.append(_technical_issue(
            code, str(probe.get("error") or
                      "视频没有完成 ffprobe 实测，禁止按声明元数据放行")))
        return {
            "schema": TECHNICAL_QC_SCHEMA,
            "passed": False,
            "probe": probe,
            "checks": checks,
            "issues": issues,
            "reference_chain_eligible": False,
        }

    has_video = probe.get("has_video") is True
    checks.append({"name": "video_stream", "passed": has_video,
                   "actual": len(probe.get("video_streams") or [])})
    if not has_video:
        issues.append(_technical_issue(
            "video_stream_missing", "视频中没有可识别的视频流"))

    width = probe.get("width")
    height = probe.get("height")
    fps = _positive_float(probe.get("fps"))
    valid_geometry = bool(
        isinstance(width, int) and width > 0
        and isinstance(height, int) and height > 0)
    checks.append({
        "name": "video_geometry", "passed": valid_geometry,
        "actual": {"width": width, "height": height, "fps": fps},
    })
    if not valid_geometry:
        issues.append(_technical_issue(
            "video_geometry_missing", "无法实测有效的视频宽高"))
    if fps is None:
        issues.append(_technical_issue(
            "video_fps_missing", "无法实测有效的视频帧率"))

    dimensions = expected_dimensions(expected_aspect, expected_resolution)
    if dimensions is None:
        issues.append(_technical_issue(
            "expected_dimensions_invalid",
            f"无法解析期望规格 {expected_aspect} / {expected_resolution}"))
    elif valid_geometry:
        actual_dimensions = {"width": width, "height": height}
        dimensions_match = actual_dimensions == dimensions
        checks.append({
            "name": "resolution", "passed": dimensions_match,
            "expected": dimensions, "actual": actual_dimensions,
        })
        if not dimensions_match:
            issues.append(_technical_issue(
                "resolution_mismatch",
                f"实际分辨率 {width}x{height}，应为 "
                f"{dimensions['width']}x{dimensions['height']} "
                f"({expected_aspect} {expected_resolution})",
                expected=dimensions, actual=actual_dimensions))

    actual_duration = _positive_float(probe.get("duration"))
    checks.append({
        "name": "duration_readable", "passed": actual_duration is not None,
        "actual": actual_duration,
    })
    if actual_duration is None:
        issues.append(_technical_issue(
            "duration_missing", "无法实测有效的视频时长"))
    expected_duration_value = _positive_float(expected_duration)
    if expected_duration is not None and expected_duration_value is None:
        issues.append(_technical_issue(
            "expected_duration_invalid", "期望时长必须为正数",
            expected=expected_duration))
    elif actual_duration is not None and expected_duration_value is not None:
        tolerance = _safe_float(duration_tolerance)
        if tolerance is None:
            tolerance = max(0.1, 1.0 / fps) if fps else 0.1
        tolerance = max(0.0, tolerance)
        duration_match = (
            abs(actual_duration - expected_duration_value)
            <= tolerance + 1e-9)
        checks.append({
            "name": "duration", "passed": duration_match,
            "expected": expected_duration_value,
            "actual": actual_duration,
            "tolerance": tolerance,
        })
        if not duration_match:
            issues.append(_technical_issue(
                "duration_mismatch",
                f"实际时长 {actual_duration:g}s，与提交时长 "
                f"{expected_duration_value:g}s 相差超过 {tolerance:g}s",
                expected={"duration": expected_duration_value,
                          "tolerance": tolerance},
                actual=actual_duration))

    has_audio = probe.get("has_audio") is True
    checks.append({
        "name": "audio_stream", "passed": has_audio or not require_audio,
        "required": bool(require_audio),
        "actual": len(probe.get("audio_streams") or []),
    })
    if require_audio and not has_audio:
        issues.append(_technical_issue(
            "audio_stream_missing",
            "已要求即梦内置配音/对口型，但成片未实测到音轨"))

    return {
        "schema": TECHNICAL_QC_SCHEMA,
        "passed": not issues,
        "probe": probe,
        "checks": checks,
        "issues": issues,
        "expected": {
            "aspect": expected_aspect,
            "resolution": expected_resolution,
            "dimensions": dimensions,
            "duration": expected_duration_value,
            "audio_required": bool(require_audio),
        },
        "reference_chain_eligible": False,
    }


def extract_video_qc_frames(
        source: Any, cache_root: Any, *,
        probe: Optional[Mapping[str, Any]] = None,
        ffmpeg: str = "ffmpeg", runner: Optional[Callable] = None,
        binary_finder: Optional[Callable] = None,
        timeout_per_frame: float = 60.0) -> Dict[str, Any]:
    """Extract deterministic QC frames into a non-asset cache directory."""
    source_text = str(source or "").strip()
    probe_data = dict(probe or {})
    if not probe_data or probe_data.get("probed") is not True:
        issue = _technical_issue(
            "probe_required",
            "抽帧前必须先完成 ffprobe；远程 URL 不允许未经探测即通过")
        return {
            "schema": FRAME_QC_SCHEMA,
            "passed": False,
            "source": source_text,
            "samples": [],
            "issues": [issue],
            "reference_chain_eligible": False,
            "asset_registration_allowed": False,
        }
    if probe_data.get("probe_ok") is not True:
        issue = _technical_issue(
            str(probe_data.get("error_code") or "probe_failed"),
            str(probe_data.get("error") or "视频探测未通过，不能抽帧"))
        return {
            "schema": FRAME_QC_SCHEMA,
            "passed": False,
            "source": source_text,
            "samples": [],
            "issues": [issue],
            "reference_chain_eligible": False,
            "asset_registration_allowed": False,
        }
    executable = _resolve_binary(ffmpeg, binary_finder)
    if not executable:
        issue = _technical_issue(
            "ffmpeg_unavailable", "ffmpeg 不可用，无法抽取视频 QC 帧")
        return {
            "schema": FRAME_QC_SCHEMA,
            "passed": False,
            "source": source_text,
            "samples": [],
            "issues": [issue],
            "reference_chain_eligible": False,
            "asset_registration_allowed": False,
        }

    try:
        signature = video_media_signature(source_text, probe_data)
        points = build_sample_points(
            probe_data.get("duration"), probe_data.get("fps"))
    except (OSError, ValueError) as exc:
        issue = _technical_issue("sampling_input_invalid", str(exc))
        return {
            "schema": FRAME_QC_SCHEMA,
            "passed": False,
            "source": source_text,
            "samples": [],
            "issues": [issue],
            "reference_chain_eligible": False,
            "asset_registration_allowed": False,
        }

    cache_dir = (Path(cache_root).expanduser() / QC_CACHE_DIRECTORY
                 / signature[:24])
    cache_dir.mkdir(parents=True, exist_ok=True)
    run = runner or subprocess.run
    samples = []
    issues = []
    for index, point in enumerate(points):
        percent_slug = "_".join(
            f"{value:03d}" for value in point["percentages"])
        output = cache_dir / (
            f"sample_{index + 1:02d}_{percent_slug}pct_"
            f"{int(round(point['timestamp'] * 1000)):08d}ms.png")
        reused = output.is_file() and output.stat().st_size > 0
        if not reused:
            command = [
                executable,
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", source_text,
                "-ss", f"{point['timestamp']:.6f}",
                "-map", "0:v:0", "-frames:v", "1",
                "-an", "-sn", "-dn",
                str(output),
            ]
            try:
                completed = run(
                    command, capture_output=True, text=True,
                    timeout=timeout_per_frame, check=False)
            except subprocess.TimeoutExpired:
                completed = None
                detail = f"抽帧超过 {timeout_per_frame:g} 秒"
            except (FileNotFoundError, PermissionError, OSError) as exc:
                completed = None
                detail = f"ffmpeg 无法执行: {exc}"
            else:
                detail = str(completed.stderr or completed.stdout or "").strip()
            if (completed is None or completed.returncode != 0
                    or not output.is_file() or output.stat().st_size == 0):
                issues.append(_technical_issue(
                    "frame_extract_failed",
                    f"{point['label']} 抽帧失败"
                    + (f": {detail[:500]}" if detail else ""),
                    expected={"timestamp": point["timestamp"]}))
                continue
        samples.append({
            **point,
            "kind": "video_qc_frame",
            "uri": str(output),
            "reused": reused,
            "media_signature": signature,
            "reference_chain_eligible": False,
            "asset_registration_allowed": False,
        })

    manifest_path = cache_dir / "manifest.json"
    report = {
        "schema": FRAME_QC_SCHEMA,
        "passed": not issues and len(samples) == len(points),
        "source": source_text,
        "is_remote": _is_remote(source_text),
        "media_signature": signature,
        "cache_dir": str(cache_dir),
        "requested_ratios": list(SAMPLE_RATIOS),
        "sample_count": len(samples),
        "samples": samples,
        "issues": issues,
        "reference_chain_eligible": False,
        "asset_registration_allowed": False,
    }
    manifest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["manifest"] = str(manifest_path)
    return report


__all__ = [
    "FRAME_QC_SCHEMA",
    "PROBE_SCHEMA",
    "QC_CACHE_DIRECTORY",
    "SAMPLE_RATIOS",
    "TECHNICAL_QC_SCHEMA",
    "build_sample_points",
    "evaluate_video_technical",
    "expected_dimensions",
    "extract_video_qc_frames",
    "probe_video",
    "sample_timestamps",
    "video_media_signature",
]
