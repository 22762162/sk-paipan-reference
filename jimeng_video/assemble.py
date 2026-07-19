"""成片拼接：逐段重定时（15s→14.5s / U03 保留 15s）后 concat 到精确 102 秒。"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg, spec
from .package import Package


@dataclass
class Plan:
    """单段的重定时计划。"""

    seg: spec.Segment
    src: Path
    src_duration: float
    target: float

    @property
    def speed(self) -> float:
        return self.src_duration / self.target


def build_plans(pkg: Package) -> list[Plan]:
    """探测每段实测时长，生成重定时计划（需要 ffprobe）。"""

    ffmpeg.require("ffprobe")
    plans: list[Plan] = []
    for seg in spec.SEGMENTS:
        src = pkg.video(seg)
        if not src.is_file():
            raise FileNotFoundError(f"缺生成视频：{src}")
        info = ffmpeg.probe(src)
        if not info.duration or info.duration <= 0:
            raise RuntimeError(f"无法读取时长：{src}")
        plans.append(Plan(seg, src, info.duration, seg.target_seconds))
    return plans


def plan_summary(plans: list[Plan]) -> str:
    rows = ["段    实测     →  目标     变速×", "-" * 34]
    for p in plans:
        rows.append(
            f"{p.seg.name}  {p.src_duration:6.2f}s  →  {p.target:5.2f}s  {p.speed:.4f}"
        )
    total = sum(p.target for p in plans)
    rows.append("-" * 34)
    rows.append(f"合计目标时长：{total:.2f}s")
    return "\n".join(rows)


def assemble(
    pkg: Package,
    *,
    output: Path | None = None,
    fps: int = 30,
    keep_audio: bool = True,
    dry_run: bool = False,
) -> Path:
    """执行拼接，返回成片路径。dry_run 只打印将执行的命令。"""

    ffmpeg.require("ffprobe", "ffmpeg")
    plans = build_plans(pkg)

    dst = output or pkg.final_path
    dst.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="jimeng_") as tmp:
        work = Path(tmp)
        parts: list[Path] = []
        for p in plans:
            part = work / f"{p.seg.name}_retimed.mp4"
            cmd = ffmpeg.build_retime_cmd(
                p.src, part, p.src_duration, p.target,
                fps=fps, keep_audio=keep_audio, has_audio=_has_audio(p.src),
            )
            if dry_run:
                print(" ".join(cmd))
            else:
                ffmpeg.run(cmd)
            parts.append(part)

        list_file = work / "concat.txt"
        list_body = "".join(f"file '{p.as_posix()}'\n" for p in parts)
        concat_cmd = ffmpeg.build_concat_cmd(list_file, dst, keep_audio=keep_audio)
        if dry_run:
            print(f"# {list_file} 内容：\n{list_body.rstrip()}")
            print(" ".join(concat_cmd))
            return dst

        list_file.write_text(list_body, encoding="utf-8")
        ffmpeg.run(concat_cmd)

    _verify_final(dst)
    return dst


def _has_audio(path: Path) -> bool:
    try:
        return ffmpeg.probe(path).has_audio
    except Exception:  # noqa: BLE001
        return False


def _verify_final(dst: Path) -> None:
    info = ffmpeg.probe(dst)
    if info.duration is None:
        return
    drift = abs(info.duration - spec.TARGET_TOTAL_SECONDS)
    if drift > spec.FINAL_TOLERANCE:
        raise RuntimeError(
            f"成片时长 {info.duration:.2f}s 偏离 102.00s 达 {drift:.2f}s，超过容差。"
        )
