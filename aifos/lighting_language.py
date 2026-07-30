"""光影语言:把"氛围感"翻译成可执行的布光、光质与景深条款。

2026-07-28 盘点发现的空洞:镜头语言只有景别(6)、角度(4)、机位(4)
共 14 条,全库关于光的表述只有一句"主光方向保持一致"的连续性锚点。
提示词只告诉模型「机器放哪儿、画面里有谁」,从没告诉它「怎么打光」
——模型于是默认给平光,成片自然没有氛围。

本模块补齐第四、第五维度:
- 布光风格(逆光轮廓光/伦勃朗/低调/顶光/实用光源主导…)
- 光质、色温与冷暖对比、体积光与空气感
- 景深与焦外(大光圈虚化、光源化成光斑)

选型不靠猜:按场景的时间、地点属性与镜头情绪自动匹配,并显式声明
「光影服从场景母版的主光方向」以免与连续性锚点打架。画风门控与
realism_language 同口径:非写实画风不注入摄影术语。
"""

from .realism_language import realism_applicable

# ---- 布光风格:每种给出「光源位置 + 画面可验证特征」 ----
LIGHTING_STYLES = {
    "rim_backlight": {
        "label": "逆光轮廓光",
        "clause": (
            "主光位于人物后方偏侧,先于面部照亮轮廓:发丝边缘出现连续的"
            "亮金色描边,肩线与手臂外缘有明确高光带;面部处于相对暗部,"
            "由环境反射补出可读的五官,禁止把脸打成死黑"),
    },
    "rembrandt": {
        "label": "伦勃朗光",
        "clause": (
            "主光在人物斜前方约45度、略高于眼位:暗侧脸颊出现倒三角形"
            "亮斑,鼻影与脸颊阴影自然衔接不断裂,眼窝保留细节"),
    },
    "low_key": {
        "label": "低调照明",
        "clause": (
            "画面整体压暗,亮部只占小面积并集中在主体:大面积暗部必须"
            "保留可辨的层次与材质,禁止死黑色块;背景明显暗于人物"),
    },
    "practical_lit": {
        "label": "实用光源主导",
        "clause": (
            "画面内的灯笼、烛火、油灯、窗光等实体光源就是照明动机:"
            "光源本身在画面中可见并发亮,人物身上的受光方向、衰减与"
            "色温必须与该光源一致,越近越亮"),
    },
    "top_light": {
        "label": "顶光",
        "clause": (
            "主光自上方垂直照下:头顶、肩上表面最亮,眼窝、鼻下与下颌"
            "下方形成明确投影,气氛压抑"),
    },
    "soft_daylight": {
        "label": "柔和天光",
        "clause": (
            "大面积柔光自侧上方进入:阴影边缘柔和过渡、无硬边,肤色通透,"
            "画面反差适中"),
    },
    "overcast_flat": {
        "label": "阴天散射光",
        "clause": (
            "光线来自整片天空,方向弱但仍有上下差:头顶略亮、下颌略暗,"
            "阴影极淡,禁止完全无影的纯平光"),
    },
}

# ---- 轮廓分离(作为附加项时的正确写法) ----
# 2026-07-31 修:rim 作为 extras 附加时,原先直接拼 rim_backlight 整段,
# 而那段开头就是「主光位于人物后方偏侧」——于是「灯笼是主光」和「主光
# 在后方」同时出现在一条提示词里,两个主光互相打架,模型只能二选一或各
# 打一半。附加项只承担「轮廓分离」这一辅助功能,不得再声明一次主光位置。
RIM_ACCENT_CLAUSE = (
    "轮廓分离(辅助光,不是主光):在主光反侧补一道弱后侧光,勾出发丝边缘、"
    "肩线与衣缘,把人物从背景里剥离;强度明显低于主光,不改变主光方向,"
    "面部结构仍由主光决定,禁止轮廓与正脸同时无因过亮")

# ---- 动机化打光的因果收口 ----
# 依据知识大脑 motivated-lighting-contract(源 super-i info-2899,已实测)。
# A/B 实测暴露本模块的真实缺口:只写灯型、色温和景深,模型会交出「画面里
# 的灯笼没点亮、墙面却有一块无来源亮斑」这种无因照明。补上受光区域、暗部
# 边界、补光/眼神光与背景衰减四项,光才有可核验的因果,而不只是有风格。
LIT_AREA_CLAUSE = (
    "受光区域:逐项落实主光先照到哪里——近侧脸颊、鼻梁、肩线、手部与手中"
    "道具按距离依次变亮,背光侧顺次转暗;暗部保留五官、皮肤与衣料的可辨"
    "细节,不得死黑,亮部不得整片过曝")
