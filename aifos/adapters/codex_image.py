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
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path


# 非交互出图必需:可写沙箱 + 跳过 git 仓库检查(产物目录不是 git 仓库)。
# 旧版 codex 不认识这些参数时,run() 会自动去掉重试。
DEFAULT_EXEC_ARGS = ["--sandbox", "workspace-write", "--skip-git-repo-check"]

# 强制真实出图:Codex 是编码代理,放任它就会用 Pillow 画示意图充数
# 画面语义硬约束:角色名不是物种;不画剧情外的杂物
SUBJECT_DIRECTIVE = (
    "画面语义约束:角色形态严格以提示词中的人物设定为准——设定写明"
    "物种(动物/精怪/机器人等)就按设定画,未写明的一律默认人类;"
    "名字不代表物种,「小鹿」「石头」这类名字不能因为字面画成动物或"
    "物体;同一角色在所有画面中形态必须一致。除剧情明确需要的道具外,"
    "不要出现无关杂物(如悬挂的衣物、衣架、多余的人形)。")

CHARACTER_BACKGROUND_DIRECTIVE = (
    "人物立绘/人物设定图必须使用纯净无场景背景，只允许纯色、柔和渐变或"
    "干净棚拍底；禁止文字、字幕、Logo、水印、建筑、室内、街道、自然场景、"
    "道具和其他人物。若角色有职业身份，必须穿真实可辨认的工作服或制服，"
    "不得用普通便服代替。")

GEN_DIRECTIVE = (
    "收到任务后第一步立即调用内置 $imagegen 图像生成能力，不要搜索资料、"
    "解释方案或编写绘图代码。你必须真实生成图片;禁止用 Pillow / "
    "matplotlib / SVG 等"
    "代码绘制示意图或占位图充数。如果完全没有图像生成能力,打印错误"
    "并以非零码退出,不要伪造图片。")


def _space_line(payload):
    constraint = str(payload.get("spatial_constraint") or "").strip()
    if not constraint:
        return ""
    return (constraint + "这些俯视坐标只用于锁定人物相对位置、人数、机位和"
            "运动方向；最终画面不得画出坐标、节点、箭头、标签或示意图。")


def _style_line(payload):
    style = payload.get("style") or (
        "剧情自适应精品漫剧；服装、发型、道具、建筑和光影服从剧本时代/世界观、"
        "地域、职业、人物性格与剧情阶段")
    if payload.get("portrait_candidate"):
        return (f"画风要求:{style}；高细节；严格按该媒介和时代执行；"
                "定角候选全部继承同一个项目画风，不存在候选画风选项；"
                "候选只比较同一画风下的人物身份、表情、轻微姿态和剧情造型细节；"
                "有参考图时锁脸、年龄、性别表达、发型和妆造身份，只允许剧情服装"
                "细节与表情差异；不得通过更换媒介、渲染、色彩系统或时代制造差异；"
                "不得用同一造型只换动作。")
    return (f"画风要求:{style}；高细节；严格按该媒介、时代和服装要求执行；"
            "整部作品所有画面保持同一画风、同一人物造型。")


