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

支持能力:prompt_review(生图前提示词审核优化)、image(镜头关键图)、
frames(首尾帧)、cover(封面)。
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
import threading
from pathlib import Path

from aifos.generation_diagnostics import normalize_generation_diagnostics
from aifos.adapters.claude_script import validate_image_qc
from aifos.prompt_contract import readable_text_required, sanitize_text_whitelist


# 非交互出图必需:可写沙箱 + 跳过 git 仓库检查(产物目录不是 git 仓库)。
# 旧版 codex 不认识这些参数时,run() 会自动去掉重试。
DEFAULT_EXEC_ARGS = ["--sandbox", "workspace-write", "--skip-git-repo-check"]
PROMPT_REVIEW_EXEC_ARGS = [
    "--sandbox", "read-only", "--skip-git-repo-check",
    "--ephemeral", "--ignore-rules",
    "-c", 'model_reasoning_effort="low"',
]

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

# 资产工坊四类自建资产的单一职责约束:防止「画个场景」返回一张剧照、
# 「画件道具」返回一个人举着它。用户提示词仍是唯一内容事实源。
STUDIO_ASSET_RULES = {
    "character": (
        "这是人物形象母资产:单人全身或半身立绘,纯净无场景背景"
        "(纯色、柔和渐变或干净棚拍底),不出现第二个人、文字和多余道具;"),
    "style": (
        "这是画风基准图:只表达媒介、笔触、色调、光影和材质质感,"
        "不绑定具体人物身份,不写文字、不做拼图色卡分格;"),
    "scene": (
        "这是场景概念图:只画空间与陈设,画面中不出现任何人物;"),
    "prop": (
        "这是单件物品母资产:只画该物品本体,居中单体展示,"
        "不画人物、不画使用场景故事;"),
}

GEN_DIRECTIVE = (
    "收到任务后第一步立即调用内置 $imagegen 图像生成能力，不要搜索资料、"
    "解释方案或编写绘图代码。你必须真实生成图片;禁止用 Pillow / "
    "matplotlib / SVG 等"
    "代码绘制示意图或占位图充数。如果完全没有图像生成能力,打印错误"
    "并以非零码退出,不要伪造图片。")


def _nonnegative_int(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _functional_figures(payload):
    return [
        dict(item) for item in (payload.get("functional_figures") or [])
        if isinstance(item, dict)
        and type(item.get("count")) is int and item.get("count") > 0
    ]


def _population_counts(payload):
    """Resolve the current structured population first, then legacy fields."""
    contract = payload.get("prompt_contract")
    contract = contract if isinstance(contract, dict) else {}
    population = contract.get("population")
    population = population if isinstance(population, dict) else {}
    counts = population.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    subject = contract.get("subject")
    subject = subject if isinstance(subject, dict) else {}
    composition = contract.get("composition")
    composition = composition if isinstance(composition, dict) else {}
    figures = _functional_figures(payload)

    registered = next((
        value for value in (
            _nonnegative_int(counts.get("named_characters")),
            _nonnegative_int(subject.get("registered_count")),
            _nonnegative_int(payload.get("character_count")),
            len(payload.get("characters") or []),
        ) if value is not None), 0)
    functional = next((
        value for value in (
            _nonnegative_int(counts.get("functional_people")),
            _nonnegative_int(subject.get("functional_count")),
            sum(item["count"] for item in figures),
        ) if value is not None), 0)
    visible = next((
        value for value in (
            _nonnegative_int(counts.get("real_people_total")),
            _nonnegative_int(subject.get("visible_count")),
            _nonnegative_int(payload.get("visible_figure_count")),
            _nonnegative_int(payload.get("count")),
            _nonnegative_int(
                composition.get("expected_visible_figure_count")),
            registered + functional,
        ) if value is not None), registered + functional)
    return registered, functional, visible


def _population_line(payload):
    registered, functional, visible = _population_counts(payload)
    names = "、".join(payload.get("characters") or []) or "无"
    figures = _functional_figures(payload)
    figure_text = "、".join(
        f"{item.get('name') or item.get('label') or '功能人物'}"
        f"{item['count']}人"
        + (f"({item.get('state') or item.get('function')})"
           if item.get("state") or item.get("function") else "")
        for item in figures
    ) or ("按v2.2人口合同" if functional else "无")
    return (
        f"登记角色{registered}人（{names}）；功能人物{functional}人"
        f"（{figure_text}）；画面可见真人严格共{visible}人。"
        "身份与最终立绘核验只覆盖登记角色；功能人物只服从本镜数量、"
        "状态与剧情功能；非现实叙事叠层不计入真人总数。")


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
                "同一人物四张候选必须使用完全相同的初始状态提示词、人物造型、"
                "表情、姿态和构图，只靠图片模型随机采样比较结果；有参考图时只锁"
                "脸、年龄、性别表达、发型和妆造身份；不得换装、换妆、换动作、"
                "加入淋湿/泥污/伤情或切换剧情阶段。")
    return (f"画风要求:{style}；高细节；严格按该媒介、时代和服装要求执行；"
            "整部作品所有画面保持同一画风、同一人物造型。")


