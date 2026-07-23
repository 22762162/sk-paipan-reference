"""剧本 AI 分析与制作圣经。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone


STORY_ANALYSIS_SCHEMA = "aifos.story-analysis/v1"


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
    locked_style = _text(
        style or raw_visual.get("user_style_constraint"),
        "剧情自适应、电影级半写实精品漫剧")
    era = _text(
        raw_world.get("era_and_location")
        or story_world.get("era_and_location"),
        "时代与地域以剧本明示为准，未明示部分禁止擅自猜测")
    hard_rules = _text(
        raw_world.get("hard_rules") or story_world.get("hard_rules"),
        "能力、技术、组织、物种、身份与人物关系只以剧本声明为准")
    forbidden = _list(
        raw_visual.get("forbidden_visuals")
        or raw_world.get("forbidden_drift"),
        _default_negative(locked_style))
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
        "medium": _text(
            raw_visual.get("medium"), "电影级半写实漫剧画面"),
        "realism": _text(
            raw_visual.get("realism"), "人物结构自然，材质真实，适度美型"),
        "palette": _list(
            raw_visual.get("palette"), ["主色随剧情空间", "肤色自然", "重点色克制跳出"]),
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
