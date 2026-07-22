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
- 若主题涉及女团/男团/组合,设 2-4 名成员(队长/主唱/舞担等),
  characters 列出全部成员,台词在成员间分配;否则全部台词由
  「{persona}」一人口播;
- 台词口语化、有网感,单句不超过 22 个字;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全,characters 只含「{persona}」一人):
{{"project_title": "{persona}", "episode_number": {episode},
 "episode_title": "本期小标题", "logline": "一句话内容概要",
 "characters": [{{"name": "{persona}", "role": "主角"}}],
 "scenes": [{{"scene_no": 1, "location": "场景",
   "characters": ["{persona}"], "action": "画面动作描述",
   "lines": [{{"character": "{persona}", "dialogue": "口播台词"}}]}}]}}"""

STORYBOARD_PROMPT = """你是漫剧分镜师。基于以下剧本 JSON 生成可交给工业流继续校验的原始分镜表。

剧本:
{script}

要求:
- 每段只承载一个主要动作或一次情绪转折；台词逐字照抄，禁止改写；
- 每场先给 1 个环境/肢体镜头，再为每句台词给 1 个对白镜头；
- 关键台词后的听者反应与情绪高潮留白由平台补齐，不要用空镜凑时长；
- shot_no 从 1 连续编号；duration 单位秒，优先 5-8 秒，最长 15 秒；
- prompt 含场景、准确人物名单、主体动作、光影、机位与结尾状态；
- prompt 中人物形态按人物设定描写(名字只是称呼,「小鹿」若设定为
  人类不能当动物写;设定为动物/精怪的按设定写,全片保持一致);
- 不生成对白字幕。手机屏、弹幕、合同等可读文字只描述载体与准确文字，
  后续由 ChatGPT 关键帧锁定，不能交给视频模型从零生成；
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
    if payload.get("character_design"):
        return validate_design(script, payload)
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
    if not isinstance(storyboard["shots"], list):
        return "shots 需为数组"
    for shot in storyboard["shots"]:
        if not isinstance(shot, dict):
            return f"镜头需为对象,收到: {str(shot)[:80]}"
        for field in ("scene_no", "duration", "prompt"):
            if field not in shot:
                return f"镜头缺少字段 {field}: {shot}"
        try:
            shot["duration"] = float(shot["duration"])
        except (TypeError, ValueError):
            return f"镜头时长非法: {shot}"
        if shot["duration"] <= 0:
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


DESIGN_PROMPT = """你是漫剧人物设定师。为作品《{title}》的角色写生产级人物设定,
供 AI 出图使用(立绘/四视图/特写/妆容/服装设定全部依据它)。
画风:{style}。剧情梗概:{logline}。

角色名单(全部要写,名字必须逐字一致):{names}

要求:
- 每个字段是一段具体、可画出来的描述(不要空话套话);
- 性格要能从表情神态与站姿体现;外貌含脸型/肤色/身材比例;
- 服装要具体到款式、材质、层次;配色给出主色与点缀色;
- 只输出一个 JSON 对象,不要任何其他文字或 Markdown 代码块。

JSON 格式:
{{"designs": [{{
  "name": "角色名",
  "species": "物种/形态(默认人类;若是动物/精怪/机器人等明确写出)",
  "personality": "性格(外化到神态)",
  "temperament": "气质", "appearance": "外貌(脸型/肤色/身材)",
  "hair": "发型发色", "eyes": "眼睛(形状/瞳色/眼神)",
  "makeup": "妆容细节", "costume": "服装(款式/材质/层次)",
  "costume_detail": "服装细节(纹样/扣饰/鞋履)",
  "accessories": "配饰", "palette": "主配色与点缀色",
  "signature": "标志性辨识特征"}}]}}"""

# 人物设定必填字段;缺失时置空串,提示词侧自动跳过
DESIGN_FIELDS = ("species", "personality", "temperament",
                 "appearance", "hair",
                 "eyes", "makeup", "costume", "costume_detail",
                 "accessories", "palette", "signature")


def validate_design(data, payload):
    designs = data.get("designs")
    if not isinstance(designs, list) or not designs:
        return "缺少 designs"
    wanted = [c.get("name") for c in payload.get("characters", [])]
    got = {d.get("name"): d for d in designs if d.get("name")}
    missing = [n for n in wanted if n not in got]
    if missing:
        return f"缺少角色设定: {'、'.join(missing)}"
    for design in designs:
        for key in DESIGN_FIELDS:
            design.setdefault(key, "")
        if not design["species"]:
            design["species"] = "人类"
        if not (design.get("personality") and design.get("appearance")
                and design.get("costume")):
            return f"角色「{design.get('name')}」设定过于空泛" \
                   "(personality/appearance/costume 必填)"
    return None


