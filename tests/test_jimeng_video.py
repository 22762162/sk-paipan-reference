"""jimeng_video 单测：计划数学、探测解析、命令构造、结构/衔接帧校验。

不依赖本机 ffmpeg —— 探测解析用合成 JSON，命令构造是纯函数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jimeng_video import ffmpeg, spec
from jimeng_video.assemble import Plan, plan_summary
from jimeng_video.package import Package, file_sha256, scaffold
from jimeng_video.report import Level
from jimeng_video.validate import (
    check_frames,
    check_shared_frames,
    run_full_check,
)
from jimeng_video.report import Report


# ── 规格数学 ────────────────────────────────────────────────────────
def test_total_is_exactly_102():
    assert spec.total_target_seconds() == 102.0


def test_only_u03_keeps_15s():
    keep15 = [s.name for s in spec.SEGMENTS if s.target_seconds == 15.0]
    assert keep15 == ["U03"]


def test_seven_segments_in_order():
    assert [s.name for s in spec.SEGMENTS] == [f"U0{i}" for i in range(1, 8)]


def test_shared_pairs_reference_real_frames():
    all_frames = {s.head_frame for s in spec.SEGMENTS} | {s.tail_frame for s in spec.SEGMENTS}
    for tail, head in spec.SHARED_FRAME_PAIRS:
        assert tail in all_frames and head in all_frames


# ── ffprobe 解析 ────────────────────────────────────────────────────
def _probe_json(w=720, h=1280, dur="15.0", audio=True, fps="30/1"):
    streams = [{
        "codec_type": "video", "codec_name": "h264",
        "width": w, "height": h, "avg_frame_rate": fps, "duration": dur,
    }]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"duration": dur}}


def test_parse_probe_basic():
    info = ffmpeg.parse_probe(_probe_json())
    assert (info.width, info.height) == (720, 1280)
    assert info.duration == 15.0
    assert info.has_audio is True
    assert abs(info.aspect - spec.ASPECT_VALUE) < 1e-9
    assert info.fps == 30.0


def test_parse_probe_no_audio_and_ntsc_fps():
    info = ffmpeg.parse_probe(_probe_json(audio=False, fps="30000/1001"))
    assert info.has_audio is False
    assert round(info.fps, 2) == 29.97


def test_parse_probe_handles_missing_fields():
    info = ffmpeg.parse_probe({"streams": [], "format": {}})
    assert info.duration is None and info.width is None and info.aspect is None


# ── retime 命令构造（纯函数）────────────────────────────────────────
def test_retime_ratio_and_tempo_15_to_14_5():
    cmd = ffmpeg.build_retime_cmd(
        Path("U01.mp4"), Path("out.mp4"),
        src_duration=15.0, target_duration=14.5,
        fps=30, keep_audio=True, has_audio=True,
    )
    joined = " ".join(cmd)
    # setpts 乘子 = 14.5/15 ≈ 0.9667；atempo = 15/14.5 ≈ 1.0345
    assert "setpts=0.966666667*PTS" in joined
    assert "fps=30" in joined
    assert "atempo=1.034482759" in joined
    assert "-t 14.500000" in joined
    assert "0:a:0" in joined  # 已有音轨直接映射


def test_retime_injects_silence_when_no_audio():
    cmd = ffmpeg.build_retime_cmd(
        Path("U04.mp4"), Path("out.mp4"),
        src_duration=15.0, target_duration=14.5,
        fps=30, keep_audio=True, has_audio=False,
    )
    joined = " ".join(cmd)
    assert "anullsrc" in joined
    assert "1:a:0" in joined  # 静音源作为第二输入的音轨
    assert "-shortest" in joined


def test_retime_no_audio_flag_strips_sound():
    cmd = ffmpeg.build_retime_cmd(
        Path("U03.mp4"), Path("out.mp4"),
        src_duration=15.0, target_duration=15.0,
        fps=30, keep_audio=False, has_audio=True,
    )
    assert "-an" in cmd and "atempo" not in " ".join(cmd)


def test_u03_retime_is_near_identity():
    cmd = ffmpeg.build_retime_cmd(
        Path("U03.mp4"), Path("out.mp4"),
        src_duration=15.0, target_duration=15.0,
        fps=30, keep_audio=True, has_audio=True,
    )
    assert "setpts=1.000000000*PTS" in " ".join(cmd)


def test_concat_cmd_uses_demuxer_copy():
    cmd = ffmpeg.build_concat_cmd(Path("list.txt"), Path("final.mp4"), keep_audio=True)
    joined = " ".join(cmd)
    assert "-f concat" in joined and "-c copy" in joined and "+faststart" in joined


def test_plan_speed_and_summary():
    plans = [
        Plan(spec.SEGMENTS[0], Path("U01.mp4"), 15.0, 14.5),
        Plan(spec.SEGMENTS[2], Path("U03.mp4"), 15.0, 15.0),
    ]
    assert abs(plans[0].speed - 15.0 / 14.5) < 1e-9
    assert "U01" in plan_summary(plans) and "U03" in plan_summary(plans)


# ── 结构 / 衔接帧校验（磁盘，无需 ffmpeg）───────────────────────────
def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_scaffold_creates_dirs_and_prompts(tmp_path: Path):
    created = scaffold(tmp_path)
    assert (tmp_path / spec.PROMPTS_DIR / "U01.txt").is_file()
    assert (tmp_path / spec.VIDEOS_DIR).is_dir()
    # 幂等：再次运行不新增
    assert scaffold(tmp_path) == []
    assert created  # 首次有产出


def test_shared_frames_identical_passes(tmp_path: Path):
    pkg = Package(tmp_path)
    for tail, head in spec.SHARED_FRAME_PAIRS:
        _write(pkg.frame(tail), b"IDENTICAL-PNG-BYTES")
        _write(pkg.frame(head), b"IDENTICAL-PNG-BYTES")
    report = Report()
    check_shared_frames(pkg, report)
    assert not report.failed
    assert sum(f.level is Level.OK for f in report.findings) == len(spec.SHARED_FRAME_PAIRS)


def test_shared_frames_divergent_fails(tmp_path: Path):
    pkg = Package(tmp_path)
    tail, head = spec.SHARED_FRAME_PAIRS[0]
    _write(pkg.frame(tail), b"AAA")
    _write(pkg.frame(head), b"BBB")  # 内容不同 → 必须 FAIL
    report = Report()
    check_shared_frames(pkg, report)
    assert report.failed


def test_missing_head_frame_fails(tmp_path: Path):
    pkg = Package(tmp_path)
    _write(pkg.frame(spec.SEGMENTS[0].tail_frame), b"x")  # 只放尾帧
    report = Report()
    check_frames(pkg, report)
    assert report.failed


def test_file_sha256_matches_for_identical_bytes(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"hello"); b.write_bytes(b"hello")
    assert file_sha256(a) == file_sha256(b)


def test_run_full_check_missing_root(tmp_path: Path):
    report = run_full_check(Package(tmp_path / "nope"), probe_media=False)
    assert report.failed
