"""batch 批量制作与 QC 媒体健全性检查测试。"""

from pathlib import Path

import pytest

from aifos.cli import main
from aifos.config import Config
from aifos.qc_center import QcCenter


def test_batch_cli(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    code = main(["--workspace", ws, "batch", "万妖图录",
                 "--from", "1", "--to", "2"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("人物/道具待选") >= 2
    assert main(["--workspace", ws, "batch", "万妖图录",
                 "--from", "3", "--to", "2"]) == 2


def _qc():
    return QcCenter(Config.load("/nonexistent"))


def _sanity_issues(tmp_path, filename, content, kind="video", **ids):
    path = tmp_path / filename
    path.write_bytes(content)
    ctx = {"images": [], "videos": [], "voices": []}
    key = {"image": "images", "video": "videos", "voice": "voices"}[kind]
    ctx[key] = [{"uri": str(path), **ids}]
    return [i for i in _qc()._check_media_sanity(ctx)
            if i["check"] == "media_sanity"]


def test_corrupt_mp4_flagged(tmp_path):
    issues = _sanity_issues(
        tmp_path, "shot.mp4", b"not a real video", shot_no=1)
    assert issues and issues[0]["rerunnable"]


def test_valid_mp4_passes(tmp_path):
    issues = _sanity_issues(
        tmp_path, "shot.mp4", b"\x00\x00\x00 ftypisom....", shot_no=1)
    assert issues == []


def test_corrupt_png_flagged(tmp_path):
    issues = _sanity_issues(
        tmp_path, "img.png", b"JUNK", kind="image", shot_no=2)
    assert issues and not issues[0]["rerunnable"]


def test_valid_wav_and_svg_pass(tmp_path):
    assert _sanity_issues(
        tmp_path, "v.wav", b"RIFF....WAVE", kind="voice", line_no=1) == []
    assert _sanity_issues(
        tmp_path, "i.svg", b"<svg xmlns='x'></svg>", kind="image",
        shot_no=1) == []


def test_empty_file_flagged(tmp_path):
    issues = _sanity_issues(tmp_path, "v.wav", b"", kind="voice", line_no=1)
    assert issues and "空文件" in issues[0]["message"]


def test_json_descriptors_skipped(tmp_path):
    assert _sanity_issues(
        tmp_path, "v.video.json", b"{}", shot_no=1) == []
