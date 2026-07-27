"""把视觉质检结果编译成下一次生成可执行的修订提示词。

质检原因是证据，不能原样堆进提示词让模型自己猜该怎么改。这里把常见失败
维度转换成短的、互不冲突的动作指令，同时保留原始原因供人工审计。
"""

from __future__ import annotations

import re

from .generation_diagnostics import (
    normalize_generation_diagnostics,
    reference_actions,
    targeted_prompt_patch as render_targeted_prompt_patch,
)


_RULES = (
    ("species", ("动物", "物种", "人类", "小鹿", "animal", "species"),
     "严格按剧本声明的物种与身份生成；不要因角色名字或参考图语义误判为另一物种，纠正头身比例、面部结构和体态。"),
    ("nonface_visibility",
     ("背面", "背影", "半背影", "过肩", "侧脸", "严格侧面",
      "正脸不可见", "五官不可见", "back view", "over shoulder", "profile"),
     "保持分镜指定的侧面/背面/过肩构图，禁止强迫背影人物转成正脸；"
     "只按对应视角母资产修正发型或帽冠轮廓、侧廓/肩背体型、服装、"
     "身份配饰、道具、朝向和站位，并保留正面主体的脸。"),
    ("identity", ("身份", "脸", "五官", "发型", "换脸", "人物不一致", "同一个人",
                   "identity", "face"),
     "重新读取对应角色的最终立绘，只锁定脸型、五官骨相、发际线、发型轮廓和年龄感；只修正该角色，不改其他角色。"),
    ("gender", ("性别", "男性", "女性", "gender", "sex"),
     "严格按最终立绘纠正性别与性别表达；不得用服装、发色或气质替代身份判断。"),
    ("count", ("人数", "数量", "多余人物", "缺失人物", "新增人物", "重复人物",
                "多出", "少了", "person count"),
     "严格按分镜名单修正人数；删除多余人物，补回缺失人物，禁止复制、合并或新增路人。"),
    ("wardrobe", ("服装", "衣服", "配饰", "妆造", "发饰", "制服", "wardrobe", "outfit"),
     "按本镜剧本和对应服装参考图修正服装、配饰和妆造；身份参考图只锁脸和发型，不把错误服装带入。"),
    ("scene", ("场景", "空间", "环境", "建筑", "陈设", "地点", "scene", "location"),
     "按本镜场景基准图修正空间结构、陈设、材质和主光方向；禁止把参考图人物或文字带入。"),
    ("physics", ("物理", "物理逻辑", "空间逻辑", "空间关系", "方向反",
                  "朝向不成立", "接触点", "支撑", "漂浮", "屏幕背面",
                  "键盘朝向", "使用关系", "physics", "spatial logic"),
     "只修正质检指出的物理/空间关系：保持人物、镜头和道具在同一空间坐标中，"
     "核对前后左右、朝向、视线、手部接触、重力支撑和动作可达性；电脑/手机屏幕"
     "按真实使用方向摆放，未指出的身份、服装、场景和文字保持不变。"),
    ("text", ("文字", "字幕", "乱码", "logo", "水印", "可读", "subtitle", "watermark"),
     "画面文字只允许沿用已锁定关键帧；删除字幕、乱码、Logo和水印，不让视频模型重新写字。"),
    ("continuity", ("连续", "首帧", "尾帧", "起点", "终点", "状态", "站位", "道具",
                     "continuity", "first frame", "last frame"),
     "以首帧为唯一动作起点、尾帧为唯一动作终点；保持人物站位、朝向、服装、道具和场景连续。"),
    ("camera", ("镜头", "运镜", "机位", "景别", "角度", "视角", "构图",
                 "俯拍", "仰拍", "顶拍", "平视", "过肩", "背面", "camera",
                 "motion"),
     "只执行分镜指定的一次运镜和景别；按镜头合同「按可见特征执行并核验」"
     "条款逐项复核:景别看取景边界(出画/入画部位)、角度看俯仰透视"
     "(头顶/下颌可见性与地面天空占比)、机位看面部朝向(背面不得出现"
     "任何面部器官,侧面只见单侧眼耳);镜头运动必须服务本镜主动作,"
     "不新增第二种运镜。"),
    ("action", ("动作", "表演", "姿势", "运动", "行为", "action", "motion"),
     "只修正质检指出的主体动作/表演；不补写剧情，不增加第二个主动作。"),
    ("audio", ("配音", "口型", "声音", "音频", "lip", "audio", "voice"),
     "保持即梦内置配音与对口型；修正说话人、台词时序和口型对应，不添加字幕。"),
    ("structure", ("手指", "肢体", "穿模", "比例", "畸形", "结构", "hand", "body"),
     "修正手指、关节、肢体比例、遮挡和接触点；人物身份、服装、场景与构图保持不变。"),
    ("style", ("画风", "渲染", "材质", "色彩", "光影", "style", "render"),
     "只恢复项目风格基准的媒介、材质、色彩和光影；不改变人物身份、动作或场景布局。"),
)

