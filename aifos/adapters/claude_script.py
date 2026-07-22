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
- 每个角色都必须先写完整人物背景提示词,不能只写姓名和标签。背景要和本集剧情、时代/世界观、职业、人物性格、关系、目标与冲突绑定;
- `costume_direction` 必须给出可画的服装逻辑(款式、材质、层次、颜色、职业制服/时代服饰和剧情场合),禁止所有角色默认现代都市便服;
- `visual_variants` 至少给出 3 个剧情兼容的造型方向(例如日常、行动/冲突、仪式/舞台),后续人物候选图必须据此做明显不同的服装与气质变化;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全):
{{"project_title": "{title}", "episode_number": {episode},
 "episode_title": "本集小标题", "logline": "一句话梗概",
 "characters": [{{"name": "角色名", "role": "主角/重要配角/非重要配角/背景路人",
   "background_prompt": "可直接用于出图的详细人物背景提示词:成长经历、当前处境、核心动机、心理矛盾和视觉象征",
   "era_setting": "时代/世界观/地域", "occupation": "职业/社会身份",
   "motivation": "核心目标与本集欲望", "backstory": "关键经历、秘密或创伤",
   "relationships": "与其他角色的关系及冲突", "costume_direction": "服装如何体现身份、性格和剧情阶段",
   "signature_props": "标志性道具或动作", "visual_variants": "日常/冲突/关键场合等不同造型方向"}}],
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
- 每个成员都要写与团内定位、歌曲主题、成长经历和本期冲突绑定的人物背景提示词、
  职业/舞台身份、性格外化方式、服装逻辑和至少 3 套练习室/后台/舞台造型方向;
- 台词口语化、有网感,单句不超过 22 个字;
- 只输出一个 JSON 对象,不要任何其他文字、解释或 Markdown 代码块。

JSON 格式(字段必须齐全,characters 只含「{persona}」一人):
{{"project_title": "{persona}", "episode_number": {episode},
 "episode_title": "本期小标题", "logline": "一句话内容概要",
 "characters": [{{"name": "{persona}", "role": "主角",
   "background_prompt": "与主题和成员定位绑定的人物背景提示词",
   "era_setting": "时代/舞台世界观", "occupation": "职业/团内身份",
   "motivation": "本期目标", "backstory": "成长经历",
   "relationships": "团内关系", "costume_direction": "练习室/后台/舞台服装逻辑",
   "signature_props": "标志道具", "visual_variants": "至少三套剧情造型方向"}}],
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
    character_fields = {
        "background_prompt": "", "era_setting": "", "occupation": "",
        "motivation": "", "backstory": "", "relationships": "",
        "costume_direction": "", "signature_props": "",
        "visual_variants": [],
    }
    for character in script["characters"]:
        if not isinstance(character, dict) or not character.get("name"):
            return f"角色字段不全: {character}"
        for key, default in character_fields.items():
            character.setdefault(key, default.copy() if isinstance(default, list)
                                else default)
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
画风:{style}。剧情梗概:{logline}。本集前提:{premise}。

角色名单(全部要写,名字必须逐字一致):{names}
剧本人物背景(必须逐条吸收,不得改成模板化现代都市):{story_context}
本集场景锚点(用于决定每套服装/道具的剧情场合):{scene_context}
{references}
要求:
- 每个字段是一段具体、可画出来的描述(不要空话套话);
- 必须先理解角色的时代/世界观、地域、职业、成长经历、核心目标、关系和本集冲突,
  再决定服装和造型;人物背景与画风冲突时以剧情时代/世界观为准,不能把所有人都套成现代都市;
- `background_prompt` 要写成一段可直接拼进文生图的完整人物背景提示词,至少包含身份来源、
  当前处境、性格外化方式、动机、冲突、关系和视觉符号;
- `era_setting`、`occupation`、`motivation`、`backstory`、`relationships`、
  `costume_direction`、`signature_props` 必须具体;职业身份要能从服装和装备一眼识别;
- `visual_variants` 必须给出 3-5 个与剧情兼容的不同造型方向,每项写清场合、服装、材质、
  配色、配饰/道具和气质变化;不是同一套衣服只换动作;
- 给每个角色标注重要度角色:主角、重要配角、非重要配角或背景路人;背景路人不单独生成角色立绘;
- 性格要能从表情神态与站姿体现;外貌含脸型/肤色/身材比例;
- 服装要具体到款式、材质、层次;配色给出主色与点缀色;
- 如果角色有职业或工作身份(如外卖小哥、快递员、医生、护士、警察、消防员、
  保安、服务员、厨师、工人等),costume 必须写清该职业真实可辨认的工作服/制服
  和必要装备,禁止用普通便服代替;
- 有参考图时,参考图人物的脸和发型是最高标准;appearance/hair/eyes/makeup/
  signature/temperament 必须先看图再写,不得凭空换脸或换发型;
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
  "signature": "标志性辨识特征",
  "background_prompt": "完整人物背景提示词(身份/经历/处境/动机/冲突/视觉象征)",
  "era_setting": "时代/世界观/地域", "occupation": "职业/社会身份",
  "motivation": "核心目标", "backstory": "关键经历或秘密",
  "relationships": "关系与冲突", "costume_direction": "服装设计逻辑",
  "signature_props": "标志性道具", "visual_variants": [
    {{"label": "日常/身份", "costume": "...", "palette": "...", "props": "...", "temperament": "..."}},
    {{"label": "行动/冲突", "costume": "...", "palette": "...", "props": "...", "temperament": "..."}},
    {{"label": "关键场合", "costume": "...", "palette": "...", "props": "...", "temperament": "..."}}
  ]}}]}}"""

# 人物设定必填字段;缺失时置空串,提示词侧自动跳过
DESIGN_FIELDS = ("species", "personality", "temperament",
                 "appearance", "hair",
                 "eyes", "makeup", "costume", "costume_detail",
                 "accessories", "palette", "signature", "background_prompt",
                 "era_setting", "occupation", "motivation", "backstory",
                 "relationships", "costume_direction", "signature_props",
                 "visual_variants")


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
必须单独核对每个人物的性别与性别表达；女性被画成男性、男性被画成女性，
一律是身份硬错误，不能因发色、服装或气质相似而通过。
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
- 这是静态关键帧质检：只检查画面中能看见的最终状态。不得因为单张图无法证明
  运镜、眼神变化过程、呼吸或其他时间动作而判失败；景别裁掉且并非剧情要求必须
  出镜的裤子、腰间配饰等，也不得仅因不可见而判失败。

只输出一个 JSON 对象,不要任何其他文字:
{{"pass": true或false, "identity_checked": true或false,
"gender_checked": true或false, "gender_match": true或false,
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
    if "gender_checked" in data:
        data["gender_checked"] = bool(data["gender_checked"])
    if "gender_match" in data:
        data["gender_match"] = bool(data["gender_match"])
    if not data["pass"] and not data["issues"]:
        data["issues"] = ["未给出具体原因"]
    return None


def build_prompt(capability, payload):
    """构造编剧/分镜/人物设定提示词(CLI 桥与 Claude API Provider 共用)。"""
    if capability == "script" and payload.get("character_design"):
        names = "、".join(
            f"{c.get('name')}({c.get('role') or '角色'})"
            for c in payload.get("characters", []))
        ref_lines = []
        for c in payload.get("characters", []):
            for uri in c.get("reference_images") or []:
                ref_lines.append(f"- {c.get('name')}: {uri}")
        references = ""
        if ref_lines:
            references = (
                "\n角色参考图(文件路径,可直接查看):\n"
                + "\n".join(ref_lines)
                + "\n有参考图的角色必须先查看参考图,以图中人物的脸部特征"
                "(脸型/五官比例/眼鼻嘴/年龄感)、发型发色、妆容、气质和"
                "整体风格为最高标准逐项撰写 appearance/hair/eyes/makeup/"
                "signature/temperament 等字段,与参考图冲突的描述一律"
                "以参考图为准,不得凭空想象;脸和发型不得漂移;服装可按剧情另行设计。\n")
        return DESIGN_PROMPT.format(
            title=payload.get("project_title", ""),
            style=payload.get("style", "") or "国风漫剧",
            logline=payload.get("logline", "") or "见角色名单",
            premise=payload.get("premise", "") or "见本集剧本",
            names=names,
            story_context=json.dumps(
                payload.get("character_context") or payload.get("characters", []),
                ensure_ascii=False),
            scene_context=json.dumps(payload.get("scene_context") or [],
                                     ensure_ascii=False),
            references=references)
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
