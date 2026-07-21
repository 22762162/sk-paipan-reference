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


# 非交互出图必需:可写沙箱 + 跳过 git 仓库检查(产物目录不是 git 仓库)。
# 旧版 codex 不认识这些参数时,run() 会自动去掉重试。
DEFAULT_EXEC_ARGS = ["--sandbox", "workspace-write", "--skip-git-repo-check"]

# 强制真实出图:Codex 是编码代理,放任它就会用 Pillow 画示意图充数
GEN_DIRECTIVE = (
    "你必须使用你环境里可用的图像生成能力(图像生成技能 / 工具 / MCP,"
    "如 gpt-image 等)真实生成图片;禁止用 Pillow / matplotlib / SVG 等"
    "代码绘制示意图或占位图充数。如果完全没有图像生成能力,打印错误"
    "并以非零码退出,不要伪造图片。")


def _style_line(payload):
    style = payload.get("style") or (
        "现代都市精品漫剧，电影级半写实人物与场景，现代服装和现代建筑")
    return (f"画风要求:{style}；高细节；严格按该媒介、时代和服装要求执行；"
            "整部作品所有画面保持同一画风、同一人物造型。")


def _ref_line(payload):
    refs = [f"人物设定图 {r}" for r in payload.get("character_refs", [])]
    if payload.get("scene_ref"):
        refs.append(f"场景概念图 {payload['scene_ref']}")
    refs.extend(f"用户参考图 {r}"
                for r in payload.get("reference_images", []))
    if not refs:
        return ""
    return ("参考图(文件可直接读取;人物发型/服装/配色和场景陈设"
            "必须与参考图一致):" + ";".join(refs) + "。")


def build_instruction(capability, payload, out_dir):
    """返回 (给 codex 的指令, 期望产出的文件列表, 应答的 data 字段)。"""
    out_dir = Path(out_dir)
    width = int(payload.get("width", 1080))
    height = int(payload.get("height", 1920))
    size = f"{width}x{height},画幅 {payload.get('aspect', '9:16')}"
    feedback = payload.get("feedback", "")
    if feedback:
        payload = dict(payload)
        payload["prompt"] = (f"{payload.get('prompt', '')}。"
                             f"修改意见(必须落实):{feedback}")
    common = f"{_style_line(payload)}{GEN_DIRECTIVE}"
    if capability == "image":
        safe = "".join(c if c.isalnum() else "_"
                       for c in str(payload.get("art_name", "")))[:40]
        if payload.get("portrait"):
            target = out_dir / f"portrait_{safe}.png"
            instruction = (
                f"为角色生成立绘并保存到 {target}(PNG,{size})。"
                f"{payload.get('prompt', '')}。这张立绘是全剧的人物设定基准,"
                f"之后所有镜头都会参考它。{_ref_line(payload)}{common}"
                "只产出该文件。")
            return instruction, [target], {"name": payload.get("art_name")}
        if payload.get("character_sheet"):
            key = payload["character_sheet"]
            target = out_dir / f"sheet_{safe}_{key}.png"
            instruction = (
                f"为角色生成{payload.get('sheet_label', key)}设定资产并保存到"
                f" {target}(PNG,{size})。{payload.get('prompt', '')}。"
                "这是人物资产库的生产级设定图,必须与立绘/参考图为同一人物,"
                f"发型服装配色完全一致。{_ref_line(payload)}{common}"
                "只产出该文件。")
            return instruction, [target], {
                "name": payload.get("art_name"), "sheet": key}
        if payload.get("scene_art"):
            target = out_dir / f"scene_{safe}.png"
            instruction = (
                f"为场景生成概念图并保存到 {target}(PNG,{size})。"
                f"{payload.get('prompt', '')}。这张概念图是该场景的美术基准。"
                f"{_ref_line(payload)}{common}只产出该文件。")
            return instruction, [target], {"name": payload.get("art_name")}
        shot_no = int(payload["shot_no"])
        target = out_dir / f"shot_{shot_no:03d}.keyframe.png"
        text_asset = payload.get("readable_text") or {}
        text_rule = (
            f"画面文字载体:{text_asset.get('carrier', '')};只允许逐字出现:"
            f"{'、'.join(text_asset.get('whitelist', [])) or '白名单为空'};"
            "不得新增乱码或字幕条。"
            if text_asset.get("required") else
            "画面中不要生成字幕条、对白字幕或无关可读文字。")
        instruction = (
            f"为漫剧分镜生成一张关键图并保存到 {target}"
            f"(PNG,{size})。画面内容:{payload.get('prompt', '')}。"
            f"出场角色:{'、'.join(payload.get('characters', []))}，"
            f"严格共{payload.get('character_count', len(payload.get('characters', [])))}人，"
            f"禁止新增或复制人物。{text_rule}"
            f"镜头语言:{payload.get('camera', '')}。"
            f"{_ref_line(payload)}{common}"
            "只产出该文件,不要改动其他文件。"
        )
        return instruction, [target], {"shot_no": shot_no}
    if capability == "frames":
        shot_no = int(payload["shot_no"])
        first = out_dir / f"shot_{shot_no:03d}.first.png"
        last = out_dir / f"shot_{shot_no:03d}.last.png"
        image_uri = payload.get("image_uri", "")
        # 协议测试或人工恢复时可能给一个尚未挂载的占位路径；不要让
        # Codex 把它误判成目标文件。正式流水线的关键图已落盘，仍传绝对路径。
        if (image_uri and not image_uri.startswith(("http://", "https://"))
                and not Path(image_uri).exists()):
            image_uri = Path(image_uri).name
        instruction = (
            f"基于关键图 {image_uri}(文件可直接读取)"
            "为镜头生成首帧与尾帧,"
            f"分别保存到 {first} 和 {last}(PNG,{size})。"
            f"镜头内容:{payload.get('prompt', '')}。"
            f"起始状态:{payload.get('start_state', {})};"
            f"结尾状态:{payload.get('end_state', {})};"
            "首帧为动作起始、尾帧为动作结束，构图与关键图连贯，"
            "保持人物、服装、道具、场景与任何已锁定文字完全一致，"
            f"不新增字幕条。{_ref_line(payload)}{common}"
            "只产出这两个文件。"
        )
        return instruction, [first, last], {
            "first": str(first), "last": str(last)}
    if capability == "cover":
        target = out_dir / "cover.png"
        instruction = (
            f"为账号内容生成封面并保存到 {target}(PNG,{size})。"
            f"作品《{payload.get('title', '')}》第{payload.get('episode', 0)}集,"
            f"主题:{payload.get('tagline', '')}。构图吸睛、适合短视频封面,"
            f"可留出大标题排版空间。{common}只产出该文件。")
        return instruction, [target], {}
    raise ValueError(f"codex 适配桥不支持能力: {capability}")


