from aifos.prompt_contract import (
    PROMPT_CONTRACT_SCHEMA,
    build_composition_contract,
    build_physical_contract,
    compile_shot_prompt,
    readable_text_required,
    sanitize_text_whitelist,
    shot_local_scene,
    validate_shot_prompt_contract,
)
from aifos.adapters.codex_image import build_instruction
from aifos.workflow import _text_asset, build_content_review


def _shot():
    return {
        "shot_no": 3,
        "characters": ["林晚", "白芷"],
        "character_number_map": {
            "P01": {"actor_id": "P01", "name": "林晚"},
            "P02": {"actor_id": "P02", "name": "白芷"},
        },
        "description": "林晚把手机转向白芷，白芷抬眼看向屏幕",
        "start_state": {
            "林晚": {"position": "左侧", "pose": "举手机", "direction": "右"},
        },
        "end_state": {
            "林晚": {"position": "左侧", "pose": "手机停在两人之间", "direction": "右"},
        },
        "five_dimensions": {
            "camera_design": {
                "shot_scale": "中景",
                "angle": "平视",
                "movement": "缓推",
                "movement_motivation": "跟随手机转向",
                "composition": "两人和手机同框",
            },
        },
        "dialogue": {"character": "林晚", "dialogue": "你看这里。"},
    }


def test_compact_contract_is_ordered_and_single_purpose():
    contract, prompt = compile_shot_prompt(
        _shot(), location="直播办公室", style="舞台偶像漫剧",
        references=[
            {"index": 1, "label": "首帧", "kind": "first_frame"},
            {"index": 3, "label": "林晚最终立绘", "kind": "identity"},
        ], mode="video")

    assert contract["schema"] == PROMPT_CONTRACT_SCHEMA
    assert prompt.index("【主体】") < prompt.index("【场景】")
    assert prompt.index("【场景】") < prompt.index("【单一主动作】")
    assert prompt.index("【单一主动作】") < prompt.index("【镜头】")
    assert "图1是唯一动作起点" in prompt
    assert "严格共2人" in prompt
    assert "只执行一个主动作和一个运镜" in prompt
    assert "图3=林晚最终立绘(身份：只锁脸、发型、年龄、性别)" in prompt
    assert "你看这里。" in prompt


def test_compact_contract_locks_per_actor_wardrobe_and_blocks_missing_state():
    shot = {
        "shot_no": 8,
        "characters": ["沈砚"],
        "character_number_map": {
            "P01": {"actor_id": "P01", "name": "沈砚"},
        },
        "description": "沈砚微微颔首",
        "appearance_state_required": True,
        "start_state": {
            "沈砚": {
                "position": "县衙门前", "pose": "立定",
                "direction": "面向县丞", "wardrobe": "青官袍、乌角革带",
                "headwear": "乌纱帽", "hair_makeup": "束发、素颜",
            },
        },
        "end_state": {
            "沈砚": {
                "position": "县衙门前", "pose": "微颔首",
                "direction": "面向县丞", "wardrobe": "青官袍、乌角革带",
                "headwear": "乌纱帽", "hair_makeup": "束发、素颜",
            },
        },
    }
    contract, prompt = compile_shot_prompt(shot, location="清河县衙门前")
    assert validate_shot_prompt_contract(contract)["passed"]
    assert "服装青官袍、乌角革带" in prompt
    assert "头饰乌纱帽" in prompt
    assert "未写换装/摘戴/改妆动作时不得自行改变" in prompt

    missing = dict(shot)
    missing["start_state"] = {"沈砚": {"pose": "立定"}}
    missing["end_state"] = {"沈砚": {"pose": "立定"}}
    broken, _ = compile_shot_prompt(missing)
    verdict = validate_shot_prompt_contract(broken)
    assert verdict["passed"] is False
    assert "沈砚缺少当前镜头唯一服装状态" in verdict["issues"]


def test_empty_scene_is_explicitly_an_empty_shot():
    shot = _shot()
    shot.update({"characters": [], "character_number_map": {}, "description": "雨水落在窗沿"})
    _, prompt = compile_shot_prompt(shot, location="夜间窗边")
    assert "严格共0人：无人" in prompt
    assert "雨水落在窗沿" in prompt


