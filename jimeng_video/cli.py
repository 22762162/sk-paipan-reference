"""《卡卡学姐的第一课》Seedance 2 生成包 —— 命令行工具。

子命令：
  plan                    打印分段/变速/102 秒计划（离线，无需素材）
  init     <生成包目录>    建立目录骨架 + 提示词占位
  check    <生成包目录>    校验参考帧/共享衔接帧/提示词/生成视频规格
  assemble <生成包目录>    逐段变速到 14.5s（U03 保留 15s）后拼成 102 秒成片

素材放在本机（如 iCloud 里的「4k高清/卡卡学姐的第一课_手机Seedance2生成包」），
把该目录路径传给对应子命令。assemble 需要本机安装 ffmpeg（含 ffprobe）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import assemble as _assemble
from . import ffmpeg, spec
from .package import Package, scaffold
from .validate import run_full_check


def _cmd_plan(_: argparse.Namespace) -> int:
    print(f"模型 {spec.MODEL} · {spec.MODE} · {spec.RESOLUTION_LABEL} · "
          f"{spec.ASPECT_RATIO} 竖屏 · 源片 {spec.SOURCE_SECONDS:.0f}s\n")
    print("段    参考帧（首帧 / 尾帧）                          目标时长")
    print("-" * 64)
    for s in spec.SEGMENTS:
        keep = "（保留 15s）" if s.target_seconds == spec.SOURCE_SECONDS else ""
        print(f"{s.name}  {s.head_frame:22s} / {s.tail_frame:22s}  {s.target_seconds:4.1f}s{keep}")
    print("-" * 64)
    print(f"6×14.5 + 15 = {spec.total_target_seconds():.1f}s（成片总时长）\n")
    print("共享衔接帧（必须完全一致）：")
    for tail, head in spec.SHARED_FRAME_PAIRS:
        print(f"  {tail}  ≡  {head}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.package).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    created = scaffold(root)
    if created:
        print(f"已在 {root} 建立骨架：")
        for p in created:
            print(f"  + {p.relative_to(root)}")
    else:
        print(f"骨架已存在，无需改动：{root}")
    print("\n下一步：把参考帧 PNG 放进 "
          f"{spec.FRAMES_DIR}/，把逐段提示词粘进 {spec.PROMPTS_DIR}/，"
          f"手机生成的 U01–U07.mp4 放进 {spec.VIDEOS_DIR}/。")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    pkg = Package(Path(args.package).expanduser())
    report = run_full_check(pkg, probe_media=not args.no_probe)
    print(report.render())
    print("\n人工目检 6 项（CLI 不自动判定）：")
    for i, item in enumerate(spec.HUMAN_CHECKLIST, 1):
        print(f"  {i}. {item}")
    if report.failed:
        print("\n存在 FAIL：请先补齐/修正后再 assemble。")
        return 1
    return 0


def _cmd_assemble(args: argparse.Namespace) -> int:
    pkg = Package(Path(args.package).expanduser())
    output = Path(args.output).expanduser() if args.output else None
    try:
        if not args.dry_run:
            # 拼接前先做一次视频存在性/规格校验，早失败。
            report = run_full_check(pkg, probe_media=True)
            if report.failed:
                print(report.render())
                print("\n校验未通过，已中止拼接。")
                return 1
        plans = _assemble.build_plans(pkg)
        print(_assemble.plan_summary(plans) + "\n")
        dst = _assemble.assemble(
            pkg,
            output=output,
            fps=args.fps,
            keep_audio=not args.no_audio,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print("\n（dry-run：以上命令未执行。）")
        else:
            print(f"\n成片已输出：{dst}")
        return 0
    except ffmpeg.FfmpegNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"拼接失败：{exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jimeng_video",
        description="《卡卡学姐的第一课》手机 Seedance 2 生成包 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="打印分段/变速/102 秒计划")
    p_plan.set_defaults(func=_cmd_plan)

    p_init = sub.add_parser("init", help="建立生成包目录骨架")
    p_init.add_argument("package", help="生成包目录路径")
    p_init.set_defaults(func=_cmd_init)

    p_check = sub.add_parser("check", help="校验生成包")
    p_check.add_argument("package", help="生成包目录路径")
    p_check.add_argument("--no-probe", action="store_true",
                         help="跳过 ffprobe 媒体探测，仅查文件是否存在")
    p_check.set_defaults(func=_cmd_check)

    p_asm = sub.add_parser("assemble", help="拼接 102 秒成片")
    p_asm.add_argument("package", help="生成包目录路径")
    p_asm.add_argument("-o", "--output", help="成片输出路径（默认 06_成片/…）")
    p_asm.add_argument("--fps", type=int, default=30, help="归一化输出帧率（默认 30）")
    p_asm.add_argument("--no-audio", action="store_true", help="成片去除音轨")
    p_asm.add_argument("--dry-run", action="store_true", help="只打印 ffmpeg 命令，不执行")
    p_asm.set_defaults(func=_cmd_assemble)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
