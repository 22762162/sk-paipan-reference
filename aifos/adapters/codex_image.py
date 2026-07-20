"""Codex 图片产线适配桥。

把 AIFOS 通用 CLI Provider 协议(stdin JSON 请求 → stdout JSON 应答)
转换为 `codex exec` 调用:构造明确的出图指令让 Codex 在指定路径产出
图片文件,随后校验文件是否落盘。

配置示例(workspace/config.json):
  "codex": {
    "enabled": true,
    "command": ["python3", "-m", "aifos.adapters.codex_image",
                "--codex", "/Users/sk/.local/node22/bin/codex"]
  }

支持能力:image(镜头关键图)、frames(首尾帧)、cover(封面)。
说明:这是通用出图桥;若你的 Codex 工作流有专门的出图技能/脚本,
把 build_instruction 中的指令替换为对应调用即可。
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def build_instruction(capability, payload, out_dir):
    """返回 (给 codex 的指令, 期望产出的文件列表, 应答的 data 字段)。"""
    out_dir = Path(out_dir)
    if capability == "image":
        shot_no = int(payload["shot_no"])
        target = out_dir / f"shot_{shot_no:03d}.keyframe.png"
        instruction = (
            f"为漫剧分镜生成一张关键图并保存到 {target}"
            f"(PNG,1280x720,16:9)。画面内容:{payload.get('prompt', '')}。"
            f"出场角色:{'、'.join(payload.get('characters', []))}。"
            "可用 Python(Pillow/绘制 SVG 后转换)或其他可用工具完成;"
            "只产出该文件,不要改动其他文件。"
        )
        return instruction, [target], {"shot_no": shot_no}
    if capability == "frames":
        shot_no = int(payload["shot_no"])
        first = out_dir / f"shot_{shot_no:03d}.first.png"
        last = out_dir / f"shot_{shot_no:03d}.last.png"
        instruction = (
            f"基于关键图 {payload.get('image_uri', '')} 为镜头生成首帧与尾帧,"
            f"分别保存到 {first} 和 {last}(PNG,1280x720)。"
            f"镜头内容:{payload.get('prompt', '')}。"
            "首帧为动作起始、尾帧为动作结束,保持角色与场景一致;"
            "只产出这两个文件。"
        )
        return instruction, [first, last], {
            "first": str(first), "last": str(last)}
    if capability == "cover":
        target = out_dir / "cover.png"
        instruction = (
            f"为漫剧生成竖版封面并保存到 {target}(PNG,810x1080)。"
            f"作品《{payload.get('title', '')}》第{payload.get('episode', 0)}集,"
            f"主题:{payload.get('tagline', '')}。只产出该文件。"
        )
        return instruction, [target], {}
    raise ValueError(f"codex 适配桥不支持能力: {capability}")


def run(request, codex, timeout, extra_args):
    capability = request["capability"]
    payload = request.get("payload", {})
    out_dir = Path(request["out_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which(codex) is None and not Path(codex).exists():
        return {"ok": False, "error": f"codex 命令不存在: {codex}"}
    instruction, targets, data = build_instruction(
        capability, payload, out_dir)
    cmd = [codex, "exec", *extra_args, instruction]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(out_dir))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"codex 调用失败: {exc}"}
    log_path = out_dir / f"codex_{capability}.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"$ {' '.join(cmd[:2])} …\n{proc.stdout}\n{proc.stderr}\n")
    if proc.returncode != 0:
        return {"ok": False,
                "error": f"codex 退出码 {proc.returncode}: "
                         f"{proc.stderr.strip()[:300]}"}
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        return {"ok": False,
                "error": f"codex 未产出期望文件: {', '.join(missing)}"}
    return {"ok": True, "data": data, "uri": str(targets[0])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS Codex 图片适配桥")
    parser.add_argument("--codex", default="codex", help="codex 可执行文件路径")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--extra", action="append", default=[],
                        help="附加给 codex exec 的参数,可多次指定")
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        reply = run(request, args.codex, args.timeout, args.extra)
    except Exception as exc:  # 协议层兜底:任何异常都以 ok:false 应答
        reply = {"ok": False, "error": str(exc)}
    # 始终退出 0:失败经 ok:false 应答传递错误详情(协议层约定)
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