# 屏幕/书页/文件页是“画面内文字载体”，不应落入 generic。尤其是
# “空白冷白、未显示页面”这类原因，旧版会只附加“保持其余内容不变”，
# 导致模型继续沿用失败图里的空白屏幕。
_SCREEN_TEXT_TOKENS = (
    "电脑", "笔记本", "屏幕", "显示器", "手机屏", "平板", "页面",
    "书页", "史书", "网页", "文档", "合同上", "合同文本", "奏折", "纸张",
    "冷白", "空白屏", "空白画面",
    # 注意:不得使用裸词「合同」——质检判词大量出现「不符合镜头合同
    # 要求」,会把视角/人数类失败误归为屏幕文字,修订指令随之跑偏。
)
_TEXT_MISSING_TOKENS = (
    "未显示", "未呈现", "未出现", "没有显示", "缺少", "缺失", "未包含",
    "空白", "冷白", "不见", "未看到", "未出现",
)
_TEXT_UNWANTED_TOKENS = (
    "乱码", "错字", "多余文字", "错误文字", "新增文字", "字幕条", "水印",
    "logo", "标志", "标签",
)


def _screen_text_instruction(reason, whitelist=None, carrier=""):
    """把屏幕/书页文字问题变成可执行的局部修订指令。"""
    reason = str(reason or "")
    wanted = [str(item).strip() for item in (whitelist or []) if str(item).strip()]
    # 没有结构化白名单时，从质检证据中保留书名号内容；这是比“请显示页面”
    # 更明确的补救，且不会凭空编造未被质检指出的文字。
    if not wanted:
        wanted = list(dict.fromkeys(re.findall(
            r"[《「『【]([^》」』】]{1,40})[》」』】]", reason)))
    wanted_text = "、".join(dict.fromkeys(wanted)) or "质检原因中明确指出的页面原文"
    missing = any(token in reason.lower() for token in _TEXT_MISSING_TOKENS)
    unwanted = any(token in reason.lower() for token in _TEXT_UNWANTED_TOKENS)
    if missing and not unwanted:
        carrier = str(carrier or "本镜声明的屏幕/页面载体").strip()
        return (
            f"【TEXT ASSET HARD GATE】只修正{carrier}这一块局部；"
            "载体的开启状态、朝向、使用者关系和机位必须服从当前镜头物理合同；"
            "不得凭空指定笔记本、手机或其他未声明设备；载体内实际显示"
            f"{wanted_text}；逐字沿用该原文，不得改写、缩写或用随机乱码替代。"
            "禁止空白冷白屏、纯白发光占位面、黑屏占位面和模糊不可读页面；"
            "屏幕外的人物、服装、配饰、场景、构图、光线和金属机身全部保持失败"
            "图不变；背景仍不得出现额外字幕、Logo、水印或无关文字。"
        )
    return (
        "【TEXT ASSET HARD GATE】只修正质检指出的屏幕/页面文字；删除乱码、"
        "错字、字幕条、Logo、水印和未授权文字，保留已锁定的合法原文；"
        "屏幕外人物、服装、场景、构图和光线保持不变。"
    )


def _issue_text(issue):
    if isinstance(issue, dict):
        return str(issue.get("message") or issue.get("reason")
                   or issue.get("check") or "").strip()
    return str(issue or "").strip()


