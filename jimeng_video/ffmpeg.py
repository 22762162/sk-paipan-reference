"""ffmpeg / ffprobe 封装。

命令**构造**（纯函数）与命令**执行**（subprocess）分离：
构造函数可离线单测，执行函数只在本地有 ffmpeg 时调用。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FfmpegNotFound(RuntimeError):
    """本机未安装 ffmpeg / ffprobe。"""


@dataclass(frozen=True)
class VideoInfo:
    duration: float | None
    width: int | None
    height: int | None
    codec: str | None
    has_audio: bool
    fps: float | None

    @property
    def aspect(self) -> float | None:
        if self.width and self.height:
            return self.width / self.height
        return None


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def require(*tools: str) -> None:
    missing = [t for t in tools if not have(t)]
    if missing:
        raise FfmpegNotFound(
            "本机缺少：" + "、".join(missing) + "。请先安装 ffmpeg（含 ffprobe），"
            "例如 macOS：`brew install ffmpeg`。"
        )


def _parse_fps(rate: str | None) -> float | None:
    """解析 ffprobe 的 "30000/1001" 形式帧率。"""

    if not rate or rate in ("0/0", "0"):
        return None
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            return None
        return n / d if d else None
    try:
        return float(rate)
    except ValueError:
        return None


def parse_probe(payload: dict) -> VideoInfo:
    """把 `ffprobe -show_format -show_streams -print_format json` 的字典解析成 VideoInfo。

    纯函数：不触碰磁盘，便于单测。
    """

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = None
    fmt = payload.get("format") or {}
    for src in (fmt.get("duration"), (video or {}).get("duration")):
        if src is not None:
            try:
                duration = float(src)
                break
            except (TypeError, ValueError):
                continue

    width = height = None
    codec = None
    fps = None
    if video is not None:
        width = _as_int(video.get("width"))
        height = _as_int(video.get("height"))
        codec = video.get("codec_name")
        fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))

    return VideoInfo(duration, width, height, codec, has_audio, fps)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> VideoInfo:
    """探测单个视频文件（需要 ffprobe）。"""

    require("ffprobe")
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{path}\n{proc.stderr.strip()}")
    return parse_probe(json.loads(proc.stdout or "{}"))


def build_retime_cmd(
    src: Path,
    dst: Path,
    src_duration: float,
    target_duration: float,
    *,
    fps: int,
    keep_audio: bool,
    has_audio: bool,
) -> list[str]:
    """把单段重定时到精确目标时长并归一化编码（纯函数，返回 argv）。

    setpts 乘子 = 目标/实测；音频 atempo = 实测/目标（同步变速）。
    音频若缺失且需要保留，则注入等长静音，保证 concat 各段流一致。
    """

    ratio = target_duration / src_duration  # 视频 PTS 乘子
    tempo = src_duration / target_duration  # 音频速率

    cmd = ["ffmpeg", "-y", "-i", str(src)]
    inject_silence = keep_audio and not has_audio
    if inject_silence:
        cmd += ["-f", "lavfi", "-t", f"{target_duration:.6f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    cmd += ["-filter:v", f"setpts={ratio:.9f}*PTS,fps={fps}"]

    if keep_audio:
        if inject_silence:
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        else:
            cmd += ["-filter:a", f"atempo={tempo:.9f}", "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]

    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        # 变速后固定时长，避免累计漂移。
        "-t", f"{target_duration:.6f}",
        str(dst),
    ]
    return cmd


def build_concat_cmd(list_file: Path, dst: Path, *, keep_audio: bool) -> list[str]:
    """concat demuxer 拼接（各段已在 retime 阶段归一化，可直接 -c copy）。"""

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy",
    ]
    if not keep_audio:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


def run(cmd: list[str]) -> None:
    require(cmd[0])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "命令失败：" + " ".join(cmd) + "\n" + proc.stderr.strip()[-2000:]
        )