WIDE_LIT_AREA_CLAUSE = (
    "受光区域:逐项落实主光先照到哪里——主体、地面与近处结构按距离依次"
    "变亮,远处顺次转暗;暗部保留可辨的结构与材质,不得死黑,亮部不得"
    "整片过曝")
FILL_CLAUSE = (
    "补光:只保留一道来源可信的弱补光(环境反射或墙面回光),强度明显低于"
    "主光,不得把明暗结构重新抹平")
CATCHLIGHT_CLAUSE = (
    "眼神光:双眼各一个小型眼神光,方位、数量与色温必须与主光一致,"
    "禁止无来源的多重眼神光")
BACKGROUND_FALLOFF_CLAUSE = (
    "背景关系:背景比主体暗一至两档,靠光线自然衰减退开;画面里没有对应"
    "光源的地方不得出现亮斑")

_WIDE_TOKENS = ("远景", "全景")
_CLOSE_TOKENS = ("特写", "近景")


def causality_clauses(camera=""):
    """返回本镜的因果收口条款(受光/暗部、补光、眼神光、背景衰减)。

    按景别裁剪:远景写主体与环境的受光层次,近景才写脸颊鼻梁;眼神光
    只在看得见眼睛的景别里要求——远景强行要眼神光是给模型加噪声。
    """
    camera_text = str(camera or "")
    wide = _has(camera_text, _WIDE_TOKENS)
    parts = [WIDE_LIT_AREA_CLAUSE if wide else LIT_AREA_CLAUSE, FILL_CLAUSE]
    if _has(camera_text, _CLOSE_TOKENS):
        parts.append(CATCHLIGHT_CLAUSE)
    parts.append(BACKGROUND_FALLOFF_CLAUSE)
    return parts


# ---- 光质与氛围附加条款 ----
VOLUMETRIC_CLAUSE = (
    "空气中有可见的介质(薄雾、尘埃、香烟或雨丝),光线穿过时形成可辨的"
    "光柱与光晕,靠近光源处介质更亮")
WARM_COOL_CLAUSE = (
    "冷暖对比:受光面偏暖(琥珀/金橙),暗部与环境偏冷(青蓝/灰蓝),"
    "两者在人物边缘交界,禁止全画面同一色调的塑料感")
SHALLOW_DOF_CLAUSE = (
    "浅景深:焦点落在主体面部,背景明显虚化,画面内的点状光源在焦外"
    "化为柔和圆形光斑;虚化不得波及主体识别特征")
DEEP_FOCUS_CLAUSE = (
    "大景深:主体与环境同时清晰,用于交代空间关系与人物站位")

# 光影负面控制:直接对着"平光废片"的病灶写
NEGATIVE_CONTROLS = (
    "【光影负面】禁止:无方向的平光与全局均匀曝光、人物像被贴在背景上"
    "的剪贴感、没有任何阴影或投影、暗部糊成一团死黑、光源方向与画面内"
    "可见光源矛盾、画面内的灯具没点亮却出现它的照明效果、墙面地面出现"
    "无来源亮斑、同时出现两个方向互相矛盾的主光、同一场景跨镜主光忽左"
    "忽右")

# ---- 自动选型:按时间/地点/情绪匹配布光 ----
_NIGHT_TOKENS = ("夜", "晚", "深夜", "子时", "黄昏", "傍晚", "入夜")
_INTERIOR_TOKENS = ("室内", "屋", "房", "厅", "殿", "阁", "帐", "棚",
                    "书房", "闺", "内室", "堂")
_PRACTICAL_TOKENS = ("灯", "烛", "灯笼", "油灯", "篝火", "火把", "炉",
                     "香炉", "壁灯")
_TENSE_TOKENS = ("紧张", "压抑", "阴森", "危险", "对峙", "威胁", "审问",
                 "凶", "杀", "刑", "囚")
_HAZE_TOKENS = ("雾", "烟", "雨", "尘", "香", "蒸汽", "水汽", "霭")
_SOFT_TOKENS = ("温柔", "温馨", "回忆", "抒情", "宁静", "日常")


def _has(text, tokens):
    return any(token in text for token in tokens)


