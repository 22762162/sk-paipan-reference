"""Deterministic, dependency-free timeline exchange for AIFOS episodes.

The format is inspired by OTIO's separation of timeline ranges and media
references, but intentionally uses only JSON-compatible Python values.  It is
an interchange/audit layer, not an editor implementation.

The builder requires exactly one video output for every storyboard shot.  It
records planned and actual timing, video/audio references, outgoing edit
transitions, hashes of the source documents and media, and a self-verifying
timeline hash.  Validation rejects duplicate or missing shots, overlaps,
non-positive durations, invalid transitions, and stale/tampered hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


TIMELINE_SCHEMA = "aifos.timeline-exchange/v1"
TIMELINE_VERSION = 1
HASH_ALGORITHM = "sha256"
TIME_PRECISION = 6
DEFAULT_FRAME_RATE = 24.0


class TimelineValidationError(ValueError):
    """The timeline cannot safely represent the requested episode."""


def _stable_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        default=str,
    )


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _clean_text(value: Any, *, limit: int = 1600) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _time_value(value: Any, *, field: str, allow_zero: bool = True) -> float:
    if isinstance(value, bool):
        raise TimelineValidationError(f"{field} 必须是有限数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TimelineValidationError(f"{field} 必须是有限数字") from exc
    if not math.isfinite(number):
        raise TimelineValidationError(f"{field} 必须是有限数字")
    if number < 0 or (not allow_zero and number == 0):
        qualifier = "非负" if allow_zero else "正"
        raise TimelineValidationError(f"{field} 必须是{qualifier}时长")
    return round(number, TIME_PRECISION)


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise TimelineValidationError(f"{field} 必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TimelineValidationError(f"{field} 必须是正整数") from exc
    if number <= 0 or str(value).strip() != str(number):
        raise TimelineValidationError(f"{field} 必须是正整数")
    return number


def _shots_from_storyboard(storyboard: Any) -> list[Mapping[str, Any]]:
    if isinstance(storyboard, Mapping):
        shots = storyboard.get("shots")
    else:
        shots = storyboard
    if not isinstance(shots, Sequence) or isinstance(shots, (str, bytes)):
        raise TimelineValidationError("storyboard.shots 必须是非空列表")
    output = [shot for shot in shots if isinstance(shot, Mapping)]
    if not output or len(output) != len(shots):
        raise TimelineValidationError("storyboard.shots 包含空值或非对象镜头")
    return output


def _normalize_video_outputs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping) and isinstance(
            value.get("videos") or value.get("shots"), Sequence):
        value = value.get("videos") or value.get("shots")
    if isinstance(value, Mapping):
        rows = []
        for key, raw in value.items():
            if isinstance(raw, Mapping):
                row = dict(raw)
                row.setdefault("shot_no", key)
            else:
                row = {"shot_no": key, "uri": raw}
            rows.append(row)
        return rows
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TimelineValidationError("video_outputs 必须是镜头列表或映射")
    rows = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TimelineValidationError("video_outputs 包含非对象镜头")
        rows.append(dict(raw))
    return rows


def _shot_identity(
        shot: Mapping[str, Any], position: int) -> tuple[int, str]:
    shot_no = _positive_int(
        shot.get("shot_no", position), field=f"第{position}镜 shot_no")
    explicit = _clean_text(shot.get("shot_id") or shot.get("id"), limit=300)
    return shot_no, explicit or f"shot:{shot_no:03d}"


def _provided_start(*sources: Mapping[str, Any]) -> float | None:
    for source in sources:
        for key in ("timeline_start", "start_seconds"):
            if key in source and source.get(key) not in (None, ""):
                return _time_value(
                    source.get(key), field=key, allow_zero=True)
    return None


def _clip_duration(
        shot: Mapping[str, Any], output: Mapping[str, Any], shot_no: int,
) -> tuple[float, float]:
    planned = _time_value(
        shot.get("duration"), field=f"镜头{shot_no}计划时长",
        allow_zero=False)
    raw_actual = output.get("actual_duration")
    if raw_actual in (None, ""):
        raw_actual = output.get("duration")
    actual = planned if raw_actual in (None, "") else _time_value(
        raw_actual, field=f"镜头{shot_no}视频时长", allow_zero=False)
    return planned, actual


def _transition(value: Any, *, shot_no: int) -> dict[str, Any]:
    if value in (None, "", False):
        return {"type": "cut", "duration": 0.0}
    if isinstance(value, str):
        return {"type": _clean_text(value, limit=120) or "cut",
                "duration": 0.0}
    if not isinstance(value, Mapping):
        raise TimelineValidationError(f"镜头{shot_no} transition 必须是对象或文字")
    kind = _clean_text(
        value.get("type") or value.get("name") or value.get("kind"),
        limit=120) or "cut"
    duration = _time_value(
        value.get("duration", 0), field=f"镜头{shot_no}转场时长",
        allow_zero=True)
    if kind.casefold() in {"cut", "hard_cut", "硬切"} and duration != 0:
        raise TimelineValidationError(f"镜头{shot_no}硬切时长必须为0")
    return {"type": kind, "duration": duration}


def _declared_hash(source: Mapping[str, Any]) -> str:
    for key in (
            "source_hash", "content_hash", "sha256", "media_hash"):
        raw = source.get(key)
        if isinstance(raw, Mapping):
            raw = raw.get("value")
        value = _clean_text(raw, limit=300)
        if value:
            return value
    return ""


def _media_hash(uri: str, source: Mapping[str, Any]) -> dict[str, str]:
    declared = _declared_hash(source)
    if declared:
        algorithm = HASH_ALGORITHM if re.fullmatch(
            r"[0-9a-fA-F]{64}", declared) else "declared"
        return {"algorithm": algorithm, "value": declared,
                "basis": "declared"}
    if uri and not uri.startswith(("http://", "https://")):
        path = Path(uri).expanduser()
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {"algorithm": HASH_ALGORITHM,
                    "value": digest.hexdigest(), "basis": "content"}
    return {"algorithm": HASH_ALGORITHM,
            "value": _stable_hash({"uri": uri}), "basis": "reference"}


def _video_ref(output: Mapping[str, Any], shot_no: int) -> dict[str, Any]:
    uri = _clean_text(
        output.get("uri") or output.get("video_uri")
        or output.get("final_uri"), limit=2000)
    if not uri:
        raise TimelineValidationError(f"镜头{shot_no}缺少视频地址")
    try:
        stream_index = int(output.get("video_stream_index") or 0)
    except (TypeError, ValueError) as exc:
        raise TimelineValidationError(
            f"镜头{shot_no}视频 stream_index 无效") from exc
    if stream_index < 0:
        raise TimelineValidationError(
            f"镜头{shot_no}视频 stream_index 不能为负")
    return {
        "uri": uri,
        "source_hash": _media_hash(uri, output),
        "provider": _clean_text(output.get("provider"), limit=160),
        "model": _clean_text(output.get("model"), limit=160),
        "stream_index": stream_index,
    }


def _raw_audio_refs(
        shot: Mapping[str, Any], output: Mapping[str, Any]) -> list[Any]:
    refs: list[Any] = []
    for source in (shot, output):
        value = source.get("audio_refs")
        if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes)):
            refs.extend(value)
        audio_uri = source.get("audio_uri")
        if audio_uri:
            refs.append({"uri": audio_uri})
    if output.get("audio_in_video") is True:
        refs.append({
            "uri": output.get("uri") or output.get("video_uri")
            or output.get("final_uri"),
            "embedded": True,
            "stream_index": output.get("audio_stream_index", 0),
            "source_hash": output.get("source_hash")
            or output.get("content_hash") or output.get("sha256"),
        })
    return refs


def _audio_refs(
        shot: Mapping[str, Any], output: Mapping[str, Any], shot_no: int,
        video_ref: Mapping[str, Any],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, bool, int]] = set()
    for position, raw in enumerate(_raw_audio_refs(shot, output), 1):
        if isinstance(raw, str):
            row: Mapping[str, Any] = {"uri": raw}
        elif isinstance(raw, Mapping):
            row = raw
        else:
            raise TimelineValidationError(
                f"镜头{shot_no}第{position}条音频引用无效")
        uri = _clean_text(row.get("uri"), limit=2000)
        if not uri:
            raise TimelineValidationError(
                f"镜头{shot_no}第{position}条音频引用缺少地址")
        embedded = row.get("embedded") is True
        try:
            stream_index = int(row.get("stream_index") or 0)
        except (TypeError, ValueError) as exc:
            raise TimelineValidationError(
                f"镜头{shot_no}音频 stream_index 无效") from exc
        if stream_index < 0:
            raise TimelineValidationError(
                f"镜头{shot_no}音频 stream_index 不能为负")
        key = (uri, embedded, stream_index)
        if key in seen:
            continue
        seen.add(key)
        source_hash = (
            dict(video_ref["source_hash"])
            if embedded and uri == video_ref.get("uri") else
            _media_hash(uri, row))
        normalized.append({
            "uri": uri,
            "embedded": embedded,
            "stream_index": stream_index,
            "source_hash": source_hash,
        })
    return normalized


def _timeline_hash_document(timeline: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in timeline.items()
            if key != "timeline_hash"}


def _validate_hash_record(value: Any, *, field: str) -> None:
    if not isinstance(value, Mapping):
        raise TimelineValidationError(f"{field} 不是哈希对象")
    algorithm = _clean_text(value.get("algorithm"), limit=40)
    digest = _clean_text(value.get("value"), limit=300)
    basis = _clean_text(value.get("basis"), limit=40)
    if algorithm not in {HASH_ALGORITHM, "declared"}:
        raise TimelineValidationError(f"{field} 哈希算法不受支持")
    if not digest:
        raise TimelineValidationError(f"{field} 哈希值为空")
    if algorithm == HASH_ALGORITHM and not re.fullmatch(
            r"[0-9a-fA-F]{64}", digest):
        raise TimelineValidationError(f"{field} 不是有效 SHA-256")
    if basis not in {"content", "declared", "reference"}:
        raise TimelineValidationError(f"{field} 哈希依据无效")


def build_timeline(
        storyboard: Any,
        video_outputs: Any,
        *,
        timeline_id: str = "",
        name: str = "",
        frame_rate: Any = DEFAULT_FRAME_RATE,
) -> dict[str, Any]:
    """Build and validate one deterministic episode timeline.

    Storyboard order is the canonical clip order.  A clip start is contiguous
    unless ``timeline_start``/``start_seconds`` is explicitly supplied on the
    storyboard shot or its video output; explicit ranges are still rejected if
    they overlap another shot.
    """
    shots = _shots_from_storyboard(storyboard)
    outputs = _normalize_video_outputs(video_outputs)
    fps = _time_value(frame_rate, field="frame_rate", allow_zero=False)
    shot_rows: list[tuple[Mapping[str, Any], int, str]] = []
    shot_numbers: set[int] = set()
    shot_ids: set[str] = set()
    for position, shot in enumerate(shots, 1):
        shot_no, shot_id = _shot_identity(shot, position)
        if shot_no in shot_numbers:
            raise TimelineValidationError(f"storyboard 镜头号重复:{shot_no}")
        if shot_id in shot_ids:
            raise TimelineValidationError(f"storyboard shot_id 重复:{shot_id}")
        shot_numbers.add(shot_no)
        shot_ids.add(shot_id)
        shot_rows.append((shot, shot_no, shot_id))

    output_by_number: dict[int, Mapping[str, Any]] = {}
    for position, output in enumerate(outputs, 1):
        shot_no = _positive_int(
            output.get("shot_no"), field=f"第{position}个视频 shot_no")
        if shot_no in output_by_number:
            raise TimelineValidationError(f"视频输出镜头号重复:{shot_no}")
        output_by_number[shot_no] = output
    missing = sorted(shot_numbers - set(output_by_number))
    extra = sorted(set(output_by_number) - shot_numbers)
    if missing:
        raise TimelineValidationError(
            "视频输出丢镜:" + "、".join(str(item) for item in missing))
    if extra:
        raise TimelineValidationError(
            "视频输出含 storyboard 外镜头:"
            + "、".join(str(item) for item in extra))

    storyboard_hash = _stable_hash(storyboard)
    # Parallel video workers may finish in any order.  Hash the outputs in the
    # authoritative storyboard order so identical episode facts always yield
    # the same exchange document.
    canonical_outputs = [
        output_by_number[shot_no] for _shot, shot_no, _shot_id in shot_rows]
    video_outputs_hash = _stable_hash(canonical_outputs)
    source_hashes = {
        "algorithm": HASH_ALGORITHM,
        "storyboard": storyboard_hash,
        "video_outputs": video_outputs_hash,
    }
    identifier = _clean_text(timeline_id, limit=500) or (
        "timeline:" + _stable_hash(source_hashes)[:24])
    clips: list[dict[str, Any]] = []
    cursor = 0.0
    for shot, shot_no, shot_id in shot_rows:
        output = output_by_number[shot_no]
        planned, duration = _clip_duration(shot, output, shot_no)
        explicit_start = _provided_start(output, shot)
        start = cursor if explicit_start is None else explicit_start
        start = round(start, TIME_PRECISION)
        transition_value = output.get("transition")
        if transition_value in (None, "", False):
            transition_value = shot.get("edit_transition")
        video_ref = _video_ref(output, shot_no)
        clip = {
            "clip_id": f"clip:{shot_id}",
            "shot_id": shot_id,
            "shot_no": shot_no,
            "source_unit_id": _clean_text(shot.get("unit_id"), limit=300),
            "start": start,
            "duration": duration,
            "end": round(start + duration, TIME_PRECISION),
            "planned_duration": planned,
            "source_range": {"start": 0.0, "duration": duration},
            "video_ref": video_ref,
            "audio_refs": _audio_refs(
                shot, output, shot_no, video_ref),
            "transition": _transition(transition_value, shot_no=shot_no),
            "source_hashes": {
                "storyboard_shot": _stable_hash(shot),
                "video_output": _stable_hash(output),
            },
        }
        clips.append(clip)
        cursor = clip["end"]

    timeline = {
        "schema": TIMELINE_SCHEMA,
        "version": TIMELINE_VERSION,
        "timeline_id": identifier,
        "name": _clean_text(name, limit=500),
        "timebase": {"frame_rate": fps, "unit": "seconds"},
        "duration": round(max((clip["end"] for clip in clips), default=0.0),
                          TIME_PRECISION),
        "source_shot_ids": [shot_id for _shot, _no, shot_id in shot_rows],
        "source_shot_numbers": [shot_no for _shot, shot_no, _id in shot_rows],
        "tracks": [
            {"track_id": "video:1", "kind": "video",
             "clip_ids": [clip["clip_id"] for clip in clips]},
            {"track_id": "audio:1", "kind": "audio",
             "clip_ids": [clip["clip_id"] for clip in clips
                          if clip["audio_refs"]]},
        ],
        "clips": clips,
        "source_hashes": source_hashes,
    }
    validate_timeline(timeline, verify_hash=False)
    timeline["timeline_hash"] = {
        "algorithm": HASH_ALGORITHM,
        "value": _stable_hash(_timeline_hash_document(timeline)),
    }
    validate_timeline(timeline)
    return timeline


def validate_timeline(
        timeline: Any,
        *,
        expected_shot_ids: Sequence[str] | None = None,
        verify_hash: bool = True,
) -> dict[str, Any]:
    """Strictly validate a timeline and return a compact audit summary."""
    if not isinstance(timeline, Mapping):
        raise TimelineValidationError("timeline 必须是对象")
    if timeline.get("schema") != TIMELINE_SCHEMA:
        raise TimelineValidationError("timeline schema 不受支持")
    if timeline.get("version") != TIMELINE_VERSION:
        raise TimelineValidationError("timeline version 不受支持")
    if not _clean_text(timeline.get("timeline_id"), limit=500):
        raise TimelineValidationError("timeline_id 不能为空")
    timebase = timeline.get("timebase")
    if not isinstance(timebase, Mapping):
        raise TimelineValidationError("timeline 缺少 timebase")
    _time_value(timebase.get("frame_rate"), field="frame_rate",
                allow_zero=False)
    clips = timeline.get("clips")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
        raise TimelineValidationError("timeline.clips 必须是列表")
    if not clips:
        raise TimelineValidationError("timeline.clips 不能为空")
    seen_clip_ids: set[str] = set()
    seen_shot_ids: set[str] = set()
    seen_shot_numbers: set[int] = set()
    normalized_ranges: list[tuple[float, float, str]] = []
    clip_order_ids: list[str] = []
    clip_order_numbers: list[int] = []
    transition_durations: list[float] = []
    for position, raw in enumerate(clips, 1):
        if not isinstance(raw, Mapping):
            raise TimelineValidationError(f"第{position}个 clip 不是对象")
        clip_id = _clean_text(raw.get("clip_id"), limit=500)
        shot_id = _clean_text(raw.get("shot_id"), limit=500)
        shot_no = _positive_int(raw.get("shot_no"), field="clip.shot_no")
        if not clip_id or not shot_id:
            raise TimelineValidationError(f"第{position}个 clip 缺少 clip/shot id")
        if clip_id in seen_clip_ids:
            raise TimelineValidationError(f"clip_id 重复:{clip_id}")
        if shot_id in seen_shot_ids:
            raise TimelineValidationError(f"shot_id 重复:{shot_id}")
        if shot_no in seen_shot_numbers:
            raise TimelineValidationError(f"shot_no 重复:{shot_no}")
        seen_clip_ids.add(clip_id)
        seen_shot_ids.add(shot_id)
        seen_shot_numbers.add(shot_no)
        clip_order_ids.append(shot_id)
        clip_order_numbers.append(shot_no)
        start = _time_value(raw.get("start"), field=f"{clip_id}.start")
        duration = _time_value(
            raw.get("duration"), field=f"{clip_id}.duration",
            allow_zero=False)
        end = round(start + duration, TIME_PRECISION)
        if _time_value(raw.get("end"), field=f"{clip_id}.end") != end:
            raise TimelineValidationError(f"{clip_id} end 与 start+duration 不一致")
        video_ref = raw.get("video_ref")
        if not isinstance(video_ref, Mapping) or not _clean_text(
                video_ref.get("uri"), limit=2000):
            raise TimelineValidationError(f"{clip_id} 缺少 video_ref")
        _validate_hash_record(
            video_ref.get("source_hash"),
            field=f"{clip_id}.video_ref.source_hash")
        try:
            video_stream_index = int(video_ref.get("stream_index") or 0)
        except (TypeError, ValueError) as exc:
            raise TimelineValidationError(
                f"{clip_id} 视频 stream_index 无效") from exc
        if video_stream_index < 0:
            raise TimelineValidationError(f"{clip_id} 视频 stream_index 不能为负")
        audio_refs = raw.get("audio_refs")
        if not isinstance(audio_refs, Sequence) or isinstance(
                audio_refs, (str, bytes)):
            raise TimelineValidationError(f"{clip_id}.audio_refs 必须是列表")
        for audio in audio_refs:
            if (not isinstance(audio, Mapping)
                    or not _clean_text(audio.get("uri"), limit=2000)):
                raise TimelineValidationError(f"{clip_id} 音频引用无效")
            _validate_hash_record(
                audio.get("source_hash"),
                field=f"{clip_id}.audio_ref.source_hash")
            try:
                audio_stream_index = int(audio.get("stream_index") or 0)
            except (TypeError, ValueError) as exc:
                raise TimelineValidationError(
                    f"{clip_id} 音频 stream_index 无效") from exc
            if audio_stream_index < 0:
                raise TimelineValidationError(
                    f"{clip_id} 音频 stream_index 不能为负")
        transition = raw.get("transition")
        if not isinstance(transition, Mapping):
            raise TimelineValidationError(f"{clip_id} 缺少 transition")
        transition_duration = _time_value(
            transition.get("duration"), field=f"{clip_id}.transition.duration")
        if not _clean_text(transition.get("type"), limit=120):
            raise TimelineValidationError(f"{clip_id} transition.type 不能为空")
        if transition_duration > duration:
            raise TimelineValidationError(f"{clip_id} 转场时长超过镜头时长")
        transition_durations.append(transition_duration)
        source_range = raw.get("source_range")
        if not isinstance(source_range, Mapping):
            raise TimelineValidationError(f"{clip_id} 缺少 source_range")
        _time_value(source_range.get("start"),
                    field=f"{clip_id}.source_range.start")
        if _time_value(
                source_range.get("duration"),
                field=f"{clip_id}.source_range.duration",
                allow_zero=False) != duration:
            raise TimelineValidationError(
                f"{clip_id} source_range.duration 与 clip 不一致")
        _time_value(raw.get("planned_duration"),
                    field=f"{clip_id}.planned_duration", allow_zero=False)
        hashes = raw.get("source_hashes")
        if not isinstance(hashes, Mapping) or not all(
                _clean_text(hashes.get(key), limit=300)
                for key in ("storyboard_shot", "video_output")):
            raise TimelineValidationError(f"{clip_id} 缺少来源哈希")
        for key in ("storyboard_shot", "video_output"):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(hashes[key])):
                raise TimelineValidationError(f"{clip_id}.{key} 哈希无效")
        normalized_ranges.append((start, end, clip_id))

    ordered_ranges = sorted(normalized_ranges, key=lambda item: (item[0], item[1]))
    if normalized_ranges != ordered_ranges:
        raise TimelineValidationError("clip 顺序与时间顺序不一致")
    for previous, current in zip(ordered_ranges, ordered_ranges[1:]):
        if current[0] < previous[1] - 10 ** (-TIME_PRECISION):
            raise TimelineValidationError(
                f"时间线重叠:{previous[2]} 与 {current[2]}")
    for index, transition_duration in enumerate(transition_durations[:-1]):
        next_duration = ordered_ranges[index + 1][1] - ordered_ranges[index + 1][0]
        if transition_duration > next_duration:
            raise TimelineValidationError(
                f"{ordered_ranges[index][2]} 转场时长超过下一镜时长")

    declared_ids = timeline.get("source_shot_ids")
    if not isinstance(declared_ids, Sequence) or isinstance(
            declared_ids, (str, bytes)):
        raise TimelineValidationError("timeline 缺少 source_shot_ids")
    declared_ids = [str(item) for item in declared_ids]
    if len(declared_ids) != len(set(declared_ids)):
        raise TimelineValidationError("source_shot_ids 含重复镜头")
    if set(declared_ids) != seen_shot_ids:
        missing = sorted(set(declared_ids) - seen_shot_ids)
        extra = sorted(seen_shot_ids - set(declared_ids))
        raise TimelineValidationError(
            f"时间线镜头集合不完整:missing={missing},extra={extra}")
    if declared_ids != clip_order_ids:
        raise TimelineValidationError("source_shot_ids 与 clip 顺序不一致")
    declared_numbers = timeline.get("source_shot_numbers")
    if not isinstance(declared_numbers, Sequence) or isinstance(
            declared_numbers, (str, bytes)):
        raise TimelineValidationError("timeline 缺少 source_shot_numbers")
    parsed_numbers = [
        _positive_int(item, field="source_shot_numbers")
        for item in declared_numbers]
    if parsed_numbers != clip_order_numbers:
        raise TimelineValidationError("source_shot_numbers 与 clip 顺序不一致")
    if expected_shot_ids is not None:
        expected = [str(item) for item in expected_shot_ids]
        if set(expected) != seen_shot_ids or len(expected) != len(clips):
            raise TimelineValidationError("时间线与预期镜头集合不一致")

    calculated_duration = round(max(item[1] for item in normalized_ranges),
                                TIME_PRECISION)
    if _time_value(timeline.get("duration"), field="timeline.duration") != \
            calculated_duration:
        raise TimelineValidationError("timeline.duration 与镜头范围不一致")
    source_hashes = timeline.get("source_hashes")
    if (not isinstance(source_hashes, Mapping)
            or source_hashes.get("algorithm") != HASH_ALGORITHM or not all(
            _clean_text(source_hashes.get(key), limit=300)
            for key in ("storyboard", "video_outputs"))):
        raise TimelineValidationError("timeline 缺少 storyboard/video 来源哈希")
    for key in ("storyboard", "video_outputs"):
        if not re.fullmatch(
                r"[0-9a-fA-F]{64}", str(source_hashes[key])):
            raise TimelineValidationError(f"timeline.source_hashes.{key} 无效")
    tracks = timeline.get("tracks")
    if not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes)):
        raise TimelineValidationError("timeline.tracks 必须是列表")
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    track_ids: set[str] = set()
    for raw in tracks:
        if not isinstance(raw, Mapping):
            raise TimelineValidationError("timeline.tracks 包含非对象")
        track_id = _clean_text(raw.get("track_id"), limit=300)
        kind = _clean_text(raw.get("kind"), limit=80)
        clip_ids = raw.get("clip_ids")
        if (not track_id or not kind or not isinstance(clip_ids, Sequence)
                or isinstance(clip_ids, (str, bytes))):
            raise TimelineValidationError("timeline track 字段无效")
        if track_id in track_ids:
            raise TimelineValidationError(f"track_id 重复:{track_id}")
        track_ids.add(track_id)
        normalized_clip_ids = [str(item) for item in clip_ids]
        if len(normalized_clip_ids) != len(set(normalized_clip_ids)):
            raise TimelineValidationError(f"{track_id} 含重复 clip")
        if any(item not in seen_clip_ids for item in normalized_clip_ids):
            raise TimelineValidationError(f"{track_id} 引用了未知 clip")
        by_kind.setdefault(kind, []).append({
            "track_id": track_id, "clip_ids": normalized_clip_ids})
    if len(by_kind.get("video", [])) != 1:
        raise TimelineValidationError("timeline 必须且只能有一条视频轨")
    if by_kind["video"][0]["clip_ids"] != [
            str(clip.get("clip_id")) for clip in clips]:
        raise TimelineValidationError("视频轨没有按顺序覆盖全部镜头")
    if len(by_kind.get("audio", [])) > 1:
        raise TimelineValidationError("timeline 最多只能有一条音频轨")
    expected_audio = [
        str(clip.get("clip_id")) for clip in clips
        if clip.get("audio_refs")]
    if by_kind.get("audio"):
        if by_kind["audio"][0]["clip_ids"] != expected_audio:
            raise TimelineValidationError("音频轨与 clip 音频引用不一致")
    elif expected_audio:
        raise TimelineValidationError("存在音频引用但缺少音频轨")
    if verify_hash:
        timeline_hash = timeline.get("timeline_hash")
        if not isinstance(timeline_hash, Mapping):
            raise TimelineValidationError("timeline 缺少 timeline_hash")
        if timeline_hash.get("algorithm") != HASH_ALGORITHM:
            raise TimelineValidationError("timeline_hash 算法不受支持")
        expected_hash = _stable_hash(_timeline_hash_document(timeline))
        if str(timeline_hash.get("value") or "") != expected_hash:
            raise TimelineValidationError("timeline_hash 不匹配，时间线已变更")
    return {
        "valid": True,
        "schema": TIMELINE_SCHEMA,
        "version": TIMELINE_VERSION,
        "clips": len(clips),
        "duration": calculated_duration,
        "shot_ids": declared_ids,
    }


def export_timeline_json(
        timeline: Mapping[str, Any], destination: str | os.PathLike[str],
        *,
        indent: int = 2,
) -> Path:
    """Validate and atomically export a deterministic UTF-8 JSON document."""
    validate_timeline(timeline)
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _stable_json(timeline, indent=indent) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def load_timeline_json(source: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and validate an exported timeline without trusting its hash."""
    path = Path(source).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TimelineValidationError(f"无法读取时间线 JSON:{exc}") from exc
    validate_timeline(value)
    return value
