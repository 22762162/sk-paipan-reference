import json
import subprocess
from pathlib import Path

import pytest

from aifos.video_media_qc import (
    QC_CACHE_DIRECTORY,
    build_sample_points,
    evaluate_video_technical,
    expected_dimensions,
    extract_video_qc_frames,
    probe_video,
    sample_timestamps,
    video_media_signature,
)


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode,
        stdout=stdout, stderr=stderr)


def _valid_probe(**overrides):
    probe = {
        "schema": "aifos.video-media-probe/v1",
        "source": "/tmp/shot.mp4",
        "is_remote": False,
        "probed": True,
        "probe_ok": True,
        "ok": True,
        "error_code": "",
        "error": "",
        "has_video": True,
        "has_audio": True,
        "video_streams": [{
            "index": 0, "codec": "h264", "width": 720,
            "height": 1280, "fps": 25.0, "duration": 8.0,
        }],
        "audio_streams": [{
            "index": 1, "codec": "aac", "channels": 2,
            "sample_rate": 48000, "duration": 8.0,
        }],
        "width": 720,
        "height": 1280,
        "fps": 25.0,
        "duration": 8.0,
    }
    probe.update(overrides)
    return probe


def test_probe_video_reads_streams_dimensions_fps_duration_and_audio(tmp_path):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"video")
    payload = {
        "streams": [
            {
                "index": 0, "codec_type": "video", "codec_name": "h264",
                "width": 720, "height": 1280,
                "avg_frame_rate": "25/1", "duration": "7.96",
                "pix_fmt": "yuv420p",
            },
            {
                "index": 1, "codec_type": "audio", "codec_name": "aac",
                "channels": 2, "sample_rate": "48000", "duration": "8.0",
            },
        ],
        "format": {
            "duration": "8.000000", "format_name": "mov,mp4",
            "size": "2048", "bit_rate": "1000000",
        },
    }
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(json.dumps(payload))

    result = probe_video(
        video, runner=runner, binary_finder=lambda _name: "/fake/ffprobe")

    assert result["probe_backend"] == "ffprobe"
    assert result["probe_ok"] is True
    assert result["has_video"] is True
    assert result["has_audio"] is True
    assert result["width"] == 720
    assert result["height"] == 1280
    assert result["fps"] == 25.0
    assert result["duration"] == 8.0
    assert result["video_streams"][0]["codec"] == "h264"
    assert result["audio_streams"][0]["codec"] == "aac"
    assert calls[0][0][-1] == str(video)
    assert calls[0][1]["timeout"] == 30.0
    assert len(calls) == 1, "ffprobe 成功后不得重复调用 ffmpeg"


def test_probe_video_applies_rotation_to_display_dimensions(tmp_path):
    video = tmp_path / "rotated.mp4"
    video.write_bytes(b"video")
    payload = {
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "h264",
            "width": 1280, "height": 720, "avg_frame_rate": "30/1",
            "side_data_list": [{"rotation": -90}],
        }],
        "format": {"duration": "4.0"},
    }

    result = probe_video(
        video, runner=lambda *_args, **_kwargs: _completed(
            json.dumps(payload)),
        binary_finder=lambda _name: "/fake/ffprobe")

    assert result["video_streams"][0]["coded_width"] == 1280
    assert result["video_streams"][0]["coded_height"] == 720
    assert result["width"] == 720
    assert result["height"] == 1280
    assert result["video_streams"][0]["rotation"] == 270


def test_both_media_probes_unavailable_is_an_explicit_failure(tmp_path):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"video")

    result = probe_video(video, binary_finder=lambda _name: None)
    technical = evaluate_video_technical(result)

    assert result["probed"] is False
    assert result["error_code"] == "media_probe_unavailable"
    assert technical["passed"] is False
    assert technical["issues"][0]["code"] == "media_probe_unavailable"