def test_codex_image_bridge_sends_compiled_prompt_not_audit_long_form(tmp_path):
    instruction, _, _ = build_instruction(
        "image",
        {
            "shot_no": 1,
            "prompt": "审计原文：包含大量制作圣经与背景",
            "prompt_compact": "【镜头合同v1】动作：林晚抬头",
            "characters": ["林晚"],
            "character_count": 1,
            "aspect": "9:16",
        },
        tmp_path,
    )
    assert "【镜头合同v1】动作：林晚抬头" in instruction
    assert "审计原文：包含大量制作圣经与背景" not in instruction


def test_over_shoulder_tracks_front_and_back_actor_without_double_counting():
    shot = {
        "shot_no": 4,
        "characters": ["朱慈烺", "李继周"],
        "character_number_map": {
            "P01": {"actor_id": "P01", "name": "朱慈烺"},
            "P02": {"actor_id": "P02", "name": "李继周"},
        },
        "description": "李继周以半个背影作过肩前景，朱慈烺正面对他说话",
        "camera": "中近景过肩机位",
        "dialogue": {"character": "朱慈烺", "dialogue": "照此办理。"},
        "character_visuals": {
            "朱慈烺": "白皙清瘦少年、明代交领中衣",
            "李继周": "束发、深色圆领袍、肩背略宽、手持拂尘",
        },
    }
    composition = build_composition_contract(shot)
    by_name = {
        actor["character"]: actor for actor in composition["actors"]}

    assert composition["composition_type"] == "over_shoulder_dialogue"
    assert composition["expected_primary_count"] == 1
    assert composition["expected_visible_figure_count"] == 2
    assert by_name["朱慈烺"]["identity_basis"] == "face"
    assert by_name["李继周"]["identity_basis"] == "back_silhouette"
    assert by_name["李继周"]["coverage"] == "partial"

    _, prompt = compile_shot_prompt(shot, location="东宫寝殿")
    assert "实际可见人形2人" in prompt
    assert "不得另算成新增人物或人物复制" in prompt
    assert "束发、深色圆领袍、肩背略宽、手持拂尘" in prompt


def test_negative_subtitle_instruction_is_not_a_readable_text_asset():
    detected = _text_asset({
        "description": "朱慈烺转身，无字幕、无Logo、无水印",
        "prompt": "禁止对白字幕",
    })
    assert detected["required"] is False
    assert readable_text_required({
        "required": True, "carrier": "字幕", "whitelist": [],
    }) is False

    instruction, _, _ = build_instruction(
        "image",
        {
            "shot_no": 1,
            "prompt_compact": "朱慈烺转身",
            "characters": ["朱慈烺"],
            "character_count": 1,
            "readable_text": {
                "required": True, "carrier": "字幕", "whitelist": [],
            },
        },
        "/tmp",
    )
    assert "白名单为空" not in instruction
    assert "画面中不要生成字幕条" in instruction

    review = build_content_review(
        {},
        {"shots": [{
            "unit_id": "U01",
            "script_reference": "scene:1",
            "characters": ["朱慈烺"],
            "readable_text": {
                "required": True, "carrier": "字幕", "whitelist": [],
            },
        }]},
        {
            "characters": [{"name": "朱慈烺"}],
            "scenes": [{"location": "东宫"}],
        },
    )
    assert review["passed"] is True
    assert review["units"][0]["text_accuracy"] is None