def _state_brief(state, limit=300):
    """人物状态 dict → 紧凑中文行。

    旧行为把 Python dict 的 repr 直接写进提示词:带英文键名/引号/花括号,
    单条 1022 字(占该提示词 20%),且内容与上文自然语言合同完全重复。
    """
    if not isinstance(state, dict) or not state:
        return "见上文合同"
    parts = []
    for name, value in list(state.items())[:4]:
        if isinstance(value, dict):
            bits = [str(value.get(key)) for key in
                    ("pose", "direction", "position", "prop", "emotion")
                    if value.get(key)]
            parts.append(f"{name}:{'、'.join(bits) if bits else '见上文'}")
        elif value:
            parts.append(f"{name}:{value}")
    return ("；".join(parts) or "见上文合同")[:limit]


def _ref_line(payload, prompt_text=""):
    # 导演中心前置生成的参考图对照表:编号绑定"谁参考哪张图",
    # CLI 侧原样照读,禁止自行重排或忽略
    manifest = payload.get("reference_manifest") or []
    if manifest and "参考图对照表" in str(prompt_text):
        # 编译合同里已带整份对照表(含每图 binding)。旧行为在末尾再展开
        # 一遍,同一份绑定文字在一条提示词里出现两次、合计占下发稿约47%,
        # 审核环节刚压缩掉的又被原样加回。只留执行指针,不再复注。
        return ("参考图对照表已在上文——必须逐张真实打开读取,"
                "严格按编号执行各自绑定职责,禁止跨用途传播。")
    if manifest:
        numbered = ";".join(
            f"图{item.get('index')}={item.get('label', '参考图')}"
            f" {item.get('uri', '')}({item.get('binding', '')}"
            + (f"；inherits={','.join(item.get('inherits') or [])}"
               if item.get("inherits") else "")
            + (f"；excludes={','.join(item.get('excludes') or [])}"
               if item.get("excludes") else "")
            + ")"
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
    refs.extend(f"核心道具母资产 {r}" for r in payload.get("prop_refs", []))
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
                "特征必须保持同一个人；参考图服装、配饰、手持物、姿势、背景和"
                "光线不得覆盖本次初始造型合同；四张候选不得改脸、换发型、"
                "换妆造、换服装、换动作或换画风):"
                + ";".join(refs) + "。")
        else:
            lines.append("参考图(必须真实打开读取，不得只使用文字描述；人物身份以"
                         "人工锁定最终立绘为最高基准；脸型、五官、发型、发色、妆容、"
                         "年龄感和身份配饰必须与对应人物参考一致，禁止换脸或换发型；"
                         "服装、服装颜色/材质、动作、场景和光影按本镜剧本及当集造型，"
                         "允许与人物参考图服装不同，除非提示词明确要求保留):"
                         + ";".join(refs) + "。")
    return "".join(lines)


def _screen_prop_rule(prompt_text, text_asset=None):
    """识别道具提示里的屏幕/页面，并追加局部可读文字硬约束。"""
    source = str(prompt_text or "")
    asset = text_asset if isinstance(text_asset, dict) else {}
    carrier = str(asset.get("carrier") or "")
    screen_tokens = (
        "电脑", "笔记本电脑", "屏幕", "显示器", "网页",
        "手机屏", "手机界面", "平板屏", "平板界面",
    )
    if not any(token in source for token in screen_tokens) \
            and not any(token in carrier for token in screen_tokens):
        return ""
    whitelist = sanitize_text_whitelist(asset.get("whitelist", []))
    # Only the explicit structured whitelist may authorize readable text.
    # Extracting bracketed phrases from prompt_text turns structural headings
    # such as 【镜头合同v1】【主体】 into accidental on-screen copy.
    exact = list(dict.fromkeys(whitelist))
    if not exact:
        return (
            "【屏幕文字边界】本镜没有显式可读文字白名单；不得从"
            "【镜头合同】【主体】【场景】【动作】等提示词结构标题或剧情描述"
            "中抽取、猜测或新增任何可读文字。屏幕仅呈现不可读的自然界面细节，"
            "画面仍禁止字幕、Logo、水印和乱码。"
        )
    wanted = "、".join(exact)
    presentation = "；".join(filter(None, (
        f"版式/位置:{str(asset.get('layout') or '').strip()}"
        if str(asset.get("layout") or "").strip() else "",
        f"字体/颜色/层级:{str(asset.get('style') or '').strip()}"
        if str(asset.get("style") or "").strip() else "",
        f"透视/反光:{str(asset.get('perspective') or '').strip()}"
        if str(asset.get("perspective") or "").strip() else "",
    )))
    return (
        "【屏幕/页面文字硬锁】电脑必须保持打开，屏幕正对镜头并在画面中清晰"
        "可见；禁止空白冷白屏、纯白发光占位面和空白占位内容，屏幕必须实际"
        "显示并尽量逐字"
        f"呈现{wanted}"
        + (f"；{presentation}" if presentation else "")
        + "，不得用随机乱码、模糊色块或纯白发光替代。只修改"
        "屏幕内页面，电脑金属外壳、人物、服装、场景、构图和光线保持不变；"
        "屏幕外仍禁止字幕、Logo、水印和无关文字。"
    )