def select_lighting(*, location="", time_of_day="", mood="",
                    camera="", scene_action="", genre=""):
    """按题材基调 + 场景事实选布光风格,返回 (style_key, extras)。

    先看题材盘定基调(仙侠逆光体积光/悬疑低调硬光/甜宠柔光高调…),
    再用场景的时间、地点、光源事实微调——避免各种题材的剧全长成
    同一副样子(用户:不同风格的漫剧不同镜头效果)。选型只用剧本已有
    事实,不发明新剧情;毫无线索时给柔和天光这一最安全档。
    """
    text = " ".join(str(value or "") for value in (
        location, time_of_day, mood, scene_action))
    genre_key = match_genre(genre) if genre else ""
    genre_look = GENRE_LOOKS.get(genre_key)
    night = _has(text, _NIGHT_TOKENS)
    interior = _has(text, _INTERIOR_TOKENS)
    practical = _has(text, _PRACTICAL_TOKENS)
    extras = list(genre_look["extras"]) if genre_look else []
    # 题材基调优先,但画面内有实体光源这类硬事实仍要压过题材默认:
    # 夜里点着灯笼就必须由灯笼供光,不能因为"甜宠片"硬打柔和天光。
    if genre_look and not (practical and (night or interior)):
        style = genre_look["style"]
        if style == "rim_backlight" or night or interior:
            extras.append("rim")
        return style, _finish_extras(extras, text, camera, night, practical,
                                     interior)
    if practical and (night or interior):
        style = "practical_lit"
        extras.append("rim")
    elif night:
        style = "low_key"
        extras.append("rim")
    elif _has(text, _TENSE_TOKENS):
        style = "top_light"
    elif interior:
        style = "rembrandt"
    elif _has(text, _SOFT_TOKENS):
        style = "soft_daylight"
    else:
        style = "soft_daylight"
    return style, _finish_extras(extras, text, camera, night, practical,
                                 interior)


def _finish_extras(extras, text, camera, night, practical, interior):
    """补齐与场景/景别绑定的附加项(体积光、冷暖、景深)。"""
    extras = list(extras)
    if _has(text, _HAZE_TOKENS) or (night and practical):
        extras.append("volumetric")
    if night or practical or interior:
        extras.append("warm_cool")
    # 特写/近景默认浅景深;全景/远景要交代空间,改用大景深
    camera_text = str(camera or "")
    if _has(camera_text, ("远景", "全景")):
        extras = [item for item in extras if item != "shallow_dof"]
        extras.append("deep_focus")
    elif "deep_focus" not in extras:
        extras.append("shallow_dof")
    return list(dict.fromkeys(extras))


def lighting_clause(*, location="", time_of_day="", mood="", camera="",
                    scene_action="", style_override="", genre=""):
    """生成本镜的【光影】执行条款(单行,可直接进镜头合同)。"""
    key = str(style_override or "").strip()
    if key not in LIGHTING_STYLES:
        key, extras = select_lighting(
            location=location, time_of_day=time_of_day, mood=mood,
            camera=camera, scene_action=scene_action, genre=genre)
    else:
        _auto, extras = select_lighting(
            location=location, time_of_day=time_of_day, mood=mood,
            camera=camera, scene_action=scene_action, genre=genre)
    entry = LIGHTING_STYLES[key]
    parts = [f"{entry['label']}:{entry['clause']}"]
    if "rim" in extras and key != "rim_backlight":
        parts.append(RIM_ACCENT_CLAUSE)
    parts.extend(causality_clauses(camera))
    if "volumetric" in extras:
        parts.append(VOLUMETRIC_CLAUSE)
    if "warm_cool" in extras:
        parts.append(WARM_COOL_CLAUSE)
    if "shallow_dof" in extras:
        parts.append(SHALLOW_DOF_CLAUSE)
    if "deep_focus" in extras:
        parts.append(DEEP_FOCUS_CLAUSE)
    genre_look = GENRE_LOOKS.get(match_genre(genre)) if genre else None
    if genre_look:
        parts.append(f"本剧题材视听基调({genre_look['label']}):"
                     + genre_look["grammar"])
    parts.append(
        "以上光影服从本场景母版已确定的主光方向与色温,同场跨镜不得"
        "忽左忽右")
    return "；".join(parts)


def lighting_lines(style, *, location="", time_of_day="", mood="",
                   camera="", scene_action="", style_override="",
                   genre=""):
    """返回应追加进提示词的光影条款(非写实画风不注入)。"""
    if not realism_applicable(style):
        return []
    return [
        "【光影】" + lighting_clause(
            location=location, time_of_day=time_of_day, mood=mood,
            camera=camera, scene_action=scene_action,
            style_override=style_override, genre=genre),
        NEGATIVE_CONTROLS,
    ]


