"""生成包校验：结构、参考帧、共享衔接帧、生成视频技术规格。"""

from __future__ import annotations

from . import ffmpeg, spec
from .package import Package, file_sha256
from .report import Report


def check_frames(pkg: Package, report: Report) -> None:
    """每段首帧/尾帧文件是否齐备。"""

    for seg in spec.SEGMENTS:
        for role, name in (("首帧", seg.head_frame), ("尾帧", seg.tail_frame)):
            path = pkg.frame(name)
            if path.is_file():
                report.ok(seg.name, f"{role} 就位：{name}")
            else:
                report.fail(seg.name, f"缺{role}参考帧：{spec.FRAMES_DIR}/{name}")


def check_shared_frames(pkg: Package, report: Report) -> None:
    """U01/U02、U02/U03、U06/U07 的共享衔接帧必须字节完全一致。"""

    for tail_name, head_name in spec.SHARED_FRAME_PAIRS:
        tail, head = pkg.frame(tail_name), pkg.frame(head_name)
        if not tail.is_file() or not head.is_file():
            report.warn("衔接帧", f"无法比对（缺文件）：{tail_name} ↔ {head_name}")
            continue
        if file_sha256(tail) == file_sha256(head):
            report.ok("衔接帧", f"共享帧一致：{tail_name} ≡ {head_name}")
        else:
            report.fail(
                "衔接帧",
                f"共享帧不一致（必须同一张，不能换图/裁图/加滤镜）："
                f"{tail_name} ≠ {head_name}",
            )


def check_prompts(pkg: Package, report: Report) -> None:
    """逐段提示词是否存在且非占位空文件。"""

    for seg in spec.SEGMENTS:
        path = pkg.prompt(seg)
        if not path.is_file():
            report.warn(seg.name, f"缺提示词：{spec.PROMPTS_DIR}/{seg.prompt_file}")
            continue
        body = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        if body.strip():
            report.ok(seg.name, "提示词已填写")
        else:
            report.warn(seg.name, "提示词还是占位空文件，未粘贴正文")


def _strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def check_videos(pkg: Package, report: Report, *, probe_media: bool = True) -> None:
    """生成视频是否齐备并满足 720p / 9:16 / ~15s 规格。

    probe_media=False 时只查文件是否存在（无 ffprobe 环境下降级）。
    """

    can_probe = probe_media and ffmpeg.have("ffprobe")
    if probe_media and not can_probe:
        report.warn("视频", "未找到 ffprobe，跳过分辨率/时长探测，仅检查文件是否存在。")

    for seg in spec.SEGMENTS:
        path = pkg.video(seg)
        if not path.is_file():
            report.fail(seg.name, f"缺生成视频：{spec.VIDEOS_DIR}/{seg.video_file}")
            continue
        if not can_probe:
            report.ok(seg.name, f"视频就位：{seg.video_file}")
            continue

        try:
            info = ffmpeg.probe(path)
        except Exception as exc:  # noqa: BLE001 — 探测失败降级为警告，不中断整体校验
            report.warn(seg.name, f"ffprobe 读取失败：{exc}")
            continue
        _judge_video(seg, info, report)


def _judge_video(seg: spec.Segment, info: ffmpeg.VideoInfo, report: Report) -> None:
    # 分辨率 / 画幅
    if info.width and info.height:
        if (info.width, info.height) == (spec.FRAME_WIDTH, spec.FRAME_HEIGHT):
            report.ok(seg.name, f"分辨率 {info.width}×{info.height}（720p 竖屏）")
        elif info.aspect and abs(info.aspect - spec.ASPECT_VALUE) < 0.02:
            report.warn(
                seg.name,
                f"画幅 9:16 但分辨率 {info.width}×{info.height} 非 720×1280。",
            )
        else:
            report.fail(
                seg.name,
                f"画幅不符 9:16：{info.width}×{info.height}"
                f"（比值 {info.aspect:.4f}，应 {spec.ASPECT_VALUE:.4f}）。",
            )
    else:
        report.warn(seg.name, "无法读取视频宽高。")

    # 时长（源片名义 15s）
    if info.duration is None:
        report.warn(seg.name, "无法读取时长。")
    elif abs(info.duration - spec.SOURCE_SECONDS) <= spec.SOURCE_TOLERANCE:
        report.ok(seg.name, f"源时长 {info.duration:.2f}s（≈15s）")
    else:
        report.warn(
            seg.name,
            f"源时长 {info.duration:.2f}s 偏离 15s；assemble 会按实测重定时到 "
            f"{seg.target_seconds:.1f}s，但请确认不是手机端裁切所致。",
        )

    if info.has_audio:
        report.ok(seg.name, "含音轨（环境音；请确认未开自动配乐）。")


def run_full_check(pkg: Package, *, probe_media: bool = True) -> Report:
    report = Report()
    if not pkg.root.is_dir():
        report.fail("结构", f"生成包目录不存在：{pkg.root}")
        return report

    for sub, label in (
        (pkg.frames_dir, spec.FRAMES_DIR),
        (pkg.prompts_dir, spec.PROMPTS_DIR),
        (pkg.videos_dir, spec.VIDEOS_DIR),
    ):
        if sub.is_dir():
            report.ok("结构", f"目录就位：{label}/")
        else:
            report.warn("结构", f"缺目录：{label}/（可先跑 init 建骨架）")

    check_frames(pkg, report)
    check_shared_frames(pkg, report)
    check_prompts(pkg, report)
    check_videos(pkg, report, probe_media=probe_media)
    return report
