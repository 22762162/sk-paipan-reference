"""macOS `say` 配音适配桥:在 Mac 上零成本产出真实配音(WAV)。

即梦 CLI 暂无 TTS 能力,此桥作为过渡产线;即梦上线配音后路由自动优先
即梦。输出 WAV(LEI16@22050),时长从 WAV 头精确读取。

配置示例(workspace/config.json):
  "say": {
    "enabled": true,
    "command": ["python3", "-m", "aifos.adapters.say_voice",
                "--voice", "Tingting"]
  }
"""

import argparse
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path


def wav_duration(path):
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate() or 1
        return round(frames / rate, 2)


def run(request, say, voice, rate, timeout):
    if request["capability"] != "voice":
        return {"ok": False,
                "error": f"say 适配桥不支持能力: {request['capability']}"}
    payload = request.get("payload", {})
    text = payload.get("text", "")
    if not text:
        return {"ok": False, "error": "台词为空"}
    if shutil.which(say) is None and not Path(say).exists():
        return {"ok": False, "error": f"say 命令不存在: {say}"}
    out_dir = Path(request["out_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    line_no = int(payload.get("line_no", 0))
    out = out_dir / f"line_{line_no:03d}.wav"
    cmd = [say, "-v", voice]
    if rate:
        cmd += ["-r", str(rate)]
    cmd += ["--file-format=WAVE", "--data-format=LEI16@22050",
            "-o", str(out), text]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"say 调用失败: {exc}"}
    if proc.returncode != 0:
        return {"ok": False,
                "error": f"say 退出码 {proc.returncode}: "
                         f"{proc.stderr.strip()[:300]}"}
    if not out.exists():
        return {"ok": False, "error": f"say 未产出音频: {out}"}
    try:
        duration = wav_duration(out)
    except Exception:
        duration = round(max(1.0, len(text) * 0.18), 2)
    return {"ok": True, "uri": str(out),
            "data": {"line_no": line_no, "duration": duration,
                     "voice": voice}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS macOS say 配音适配桥")
    parser.add_argument("--say", default="say", help="say 可执行文件路径")
    parser.add_argument("--voice", default="Tingting",
                        help="发音人(中文默认 Tingting)")
    parser.add_argument("--rate", type=int, default=0, help="语速(字/分)")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        reply = run(request, args.say, args.voice, args.rate, args.timeout)
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)}
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