def _ref_line(payload):
    # 导演中心前置生成的参考图对照表:编号绑定"谁参考哪张图",
    # CLI 侧原样照读,禁止自行重排或忽略
    manifest = payload.get("reference_manifest") or []
    if manifest:
        numbered = ";".join(
            f"图{item.get('index')}={item.get('label', '参考图')}"
            f" {item.get('uri', '')}({item.get('binding', '')})"
            for item in manifest if item.get("uri"))
        return ("参考图对照表(必须逐张真实打开读取,严格按编号对应使用:"
                "每个人物只参考自己名下的图,禁止把一个人的脸画成另一张"
                "参考图中的人;服装、动作、场景按本镜剧本,除非绑定说明"
                f"要求保留):{numbered}。")
    identity_refs = payload.get("identity_references") or []
    refs = [
        f"{r.get('character', '角色')}人工锁定最终立绘 {r.get('uri', '')}"
        for r in identity_refs if isinstance(r, dict) and r.get("uri")]
    locked_uris = {r.get("uri") for r in identity_refs
                   if isinstance(r, dict)}
    refs.extend(f"人物设定图 {r}" for r in payload.get("character_refs", [])
                if r not in locked_uris)
    if payload.get("spatial_ref"):
        refs.append(f"本镜空间调度图 {payload['spatial_ref']}")
    if payload.get("scene_ref"):
        refs.append(f"场景概念图 {payload['scene_ref']}")
    refs.extend(f"用户参考图 {r}"
                for r in payload.get("reference_images", []))
    lines = []
    if payload.get("style_ref"):
        lines.append(
            f"风格基准图 {payload['style_ref']}(全项目画风统一的唯一基准:"
            "绘画风格、线条、上色、光影、质感必须与这张图完全一致,"
            "禁止任何风格漂移)。")
    if refs:
        if payload.get("portrait_candidate"):
            lines.append(
                "定角候选参考图(必须真实打开读取，不得只使用文字描述；参考图人物的"
                "脸是最高标准，脸型、五官比例、眼鼻嘴结构、肤色、年龄感、"
                "性别表达、发际线、发型轮廓、发量、发色家族、妆造和稳定身份"
                "特征必须保持同一个人；候选只允许剧情服装细节、表情和轻微姿态"
                "差异，不得改脸、换发型、换妆造或换画风):"
                + ";".join(refs) + "。")
        else:
            lines.append("参考图(必须真实打开读取，不得只使用文字描述；人物身份以"
                         "人工锁定最终立绘为最高基准；脸型、五官、发型、发色、妆容、"
                         "年龄感和身份配饰必须与对应人物参考一致，禁止换脸或换发型；"
                         "服装、服装颜色/材质、动作、场景和光影按本镜剧本及当集造型，"
                         "允许与人物参考图服装不同，除非提示词明确要求保留):"
                         + ";".join(refs) + "。")
    return "".join(lines)


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
    common = (f"{_style_line(payload)}{_space_line(payload)}"
              f"{SUBJECT_DIRECTIVE}{GEN_DIRECTIVE}")
    if capability == "image":
        safe = "".join(c if c.isalnum() else "_"
                       for c in str(payload.get("art_name", "")))[:40]
        if payload.get("portrait"):
            target = out_dir / f"portrait_{safe}.png"
            purpose = ("这只是供人工挑选的定妆候选，尚不是最终身份锚点"
                       if payload.get("portrait_candidate") else
                       "这张立绘是全剧的人物设定基准，之后所有镜头都会参考它")
            instruction = (
                f"为角色生成立绘并保存到 {target}(PNG,{size})。"
                f"{payload.get('prompt', '')}。{purpose}。"
                f"{CHARACTER_BACKGROUND_DIRECTIVE}{_ref_line(payload)}{common}"
                "只产出该文件。")
            return instruction, [target], {"name": payload.get("art_name")}
        if payload.get("character_sheet"):
            key = payload["character_sheet"]
            target = out_dir / f"sheet_{safe}_{key}.png"
            instruction = (
                f"为角色生成{payload.get('sheet_label', key)}设定资产并保存到"
                f" {target}(PNG,{size})。{payload.get('prompt', '')}。"
                "这是人物资产库的生产级设定图,必须与立绘/参考图为同一人物,"
                f"发型服装配色完全一致。{CHARACTER_BACKGROUND_DIRECTIVE}"
                f"{_ref_line(payload)}{common}"
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
        chain_first = payload.get("chain_first_uri", "")
        if chain_first and Path(chain_first).exists():
            # 帧链:首帧固定为上一镜尾帧(平台已定),只生成尾帧,
            # 保证两段视频拼接处画面连贯
            shutil.copyfile(chain_first, first)
            instruction = (
                f"本镜首帧已固定为上一镜的尾帧(文件已就位:{first},"
                "不要改动它)。请基于该首帧与关键图 "
                f"{image_uri}(均可直接读取)只生成本镜尾帧,"
                f"保存到 {last}(PNG,{size})。"
                f"镜头内容:{payload.get('prompt', '')}。"
                f"结尾状态:{payload.get('end_state', {})};"
                "画面从首帧状态自然演进到结尾状态,"
                "人物、服装、道具、场景与首帧完全一致,"
                f"不新增字幕条。{_ref_line(payload)}{common}"
                "只产出尾帧这一个文件。"
            )
            return instruction, [first, last], {
                "first": str(first), "last": str(last),
                "first_source": "previous_tail"}
        keyframe = Path(image_uri) if image_uri else None
        if (keyframe and keyframe.exists()
                and keyframe.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")):
            # 每场第一镜已有通过质检的关键帧。复用为首帧可少生成一张图，
            # 同时比重新绘制首帧更能锁住人物身份、服装与构图。
            shutil.copyfile(keyframe, first)
            instruction = (
                f"本镜首帧已直接复用通过质检的关键图(文件已就位:{first},"
                "不要改动它)。请基于该首帧只生成本镜尾帧,"
                f"保存到 {last}(PNG,{size})。"
                f"镜头内容:{payload.get('prompt', '')}。"
                f"结尾状态:{payload.get('end_state', {})};"
                "画面从首帧自然演进到结尾状态，人物、服装、道具、场景与"
                f"首帧完全一致，不新增字幕条。{_ref_line(payload)}{common}"
                "只产出尾帧这一个文件。")
            return instruction, [first, last], {
                "first": str(first), "last": str(last),
                "first_source": "keyframe"}
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
            "first": str(first), "last": str(last),
            "first_source": "generated"}
    if capability == "image_qc":
        image = payload.get("image_uri", "")
        chars = "、".join(payload.get("characters", [])) or "无人(空镜)"
        forbid = "、".join(payload.get("forbid", [])) or "无"
        identity_refs = payload.get("identity_references") or []
        identity_line = "；".join(
            f"{r.get('character', '角色')}={r.get('uri', '')}"
            for r in identity_refs if isinstance(r, dict)) or "无人空镜"
        count_rule = (
            "本图为同一角色的多视角/局部设定图(四视图/特写/服装细节等):"
            "画面中出现的每个人形、头像或局部都必须是该角色同一人,"
            "人数不按出场人数核对"
            if payload.get("multi_view") else
            f"严格共 {payload.get('count', len(payload.get('characters', [])))} 个")
        instruction = (
            f"你是漫剧图片质检员。用你的视觉能力查看图片文件 {image}"
            "(可直接读取该文件),逐项核对是否符合以下生产要求,"
            "看不到文件或无法判断时 pass 记 false。\n"
            f"- 出场角色:{chars}({count_rule};"
            "角色形态必须与人物设定一致——名字不代表物种,"
            "设定是人类就必须是人类)\n"
            f"- 人物设定要点(脸型/五官/发型/发色/妆容/年龄感/标志特征必须一致；"
            "服装按本镜剧本设定，允许与身份参考图不同):"
            f"{payload.get('designs', '见参考图')}\n"
            "- 角色性别硬事实:"
            + ("；".join(
                f"{name}={gender}" for name, gender in
                (payload.get("expected_genders") or {}).items())
               or "以最终立绘与人物设定为准")
            + "\n"
            f"- 人工锁定的最终立绘:{identity_line}\n"
            "必须真实打开待检图和上述最终立绘逐人做视觉比对；脸型、五官比例、"
            "眼鼻嘴、发际线、发型轮廓、年龄感和体型以最终立绘为准。"
            "不得只根据文字判断；文字与最终立绘冲突时，身份以最终立绘为准。\n"
            "必须单独核对每个人物的性别与性别表达。女性画成男性、男性画成女性"
            "一律判失败，不能因服装、发色或气质相似而放行。\n"
            "必须点数画面实际可见人物；多一个、少一个、角色被复制或两人合成一人"
            "都必须判失败。\n"
            f"- 场景:{payload.get('location', '按提示词')};"
            f"动作:{payload.get('action', '按提示词')};"
            f"镜头景别:{payload.get('camera', '不限')}\n"
            f"- 不允许出现:{forbid}、字幕条、乱码文字、多余或缺失的人物\n"
            "- 当前检查对象是静态关键帧：只检查图中可见的最终状态。不得因为"
            "单张图无法证明运镜、眼神变化过程、呼吸等时间动作而判失败；景别"
            "裁掉且非剧情要求必须出镜的裤子、腰间配饰等，不得仅因不可见判失败。\n"
            "只在标准输出打印一行 JSON,不要产出任何文件,不要多余文字:"
            '{"pass": true或false, "identity_checked": true或false, '
            '"identity_match": true或false, '
            '"gender_checked": true或false, "gender_match": true或false, '
            '"count_checked": true或false, "count_match": true或false, '
            '"detected_count": 画面实际人数整数, '
            '"issues": ["每条一句具体原因"]}'
        )
        return instruction, [], {"qc": True}
    if capability == "cover":
        target = out_dir / "cover.png"
        instruction = (
            f"为账号内容生成封面并保存到 {target}(PNG,{size})。"
            f"作品《{payload.get('title', '')}》第{payload.get('episode', 0)}集,"
            f"主题:{payload.get('tagline', '')}。构图吸睛、适合短视频封面,"
            f"可留出大标题排版空间。封面若出现人物，只允许出现并严格对应:"
            f"{'、'.join(payload.get('characters', [])) or '无人'}。"
            f"{_ref_line(payload)}{common}只产出该文件。")
        return instruction, [target], {}
    raise ValueError(f"codex 适配桥不支持能力: {capability}")


def _extract_json(text):
    """从 codex 输出中提取第一个合法 JSON 对象(容忍前后杂讯/日志)。"""
    decoder = json.JSONDecoder()
    idx = (text or "").find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        idx = text.find("{", idx + 1)
    return None


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
    if payload.get("require_reference_images"):
        character_refs = list(payload.get("character_refs") or [])
        declared = list(character_refs)
        declared.extend(
            ref.get("uri") for ref in
            (payload.get("identity_references") or [])
            if isinstance(ref, dict) and ref.get("uri"))
        declared.extend(payload.get("reference_images") or [])
        declared.extend(
            payload.get(key) for key in (
                "image_uri", "chain_first_uri", "scene_ref", "style_ref")
            if payload.get(key))
        declared = list(dict.fromkeys(str(uri) for uri in declared if uri))
        missing = [
            uri for uri in declared
            if not uri.startswith(("http://", "https://"))
            and not Path(uri).exists()]
        if payload.get("characters") and not character_refs:
            return {"ok": False, "error": "人物出图要求最终立绘，但请求未携带人物参考图"}
        if not declared:
            return {"ok": False, "error": "本次出图要求参考图，但请求未携带可用参考图"}
        if missing:
            return {"ok": False,
                    "error": "参考图不存在: " + "、".join(missing)}
    if payload.get("identity_required"):
        identity_refs = payload.get("identity_references") or []
        missing = [str(ref.get("uri", "")) for ref in identity_refs
                   if not isinstance(ref, dict)
                   or not ref.get("uri") or not Path(ref["uri"]).exists()]
        if not identity_refs or missing:
            return {"ok": False,
                    "error": "人物质检缺少可读取的最终立绘"
                             + ((":" + "、".join(missing)) if missing else "")}
    if shutil.which(codex) is None and not Path(codex).exists():
        return {"ok": False, "error": f"codex 命令不存在: {codex}"}
    instruction, targets, data = build_instruction(
        capability, payload, out_dir)
    exec_args = [] if plain else list(DEFAULT_EXEC_ARGS)

    def invoke(args):
        proc = subprocess.Popen(
            [codex, "exec", *args, *extra_args, instruction],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(out_dir), start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # subprocess.run 只会终止直接子进程；Codex 内置 imagegen 可能继续
            # 留在后台占用并发。整组 TERM→KILL，保证超时后没有孤儿进程。
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
            raise
        return subprocess.CompletedProcess(
            proc.args, proc.returncode, stdout, stderr)

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
    if capability == "image_qc":
        verdict = _extract_json(proc.stdout)
        if verdict is None or "pass" not in verdict:
            # 看不到可靠的结构化结论就失败关闭。伪造 checked/match=true
            # 会让换性别、人数错误或串脸图片绕过导演层硬门槛。
            return {"ok": True, "data": {
                "pass": False,
                "issues": ["Codex 未返回可解析的视觉质检 JSON，图片未放行"],
                "identity_checked": False, "identity_match": False,
                "gender_checked": False, "gender_match": False,
                "count_checked": False, "count_match": False,
                "note": "codex 未返回可解析判定,失败关闭"}, "uri": ""}
        verdict.setdefault("issues", [])
        return {"ok": True, "data": verdict, "uri": "",
                "model": "Codex 视觉质检"}
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        return {"ok": False,
                "error": f"codex 未产出期望文件: {', '.join(missing)}"}
    return {"ok": True, "data": data, "uri": str(targets[0]),
            "model": "gpt-image-2 (Codex 内置 image_gen)"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIFOS Codex 图片适配桥")
    parser.add_argument("--codex", default="codex", help="codex 可执行文件路径")
    parser.add_argument("--timeout", type=int, default=1200)
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
