"""全景机位切片:720° 全景母版 → 本镜机位方向的确定性背景基准。

空间前置架构的核心一环(v7 实证):场景几何唯一真相是全景母版;每一镜的
背景不靠模型想象,而是用 ffmpeg v360 从全景按本镜机位(yaw/pitch/视场)
做等距圆柱→透视的数学投影,零成本、零漂移。

坐标约定(全平台统一):全景图水平中心(yaw=0)即 blocking 三维空间的 +Z
方向("场景正向");yaw 向 +X 为正(向右)。blocking 的摄影机三维数据
(start_3d→target_3d)据此直接换算成切片参数。
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

# 9:16 出片的切片默认尺寸;宽视场时 v360 自动处理透视
SLICE_SIZE = (810, 1440)
_FFMPEG_CANDIDATES = ("ffmpeg", str(Path.home() / ".local/bin/ffmpeg"))


def find_ffmpeg():
    for candidate in _FFMPEG_CANDIDATES:
        path = shutil.which(candidate) or (
            candidate if Path(candidate).exists() else None)
        if path:
            return path
    return ""


def view_params_from_block(block):
    """blocking 单镜条目 → (yaw°, pitch°, h_fov°);缺数据返回 None。

    yaw/pitch 由摄影机位置指向瞄准点的向量得出;h_fov 直接取 blocking
    已算好的 fov_degrees(它就是水平视场)。
    """
    camera = (block or {}).get("camera") or {}
    cam = camera.get("start_3d") or camera.get("position_3d") or {}
    target = (camera.get("target_start_3d") or camera.get("target_3d")
              or {})
    try:
        dx = float(target["x"]) - float(cam["x"])
        dy = float(target.get("y", cam.get("y", 0))) - float(cam.get("y", 0))
        dz = float(target["z"]) - float(cam["z"])
    except (KeyError, TypeError, ValueError):
        return None
    horizontal = math.hypot(dx, dz)
    if horizontal < 1e-6 and abs(dy) < 1e-6:
        return None
    yaw = math.degrees(math.atan2(dx, dz))
    pitch = math.degrees(math.atan2(dy, horizontal))
    try:
        h_fov = float(camera.get("fov_degrees") or 0)
    except (TypeError, ValueError):
        h_fov = 0
    if not 5 <= h_fov <= 120:
        h_fov = 46.0          # 中景兜底
    # 俯仰钳制:超±40°的切片畸变过大,对背景参考已无意义
    pitch = max(-40.0, min(40.0, pitch))
    return round(yaw, 1), round(pitch, 1), round(h_fov, 1)


def slice_panorama(pano_path, out_dir, yaw, pitch, h_fov,
                   size=SLICE_SIZE, ffmpeg=""):
    """从等距圆柱全景切出透视背景图;同参数缓存复用。返回路径或 ""。"""
    pano = Path(str(pano_path or ""))
    if not pano.exists():
        return ""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return ""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = size
    v_fov = math.degrees(
        2 * math.atan(math.tan(math.radians(h_fov) / 2) * height / width))
    dest = out_dir / (
        f"slice_y{yaw:+.1f}_p{pitch:+.1f}_f{h_fov:.1f}"
        f"_{width}x{height}.png").replace("+", "p").replace("-", "m")
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(pano), "-vf",
             f"v360=input=e:output=flat:yaw={yaw}:pitch={pitch}"
             f":h_fov={h_fov:.1f}:v_fov={v_fov:.1f},"
             f"scale={width}:{height}",
             str(dest)],
            check=True, capture_output=True, timeout=180,
            stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return ""
    return str(dest) if dest.exists() else ""


def slice_for_block(pano_path, out_dir, block, ffmpeg=""):
    """一步到位:blocking 单镜条目 → 本镜背景切片路径(失败静默返 "")。"""
    params = view_params_from_block(block)
    if not params:
        return ""
    yaw, pitch, h_fov = params
    return slice_panorama(pano_path, out_dir, yaw, pitch, h_fov,
                          ffmpeg=ffmpeg)