IMAGE_QC_PROMPT = """你是漫剧图片质检员。查看图片文件 {image}(可直接读取),
逐项核对是否符合生产要求。

最终立绘视觉基准(这些图片是身份事实来源，优先级高于早期文字描述):
{identity_references}
只要画面中有人物，就必须把待检图与对应最终立绘逐人做视觉比对：脸型、
五官比例、眼鼻嘴结构、发际线、发型轮廓、年龄感、体型和标志特征。
文字设定只补充剧情、动作、场景和当镜服装；与最终立绘冲突时以最终立绘为准。

画面要求:
- 出场角色:{characters}(严格共 {count} 个;角色形态必须与人物设定
  一致——设定写明物种就按设定画,没写明的默认人类;名字不代表物种,
  「小鹿」若设定是人类就必须是人类,不能因为名字画成动物)
- 人物设定要点(脸型/五官/发型/发色/妆容/年龄感/标志特征必须一致；
  服装按本镜剧本和当集造型核对,允许与身份参考图不同):{designs}
- 场景:{location};动作:{action};镜头:{camera}
  (景别需大致相符:要求全景/远景不能给成特写,反之亦然)
- 不允许出现:与设定形态不符的角色、与剧情无关的杂物(悬挂的衣物/衣架)、
  字幕条、乱码文字、多余或缺失的人物{extra}

只输出一个 JSON 对象,不要任何其他文字:
{{"pass": true或false, "identity_checked": true或false,
"issues": ["不通过的具体原因,每条一句,指出在画面哪里"]}}"""


def build_qc_prompt(payload):
    characters = payload.get("characters") or []
    identity_refs = payload.get("identity_references") or []
    ref_lines = "\n".join(
        f"- {ref.get('character', '角色')}: {ref.get('uri', '')}"
        for ref in identity_refs if isinstance(ref, dict))
    return IMAGE_QC_PROMPT.format(
        image=payload.get("image_uri", ""),
        identity_references=(ref_lines or "无人空镜，无需人物身份比对"),
        characters="、".join(characters) or "无人(空镜)",
        count=payload.get("count", len(characters)),
        designs=payload.get("designs") or "见参考图",
        location=payload.get("location") or "按提示词",
        action=payload.get("action") or "按提示词",
        camera=payload.get("camera") or "按提示词",
        extra=("、" + "、".join(payload.get("forbid", []))
               if payload.get("forbid") else ""))


def validate_image_qc(data):
    if not isinstance(data, dict) or "pass" not in data:
        return "缺少 pass 字段"
    data["pass"] = bool(data["pass"])
    issues = data.get("issues")
    if not isinstance(issues, list):
        issues = [str(issues)] if issues else []
    data["issues"] = [str(item) for item in issues][:8]
    if "identity_checked" in data:
        data["identity_checked"] = bool(data["identity_checked"])
    if not data["pass"] and not data["issues"]:
        data["issues"] = ["未给出具体原因"]
    return None


def build_prompt(capability, payload):
    """构造编剧/分镜/人物设定提示词(CLI 桥与 Claude API Provider 共用)。"""
    if capability == "script" and payload.get("character_design"):
        names = "、".join(
            f"{c.get('name')}({c.get('role') or '角色'})"
            for c in payload.get("characters", []))
        return DESIGN_PROMPT.format(
            title=payload.get("project_title", ""),
            style=payload.get("style", "") or "国风漫剧",
            logline=payload.get("logline", "") or "见角色名单",
            names=names)
    if capability == "script":
        feedback = payload.get("feedback", "")
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
        if feedback:
            previous = json.dumps(
                payload.get("previous_script", {}), ensure_ascii=False)
            prompt = (
                f"这是上一版剧本:\n{previous}\n\n"
                f"用户的修改意见(必须逐条落实):{feedback}\n\n"
                f"请在保留可取之处的前提下按意见重写。{prompt}")
        return prompt
    if capability == "storyboard":
        return STORYBOARD_PROMPT.format(
            script=json.dumps(payload.get("script", {}), ensure_ascii=False))
    if capability == "image_qc":
        return build_qc_prompt(payload)
    raise ValueError(f"claude 编剧不支持能力: {capability}")


def run(request, claude, timeout):
    capability = request["capability"]
    payload = request.get("payload", {})
    if shutil.which(claude) is None and not Path(claude).exists():
        return {"ok": False, "error": f"claude 命令不存在: {claude}"}
    try:
        prompt = build_prompt(capability, payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
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
    if capability == "image_qc":
        error = validate_image_qc(data)
    elif capability == "script":
        error = validate_script(data, payload)
    else:
        error = validate_storyboard(data)
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
