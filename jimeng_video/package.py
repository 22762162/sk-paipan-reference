"""生成包目录模型：定位、脚手架（init）、文件哈希。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import spec


@dataclass(frozen=True)
class Package:
    """一个「_手机Seedance2生成包」目录。"""

    root: Path

    @property
    def frames_dir(self) -> Path:
        return self.root / spec.FRAMES_DIR

    @property
    def prompts_dir(self) -> Path:
        return self.root / spec.PROMPTS_DIR

    @property
    def videos_dir(self) -> Path:
        return self.root / spec.VIDEOS_DIR

    @property
    def final_dir(self) -> Path:
        return self.root / spec.FINAL_DIR

    def frame(self, name: str) -> Path:
        return self.frames_dir / name

    def prompt(self, seg: spec.Segment) -> Path:
        return self.prompts_dir / seg.prompt_file

    def video(self, seg: spec.Segment) -> Path:
        return self.videos_dir / seg.video_file

    @property
    def final_path(self) -> Path:
        return self.final_dir / spec.FINAL_NAME


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scaffold(root: Path) -> list[Path]:
    """创建生成包目录骨架与提示词占位文件；返回新建的路径列表。

    已存在的目录/文件不覆盖（幂等）。
    """

    created: list[Path] = []

    for sub in (spec.FRAMES_DIR, spec.PROMPTS_DIR, spec.VIDEOS_DIR, spec.FINAL_DIR):
        d = root / sub
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)

    for seg in spec.SEGMENTS:
        p = root / spec.PROMPTS_DIR / seg.prompt_file
        if not p.exists():
            p.write_text(spec.PROMPT_PLACEHOLDER.format(name=seg.name), encoding="utf-8")
            created.append(p)

    readme = root / "README.txt"
    if not readme.exists():
        readme.write_text(_readme_text(), encoding="utf-8")
        created.append(readme)

    return created


def _readme_text() -> str:
    frame_lines = "\n".join(
        f"  {s.name}: {s.head_frame} + {s.tail_frame}" for s in spec.SEGMENTS
    )
    return (
        "《卡卡学姐的第一课》手机 Seedance 2 生成包\n"
        "==========================================\n\n"
        f"目录结构：\n"
        f"  {spec.FRAMES_DIR}/   放全部参考帧 PNG（首帧/尾帧/共享衔接帧）\n"
        f"  {spec.PROMPTS_DIR}/  逐段提示词 TXT（U01.txt … U07.txt）\n"
        f"  {spec.VIDEOS_DIR}/   手机生成后下载的 U01.mp4 … U07.mp4 放这里\n"
        f"  {spec.FINAL_DIR}/    CLI 拼接输出的 102 秒成片\n\n"
        "各段参考帧：\n" + frame_lines + "\n\n"
        "生成参数：Seedance 2.0 / 首尾帧 / 720p / 9:16 竖屏 / 15 秒 / 每段先 1 条；\n"
        "不要字幕贴纸水印，关闭自动配乐（环境音可留）。\n\n"
        "校验与拼接：\n"
        "  python3 -m jimeng_video check    <本目录>\n"
        "  python3 -m jimeng_video assemble <本目录>\n"
    )