# ---- 题材风格光影盘(用户要求:不同风格的漫剧不同镜头效果) ----
# 每种题材有自己的成熟视听语法:仙侠靠逆光与体积光造仙气,悬疑靠
# 低调硬光与失衡构图造压迫,甜宠靠柔光高调与浅景深造亲密。选型先看
# 题材盘,再按场景时间地点微调——避免所有剧都长成同一副样子。
GENRE_LOOKS = {
    "xianxia": {
        "label": "仙侠/古风幻想",
        "tokens": ("仙侠", "修仙", "玄幻", "古风幻想", "修真", "神话",
                   "门派", "灵气"),
        "style": "rim_backlight",
        "extras": ("volumetric", "warm_cool", "shallow_dof"),
        "grammar": (
            "偏爱逆光与体积光营造仙气:发丝与衣袂边缘常有金色描边,"
            "空气中有可见灵尘或雾霭;构图多用留白与大远景交代天地尺度"),
        "camera": {"构图": "留白", "运镜": "推"},
    },
    "suspense": {
        "label": "悬疑/罪案",
        "tokens": ("悬疑", "罪案", "凶杀", "推理", "惊悚", "刑侦",
                   "命案", "谜"),
        "style": "low_key",
        "extras": ("warm_cool", "shallow_dof"),
        "grammar": (
            "低调硬光,大面积暗部只留少量关键亮部;单一光源制造强对比"
            "与长投影;构图多用框中框、前景遮挡与斜角制造失衡与窥视感"),
        "camera": {"构图": "前景遮挡", "角度": "斜角"},
    },
    "romance": {
        "label": "甜宠/言情",
        "tokens": ("甜宠", "言情", "恋爱", "都市情感", "青春", "校园",
                   "暗恋"),
        "style": "soft_daylight",
        "extras": ("warm_cool", "shallow_dof"),
        "grammar": (
            "柔光高调,反差小、肤色通透;大光圈浅景深把环境化成暖色光斑,"
            "多用近景与四分之三面强化亲密;避免硬阴影与压迫性俯仰"),
        "camera": {"景别": "近景", "机位": "四分之三面"},
    },
    "palace": {
        "label": "宫斗/权谋",
        "tokens": ("宫斗", "权谋", "朝堂", "宅斗", "后宫", "官场",
                   "夺嫡"),
        "style": "practical_lit",
        "extras": ("rim", "volumetric", "warm_cool", "shallow_dof"),
        "grammar": (
            "以殿内烛火、宫灯为光源动机,暖光与幽深暗部并置;构图多用"
            "中心对称与框中框强调等级与规训,人物常被建筑结构包围"),
        "camera": {"构图": "中心对称"},
    },
    "urban": {
        "label": "都市/职场现代",
        "tokens": ("都市", "职场", "商战", "现代", "写字楼", "医疗",
                   "律政"),
        "style": "soft_daylight",
        "extras": ("shallow_dof",),
        "grammar": (
            "干净的冷调布光,窗光与顶灯为主,反差适中;构图规整偏三分法,"
            "少用夸张俯仰,靠景别切换推进节奏"),
        "camera": {"构图": "三分法", "角度": "平视"},
    },
    "wuxia": {
        "label": "武侠/江湖",
        "tokens": ("武侠", "江湖", "镖局", "刀客", "剑客", "侠"),
        "style": "rembrandt",
        "extras": ("rim", "warm_cool", "shallow_dof"),
        "grammar": (
            "硬朗侧光刻画轮廓与肌理,尘土与刀光提供空气感;构图多用"
            "对角线与低角度强化力量与对峙"),
        "camera": {"构图": "对角线", "角度": "低角度"},
    },
    "horror": {
        "label": "灵异/恐怖",
        "tokens": ("灵异", "恐怖", "鬼", "诡", "邪祟", "阴宅"),
        "style": "top_light",
        "extras": ("volumetric", "shallow_dof"),
        "grammar": (
            "自下或自上的单一冷光源制造非常态阴影,眼窝与下颌投影加重;"
            "大量负空间与不对称构图,重要信息常藏在暗部边缘"),
        "camera": {"构图": "留白", "角度": "斜角"},
    },
}


def match_genre(*texts):
    """题材/画风文本 → 风格盘 key(无命中返回空串)。"""
    text = " ".join(str(value or "") for value in texts)
    for key, look in GENRE_LOOKS.items():
        if any(token in text for token in look["tokens"]):
            return key
    return ""


def genre_camera_defaults(genre_key):
    """该题材的默认镜头倾向(仅在分镜没写时作为建议,不覆盖显式值)。"""
    look = GENRE_LOOKS.get(genre_key or "")
    return dict(look["camera"]) if look else {}