def test_ffmpeg_fallback_parses_real_stream_facts_when_ffprobe_is_missing(
        tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video")
    ffmpeg_output = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'final.mp4':
  Metadata:
    major_brand     : isom
  Duration: 00:02:03.02, start: 0.000000, bitrate: 2048 kb/s
  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 720x1280, 24 fps, 24 tbr
  Stream #0:1[0x2](und): Audio: aac (LC), 48000 Hz, stereo, fltp
Stream mapping:
  Stream #0:0 -> #0:0 (copy)
  Stream #0:1 -> #0:1 (copy)
frame= 2952 fps=0.0 q=-1.0 Lsize=N/A time=00:02:03.01 bitrate=N/A speed=2e+03x
"""
    calls = []

    def finder(name):
        return None if name == "ffprobe" else "/fake/ffmpeg"

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(stderr=ffmpeg_output)

    result = probe_video(
        video, runner=runner, binary_finder=finder, timeout=45)

    assert result["probe_backend"] == "ffmpeg"
    assert result["probe_ok"] is True
    assert result["width"] == 720
    assert result["height"] == 1280
    assert result["fps"] == 24.0
    assert result["duration"] == 123.02
    assert result["has_audio"] is True
    assert result["audio_streams"][0]["codec"] == "aac"
    assert result["audio_streams"][0]["channels"] == 2
    assert result["audio_streams"][0]["sample_rate"] == 48000
    assert calls[0][0] == [
        "/fake/ffmpeg", "-hide_banner", "-nostdin", "-i", str(video),
        "-map", "0:v?", "-map", "0:a?", "-c", "copy",
        "-f", "null", "-",
    ]
    assert calls[0][1]["timeout"] == 45


def test_ffprobe_failure_falls_back_to_successful_ffmpeg_probe(tmp_path):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"video")
    ffmpeg_output = """
Input #0, mov,mp4, from 'shot.mp4':
  Duration: 00:00:08.00, start: 0.000000, bitrate: 1000 kb/s
  Stream #0:0: Video: h264, yuv420p, 720x1280, 25 fps, 25 tbr
  Stream #0:1: Audio: aac, 44100 Hz, mono, fltp
frame=200 time=00:00:07.96
"""
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[0] == "/fake/ffprobe":
            return _completed(stderr="invalid atom", returncode=1)
        return _completed(stderr=ffmpeg_output)

    result = probe_video(
        video, runner=runner,
        binary_finder=lambda name: f"/fake/{name}")

    assert len(calls) == 2
    assert result["probe_backend"] == "ffmpeg"
    assert result["probe_ok"] is True
    assert result["fallback_from"]["error_code"] == "ffprobe_failed"


def test_ffmpeg_fallback_requires_successful_execution(tmp_path):
    video = tmp_path / "broken.mp4"
    video.write_bytes(b"broken")

    result = probe_video(
        video,
        runner=lambda *_args, **_kwargs: _completed(
            stderr="Invalid data found", returncode=1),
        binary_finder=lambda name: (
            None if name == "ffprobe" else "/fake/ffmpeg"))

    assert result["probed"] is False
    assert result["probe_ok"] is False
    assert result["probe_backend"] == "ffmpeg"
    assert result["error_code"] == "ffmpeg_probe_failed"


def test_remote_url_cannot_pass_without_real_probe():
    unchecked = {
        "source": "https://example.invalid/shot.mp4",
        "is_remote": True,
        "probed": False,
        "probe_ok": False,
        "has_video": True,
        "width": 720,
        "height": 1280,
        "fps": 25.0,
        "duration": 8.0,
    }

    technical = evaluate_video_technical(unchecked)
    extraction = extract_video_qc_frames(
        unchecked["source"], "/tmp/unused", probe=unchecked,
        binary_finder=lambda _name: "/fake/ffmpeg")

    assert technical["passed"] is False
    assert technical["issues"][0]["code"] == "probe_required"
    assert extraction["passed"] is False
    assert extraction["issues"][0]["code"] == "probe_required"
    with pytest.raises(ValueError, match="must be probed"):
        video_media_signature(unchecked["source"], unchecked)


def test_sample_points_are_exact_ratios_and_do_not_seek_to_eof():
    points = build_sample_points(8.0, 25.0)

    assert [point["percentage"] for point in points] == [0, 25, 50, 75, 100]
    assert sample_timestamps(8.0, 25.0) == [0.0, 2.0, 4.0, 6.0, 7.96]
    assert points[-1]["timestamp"] < 8.0


def test_extremely_short_video_deduplicates_same_decodable_frame():
    points = build_sample_points(0.02, 25.0)

    assert len(points) == 1
    assert points[0]["timestamp"] == 0.0
    assert points[0]["percentages"] == [0, 25, 50, 75, 100]
    assert points[0]["label"] == "0%/25%/50%/75%/100%"


@pytest.mark.parametrize("duration", [0, -1, float("nan"), None])
def test_sample_points_reject_invalid_duration(duration):
    with pytest.raises(ValueError, match="duration"):
        build_sample_points(duration, 25)


def test_stable_signature_tracks_content_not_path_or_mtime(tmp_path):
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(b"same-video-content")
    second.write_bytes(b"same-video-content")

    first_signature = video_media_signature(first)
    assert first_signature == video_media_signature(first)
    assert first_signature == video_media_signature(second)

    second.write_bytes(b"changed-video-content")
    assert first_signature != video_media_signature(second)


def test_expected_720p_dimensions_follow_aspect():
    assert expected_dimensions("9:16", "720p") == {
        "width": 720, "height": 1280}
    assert expected_dimensions("16:9", "720p") == {
        "width": 1280, "height": 720}
    assert expected_dimensions("1:1", "720p") == {
        "width": 720, "height": 720}
    assert expected_dimensions("bad", "720p") is None


def test_technical_qc_passes_valid_720p_portrait_with_integrated_audio():
    result = evaluate_video_technical(
        _valid_probe(), expected_aspect="9:16",
        expected_resolution="720p", expected_duration=8.0,
        require_audio=True)

    assert result["passed"] is True
    assert result["issues"] == []
    assert result["expected"]["dimensions"] == {
        "width": 720, "height": 1280}
    assert result["reference_chain_eligible"] is False


@pytest.mark.parametrize(
    ("probe", "kwargs", "code"),
    [
        (_valid_probe(width=1280, height=720), {}, "resolution_mismatch"),
        (_valid_probe(duration=8.5), {"expected_duration": 8.0},
         "duration_mismatch"),
        (_valid_probe(has_audio=False, audio_streams=[]),
         {"require_audio": True}, "audio_stream_missing"),
        (_valid_probe(fps=None), {}, "video_fps_missing"),
    ],
)
def test_technical_qc_rejects_wrong_resolution_duration_audio_or_fps(
        probe, kwargs, code):
    result = evaluate_video_technical(probe, **kwargs)

    assert result["passed"] is False
    assert code in {issue["code"] for issue in result["issues"]}


def test_duration_default_tolerance_is_one_frame_or_point_one_seconds():
    within = evaluate_video_technical(
        _valid_probe(duration=8.09, fps=25.0), expected_duration=8.0)
    outside = evaluate_video_technical(
        _valid_probe(duration=8.11, fps=25.0), expected_duration=8.0)

    assert within["passed"] is True
    assert outside["passed"] is False
    check = next(item for item in outside["checks"]
                 if item["name"] == "duration")
    assert check["tolerance"] == 0.1


def test_extract_frames_uses_dedicated_cache_and_never_marks_assets_eligible(
        tmp_path):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"deterministic-video")
    run_count = 0

    def runner(command, **_kwargs):
        nonlocal run_count
        run_count += 1
        Path(command[-1]).write_bytes(b"\x89PNG\r\nqc-frame")
        return _completed()

    report = extract_video_qc_frames(
        video, tmp_path / "cache", probe=_valid_probe(source=str(video)),
        runner=runner, binary_finder=lambda _name: "/fake/ffmpeg")

    assert report["passed"] is True
    assert report["sample_count"] == 5
    assert run_count == 5
    assert Path(report["cache_dir"]).parent.name == QC_CACHE_DIRECTORY
    assert Path(report["manifest"]).is_file()
    assert report["reference_chain_eligible"] is False
    assert report["asset_registration_allowed"] is False
    assert all(sample["reference_chain_eligible"] is False
               for sample in report["samples"])
    assert all(sample["asset_registration_allowed"] is False
               for sample in report["samples"])

    cached = extract_video_qc_frames(
        video, tmp_path / "cache", probe=_valid_probe(source=str(video)),
        runner=runner, binary_finder=lambda _name: "/fake/ffmpeg")
    assert cached["passed"] is True
    assert run_count == 5
    assert all(sample["reused"] is True for sample in cached["samples"])


def test_extract_frames_reports_ffmpeg_unavailable_without_writing_cache(
        tmp_path):
    video = tmp_path / "shot.mp4"
    video.write_bytes(b"video")

    report = extract_video_qc_frames(
        video, tmp_path / "cache", probe=_valid_probe(source=str(video)),
        binary_finder=lambda _name: None)

    assert report["passed"] is False
    assert report["samples"] == []
    assert report["issues"][0]["code"] == "ffmpeg_unavailable"
    assert not (tmp_path / "cache" / QC_CACHE_DIRECTORY).exists()


def test_remote_signature_requires_completed_probe_and_is_deterministic():
    url = "https://media.example/immutable/shot.mp4"
    probe = _valid_probe(source=url, is_remote=True)

    assert video_media_signature(url, probe) == video_media_signature(url, probe)
    with pytest.raises(ValueError, match="must be probed"):
        video_media_signature(url)