def build_instruction(capability, payload, out_dir):
    """返回 (给 codex 的指令, 期望产出的文件列表, 应答的 data 字段)。"""
    out_dir = Path(out_dir)
    if capability == "prompt_review":
        source = str(payload.get("review_prompt") or "").strip()
        context = payload.get("review_context") or {}
        schema = str(
            payload.get("review_schema")
            or "aifos.codex-prompt-review/v1")
        instruction = (
            "你是AIFOS图片生成前的提示词审核员。只审核并优化提示词，"
            "禁止调用imagegen、禁止生成图片、禁止修改任何文件。\n"
            "目标：把AIFOS已编译提示词改成更准确、无冲突、可直接执行的"
            "最终生图提示词，同时绝不改变剧本事实、人物身份、人数、服装、"
            "头饰、妆发、道具、场景、动作、机位、起止状态、文字白名单和"
            "参考图职责。\n"
            "审核规则：\n"
            "1. 删除重复、空泛、互相冲突和不可见的心理/背景描述；保留所有"
            "生成所需的明确视觉事实与硬约束。\n"
            "2. 不得新增人物、剧情、动作、道具、服装、颜色、文字、Logo或"
            "参考图；不得把参考图服装错误升级为本镜服装。\n"
            "3. 人物、场景、起止状态、镜头、可读文字和图N参考职责必须与"
            "审核上下文完全一致；事实并列冲突时先按【冲突裁决规则】取"
            "高优先级事实执行,不确定且无法裁决时才不得猜测。\n"
            "4. 含mock/占位模板污染、或缺少无法由【冲突裁决规则】补位的"
            "决定性事实时，approved必须为false并说明阻断原因。事实源冲突"
            "本身不再是阻断理由:显式裁决条款(master_state_precedence、"
            "text_policy 等)直接执行;其余按【冲突裁决规则】的优先级取高;"
            "只有同级互斥且无显式裁决时才阻断,且必须写明是哪两条同级事实。\n"
            "6. must_keep_verbatim 列出的每一项都是下游合同逐字校验的不可变"
            "事实，优化稿必须原样保留(可另加修饰，但不得改写、缩写、拆分或"
            "删除)；删除任何一项都会导致整张图被拒绝生成。"
            "其中【质检同源合同】这类结构区块标记，必须连同其区块内容"
            "整段原样保留，不得视为可删的结构标题。\n"
            "7. 人数必须保留明确字面表述:0人写「空镜/画面中不出现人物」，"
            "1人写「单人/1名人物」，N人写「共N人」；不得只留人名或隐含"
            "表达，否则整张图被拒绝生成。\n"
            "5. optimized_prompt必须是可以直接交给图片模型的完整提示词，"
            "不得包含审核过程、Markdown代码围栏或JSON以外的说明。\n"
            f"审核输出schema={schema}。\n"
            "只输出一个JSON对象，严格使用以下字段："
            '{"schema":"aifos.codex-prompt-review/v1",'
            '"approved":true,'
            '"optimized_prompt":"完整优化稿",'
            '"issues_found":["原稿问题"],'
            '"changes_made":["实际修改"],'
            '"blocking_reason":""}。\n'
            "当无法安全优化时approved=false、optimized_prompt置空。\n"
            "【AIFOS原始提示词】\n"
            f"{source[:24000]}\n"
            "【冲突裁决规则】\n"
            f"{json.dumps(payload.get('adjudication') or {}, ensure_ascii=False)[:4000]}\n"
            "【必须逐字保留的词】\n"
            f"{json.dumps(payload.get('must_keep_verbatim') or [], ensure_ascii=False)}\n"
            "【不可变审核上下文】\n"
            f"{json.dumps(context, ensure_ascii=False, sort_keys=True)[:30000]}"
        )
        return instruction, [], {
            "schema": schema,
            "source_length": len(source),
        }
    width = int(payload.get("width", 1080))
    height = int(payload.get("height", 1920))
    size = f"{width}x{height},画幅 {payload.get('aspect', '9:16')}"
    feedback = payload.get("feedback", "")
    # 镜头类请求优先使用导演编译的短合同；完整 prompt 仍随 payload
    # 保存，供审计和人工复核，不让它重复占用模型的注意力。
    prompt_text = payload.get("prompt_compact") or payload.get("prompt", "")
    if feedback:
        prompt_text = f"{prompt_text}。修改意见(必须落实):{feedback}"
    # A complete visual contract already carries style/composition/background.
    # Keep only universal safety and execution rules instead of repeating the
    # same character card around the actual image prompt.
    semantic_context = (
        "" if payload.get("prompt_contract_complete")
        else f"{_style_line(payload)}{_space_line(payload)}")
    common = f"{semantic_context}{SUBJECT_DIRECTIVE}{GEN_DIRECTIVE}"
    if capability == "image":
        safe = "".join(c if c.isalnum() else "_"
                       for c in str(payload.get("art_name", "")))[:40]
        if payload.get("studio_asset"):
            # 资产工坊:用户自建资产库。提示词由用户自己写或 AI 代写,
            # 已是唯一事实源;这里只负责真实出图与单一职责约束。
            kind = str(payload.get("studio_asset"))
            target = out_dir / f"studio_{kind}_{safe}.png"
            instruction = (
                f"生成一张{payload.get('studio_asset_label', '资产')}图片并保存到"
                f" {target}(PNG,{size})。{prompt_text}。"
                f"{STUDIO_ASSET_RULES.get(kind, '')}"
                "这张图会进入用户的资产库并在后续制作中作为参考图复用,"
                "必须干净可复用:不加字幕条、水印、Logo、边框和拼图分格。"
                f"{_ref_line(payload, prompt_text)}{common}只产出该文件。")
            return instruction, [target], {
                "name": payload.get("art_name"), "studio_asset": kind}
        if payload.get("prop_candidate"):
            target = out_dir / f"prop_{safe}.png"
            instruction = (
                f"为核心道具生成单件候选图并保存到 {target}(PNG,{size})。"
                f"{prompt_text}。这是供人工四选一的道具母资产候选，"
                "不得画成人物立绘或场景图。"
                f"{_ref_line(payload, prompt_text)}{common}只产出该文件。")
            return instruction, [target], {
                "name": payload.get("art_name"),
                "prop": payload.get("prop_name", "")}
        if payload.get("portrait"):
            target = out_dir / f"portrait_{safe}.png"
            purpose = ("这只是供人工挑选的定妆候选，尚不是最终身份锚点"
                       if payload.get("portrait_candidate") else
                       "这张立绘是全剧的人物设定基准，之后所有镜头都会参考它")
            instruction = (
                f"为角色生成立绘并保存到 {target}(PNG,{size})。"
                f"{prompt_text}。{purpose}。"
                + ("" if payload.get("prompt_contract_complete")
                   else CHARACTER_BACKGROUND_DIRECTIVE)
                + f"{_ref_line(payload, prompt_text)}{common}"
                "只产出该文件。")
            return instruction, [target], {"name": payload.get("art_name")}
        if payload.get("character_sheet"):
            key = payload["character_sheet"]
            target = out_dir / f"sheet_{safe}_{key}.png"
            instruction = (
                f"为角色生成{payload.get('sheet_label', key)}设定资产并保存到"
                f" {target}(PNG,{size})。{prompt_text}。"
                "这是人物资产库的生产级设定图,必须与立绘/参考图为同一人物,"
                "发型服装配色完全一致。"
                + ("" if payload.get("prompt_contract_complete")
                   else CHARACTER_BACKGROUND_DIRECTIVE)
                + _screen_prop_rule(prompt_text)
                + f"{_ref_line(payload, prompt_text)}{common}"
                "只产出该文件。")
            return instruction, [target], {
                "name": payload.get("art_name"), "sheet": key}
        if payload.get("scene_art"):
            target = out_dir / f"scene_{safe}.png"
            instruction = (
                f"为场景生成概念图并保存到 {target}(PNG,{size})。"
                f"{prompt_text}。这张概念图是该场景的美术基准。"
                f"{_ref_line(payload, prompt_text)}{common}只产出该文件。")
            return instruction, [target], {"name": payload.get("art_name")}
        shot_no = int(payload["shot_no"])
        target = out_dir / f"shot_{shot_no:03d}.keyframe.png"
        text_asset = payload.get("readable_text") or {}
        text_rule = (
            f"画面文字载体:{text_asset.get('carrier', '')};只允许逐字出现:"
            f"{'、'.join(sanitize_text_whitelist(text_asset.get('whitelist', []))) or '白名单为空'};"
            "不得新增乱码或字幕条。"
            if readable_text_required(text_asset) else
            "画面中不要生成字幕条、对白字幕或无关可读文字。")
        text_rule += _screen_prop_rule(prompt_text, text_asset)
        instruction = (
            f"为漫剧分镜生成一张关键图并保存到 {target}"
            f"(PNG,{size})。画面内容:{prompt_text}。"
            f"人物总量合同:{_population_line(payload)}"
            f"禁止新增、漏画、复制或合并任何真人。{text_rule}"
            f"镜头语言:{payload.get('camera', '')}。"
            f"{_ref_line(payload, prompt_text)}{common}"
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
                f"镜头内容:{prompt_text}。"
                f"结尾状态:{_state_brief(payload.get('end_state'))};"
                "画面从首帧状态自然演进到结尾状态,"
                "人物、服装、道具、场景与首帧完全一致,"
                f"不新增字幕条。{_ref_line(payload, prompt_text)}{common}"
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
                f"镜头内容:{prompt_text}。"
                f"结尾状态:{_state_brief(payload.get('end_state'))};"
                "画面从首帧自然演进到结尾状态，人物、服装、道具、场景与"
                f"首帧完全一致，不新增字幕条。{_ref_line(payload, prompt_text)}{common}"
                "只产出尾帧这一个文件。")
            return instruction, [first, last], {
                "first": str(first), "last": str(last),
                "first_source": "keyframe"}
        instruction = (
            f"基于关键图 {image_uri}(文件可直接读取)"
            "为镜头生成首帧与尾帧,"
            f"分别保存到 {first} 和 {last}(PNG,{size})。"
            f"镜头内容:{prompt_text}。"
            f"起始状态:{_state_brief(payload.get('start_state'))};"
            f"结尾状态:{_state_brief(payload.get('end_state'))};"
            "首帧为动作起始、尾帧为动作结束，构图与关键图连贯，"
            "保持人物、服装、道具、场景与任何已锁定文字完全一致，"
            f"不新增字幕条。{_ref_line(payload, prompt_text)}{common}"
            "只产出这两个文件。"
        )
        return instruction, [first, last], {
            "first": str(first), "last": str(last),
            "first_source": "generated"}
    if capability == "image_qc":
        image = payload.get("image_uri", "")
        chars = "、".join(payload.get("characters", [])) or "无人(空镜)"
        _, _, expected_real_people = _population_counts(payload)
        forbid = "、".join(payload.get("forbid", [])) or "无"
        identity_refs = payload.get("identity_references") or []
        identity_line = "；".join(
            f"{r.get('character', '角色')}"
            f"({r.get('reference_view') or 'final_portrait'})="
            f"{r.get('uri', '')}"
            for r in identity_refs if isinstance(r, dict)) or "无人空镜"
        composition = payload.get("composition_contract") or {}
        actor_rules = "；".join(
            f"{item.get('character')}={item.get('role')}/"
            f"{item.get('expected_view')}/按{item.get('identity_basis')}核验"
            for item in composition.get("actors") or []
            if isinstance(item, dict))
        quality = composition.get("quality_requirements") or {}
        quality_line = (
            f"质检必须满足={ '；'.join(quality.get('required') or []) };"
            f"质检禁止={ '；'.join(quality.get('forbidden') or []) }"
            if isinstance(quality, dict) else "")
        composition_visible = composition.get(
            "expected_visible_figure_count")
        if composition_visible is None:
            composition_visible = expected_real_people
        composition_line = (
            f"类型={composition.get('composition_type')};"
            f"正面主体={composition.get('expected_primary_count')};"
            f"实际可见真人={composition_visible};"
            f"{actor_rules};{composition.get('count_rule', '')};{quality_line}"
            if composition else "标准构图；按待检图实际可见视角逐人核验")
        physical = payload.get("physical_contract") or {}
        physical_rules = "；".join(physical.get("rules") or [])
        physical_objects = "；".join(physical.get("objects") or [])
        physical_line = (
            ("必须执行硬检查；" if payload.get("physical_logic_required")
             else "仅作辅助检查；")
            + (physical_rules or "人物、镜头、道具关系按当前镜头实际构图核对")
            + (f"；对象关系：{physical_objects}" if physical_objects else ""))
        count_rule = (
            "本图为同一角色的多视角/局部设定图(四视图/特写/服装细节等):"
            "画面中出现的每个人形、头像或局部都必须是该角色同一人,"
            "人数不按出场人数核对"
            if payload.get("multi_view") else
            _population_line(payload)
            + (composition.get("count_rule") or "每个人物只计一次"))
        overlays = [
            item for item in (payload.get("narrative_overlays") or [])
            if isinstance(item, dict)
        ][:1]
        if overlays:
            overlay = overlays[0]
            overlay_line = (
                "- 本镜另有1个非现实内心Q版叠层:"
                f"{overlay.get('name') or '内心Q版'}，宿主="
                f"{overlay.get('host_character') or '宿主'}。detected_count"
                "只统计真实人物，不把Q版计入；detected_overlay_count单独统计"
                "Q版且必须为1。Q版不得成为真人实体、不得进入真实站位或遮挡、"
                "不得被其他人物看见/回应/触碰；继承当前衣着、无默认道具，"
                "表情动作应夸张；比例约1.8头身，头占总高约58%，身体与四肢"
                "明显小于头。内心发声时真人宿主闭口，不得出现旁白字幕。\n")
        else:
            overlay_line = (
                "- 本镜不允许内心Q版叠层；detected_overlay_count必须为0，"
                "不得新增Q版、分身、幽灵或意识小人。\n")
        generation = payload.get("generation_input")
        generation = generation if isinstance(generation, dict) else {}
        generation_prompt = (
            generation.get("prompt")
            or payload.get("generation_prompt")
            or payload.get("prompt_used")
            or payload.get("prompt_compact")
            or "")
        generation_references = (
            generation.get("reference_manifest")
            or payload.get("reference_manifest")
            or [])
        generation_scope = generation.get("scope") or payload.get("scope") or {
            "item_id": payload.get("item_id", ""),
            "shot_no": payload.get("shot_no"),
            "frame_kind": payload.get("frame_kind", "keyframe"),
        }
        escalation = payload.get("codex_escalation_context")
        escalation = escalation if isinstance(escalation, dict) else {}
        escalation_line = ""
        escalation_schema = ""
        if escalation:
            failures = int(escalation.get("consecutive_failures") or 0)
            if failures <= 0:
                # 预授权模式:随首检下发。判定本身保持中立,只有判不通过
                # 时才附升级结论——绝不能因为带了升级上下文就预设失败。
                stage_line = (
                    "本次是正常质检,不预设结论。仅当你判定不通过时,"
                    "才需要附 codex_escalation:AIFOS 会立即按你给出的"
                    "新提示词生成3张候选并自动选优,所以 aifos_instructions 必须是"
                    "可以直接拼进下一次生成提示词的完整、唯一、无歧义"
                    "表述,不要写「建议」「可考虑」这类不可执行的话。"
                    "只要重画能救回来就用 targeted_redraw;判定通过则"
                    "省略 codex_escalation。\n")
            elif failures < 2:
                stage_line = (
                    "本镜此前已失败 1 次:AIFOS 会按你给出的新提示词"
                    "一次生成3张候选、全部质检后自动选最优，所以 "
                    "aifos_instructions 必须是可直接拼进下一次生成提示词的"
                    "完整、唯一、无歧义表述，不要写"
                    "「建议」「可考虑」这类不可执行的话。只要重画能救回来就用"
                    "targeted_redraw。\n")
            else:
                stage_line = (
                    "本镜的修订候选组仍未通过：请输出下一轮唯一可执行的"
                    "合同修复或定向重画指令；AIFOS 会自动应用，不会停在"
                    "人工确认点。\n")
            escalation_line = (
                ("- 质检附升级预授权(不改变判定标准)。"
                 if failures <= 0 else
                 "- 这是质检失败后的 Codex 升级分析，不是普通复检。")
                + f"此前连续失败次数={failures}；" + stage_line
                + "必须先判断失败来自画面、提示词/参考图合同冲突，还是一个静态"
                "关键帧无法同时承载多个先后动作。藏入袖内、被手掌或身体合理"
                "遮挡的道具属于不可见状态，不能强迫画面把它展示出来；双手已"
                "执行抱拳、拱手等占用动作时，也不能同时要求同一只手清楚展示"
                "被遮挡道具。若合同要求在一张图里同时表现接取、检查、归还等"
                "先后动作，应选择单一冻结瞬间或建议拆镜，不能继续盲目重画。"
                "请用 codex_escalation.aifos_action 明确通知 AIFOS 下一步。\n")
            escalation_schema = (
                ', "codex_escalation": {"aifos_action":'
                '"targeted_redraw/repair_contract/split_shot/'
                'accept_current/manual_review",'
                '"reason":"为什么这样处理","aifos_instructions":'
                '["AIFOS下一步只需执行的具体修改；targeted_redraw 时必须是'
                '可直接用于下一次生成的提示词表述"],'
                '"freeze_moment":"静态关键帧唯一冻结瞬间",'
                '"visible_props":["本帧必须可见的道具"],'
                '"hidden_props":["本帧应隐藏或允许被遮挡的道具"]}')
        instruction = (
            f"你是漫剧图片质检员。用你的视觉能力查看图片文件 {image}"
            "(可直接读取该文件),逐项核对是否符合以下生产要求,"
            "看不到文件或无法判断时 pass 记 false。\n"
            "- 当前镜头本次真实生成输入（只能分析这一镜，禁止补写整集剧情"
            "或其他镜头）：\n"
            f"  范围={json.dumps(generation_scope, ensure_ascii=False)}\n"
            f"  实际提交提示词={str(generation_prompt)[:12000]}\n"
            "  实际提交参考图对照表="
            f"{json.dumps(generation_references, ensure_ascii=False)[:16000]}\n"
            "除判断画面错误外，必须分别判断提示词是否准确、简洁、无冲突、"
            "无无关剧情，以及参考图是否属于本镜、人物与用途绑定是否正确、"
            "是否缺失或冲突。必须逐项对照当前镜头剧本事实、起点、动作、"
            "终点：同一人物不能同时穿两套互斥服装；同一件关键道具不能在"
            "没有复制剧情时同时被人物持有/穿着又散置另一处；已死亡人物不能"
            "继续呼吸、眨眼或表演微表情；一个静态帧不能同时承担多个先后"
            "动作。不得虚构未提交的输入。只有提示词重复、略长或参考图说明"
            "不够简洁时才作为建议；凡与剧本不符、逻辑冲突、关键描述不清或"
            "缺少执行所需事实，input_contract_pass 必须为 false，pass 也必须"
            "为 false，并在 targeted_prompt_patch 中给出可直接用于第二次生成"
            "的唯一明确表述。\n"
            + escalation_line
            + "- 质检阈值：按手机竖屏正常播放观看，禁止放大像素挑刺。只有普通观众"
            "一眼可见、会影响身份识别、剧情理解或画面可信度的明显问题才失败："
            "明显错人/错性别/错人数、严重跑脸、关键服装/道具/场景/时代错误、"
            "剧情必需文字错误，以及明显肢体畸形、穿模、悬浮、设备反向或空间"
            "关系不可能。轻微肤质噪点、细小色差、衣物旧化程度、发丝/皱纹/"
            "妆效、轻微表情或视线偏差、非关键配饰细差和背景小物只记建议，"
            "必须通过。不确定时从宽通过，交由人工抽检。\n"
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
            f"- 当前镜头逐角色构图合同:{composition_line}\n"
            "必须真实打开待检图和对应参考图，先逐人判断实际可见视角，再核验："
            "正面/四分之三核对脸型、五官比例、眼鼻嘴、发际线、年龄感；"
            "严格侧面核对额头—鼻梁—唇—下颌侧廓、耳朵、发际线、发型轮廓、"
            "体型、服装、道具和站位，不要求完整正脸；背面/半背影/过肩前景"
            "核对后脑/帽冠和发型轮廓、肩背体型、服装背片/接缝/材质配色、"
            "身份配饰、道具、朝向和站位，正脸不可见本身不得判失败。"
            "可见身份点相符时 identity_match 必须为 true。文字与最终立绘"
            "冲突时，身份以参考图为准。\n"
            "必须单独核对每个人物的性别与性别表达。女性画成男性、男性画成女性"
            "一律判失败，不能因服装、发色或气质相似而放行。\n"
            "- 当前镜头逐角色服装/头饰/妆发状态:"
            + ("；".join(
                f"{name}={wardrobe}" for name, wardrobe in
                (payload.get("expected_wardrobe") or {}).items())
               or "本镜未声明服装状态")
            + "\n只要声明了服装状态，就必须逐人核对；无换装动作却从官服变"
            "常服、漏掉帽冠或擅自改妆发，一律判失败。侧面/背面按可见服装"
            "轮廓、背片、材质、配色、头饰和发型核对，不要求正脸。\n"
            "必须点数画面实际可见人物；多一个、少一个、角色被复制或两人合成一人"
            "都必须判失败。过肩镜中前景半身背影/肩膀是已登记的对话者本人，"
            "只计该角色1人，不得另算成第三人、陌生人或人物复制。\n"
            + overlay_line
            + f"- 场景:{payload.get('location', '按提示词')};"
            f"动作:{payload.get('action', '按提示词')};"
            f"镜头景别:{payload.get('camera', '不限')}\n"
            f"- 物理/空间逻辑硬检查:{physical_line}\n"
            "必须核对人物、镜头、道具的前后左右关系、朝向、视线、接触点、重力支撑和动作可达性；"
            "电脑/手机/屏幕等设备必须按真实使用方向成立，屏幕正面、键盘/手部和使用者关系不能反向。"
            "凡物理合同中出现“时代物件锁定—”的对象，必须逐件核对结构、"
            "材质和时代形态；例如明代开放式浅盏油灯若被画成带玻璃灯罩、"
            "灯筒或现代旋钮的煤油灯，属于普通观众可见的关键时代错误，"
            "visual_pass 必须为 false。修正指令不能只写“更符合时代”，必须"
            "写清正确结构与明确禁止的错误结构。"
            "只有明显且影响剧情理解或画面可信度的物理/空间错误才令 pass 为 false；"
            "细微透视误差、遮挡造成的不确定关系和背景小物偏差不得失败。\n"
            f"- 不允许出现:{forbid}、字幕条、乱码文字、多余或缺失的人物\n"
            + (("- 剧情允许跨时代出现(穿越/带入物,时代判断以剧本为准,"
                "这些物品出现是正确的,禁止当成时代错乱判失败):"
                + "、".join(payload.get("era_exceptions")) + "\n")
               if payload.get("era_exceptions") else "")
            + "- 当前检查对象是静态关键帧：只检查图中可见的最终状态。不得因为"
            "单张图无法证明运镜、眼神变化过程、呼吸等时间动作而判失败；景别"
            "裁掉且非剧情要求必须出镜的裤子、腰间配饰等，不得仅因不可见判失败。\n"
            "必须把画面本身是否正确与生成输入合同是否正确分开判断；参考图"
            "编号/用途冲突不能伪装成画面人物错误。\n"
            "只在标准输出打印一行 JSON,不要产出任何文件,不要多余文字:"
            '{"pass": true或false, "visual_pass": true或false, '
            '"input_contract_pass": true或false, '
            '"identity_checked": true或false, '
            '"identity_match": true或false, '
            '"identity_checks": [{"character":"角色名",'
            '"view":"front_or_three_quarter/profile/back/back_or_over_shoulder",'
            '"basis":["实际核验项"],"checked":true或false,"match":true或false}], '
            '"gender_checked": true或false, "gender_match": true或false, '
            '"wardrobe_checked": true或false, "wardrobe_match": true或false, '
            '"count_checked": true或false, "count_match": true或false, '
            '"overlay_count_checked": true或false, '
            '"overlay_count_match": true或false, '
            '"detected_overlay_count": 画面实际内心Q版叠层数整数, '
            '"physical_logic_checked": true或false, "physical_logic_match": true或false, '
            '"spatial_logic_checked": true或false, "spatial_logic_match": true或false, '
            '"detected_count": 画面实际人数整数, '
            '"issues": ["每条一句具体原因"], '
            '"image_error": {"summary":"画面错误摘要",'
            '"categories":["identity/count/camera等"],'
            '"evidence":["画面中可见证据"]}, '
            '"prompt_diagnosis": {"status":'
            '"correct/needs_patch/conflicting/insufficient",'
            '"issues":["提示词问题"],'
            '"irrelevant_or_conflicting_sections":["冲突或无关片段"]}, '
            '"reference_diagnosis": {"status":'
            '"correct/needs_adjustment/conflicting/missing/uncertain",'
            '"issues":["参考图问题"],"missing_roles":'
            '[{"role":"用途","character":"角色名或空","reason":"原因"}]}, '
            '"targeted_prompt_patch": {"instructions":'
            '["只针对当前镜头的短修正指令"],'
            '"preserve":["必须保持不变的内容"],'
            '"max_scope":"current_shot_only"}, '
            '"reference_adjustments": [{"action":'
            '"keep/remove/rebind/replace/add/drop_revision_base",'
            '"target_index":参考图编号整数,"role":"用途",'
            '"character":"角色名或空","replacement_selector":'
            '{"asset_id":已有资产ID或null,"role":"用途",'
            '"character":"角色名或空"},"reason":"调整原因"}]'
            + escalation_schema
            + '}'
        )
        return instruction, [], {"qc": True}
    if capability == "cover":
        target = out_dir / "cover.png"
        cover_prompt = str(
            payload.get("prompt_compact")
            or payload.get("prompt") or "").strip()
        instruction = (
            f"为账号内容生成封面并保存到 {target}(PNG,{size})。"
            + (f"{cover_prompt}。" if cover_prompt else
               f"作品《{payload.get('title', '')}》"
               f"第{payload.get('episode', 0)}集,"
               f"主题:{payload.get('tagline', '')}。"
               "构图吸睛、适合短视频封面,可留出大标题排版空间。")
            + "封面若出现人物，只允许出现并严格对应:"
            f"{'、'.join(payload.get('characters', [])) or '无人'}。"
            f"{_ref_line(payload, prompt_text)}{common}只产出该文件。")
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