def optimize_qc_feedback(
        issues, *, mode="image", readable_text=None, diagnostics=None):
    """返回原始原因、分类指令及可直接附加到提示词的修订文本。"""
    reasons = []
    instructions = []
    categories = []
    seen_reasons = set()
    seen_categories = set()
    for issue in issues or []:
        reason = re.sub(r"\s+", " ", _issue_text(issue)).strip()
        if not reason or reason in seen_reasons:
            continue
        seen_reasons.add(reason)
        reasons.append(reason)
        lowered = reason.lower()
        matched = False
        # 先识别物理/空间关系；“屏幕方向反了”不能被误归为文字资产问题。
        physics_tokens = (
            "物理", "物理逻辑", "空间逻辑", "空间关系", "方向反",
            "朝向不成立", "接触点", "支撑", "漂浮", "屏幕背面",
            "键盘朝向", "使用关系", "physics", "spatial logic",
        )
        if any(token.lower() in lowered for token in physics_tokens):
            category = "physics"
            if category not in seen_categories:
                seen_categories.add(category)
                categories.append(category)
                instructions.append(
                    "只修正质检指出的物理/空间关系：保持人物、镜头和道具在同一空间坐标中，"
                    "核对前后左右、朝向、视线、手部接触、重力支撑和动作可达性；电脑/手机屏幕"
                    "按真实使用方向摆放，未指出的身份、服装、场景和文字保持不变。")
            continue
        # 屏幕/页面问题必须先于 generic 和普通 text 规则处理；“电脑屏幕
        # 空白”本质是缺失文字资产，不能套用“删除字幕/乱码”。
        if any(token.lower() in lowered for token in _SCREEN_TEXT_TOKENS):
            category = "screen_text"
            if category not in seen_categories:
                seen_categories.add(category)
                categories.append(category)
                instructions.append(_screen_text_instruction(
                    reason, (readable_text or {}).get("whitelist")
                    if isinstance(readable_text, dict) else None,
                    (readable_text or {}).get("carrier")
                    if isinstance(readable_text, dict) else ""))
            continue
        for category, tokens, instruction in _RULES:
            if any(token.lower() in lowered for token in tokens):
                matched = True
                if category not in seen_categories:
                    seen_categories.add(category)
                    categories.append(category)
                    instructions.append(instruction)
                break
        if not matched:
            category = "generic"
            if category not in seen_categories:
                seen_categories.add(category)
                categories.append(category)
                instructions.append(
                    "只修正质检明确指出的问题；未指出的人物、服装、场景、道具、"
                    "构图、光线和文字保持不变。")
    if not reasons:
        reasons = ["质检未通过，但未返回具体原因"]
        categories = ["generic"]
        instructions = [
            "先重新核对质检清单，再只修正可确认的问题；其余画面保持不变。"
        ]
    normalized_diagnostics = (
        normalize_generation_diagnostics(
            diagnostics, issues=reasons)
        if diagnostics is not None else None)
    diagnostic_patch = (
        render_targeted_prompt_patch(normalized_diagnostics)
        if normalized_diagnostics is not None else "")
    if diagnostic_patch:
        # The structured QC patch has inspected the exact prompt/reference
        # inputs.  It is more specific than keyword rules; do not concatenate
        # both and recreate the prompt clutter this compiler is meant to avoid.
        instructions = list(
            normalized_diagnostics["targeted_prompt_patch"]["instructions"])
    if mode == "video":
        scope = (
            "以首帧/尾帧和已锁定参考图为硬边界；只让修订后的画面动起来，"
            "不重设计人物、场景或剧情。"
        )
    else:
        scope = (
            "以当前失败图为待修改基底；只修正上述问题，未提及内容保持不变。"
        )
    text = (
        diagnostic_patch
        if diagnostic_patch else (
            "【质检原因】" + "；".join(reasons)
            + "。\n【自动优化修订】" + "；".join(instructions)
            + "\n【修订边界】" + scope)
    )
    result = {
        "mode": mode,
        "issues": reasons,
        "categories": categories,
        "instructions": instructions,
        "text": text,
    }
    if normalized_diagnostics is not None:
        result.update({
            "diagnostics": normalized_diagnostics,
            "targeted_prompt_patch": diagnostic_patch,
            "reference_adjustments": reference_actions(
                normalized_diagnostics),
            "diagnosis_complete": normalized_diagnostics[
                "diagnosis_complete"],
        })
    return result