def test_laptop_page_is_a_hard_readable_asset_not_a_blank_light():
    shot = _shot()
    shot.update({
        "characters": ["林晚"],
        "readable_text": {
            "required": True, "carrier": "电脑",
            "whitelist": ["明季北略", "崇祯"],
        },
        "description": "林晚盯着银色笔记本电脑屏幕上的《明季北略》崇祯页面",
    })
    _, prompt = compile_shot_prompt(shot, location="现代书房")
    assert "电脑屏幕必须打开并清晰显示白名单原文:明季北略、崇祯" in prompt
    assert "屏幕不是冷白光效/空白占位面" in prompt

    instruction, _, _ = build_instruction(
        "image",
        {
            "shot_no": 1,
            "prompt_compact": prompt,
            "characters": ["林晚"],
            "character_count": 1,
            "readable_text": {
                "required": True, "carrier": "电脑",
                "whitelist": ["明季北略", "崇祯"],
            },
        },
        "/tmp",
    )
    assert "禁止空白" in instruction
    assert "明季北略、崇祯" in instruction


def test_laptop_contract_states_user_screen_and_camera_side():
    shot = _shot()
    shot.update({
        "characters": ["林晚"],
        "description": "林晚阅读银色笔记本电脑屏幕上的《明季北略》崇祯页面",
        "readable_text": {
            "required": True, "carrier": "电脑",
            "whitelist": ["明季北略", "崇祯"],
        },
    })
    physical = build_physical_contract(shot)
    assert physical["schema"] == "aifos.physical-space/v1"
    assert any("屏幕正面" in rule and "使用者" in rule
               for rule in physical["rules"])
    _, prompt = compile_shot_prompt(shot, location="现代书房")
    assert "【物理/空间逻辑】" in prompt
    assert "禁止人物坐在屏幕背面却看到屏幕正面" in prompt


def test_text_asset_card_compiles_layout_style_and_perspective():
    shot = _shot()
    shot.update({
        "characters": [],
        "description": "空镜，门牌需要清晰可读",
        "readable_text": {
            "required": True,
            "carrier": "门牌",
            "whitelist": ["东宫书房"],
            "layout": "门框右上，单行居中",
            "style": "明代匾额书法，墨黑字，暗金底",
            "perspective": "随门框透视，边缘轻微反光",
            "priority": "must_read",
        },
    })
    _, prompt = compile_shot_prompt(shot, location="明代东宫")
    assert "东宫书房" in prompt
    assert "版式/位置:门框右上，单行居中" in prompt
    assert "字体/颜色/层级:明代匾额书法，墨黑字，暗金底" in prompt
    assert "透视/反光:随门框透视，边缘轻微反光" in prompt


def test_text_asset_style_never_overwrites_project_visual_style():
    shot = _shot()
    shot.update({
        "characters": [],
        "description": "铜鱼符特写，符面文字清晰可读",
        "readable_text": {
            "required": True,
            "carrier": "铜鱼符",
            "whitelist": ["清河"],
            "style": "青铜錾刻阴文，边缘带包浆",
        },
    })
    contract, prompt = compile_shot_prompt(
        shot,
        location="明代驿站",
        style="电影级半写实精品漫剧，明末历史正剧质感",
    )
    assert contract["style"] == "电影级半写实精品漫剧，明末历史正剧质感"
    assert "【画风】电影级半写实精品漫剧，明末历史正剧质感" in prompt
    assert "字体/颜色/层级:青铜錾刻阴文，边缘带包浆" in prompt
    assert "【画风】青铜錾刻阴文，边缘带包浆" not in prompt


def test_system_prompt_fields_never_become_screen_whitelist():
    assert sanitize_text_whitelist([
        "东宫书房", "镜头合同v2", "质检原因:屏幕乱码", "主体",
    ]) == ["东宫书房"]


def test_legacy_shot_uses_local_modern_scene_before_episode_fallback():
    shot = {"description": "现代书房闪回，青年查看银色笔记本电脑"}
    assert shot_local_scene(shot, "明代东宫寝殿") == "现代书房（闪回）"


