"""剧本 AI 分析与制作圣经。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone


STORY_ANALYSIS_SCHEMA = "aifos.story-analysis/v1"
_GENERIC_ERA_MARKERS = (
    "以剧本明示为准", "未明示", "由本集剧情", "由剧情决定", "待分析",
)


def _text(value, fallback=""):
    value = str(value or "").strip()
    return value or fallback


def _list(value, fallback=None):
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
            return items
    if isinstance(value, str) and value.strip():
        return [
            item.strip() for item in value.replace("；", "，").split("，")
            if item.strip()
        ]
    return list(fallback or [])


def _dict(value):
    return value if isinstance(value, dict) else {}


def _genre(script):
    blob = " ".join((
        _text(script.get("project_title")),
        _text(script.get("episode_title")),
        _text(script.get("logline")),
        " ".join(_text(scene.get("action"))
                 for scene in script.get("scenes", [])
                 if isinstance(scene, dict)),
    ))
    for tokens, label in (
        (("大明", "明朝", "大唐", "唐朝", "大宋", "宋朝", "大清",
          "清朝", "皇帝", "太子", "陛下", "朝堂"), "历史 / 权谋"),
        (("民国", "旧上海", "军阀", "租界"), "民国 / 年代"),
        (("仙侠", "修仙", "宗门", "妖", "灵力"), "东方幻想 / 仙侠"),
        (("乙女", "恋爱", "暧昧", "心动"), "现代乙女 / 情感"),
        (("校园", "高中", "大学", "社团"), "青春校园"),
        (("赛博", "星际", "未来", "机甲"), "未来科幻"),
        (("悬疑", "案件", "凶手", "侦探"), "悬疑推理"),
        (("女团", "男团", "偶像", "舞台"), "偶像成长"),
    ):
        if any(token in blob for token in tokens):
            return label
    return "剧情向精品漫剧"


def _story_blob(script):
    parts = [
        _text(script.get("project_title")),
        _text(script.get("episode_title")),
        _text(script.get("logline")),
    ]
    story_world = _dict(script.get("story_world"))
    story_background = _dict(script.get("story_background"))
    parts.extend(_text(value) for value in story_world.values())
    parts.extend(_text(value) for value in story_background.values())
    for character in script.get("characters", []):
        if isinstance(character, dict):
            parts.extend(_text(character.get(key)) for key in (
                "name", "role", "identity", "occupation", "backstory"))
    for scene in script.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        parts.extend((_text(scene.get("location")), _text(scene.get("action"))))
        for line in scene.get("lines", []):
            if isinstance(line, dict):
                parts.extend((
                    _text(line.get("character")),
                    _text(line.get("dialogue")),
                ))
    return " ".join(parts)


def _has_any(text, tokens):
    return any(token in text for token in tokens)


def infer_story_visual_context(script):
    """从正文证据推导世界和制作风格,不把媒介或古今写死。

    这是 AI Provider 不可用时的确定性底座。在线 AI 可以给出更细分析,
    但同样必须服从剧本中的朝代、地域、人物阶层和物质文化证据。
    """
    text = _story_blob(_dict(script))
    contexts = (
        {
            "key": "ming_history",
            "tokens": ("大明", "明朝", "洪武", "永乐", "嘉靖", "万历",
                       "崇祯", "乾清宫", "锦衣卫", "东厂"),
            "era": "中国明代；具体年份、地域与政局以剧本证据为准",
            "style": (
                "明代历史题材自适应制作风格；以史料和剧本身份关系为依据，"
                "人物骨相、冠帽发式、服装制度、礼仪、兵器、建筑与器物符合"
                "明代及人物阶层；色彩、光线、镜头和写实程度随剧情气质设计"),
            "medium": "考据型电影感漫剧；写实程度由剧情与目标观众共同决定",
            "palette": ["明代物质文化色彩", "身份阶层配色", "剧情情绪重点色"],
            "forbidden": ["清代服饰", "唐宋服饰混搭", "现代物件",
                          "仙侠法术乱入", "无史料依据的宫殿与礼制"],
        },
        {
            "key": "republican",
            "tokens": ("民国", "旧上海", "军阀", "租界", "黄包车", "月份牌"),
            "era": "中国民国时期；城市、年份与阵营按剧本证据细化",
            "style": (
                "民国年代题材自适应制作风格；服装、发型、街景、交通、"
                "室内陈设和社会阶层符合具体年代与地域，影像气质服从剧情"),
            "medium": "年代电影感漫剧；材质和写实度根据故事情绪确定",
            "palette": ["年代环境本色", "人物阵营配色", "克制的情绪重点色"],
            "forbidden": ["现代电子设备", "跨年代服装", "古代宫廷混入"],
        },
        {
            "key": "historical",
            "tokens": ("皇帝", "陛下", "太子", "王爷", "朝堂", "奏折",
                       "宫门", "大殿", "县衙", "将军", "城门", "诏书"),
            "era": "中国历史时期；朝代、地域和制度必须从剧本继续核定",
            "style": (
                "历史题材自适应制作风格；先核定朝代与地域，再设计人物骨相、"
                "服制、发式、礼仪、建筑、器物和军政制度；不得用通用古装模板"),
            "medium": "考据型电影感漫剧；不预设2D、3D或真人化比例",
            "palette": ["时代材料本色", "阶层身份配色", "冲突情绪重点色"],
            "forbidden": ["通用影楼古装", "朝代混搭", "现代物件",
                          "无剧情依据的仙侠元素"],
        },
        {
            "key": "xianxia",
            "tokens": ("仙侠", "修仙", "灵力", "灵根", "宗门", "渡劫",
                       "飞剑", "妖丹", "魔尊"),
            "era": "东方幻想架空世界；宗门、地域、能力和物质规则以剧本为准",
            "style": (
                "东方幻想题材自适应制作风格；先建立宗门、种族、能力、"
                "服装结构和建筑材料规则，再按剧情气质决定写实度与渲染媒介"),
            "medium": "东方幻想精品漫剧；媒介由世界观和情绪目标推导",
            "palette": ["阵营识别色", "灵力规则色", "自然材质本色"],
            "forbidden": ["无规则法术", "宗门造型同质化", "现代都市物件"],
        },
        {
            "key": "science_fiction",
            "tokens": ("赛博", "星际", "未来", "机甲", "太空站", "仿生人",
                       "量子", "末世"),
            "era": "未来或架空科技世界；技术等级、地域与阵营按剧本锁定",
            "style": (
                "科幻题材自适应制作风格；技术、装备、交通、建筑和界面遵守"
                "统一工程逻辑，人物造型由职业、阵营和生存环境推导"),
            "medium": "电影级科幻漫剧；写实度和材质表现服从世界观",
            "palette": ["技术体系主色", "阵营识别色", "环境风险色"],
            "forbidden": ["技术等级漂移", "无功能装饰", "古装元素乱入"],
        },
        {
            "key": "modern",
            "tokens": ("现代", "都市", "手机", "微信", "直播", "写字楼",
                       "外卖", "大学", "高中", "咖啡店", "地铁"),
            "era": "当代社会；城市、季节、职业和生活方式按剧本证据细化",
            "style": (
                "当代题材自适应制作风格；人物造型由年龄、职业、阶层、"
                "生活状态和关系阶段推导，空间与品牌信息符合现实语境"),
            "medium": "当代电影感精品漫剧；美型程度与剧情气质相匹配",
            "palette": ["现实环境本色", "人物关系色", "情绪转折重点色"],
            "forbidden": ["古装", "跨年代物件", "职业服装错误", "模板网红脸"],
        },
    )
    priority = {
        "ming_history": 0, "republican": 1, "xianxia": 2,
        "science_fiction": 3, "historical": 4, "modern": 5,
    }
    matches = [
        item for item in contexts if _has_any(text, item["tokens"])]
    selected = min(
        matches, key=lambda item: priority[item["key"]]) if matches else None
    if selected is None:
        selected = {
            "key": "story_adaptive",
            "era": "时代与地域尚待从剧本证据确认，不预设现代或古代",
            "style": (
                "剧本自适应制作风格；先分析时代、地域、题材、人物身份与"
                "阶层、环境材料、情绪和目标观众，再决定写实度、媒介、"
                "色彩、光线、服化道与镜头语言；不套用固定风格模板"),
            "medium": "媒介与写实度由剧本分析决定，不预设2D、3D或真人化",
            "palette": ["故事环境本色", "人物关系色", "剧情情绪重点色"],
            "forbidden": ["无文本依据的时代设定", "题材模板套用", "风格漂移"],
        }
    result = dict(selected)
    result["basis"] = [
        token for token in selected.get("tokens", ()) if token in text
    ][:12]
    return result


def _specific_era(value):
    value = _text(value)
    return bool(value) and not any(
        marker in value for marker in _GENERIC_ERA_MARKERS)


def _importance(role):
    role = _text(role)
    if "主角" in role:
        return "主角"
    if "重要" in role:
        return "重要配角"
    if any(token in role for token in ("路人", "群演", "背景")):
        return "背景路人"
    return "非重要配角"


def _candidate_count(importance):
    return {
        "主角": 5, "重要配角": 3, "非重要配角": 1, "背景路人": 0,
    }[importance]


VISUAL_DNA_DIMENSIONS = (
    "hair_silhouette", "clothing_structure", "body_or_occupation_marks",
    "story_visual_symbol", "signature_accessory", "temperament_keywords",
)


def _character_analysis(character, item, narrative):
    raw = _dict(item.get("character_analysis"))
    return {
        "identity_and_class": _text(
            raw.get("identity_and_class"),
            character.get("identity") or character.get("occupation")
            or character.get("role"),
        ),
        "age_and_presentation": _text(
            raw.get("age_and_presentation"), character.get("age_range")),
        "upbringing": _text(
            raw.get("upbringing"),
            character.get("upbringing") or character.get("backstory"),
        ),
        "family_background": _text(
            raw.get("family_background"), character.get("family_background"),
            ),
        "education_background": _text(
            raw.get("education_background"),
            character.get("education_background")),
        "current_situation": _text(
            raw.get("current_situation"),
            character.get("current_situation")
            or narrative.get("core_conflict")),
        "core_desire": _text(
            raw.get("core_desire"),
            character.get("motivation") or narrative.get("core_conflict")),
        "greatest_fear": _text(
            raw.get("greatest_fear"),
            character.get("greatest_fear") or "失去当前最重要的目标或关系"),
        "formative_experiences": _list(
            raw.get("formative_experiences")
            or character.get("formative_experiences")
            or character.get("backstory"),
            ["以剧本已声明的关键经历为准"]),
        "strengths": _list(
            raw.get("strengths") or character.get("strengths"),
            [_text(character.get("personality"), "目标明确")]),
        "flaws": _list(
            raw.get("flaws") or character.get("flaws"),
            ["由人物冲突与行为代价反推，不使用完美人设"]),
        "behavior_habits": _list(
            raw.get("behavior_habits") or character.get("behavior_habits"),
            [_text(character.get("signature_props"), "通过眼神、站姿与动作外化")]),
    }


def _visual_dna(character, item, visual, analysis):
    raw = _dict(item.get("visual_dna"))
    symbol_fallback = (
        character.get("signature")
        or character.get("signature_props")
        or "从关键经历或职业中提炼专属视觉符号")
    keywords = _list(
        raw.get("temperament_keywords")
        or character.get("temperament_keywords")
        or character.get("personality"),
        ["克制", "有行动目标", "非模板化"])
    for fallback in ("有行动目标", "经历可见", "非模板化"):
        if len(keywords) >= 3:
            break
        if fallback not in keywords:
            keywords.append(fallback)
    return {
        "face_structure": _text(
            raw.get("face_structure"),
            character.get("appearance")
            or "脸型与骨相必须由年龄、经历和生活状态推导"),
        "hair_silhouette": _text(
            raw.get("hair_silhouette"),
            character.get("hair")
            or "发型轮廓由职业、行为习惯和剧情时代推导"),
        "body_or_occupation_marks": _text(
            raw.get("body_or_occupation_marks"),
            character.get("body_or_occupation_marks")
            or character.get("occupation")
            or "体态和职业痕迹服从人物经历"),
        "clothing_structure": _text(
            raw.get("clothing_structure"),
            character.get("costume_direction")
            or visual.get("wardrobe_and_styling")),
        "clothing_wear_state": _text(
            raw.get("clothing_wear_state"),
            character.get("clothing_wear_state")
            or "新旧、磨损与整洁程度服从社会阶层和当前处境"),
        "story_visual_symbol": _text(
            raw.get("story_visual_symbol"), symbol_fallback),
        "story_visual_symbol_origin": _text(
            raw.get("story_visual_symbol_origin"),
            "必须能追溯到人物经历、职业、关系或当前冲突"),
        "signature_accessory": _text(
            raw.get("signature_accessory"),
            character.get("signature_props")
            or "仅保留一个有剧情来源的核心配饰或道具"),
        "temperament_keywords": keywords[:8],
        "genre_system_mapping": _dict(
            raw.get("genre_system_mapping")
            or character.get("genre_system_mapping")),
        "derivation": (
            "剧情证据 → 经历与处境 → 性格与行为 → 可见外貌 → 视觉 DNA"),
        "analysis_source": analysis,
    }


def _default_negative(style):
    baseline = [
        "字幕", "水印", "Logo", "乱码文字", "多余人物", "人物复制",
        "换脸", "性别漂移", "年龄漂移", "服装跨时代", "场景结构漂移",
        "低清晰度", "畸形手指", "错误肢体",
    ]
    if any(token in _text(style) for token in ("现代", "乙女", "3D", "半写实")):
        baseline.extend(("古装", "汉服", "发簪", "长袍", "水墨",
                         "2D平涂", "动漫线稿", "历史建筑"))
    return baseline


def build_story_analysis(script, style="", raw=None, source="ai"):
    """把 AI 输出、导入剧本和旧数据规范成稳定的制作圣经。"""
    script = _dict(script)
    raw = copy.deepcopy(_dict(raw))
    raw_world = _dict(raw.get("world"))
    raw_narrative = _dict(raw.get("narrative"))
    raw_visual = _dict(raw.get("visual"))
    raw_prompts = _dict(raw.get("prompt_bible"))
    story_world = _dict(script.get("story_world"))
    story_background = _dict(script.get("story_background"))
    inferred = infer_story_visual_context(script)
    requested_style = _text(style)
    raw_style = _text(raw_visual.get("user_style_constraint"))
    if raw_style.startswith(("未指定", "自动分析", "AI自动")):
        raw_style = ""
    locked_style = requested_style or raw_style or inferred["style"]
    raw_style_source = _text(raw_visual.get("style_source"))
    style_source = (
        "user_override"
        if requested_style and requested_style != raw_style
        else raw_style_source or (
            "user_override" if requested_style else "ai_inferred"))
    script_era = _text(story_world.get("era_and_location"))
    era = _text(
        raw_world.get("era_and_location")
        or (script_era if _specific_era(script_era) else "")
        or inferred["era"])
    hard_rules = _text(
        raw_world.get("hard_rules") or story_world.get("hard_rules"),
        "能力、技术、组织、物种、身份与人物关系只以剧本声明为准")
    forbidden = list(dict.fromkeys(_list(
        raw_visual.get("forbidden_visuals")
        or raw_world.get("forbidden_drift"),
        _default_negative(locked_style) + inferred["forbidden"])))
    narrative = {
        "logline": _text(
            raw_narrative.get("logline") or script.get("logline"),
            "依据本集剧本推进核心冲突"),
        "genre": _text(raw_narrative.get("genre"), _genre(script)),
        "themes": _list(
            raw_narrative.get("themes"), ["人物欲望", "关系变化", "冲突代价"]),
        "tone": _text(
            raw_narrative.get("tone"), "情绪清晰、节奏紧凑、角色先行"),
        "target_audience": _text(
            raw_narrative.get("target_audience"), "竖屏精品漫剧观众"),
        "emotional_arc": _text(
            raw_narrative.get("emotional_arc"),
            "铺垫 → 压力上升 → 情绪转折 → 余波与钩子"),
        "core_conflict": _text(
            raw_narrative.get("core_conflict")
            or story_background.get("core_conflict")
            or script.get("logline"),
            "本集人物目标发生正面冲突"),
        "continuity_hooks": _text(
            raw_narrative.get("continuity_hooks")
            or story_background.get("continuity_hooks"),
            "结尾保留人物状态变化和下一集可承接的事件钩子"),
    }
    world = {
        "name": _text(
            raw_world.get("name") or story_world.get("name"),
            f"《{_text(script.get('project_title'), '本剧')}》故事世界"),
        "overview": _text(
            raw_world.get("overview") or story_world.get("overview"),
            narrative["logline"]),
        "era_and_location": era,
        "geography_and_climate": _text(
            raw_world.get("geography_and_climate"),
            "地理、季节、天气和昼夜随剧本场次锁定，跨镜不得漂移"),
        "social_order": _text(
            raw_world.get("social_order") or story_world.get("social_order"),
            "社会阶层、组织阵营、职业权限和人物关系服从剧本事实"),
        "culture_and_lifestyle": _text(
            raw_world.get("culture_and_lifestyle"),
            "生活方式、礼仪、语言和空间使用符合时代地域与人物身份"),
        "technology_and_props": _text(
            raw_world.get("technology_and_props"),
            "技术等级、交通、通信和道具不得超出剧本世界边界"),
        "hard_rules": hard_rules,
        "recurring_motifs": _list(
            raw_world.get("recurring_motifs"), ["关键道具", "关系距离", "环境光变化"]),
        "forbidden_drift": _list(
            raw_world.get("forbidden_drift"), forbidden),
    }
    visual = {
        "user_style_constraint": locked_style,
        "style_source": style_source,
        "analysis_basis": _list(
            raw_visual.get("analysis_basis"), inferred["basis"]),
        "medium": _text(
            raw_visual.get("medium"), inferred["medium"]),
        "realism": _text(
            raw_visual.get("realism"), "人物结构自然，材质真实，适度美型"),
        "palette": _list(
            raw_visual.get("palette"), inferred["palette"]),
        "lighting": _text(
            raw_visual.get("lighting"),
            "主光方向固定，人物面部可读，光影参与情绪叙事"),
        "camera_language": _text(
            raw_visual.get("camera_language"),
            "景别有节奏变化；对话正反打；关键台词后给反应镜，高潮给留白镜"),
        "texture_and_render": _text(
            raw_visual.get("texture_and_render"),
            "前中后景层次清楚，自然材质，避免塑料感和过度锐化"),
        "architecture_and_environment": _text(
            raw_visual.get("architecture_and_environment"),
            f"建筑、室内、道路与陈设严格符合{era}"),
        "wardrobe_and_styling": _text(
            raw_visual.get("wardrobe_and_styling"),
            f"服装、发型、妆容按{era}、职业、性格和剧情场合设计"),
        "props_and_graphics": _text(
            raw_visual.get("props_and_graphics"),
            "道具承担叙事功能；可读文字只允许白名单内容并先在关键帧锁定"),
        "forbidden_visuals": forbidden,
    }

    raw_scene_map = {
        str(item.get("scene_no") or item.get("location")): item
        for item in raw.get("scenes", []) if isinstance(item, dict)
    }
    scenes = []
    for scene in script.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        item = _dict(
            raw_scene_map.get(str(scene.get("scene_no")))
            or raw_scene_map.get(str(scene.get("location"))))
        location = _text(scene.get("location"), "未命名场景")
        scenes.append({
            "scene_no": scene.get("scene_no"),
            "location": location,
            "story_function": _text(
                item.get("story_function") or scene.get("action"),
                "承载本场冲突与人物关系变化"),
            "environment": _text(
                item.get("environment"),
                f"{location}的空间功能、入口出口、前中后景和表演动线"),
            "layout": _text(
                item.get("layout"),
                "明确入口、出口、主体区、前景遮挡和人物可移动路线"),
            "materials_and_props": _text(
                item.get("materials_and_props"),
                "材质、家具、设备和剧情道具均服从世界技术等级"),
            "time_weather": _text(
                item.get("time_weather"), "按剧本锁定时段、季节和天气"),
            "lighting": _text(item.get("lighting"), visual["lighting"]),
            "sound": _text(
                item.get("sound"), "记录可持续的环境底噪与关键动作声"),
            "continuity_anchors": _list(
                item.get("continuity_anchors"),
                ["空间结构", "主光方向", "关键道具位置", "人物出入口"]),
            "prompt_prefix": _text(
                item.get("prompt_prefix"),
                f"{location}环境基准；{visual['architecture_and_environment']}；"
                f"{visual['lighting']}；前中后景层次清楚"),
        })

    raw_character_map = {
        _text(item.get("name")): item
        for item in raw.get("characters", []) if isinstance(item, dict)
    }
    formal_character_names = [
        _text(character.get("name"))
        for character in script.get("characters", [])
        if isinstance(character, dict)
        and character.get("name")
        and _importance(character.get("role")) != "背景路人"
    ]
    characters = []
    for character in script.get("characters", []):
        if not isinstance(character, dict) or not character.get("name"):
            continue
        name = _text(character.get("name"))
        item = _dict(raw_character_map.get(name))
        importance = _importance(character.get("role"))
        entry = {
            "name": name,
            "importance": importance,
            "candidate_count": _candidate_count(importance),
            "identity_facts": _text(
                item.get("identity_facts")
                or character.get("introduction")
                or character.get("identity"),
                f"{name}的身份、年龄、性别与人物关系以剧本人物表为准"),
            "visual_direction": _text(
                item.get("visual_direction")
                or character.get("costume_direction"),
                f"造型服从{era}、职业、性格和剧情阶段"),
            "continuity_anchors": _list(
                item.get("continuity_anchors"),
                ["脸部身份", "发型轮廓", "年龄与性别表达", "标志道具"]),
            "prompt_prefix": _text(
                item.get("prompt_prefix"),
                f"{name}，同一人物身份；{visual['wardrobe_and_styling']}"),
        }
        if importance != "背景路人":
            analysis = _character_analysis(character, item, narrative)
            entry["character_analysis"] = analysis
            entry["visual_dna"] = _visual_dna(
                character, item, visual, analysis)
            raw_dedup = _dict(item.get("cast_dedup"))
            entry["cast_dedup"] = {
                "compared_with": [
                    other for other in formal_character_names
                    if other and other != name
                ],
                "dimensions": list(VISUAL_DNA_DIMENSIONS),
                "overlap_threshold": 2,
                "status": _text(
                    raw_dedup.get("status"), "pending_design_audit"),
                "conflicts": (
                    raw_dedup.get("conflicts")
                    if isinstance(raw_dedup.get("conflicts"), list) else []),
                "redesign_if_overlap": True,
            }
        characters.append(entry)

    global_prefix = _text(
        raw_prompts.get("global_image_prefix"),
        f"{locked_style}；{visual['medium']}；{visual['texture_and_render']}；"
        f"世界边界：{era}；{hard_rules}")
    prompt_bible = {
        "global_image_prefix": global_prefix,
        "negative_prompt": _text(
            raw_prompts.get("negative_prompt"), "，".join(forbidden)),
        "character_prefix": _text(
            raw_prompts.get("character_prefix"),
            f"{global_prefix}；人物身份与最终立绘一致；"
            f"{visual['wardrobe_and_styling']}"),
        "scene_prefix": _text(
            raw_prompts.get("scene_prefix"),
            f"{global_prefix}；{visual['architecture_and_environment']}；"
            f"{visual['lighting']}"),
        "keyframe_prefix": _text(
            raw_prompts.get("keyframe_prefix"),
            f"{global_prefix}；准确人物数量；动作、视线、站位和情绪清晰；"
            "画面无字幕、无水印、无乱码"),
        "seedance_prefix": _text(
            raw_prompts.get("seedance_prefix"),
            "单段不超过15秒；每段单一主动作；人物数量、身份、服装、"
            "道具、空间、光线和起止状态连续；台词逐字保真；不要字幕"),
        "readable_text_policy": _text(
            raw_prompts.get("readable_text_policy"),
            "手机、合同、招牌等可读文字必须先由关键帧准确锁字；"
            "视频模型只保持，不从零生成"),
        "continuity_rules": _list(
            raw_prompts.get("continuity_rules"),
            [
                "同名实体全程同一身份",
                "段尾姿态、伤势、道具、情绪、朝向继承到下一段",
                "镜头只出现剧本声明的在场人物",
                "关键台词后保留听者反应镜，情绪高潮保留无台词表演镜",
            ]),
    }
    return {
        "schema": STORY_ANALYSIS_SCHEMA,
        "source": _text(raw.get("source"), source),
        "analyzed_at": _text(
            raw.get("analyzed_at"),
            datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "locked": bool(raw.get("locked", False)),
        "narrative": narrative,
        "world": world,
        "visual": visual,
        "scenes": scenes,
        "characters": characters,
        "prompt_bible": prompt_bible,
        "production_rules": {
            "dialogue_verbatim": True,
            "max_segment_seconds": 15,
            "no_burned_subtitles": True,
            "visible_text_requires_locked_keyframe": True,
            "on_stage_characters_only": True,
            "reaction_after_key_dialogue": True,
            "beat_at_emotional_peak": True,
            "character_candidate_targets": {
                "main": 5, "important_supporting": 3,
                "non_main": 1, "background": 0,
            },
            "character_design_sequence": [
                "story_evidence", "experience_and_situation",
                "personality_and_behavior", "visible_traits",
                "cast_dedup", "three_view_assets",
            ],
            "three_view_contract": {
                "after_human_lock": True,
                "review_board": "16:9 face close-up + Front/Profile/Back",
                "canonical_individual_assets": [
                    "face_closeup", "front", "profile", "back",
                ],
                "profile_degrees": 90,
                "back_degrees": 180,
                "seedance_may_redesign": False,
            },
        },
    }


def validate_story_analysis(analysis):
    """返回可直接展示的错误；通过时返回 ``None``。"""
    if not isinstance(analysis, dict):
        return "制作圣经不是 JSON 对象"
    if analysis.get("schema") != STORY_ANALYSIS_SCHEMA:
        return f"制作圣经 schema 必须为 {STORY_ANALYSIS_SCHEMA}"
    for section in ("narrative", "world", "visual", "prompt_bible"):
        if not isinstance(analysis.get(section), dict):
            return f"制作圣经缺少 {section}"
    for field in ("era_and_location", "hard_rules"):
        if not _text(analysis["world"].get(field)):
            return f"制作圣经世界设定缺少 {field}"
    for field in (
            "user_style_constraint", "lighting", "camera_language",
            "architecture_and_environment", "wardrobe_and_styling"):
        if not _text(analysis["visual"].get(field)):
            return f"制作圣经视觉设定缺少 {field}"
    for field in (
            "global_image_prefix", "negative_prompt", "character_prefix",
            "scene_prefix", "keyframe_prefix", "seedance_prefix"):
        if not _text(analysis["prompt_bible"].get(field)):
            return f"制作圣经提示词母版缺少 {field}"
    if not isinstance(analysis.get("characters"), list):
        return "制作圣经缺少 characters"
    for character in analysis["characters"]:
        if not isinstance(character, dict):
            return "制作圣经角色分析必须是对象"
        if character.get("importance") == "背景路人":
            continue
        for field in ("character_analysis", "visual_dna", "cast_dedup"):
            if not isinstance(character.get(field), dict):
                return f"角色 {character.get('name')} 缺少 {field}"
        keywords = character["visual_dna"].get("temperament_keywords")
        if not isinstance(keywords, list) or not 3 <= len(keywords) <= 8:
            return (
                f"角色 {character.get('name')} 的视觉 DNA "
                "必须包含 3-8 个气质关键词")
    return None


def apply_story_analysis(script, analysis):
    """把制作圣经注入剧本，供现有下游 Provider 无缝继承。"""
    if not isinstance(script, dict) or not isinstance(analysis, dict):
        return script
    # 旧剧集可能保存过不完整的角色分析(例如只有 visual_direction)，
    # 不能因为详情页/分镜列表读取这些历史数据而直接抛出 KeyError。
    # 先按当前 schema 补齐缺失字段，再注入下游，保证历史项目可继续检查和干预。
    analysis = build_story_analysis(
        script,
        style=((analysis.get("visual") or {}).get("user_style_constraint", "")
               if isinstance(analysis.get("visual"), dict) else ""),
        raw=analysis,
        source=analysis.get("source", "legacy"),
    )
    script["production_analysis"] = copy.deepcopy(analysis)
    world = analysis["world"]
    visual = analysis["visual"]
    story_world = script.setdefault("story_world", {})
    story_world.update({
        "name": world["name"],
        "overview": world["overview"],
        "era_and_location": world["era_and_location"],
        "social_order": world["social_order"],
        "hard_rules": world["hard_rules"],
        "visual_baseline": (
            f"{visual['user_style_constraint']}；{visual['medium']}；"
            f"{visual['palette']}；{visual['lighting']}；"
            f"{visual['texture_and_render']}"),
        "forbidden_drift": list(dict.fromkeys(
            world["forbidden_drift"] + visual["forbidden_visuals"])),
    })
    prompt_bible = analysis["prompt_bible"]
    scene_map = {
        (item.get("scene_no"), item.get("location")): item
        for item in analysis.get("scenes", [])
    }
    for scene in script.get("scenes", []):
        item = scene_map.get((scene.get("scene_no"), scene.get("location")))
        if item is None:
            item = next((
                candidate for candidate in analysis.get("scenes", [])
                if candidate.get("location") == scene.get("location")), None)
        if not item:
            continue
        scene["production_design"] = copy.deepcopy(item)
        scene["prompt_prefix"] = (
            f"{prompt_bible['scene_prefix']}；{item['prompt_prefix']}")
        scene["negative_prompt"] = prompt_bible["negative_prompt"]
        scene.setdefault("time_weather", item["time_weather"])
        scene.setdefault("lighting", item["lighting"])
    character_map = {
        item.get("name"): item for item in analysis.get("characters", [])
    }
    for character in script.get("characters", []):
        item = character_map.get(character.get("name"))
        if not item:
            continue
        character["visual_direction"] = item["visual_direction"]
        character["prompt_prefix"] = (
            f"{prompt_bible['character_prefix']}；{item['prompt_prefix']}")
        character["continuity_anchors"] = item["continuity_anchors"]
        character["candidate_count"] = item["candidate_count"]
        if item.get("importance") != "背景路人":
            character["character_analysis"] = copy.deepcopy(
                item.get("character_analysis") or {})
            character["visual_dna"] = copy.deepcopy(
                item.get("visual_dna") or {})
            character["cast_dedup"] = copy.deepcopy(
                item.get("cast_dedup") or {})
    return script