def _terminate_process_group(proc, grace=5):
    """终止 Codex 及其 imagegen 后代，避免暂停后留下幽灵任务。"""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()


def run(request, codex, timeout, extra_args, plain=False):
    capability = request["capability"]
    payload = request.get("payload", {})
    out_dir = Path(request["out_dir"]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if payload.get("require_reference_images"):
        character_refs = list(payload.get("character_refs") or [])
        declared = list(character_refs)
        declared.extend(payload.get("prop_refs") or [])
        declared.extend(
            ref.get("uri") for ref in
            (payload.get("identity_references") or [])
            if isinstance(ref, dict) and ref.get("uri"))
        declared.extend(payload.get("reference_images") or [])
        declared.extend(
            payload.get(key) for key in (
                "spatial_ref", "image_uri", "chain_first_uri",
                "scene_ref", "style_ref")
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
    # 断点续跑时 canonical 目标可能已经存在。只检查 exists 会把一次
    # “Codex 没有产出任何文件”的失败调用误认成成功，并把上一轮旧图
    # 当作本轮新候选反复质检。记录调用前指纹，调用后必须看到真正更新。
    freshness_targets = list(targets)
    if capability == "frames" and data.get("first_source"):
        # 帧链/关键帧复用会在 build_instruction 中主动铺好首帧；该文件
        # 按合同就是不应修改的，只要求本轮新生成的尾帧发生变化。
        freshness_targets = [Path(data["last"])]
    before_targets = {}
    for target in freshness_targets:
        path = Path(target)
        try:
            stat = path.stat()
            before_targets[str(path)] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            before_targets[str(path)] = None
    exec_args = (
        [] if plain else list(
            PROMPT_REVIEW_EXEC_ARGS
            if capability == "prompt_review" else DEFAULT_EXEC_ARGS))

    def invoke(args):
        proc = subprocess.Popen(
            [codex, "exec", *args, *extra_args, instruction],
            # stdin 必须显式给 DEVNULL:codex exec 一旦发现 stdin 是打开
            # 的管道就停在「Reading additional input from stdin...」永等,
            # 直到超时被杀,报「退出码 1」。不指定时子进程继承父进程的
            # stdin——服务在前台跑(终端)时没事,以 nohup/launchd 起来时
            # stdin 是管道,整条出图产线全灭(《长夏记事》images 阶段实案)。
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(out_dir), start_new_session=True)
        previous_handlers = {}

        def forward_termination(signum, _frame):
            _terminate_process_group(proc)
            raise SystemExit(128 + signum)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(
                    signum, forward_termination)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # subprocess.run 只会终止直接子进程；Codex 内置 imagegen 可能继续
            # 留在后台占用并发。整组 TERM→KILL，保证超时后没有孤儿进程。
            _terminate_process_group(proc)
            raise
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
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
    if capability == "prompt_review":
        verdict = _extract_json(proc.stdout)
        if verdict is None:
            return {
                "ok": False,
                "error": "Codex 未返回可解析的提示词审核 JSON",
            }
        approved = verdict.get("approved")
        optimized = str(verdict.get("optimized_prompt") or "").strip()
        if not isinstance(approved, bool):
            return {
                "ok": False,
                "error": "Codex 提示词审核缺少布尔字段 approved",
            }
        if approved and not optimized:
            return {
                "ok": False,
                "error": "Codex 已批准提示词但没有返回 optimized_prompt",
            }
        verdict.setdefault(
            "schema", payload.get(
                "review_schema", "aifos.codex-prompt-review/v1"))
        verdict.setdefault("issues_found", [])
        verdict.setdefault("changes_made", [])
        verdict.setdefault("blocking_reason", "")
        return {
            "ok": True,
            "data": verdict,
            "uri": "",
            "model": "Codex 提示词审核优化",
        }
    if capability == "image_qc":
        verdict = _extract_json(proc.stdout)
        if verdict is None or "pass" not in verdict:
            # 看不到可靠的结构化结论就失败关闭。伪造 checked/match=true
            # 会让换性别、人数错误或串脸图片绕过导演层硬门槛。
            fallback = {
                "pass": False,
                "issues": ["Codex 未返回可解析的视觉质检 JSON，图片未放行"],
                "identity_checked": False, "identity_match": False,
                "gender_checked": False, "gender_match": False,
                "count_checked": False, "count_match": False,
                "note": "codex 未返回可解析判定,失败关闭"}
            fallback.update(normalize_generation_diagnostics(
                fallback, issues=fallback["issues"]))
            return {"ok": True, "data": fallback, "uri": ""}
        validation_error = validate_image_qc(verdict)
        if validation_error:
            fallback = {
                "pass": False,
                "issues": [
                    "Codex 视觉质检返回结构无效，图片未放行："
                    + validation_error],
                "identity_checked": False, "identity_match": False,
                "gender_checked": False, "gender_match": False,
                "count_checked": False, "count_match": False,
                "note": validation_error,
            }
            fallback.update(normalize_generation_diagnostics(
                fallback, issues=fallback["issues"]))
            return {"ok": True, "data": fallback, "uri": "",
                    "model": "Codex 视觉质检"}
        verdict.setdefault("issues", [])
        verdict.update(normalize_generation_diagnostics(
            verdict, issues=verdict.get("issues")))
        return {"ok": True, "data": verdict, "uri": "",
                "model": "Codex 视觉质检"}
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        return {"ok": False,
                "error": f"codex 未产出期望文件: {', '.join(missing)}"}
    stale = []
    for target in freshness_targets:
        path = Path(target)
        previous = before_targets.get(str(path))
        if previous is None:
            continue
        try:
            stat = path.stat()
            current = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        if current == previous:
            stale.append(str(path))
    if stale:
        return {
            "ok": False,
            "error": (
                "codex 本轮未更新期望文件，拒绝把断点旧图冒充新结果: "
                + ", ".join(stale)),
        }
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