def test_explicit_camera_and_single_subject_over_shoulder_are_authoritative():
    shot = {
        "characters": ["朱慈烺"],
        "description": "朱慈烺背对镜头，肩后望向案上奏疏",
        "camera": "低机位近景单人过肩固定镜头",
        "five_dimensions": {"camera_design": {
            "shot_scale": "全景", "angle": "平视",
            "camera_position": "正面", "movement": "环绕",
        }},
    }
    contract, image_prompt = compile_shot_prompt(shot, location="东宫")
    composition = contract["composition"]

    assert contract["camera"]["景别"] == "近景"
    assert contract["camera"]["角度"] == "仰拍"
    assert contract["camera"]["机位"] == "过肩"
    assert contract["camera"]["运镜"] == "固定"
    assert composition["composition_type"] == "single_subject_over_shoulder"
    assert composition["expected_visible_figure_count"] == 1
    assert "严格只有1名人物、1具连续身体" in image_prompt
    assert "环绕" not in image_prompt
    assert validate_shot_prompt_contract(contract)["passed"]


def test_prompt_contract_blocks_standing_state_for_lying_action():
    shot = {
        "characters": ["朱慈烺"],
        "description": "朱慈烺仰卧于床榻",
        "start_state": {"朱慈烺": {"pose": "站立"}},
        "end_state": {"朱慈烺": {"pose": "站立"}},
    }
    contract, _ = compile_shot_prompt(shot, location="寝殿")
    report = validate_shot_prompt_contract(contract)
    assert not report["passed"]
    assert any("仰卧" in issue for issue in report["issues"])


def _wake_shot(same_wardrobe=True):
    wardrobe_end = ("青色低阶文官圆领官袍" if same_wardrobe else "赭色布衣")
    return {
        "shot_no": 7, "characters": ["林川"],
        "start_state": {"林川": {
            "position": "床榻", "pose": "平躺", "direction": "面部朝上",
            "wardrobe": "青色低阶文官圆领官袍", "headwear": "网巾",
            "hair_makeup": "黑色短碎发", "prop": "枕边文书",
            "emotion": "昏迷转醒"}},
        "end_state": {"林川": {
            "position": "床榻", "pose": "坐起", "direction": "低头看官袍",
            "wardrobe": wardrobe_end, "headwear": "网巾",
            "hair_makeup": "黑色短碎发", "prop": "枕边文书",
            "emotion": "错愕惊疑"}},
        "description": "林川猛然惊醒坐起", "camera": "近景 俯拍",
    }


def test_end_state_dedups_unchanged_appearance_to_single_source():
    """同一套着装不再在起点/终点各写一遍全量——重复即漂移面。"""
    from aifos.prompt_contract import (
        build_shot_prompt_contract, render_shot_prompt)

    contract = build_shot_prompt_contract(_wake_shot(), location="驿舍")
    assert "服装、头饰、妆发同起点不变" in contract["end"]
    assert "青色低阶文官圆领官袍" not in contract["end"]
    # 真实换装必须仍逐项写明,交由连续性检查核对换装动作
    changed = build_shot_prompt_contract(
        _wake_shot(same_wardrobe=False), location="驿舍")
    assert "赭色布衣" in changed["end"]
    assert "同起点不变" not in changed["end"]
    # 情绪/道具/姿势不参与收敛
    rendered = render_shot_prompt(contract, mode="image")
    assert "情绪错愕惊疑" in rendered


def test_static_keyframe_demotes_start_and_promotes_end():
    """静态关键帧只画一个瞬间:终点是唯一入画状态,起点降为上下文。

    回归背景:起终点等权同发时"画哪一刻"有歧义,质检只能事后用
    "只定格终点"补课。视频与首/尾单帧仍保持两端等权。"""
    from aifos.prompt_contract import (
        build_shot_prompt_contract, render_shot_prompt)

    contract = build_shot_prompt_contract(_wake_shot(), location="驿舍")
    image = render_shot_prompt(contract, mode="image")
    assert "【起点·仅上下文，不入画】" in image
    assert "【终点·唯一入画状态】画面只呈现此刻：" in image
    video = render_shot_prompt(contract, mode="video")
    assert "【起点】" in video and "【起点·" not in video
    assert "【终点】" in video and "【终点·" not in video
    frame = dict(_wake_shot(), frame_kind="last_frame")
    frame_prompt = render_shot_prompt(
        build_shot_prompt_contract(frame, location="驿舍"), mode="image")
    assert "【单帧修改】" in frame_prompt and "【起点·" not in frame_prompt
