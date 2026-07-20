"""Claude 编剧适配桥:剧本 / 分镜由 Claude CLI 实际生成。

把 AIFOS 通用 CLI Provider 协议转换为 `claude -p` 非交互调用,
要求 Claude 输出严格 JSON,解析并校验后回传平台。

配置示例(workspace/config.json):
  "claude": {
    "enabled": true,
    "command": ["python3", "-m", "aifos.adapters.claude_script",
                "--claude", "claude"]
  }
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_PROMPT = """你是漫剧编剧。为作品《{title}》第{episode}集创作一集完整剧本。
风格:{style};本集前提:{premise}。

要求:
- 3 到 5 场戏,每场 2-4 句台词,适合 1-2 分钟短剧;
- 台词口语化,单句不超过 25 个字;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全):
{{"project_title": "{title}", "episode_number": {episode},
 "episode_title": "本集小标题", "logline": "一句话梗概",
 "characters": [{{"name": "角色名", "role": "主角/同伴/反派"}}],
 "scenes": [{{"scene_no": 1, "location": "地点",
   "characters": ["出场角色名"], "action": "本场动作描述",
   "lines": [{{"character": "角色名", "dialogue": "台词"}}]}}]}}"""

IDOL_PROMPT = """你是 AI 虚拟偶像「{persona}」的内容策划。为第{episode}期短视频写口播脚本。
人设风格:{style};本期主题:{premise}。

要求:
- 竖屏短视频节奏:开场 3 秒钩子 → 主体内容 → 结尾引导关注;
- 2 到 3 场(场=背景/机位切换),全部台词由「{persona}」一人口播;
- 台词口语化、有网感,单句不超过 22 个字;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全,characters 只含「{persona}」一人):
{{"project_title": "{persona}", "episode_number": {episode},
 "episode_title": "本期小标题", "logline": "一句话内容概要",
 "characters": [{{"name": "{persona}", "role": "主角"}}],
 "scenes": [{{"scene_no": 1, "location": "场景",
   "characters": ["{persona}"], "action": "画面动作描述",
   "lines": [{{"character": "{persona}", "dialogue": "口播台词"}}]}}]}}"""

STORYBOARD_PROMPT = """你是漫剧分镜师。基于以下剧本 JSON 生成分镜表。

剧本:
{script}

要求:
- 每场先给 1 个环境镜头(kind="environment",dialogue=null),
  再为每句台词给 1 个对白镜头(kind="dialogue",dialogue 填对应台词对象);
- shot_no 从 1 连续编号;duration 单位秒(2.0-4.0);
- prompt 为该镜头的文生图提示词(含场景、角色、镜头语言);
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块。

JSON 格式:
{{"episode_title": "...", "shots": [{{"shot_no": 1, "scene_no": 1,
  "kind": "environment", "description": "...", "camera": "镜头语言",
  "duration": 2.5, "characters": ["角色名"], "dialogue": null,
  "prompt": "文生图提示词"}}]}}"""


def extract_json(text):
    """从 Claude 输出中提取第一个合法 JSON 对象(容忍前后杂讯)。"""
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            return obj
        except ValueError:
            idx = text.find("{", idx + 1)
    return None


def validate_script(script, payload):
    if not isinstance(script, dict):
        return "输出不是 JSON 对象"
    if not script.get("scenes"):
        return "缺少 scenes"
    if not script.get("characters"):
        return "缺少 characters"
    for scene in script["scenes"]:
        if not scene.get("location") or "scene_no" not in scene:
            return f"场次字段不全: {scene}"
        for line in scene.get("lines", []):
            if not line.get("character") or not line.get("dialogue"):
                return f"台词字段不全: {line}"
        scene.setdefault("characters", sorted(
            {ln["character"] for ln in scene.get("lines", [])}))
        scene.setdefault("action", "")
    # 平台侧字段兜底,避免下游因缺字段中断
    script.setdefault("project_title", payload.get("project_title", ""))
    script.setdefault("episode_number", payload.get("episode_number", 0))
    script.setdefault("episode_title", "")
    script.setdefault("logline", "")
    return None


def validate_storyboard(storyboard):
    if not isinstance(storyboard, dict) or not storyboard.get("shots"):
        return "缺少 shots"
    for shot in storyboard["shots"]:
        for field in ("scene_no", "duration", "prompt"):
            if field not in shot:
                return f"镜头缺少字段 {field}: {shot}"
        if not shot["duration"] or shot["duration"] <= 0:
            return f"镜头时长非法: {shot}"
        shot.setdefault("characters", [])
        shot.setdefault("kind",
                        "dialogue" if shot.get("dialogue") else "environment")
        shot.setdefault("description", "")
        shot.setdefault("camera", "")
        shot.setdefault("dialogue", None)
    # 编号强制连续,避免下游连续性质检失败
    for index, shot in enumerate(storyboard["shots"], start=1):
        shot["shot_no"] = index
    storyboard.setdefault("episode_title", "")
    return None


def run(request, claude, timeout):
    capability = request["capability"]
    payload = request.get("payload", {})
    if shutil.which(claude) is None and not Path(claude).exists():
        return {"ok": False, "error": f"claude 命令不存在: {claude}"}
    if capability == "script":
        if payload.get("template") == "idol":
            prompt = IDOL_PROMPT.format(
                persona=payload.get("persona")
                or payload.get("project_title", ""),
                episode=payload.get("episode_number", 0),
                style=payload.get("style", "") or "元气少女",
                premise=payload.get("premise", "") or "自选一个日常主题")
        else:
            prompt = SCRIPT_PROMPT.format(
                title=payload.get("project_title", ""),
                episode=payload.get("episode_number", 0),
                style=payload.get("style", "") or "国风漫剧",
                premise=payload.get("premise", "") or "自由发挥")
    elif capability == "storyboard":
        prompt = STORYBOARD_PROMPT.format(
            script=json.dumps(payload.get("script", {}), ensure_ascii=False))
    else:
        return {"ok": False, "error": f"claude 适配桥不支持能力: {capability}"}
    try:
        proc = subprocess.run(
            [claude, "-p", prompt], capture_output=True, text=True,
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"claude 调用失败: {exc}"}
    if proc.returncode != 0:
        return {"ok": False,
                "error": f"claude 退出码 {proc.returncode}: "
                         f"{proc.stderr.strip()[:300]}"}
    data = extract_json(proc.stdout)
    if data is None:
        return {"ok": False, "error": "claude 输出中未找到 JSON 对象"}
    error = (validate_script(data, payload) if capability == "script"
             else validate_storyboard(data))
    if error:
        return {"ok": False, "error": f"claude 输出校验失败: {error}"}
    return {"ok": True, "data": data, "uri": ""}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS Claude 编剧适配桥")
    parser.add_argument("--claude", default="claude",
                        help="claude 可执行文件路径")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        reply = run(request, args.claude, args.timeout)
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)}
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
