"""《卡卡学姐的第一课》手机 Seedance 2 生成包 —— 规格常量。

全部数值直接来自生成说明（`ddff4c1f-__Seedance2____.txt`）：

- 每段 720p、9:16 竖屏、15 秒、首尾帧生成；
- 后期 U01/U02/U04/U05/U06/U07 轻微变速到 14.5 秒，U03 保留 15 秒；
  6×14.5 + 15 = 102.0 秒成片；
- U01/U02、U02/U03、U06/U07 使用完全相同的共享衔接帧。

本模块只存放事实常量，不做任何 IO / 计算，方便单测直接引用。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 生成参数（每段一致）─────────────────────────────────────────────
MODEL = "Seedance 2.0"
MODE = "首尾帧生成"
RESOLUTION_LABEL = "720p"
FRAME_WIDTH = 720
FRAME_HEIGHT = 1280  # 720p × 9:16 竖屏
ASPECT_RATIO = "9:16"
ASPECT_VALUE = FRAME_WIDTH / FRAME_HEIGHT  # 0.5625
SOURCE_SECONDS = 15.0

# ── 目录约定 ────────────────────────────────────────────────────────
FRAMES_DIR = "00_参考帧"
PROMPTS_DIR = "01_逐段提示词"
VIDEOS_DIR = "05_生成视频放这里"
FINAL_DIR = "06_成片"
FINAL_NAME = "卡卡学姐的第一课_102s.mp4"

# 后期变速目标：全片精确 102 秒。
TARGET_TOTAL_SECONDS = 102.0
# ffprobe 时长读数容差（秒）：源片名义 15s，允许编码取整误差。
SOURCE_TOLERANCE = 1.0
# 成片时长自检容差（秒）。
FINAL_TOLERANCE = 0.20


@dataclass(frozen=True)
class Segment:
    """一段（U01…U07）的静态定义。"""

    name: str          # U01
    head_frame: str    # 首帧文件名
    tail_frame: str    # 尾帧文件名
    prompt_file: str   # 逐段提示词文件名
    video_file: str    # 生成后命名
    target_seconds: float  # 后期变速目标时长


def _seg(name: str, head: str, tail: str, target: float) -> Segment:
    return Segment(
        name=name,
        head_frame=head,
        tail_frame=tail,
        prompt_file=f"{name}.txt",
        video_file=f"{name}.mp4",
        target_seconds=target,
    )


# 生成顺序与图片选择（说明书「二、生成顺序与图片选择」）。
SEGMENTS: tuple[Segment, ...] = (
    _seg("U01", "U01_首帧.png", "U01_尾帧_U02首帧.png", 14.5),
    _seg("U02", "U02_首帧_U01尾帧.png", "U02_尾帧_U03首帧.png", 14.5),
    _seg("U03", "U03_首帧_U02尾帧.png", "U03_尾帧.png", 15.0),
    _seg("U04", "U04_首帧.png", "U04_尾帧.png", 14.5),
    _seg("U05", "U05_首帧.png", "U05_尾帧.png", 14.5),
    _seg("U06", "U06_首帧.png", "U06_尾帧_U07首帧.png", 14.5),
    _seg("U07", "U07_首帧_U06尾帧.png", "U07_尾帧.png", 14.5),
)

# 必须完全相同的共享衔接帧对（尾帧文件 ≡ 下一段首帧文件）。
SHARED_FRAME_PAIRS: tuple[tuple[str, str], ...] = (
    ("U01_尾帧_U02首帧.png", "U02_首帧_U01尾帧.png"),
    ("U02_尾帧_U03首帧.png", "U03_首帧_U02尾帧.png"),
    ("U06_尾帧_U07首帧.png", "U07_首帧_U06尾帧.png"),
)

# 说明书「四、每段生成后先看这 6 项」——人工目检项，CLI 只做提醒，不自动判定。
HUMAN_CHECKLIST: tuple[str, ...] = (
    "画面始终只有卡卡学姐和小豆，不能出现第三人或人物复制。",
    "卡卡保持蓝色高马尾和淡紫制服；小豆保持金色短发和橙色制服。",
    "两人不能换脸、换装、忽高忽矮。",
    "手指、脚步、手机、自拍杆、奶盒和面包不能畸形或突然消失。",
    "不得出现可读文字、字幕、数字、品牌、水印或乱码。",
    "片段最后一秒要自然到达尾帧姿势，不能突然跳帧。",
)

# 逐段提示词占位（init 用）——真正的提示词由用户从原始生成包复制进来。
PROMPT_PLACEHOLDER = (
    "# {name} 提示词\n"
    "#\n"
    "# 把《卡卡学姐的第一课》原始生成包 01_逐段提示词/ 里 {name} 的整段提示词\n"
    "# 全选复制粘贴到本文件（不要自行增删）。生成设置见生成说明书。\n"
)


def total_target_seconds() -> float:
    """按段目标时长求和（应恒等于 102.0）。"""

    return round(sum(s.target_seconds for s in SEGMENTS), 6)