def _flags_unsupported(stderr):
    """旧版 codex 不认识默认参数时的报错特征。"""
    text = (stderr or "").lower()
    return any(marker in text for marker in (
        "unexpected argument", "unrecognized", "unknown option",
        "invalid option", "unknown argument"))


def run(request, codex, timeout, extra_args, plain=False):
    capability = request["capability"]
    payload = request.get("payload", {})
    out_dir = Path(request["out_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which(codex) is None and not Path(codex).exists():
        return {"ok": False, "error": f"codex 命令不存在: {codex}"}
    instruction, targets, data = build_instruction(
        capability, payload, out_dir)
    exec_args = [] if plain else list(DEFAULT_EXEC_ARGS)

    def invoke(args):
        return subprocess.run(
            [codex, "exec", *args, *extra_args, instruction],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(out_dir))

    try:
        proc = invoke(exec_args)
        if proc.returncode != 0 and exec_args and \
                _flags_unsupported(proc.stderr):
            proc = invoke([])   # 旧版 codex:去掉默认参数重试
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"codex 调用失败: {exc}"}
    log_path = out_dir / f"codex_{capability}.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"$ codex exec …\n{proc.stdout}\n{proc.stderr}\n")
    if proc.returncode != 0:
        return {"ok": False,
                "error": f"codex 退出码 {proc.returncode}: "
                         f"{proc.stderr.strip()[:300]}"}
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        return {"ok": False,
                "error": f"codex 未产出期望文件: {', '.join(missing)}"}
    return {"ok": True, "data": data, "uri": str(targets[0]),
            "model": "codex/image_gen-managed (底层模型未披露)"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS Codex 图片适配桥")
    parser.add_argument("--codex", default="codex", help="codex 可执行文件路径")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--extra", action="append", default=[],
                        help="附加给 codex exec 的参数,可多次指定")
    parser.add_argument("--plain", action="store_true",
                        help="不带默认的 --sandbox/--skip-git-repo-check")
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        reply = run(request, args.codex, args.timeout, args.extra,
                    plain=args.plain)
    except Exception as exc:  # 协议层兜底:任何异常都以 ok:false 应答
        reply = {"ok": False, "error": str(exc)}
    # 始终退出 0:失败经 ok:false 应答传递错误详情(协议层约定)
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
