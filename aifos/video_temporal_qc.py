"""Lightweight, non-blocking temporal evidence for generated video QC.

The production package intentionally has no CV/torch dependency.  This module
reduces already-extracted review frames to tiny grayscale signatures through
the same ffmpeg runtime used by the editor, then reports only objective clues:
blank frames, near-duplicates and unusually large whole-frame jumps.  These
clues help the visual reviewer focus its judgement; they never approve or
reject story content by themselves and never enter the reference chain.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


SCHEMA = "aifos.video-temporal-evidence/v1"
SIGNATURE_EDGE = 32
FREEZE_DELTA = 0.002
ABRUPT_DELTA = 0.75
BLANK_DARK = 0.01
BLANK_LIGHT = 0.99


def _clamp_byte(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return max(0, min(255, number))


def _default_reader(
        uri: str, *, ffmpeg: str = "ffmpeg", runner: Callable = subprocess.run,
        binary_finder: Callable = shutil.which, timeout: float = 20.0):
    """Decode one review frame to a 32x32 grayscale signature."""
    path = Path(str(uri or "")).expanduser()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    executable = str(binary_finder(ffmpeg) or "")
    if not executable:
        candidate = Path(ffmpeg).expanduser()
        executable = str(candidate) if candidate.is_file() else ""
    if not executable:
        raise RuntimeError("ffmpeg 不可用")
    command = [
        executable, "-v", "error", "-i", str(path),
        "-vf", f"scale={SIGNATURE_EDGE}:{SIGNATURE_EDGE}:flags=area,format=gray",
        "-frames:v", "1", "-f", "rawvideo", "-",
    ]
    completed = runner(
        command, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        detail = bytes(completed.stderr or b"").decode(
            "utf-8", errors="replace").strip()
        raise RuntimeError("ffmpeg 灰度解码失败" + (
            f": {detail[:300]}" if detail else ""))
    expected = SIGNATURE_EDGE * SIGNATURE_EDGE
    pixels = bytes(completed.stdout or b"")
    if len(pixels) != expected:
        raise RuntimeError(
            f"灰度签名长度异常: {len(pixels)}，应为 {expected}")
    return pixels


def _delta(left: Sequence[int], right: Sequence[int]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("相邻帧签名尺寸不一致")
    return sum(
        abs(_clamp_byte(a) - _clamp_byte(b)) for a, b in zip(left, right)
    ) / (255.0 * len(left))


def analyze_temporal_samples(
        samples: Sequence[Mapping[str, Any]], *,
        frame_reader: Optional[Callable[[str], Sequence[int]]] = None,
        ffmpeg: str = "ffmpeg") -> Dict[str, Any]:
    """Return objective frame-to-frame evidence without making a hard gate."""
    reader = frame_reader or (lambda uri: _default_reader(uri, ffmpeg=ffmpeg))
    frames = []
    warnings = []
    unavailable = []
    for index, sample in enumerate(samples or []):
        if not isinstance(sample, Mapping):
            continue
        uri = str(sample.get("uri") or "")
        label = str(sample.get("label") or f"frame-{index + 1}")
        try:
            pixels = bytes(_clamp_byte(value) for value in reader(uri))
            if not pixels:
                raise ValueError("空像素签名")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            unavailable.append({"label": label, "uri": uri,
                                "error": str(exc)[:400]})
            continue
        brightness = sum(pixels) / (255.0 * len(pixels))
        frame = {
            "index": index,
            "label": label,
            "timestamp": sample.get("timestamp"),
            "uri": uri,
            "signature_sha256": hashlib.sha256(pixels).hexdigest(),
            "mean_brightness": round(brightness, 6),
        }
        frames.append((frame, pixels))
        if brightness <= BLANK_DARK or brightness >= BLANK_LIGHT:
            warnings.append({
                "code": "near_blank_frame",
                "labels": [label],
                "delta": None,
                "message": f"{label} 接近纯色/空白帧，需确认不是闪黑、白屏或生成故障",
            })

    transitions = []
    for (left, left_pixels), (right, right_pixels) in zip(frames, frames[1:]):
        value = _delta(left_pixels, right_pixels)
        row = {
            "from": left["label"], "to": right["label"],
            "normalized_mean_abs_delta": round(value, 6),
            "classification": (
                "near_duplicate" if value <= FREEZE_DELTA else
                "abrupt_global_change" if value >= ABRUPT_DELTA else
                "ordinary_change"),
        }
        transitions.append(row)
        if value <= FREEZE_DELTA:
            warnings.append({
                "code": "near_duplicate_frames",
                "labels": [left["label"], right["label"]],
                "delta": row["normalized_mean_abs_delta"],
                "message": (
                    f"{left['label']}→{right['label']} 几乎无画面变化，"
                    "需结合镜头动作确认是否冻结/无动作"),
            })
        elif value >= ABRUPT_DELTA:
            warnings.append({
                "code": "abrupt_global_change",
                "labels": [left["label"], right["label"]],
                "delta": row["normalized_mean_abs_delta"],
                "message": (
                    f"{left['label']}→{right['label']} 出现全画面突变，"
                    "需重点核对人物、道具、布景是否漂移或无意切镜"),
            })

    usable = len(frames)
    return {
        "schema": SCHEMA,
        "advisory_only": True,
        "blocking": False,
        "reference_chain_eligible": False,
        "asset_registration_allowed": False,
        "sample_count": len(samples or []),
        "usable_sample_count": usable,
        "analysis_available": usable >= 2,
        "frames": [row for row, _pixels in frames],
        "transitions": transitions,
        "warnings": warnings,
        "unavailable": unavailable,
        "thresholds": {
            "freeze_delta_max": FREEZE_DELTA,
            "abrupt_delta_min": ABRUPT_DELTA,
            "blank_brightness": [BLANK_DARK, BLANK_LIGHT],
        },
    }
