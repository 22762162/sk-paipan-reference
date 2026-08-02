from aifos.prompt_contract import (
    PROMPT_CONTRACT_SCHEMA,
    REFERENCE_SCOPE_DEFAULTS,
    build_composition_contract,
    build_model_constraints,
    build_physical_contract,
    compile_shot_prompt,
    readable_text_required,
    sanitize_text_whitelist,
    shot_local_scene,
    synchronize_shot_execution_contract,
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
        "frame_target_policy": "legacy",
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

    assert PROMPT_CONTRACT_SCHEMA == "aifos.shot-prompt/v2.2"
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
        "frame_target_policy": "legacy",
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
    assert "头饰presence=worn,kind=official_hat,name=乌纱帽" in prompt
    assert "未写换装/摘戴/改妆动作时不得自行改变" in prompt

    missing = dict(shot)
    missing["start_state"] = {"沈砚": {"pose": "立定"}}
    missing["end_state"] = {"沈砚": {"pose": "立定"}}
    broken, _ = compile_shot_prompt(missing)
    verdict = validate_shot_prompt_contract(broken)
    assert verdict["passed"] is False
    assert "沈砚缺少当前镜头唯一服装状态" in verdict["issues"]


def test_headwear_visual_contract_survives_normalization_and_rendering():
    shot = {
        "shot_no": 9,
        "characters": ["顾明昭"],
        "character_number_map": {
            "P01": {"actor_id": "P01", "name": "顾明昭"},
        },
        "description": "顾明昭端坐审视官凭",
        "frame_target_policy": "legacy",
        "appearance_state_required": True,
    }
    state = {
        "position": "书案北侧", "pose": "端坐",
        "direction": "朝南", "wardrobe": "沉香褐窄袖比甲",
        "headwear": {
            "presence": "worn", "kind": "hair_ornament",
            "name": "横向断纹旧银簪",
            "shape": "横穿发髻的长直簪杆",
            "material": "氧化旧银",
            "color": "暗旧银色",
            "placement": "横向穿过中高发髻",
            "signature_details": "右端大号镂刻叶形端头、左端小尖饰",
            "forbidden_variants": ["短发夹", "垂坠金钗"],
        },
        "hair_visibility": "fully_visible",
        "hair_makeup": "中高扁圆髻",
    }
    shot["start_state"] = {"顾明昭": state}
    shot["end_state"] = {"顾明昭": state}

    contract, prompt = compile_shot_prompt(
        shot, location="县衙验牒书房", style="写实古风", mode="image")

    headwear = contract["start_appearance"]["顾明昭"]["headwear"]
    assert headwear["shape"] == "横穿发髻的长直簪杆"
    assert headwear["forbidden_variants"] == ["短发夹", "垂坠金钗"]
    assert "shape=横穿发髻的长直簪杆" in prompt
    assert "signature_details=右端大号镂刻叶形端头、左端小尖饰" in prompt
    assert "forbidden_variants=短发夹、垂坠金钗" in prompt
    assert "未写换装/摘戴/改妆动作时不得自行改变" in prompt


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


def test_legacy_text_carrier_without_exact_whitelist_is_not_locked_asset():
    detected = _text_asset({
        "description": "人物望向电脑屏幕，页面内容不可辨认",
        "prompt": "近景拍摄显示器",
    })

    assert detected["carrier"]
    assert detected["whitelist"] == []
    assert detected["required"] is False
    assert detected["locked_by"] == ""
    assert "禁止自行生成文字" in detected["rule"]


def test_identity_reference_scope_includes_stable_makeup():
    identity = REFERENCE_SCOPE_DEFAULTS["identity"]

    assert {"face", "hairstyle", "makeup", "stable_makeup"} <= set(
        identity["inherits"])

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


def test_moving_carriage_requires_horse_harness_driver_and_force_chain():
    physical = build_physical_contract({
        "description": "车夫赶着马车疾驰入城，车轮滚动扬起薄尘",
    })

    rendered = "；".join(physical["rules"] + physical["objects"])
    assert "只画车厢不画马" in rendered
    assert "马匹↔挽具/辕杆↔车体↔车夫缰绳" in rendered


def test_stationary_or_interior_carriage_does_not_invent_visible_horse():
    physical = build_physical_contract({
        "description": "停放的马车车厢内，人物掀开车帘低声交谈",
    })

    rendered = "；".join(physical["rules"] + physical["objects"])
    assert "移动马车完整动力链" not in rendered
    assert "马匹↔挽具" not in rendered


def test_negative_device_list_does_not_reinject_laptop_contract():
    physical = build_physical_contract({
        "description": (
            "古代书房内不得出现笔记本电脑、屏幕、键盘或任何现代设备；"
            "禁止人物坐在屏幕背面；这不表示画面中存在电脑。"),
        "action": "两人共同注视折签末端与左袖硬折的接触点",
    })

    assert not any("电脑使用关系" in rule for rule in physical["rules"])
    assert not any("笔记本电脑" in item for item in physical["objects"])


def test_screen_left_and_right_are_staging_not_display_devices():
    physical = build_physical_contract({
        "description": (
            "沈砚舟站在屏幕左前，顾明昭端坐屏幕右后，"
            "两人共同注视案上铜符。"),
    })

    assert not any("电脑使用关系" in rule for rule in physical["rules"])
    assert not any("笔记本电脑" in item for item in physical["objects"])


def test_dialogue_axis_defers_to_explicit_non_eye_gaze_target():
    physical = build_physical_contract({
        "description": "两人身体相向，共同注视案上铜符的缺角",
        "spatial_blocking": {
            "dialogue_continuity": {
                "axis_id": "S01-P01-P02-A01",
                "screen_left_name": "沈砚舟",
                "screen_right_name": "顾明昭",
                "camera_side": "positive",
                "coverage": "双人建立镜头",
            },
        },
    })
    axis_rule = next(
        rule for rule in physical["rules"]
        if "双人对话180°轴线合同" in rule)

    assert "本镜明确的动作目标" in axis_rule
    assert "不得强制改成互看双眼" in axis_rule
    assert "视线精确落在对方双眼" not in axis_rule


def test_dialogue_axis_keeps_eye_contact_when_shot_requests_it():
    physical = build_physical_contract({
        "description": "两人停住动作，隔案对视",
        "spatial_blocking": {
            "dialogue_continuity": {
                "axis_id": "S01-P01-P02-A01",
                "screen_left_name": "甲",
                "screen_right_name": "乙",
            },
        },
    })
    axis_rule = next(
        rule for rule in physical["rules"]
        if "双人对话180°轴线合同" in rule)

    assert "视线精确落在对方双眼附近" in axis_rule


def test_latest_screen_sides_override_stale_blocking_coordinates():
    physical = build_physical_contract({
        "characters": ["顾明昭", "沈砚舟"],
        "description": (
            "沈砚舟在屏幕左前站立，顾明昭在屏幕右后端坐，"
            "两人共同注视案上折签接触点。"),
        "spatial_blocking": {
            "actors": [
                {"name": "顾明昭", "start": {"x": 300, "y": 330},
                 "facing": "面向沈砚舟"},
                {"name": "沈砚舟", "start": {"x": 700, "y": 420},
                 "facing": "面向顾明昭"},
            ],
            "dialogue_continuity": {
                "axis_id": "S02-P02-P03-A01",
                "screen_left_name": "顾明昭",
                "screen_right_name": "沈砚舟",
                "camera_side": "negative",
            },
        },
    })
    axis_rule = next(
        rule for rule in physical["rules"]
        if "双人对话180°轴线合同" in rule)

    assert "沈砚舟固定成片屏幕左锚点" in axis_rule
    assert "顾明昭固定成片屏幕右锚点" in axis_rule
    assert not any("{'x':" in rule for rule in physical["rules"])


def test_compact_spatial_staging_scrubs_stale_projection_after_shot_repair():
    shot = {
        "shot_no": 4,
        "characters": ["顾明昭", "沈砚舟"],
        "description": (
            "唯一屏幕方向锁定为：沈砚舟固定在屏幕左前，"
            "顾明昭固定在屏幕右后，两人注视折签接触点。"),
        "camera": "9:16平视双人中景，从沈砚舟右后肩取景",
        "frame_target": {"phase": "end", "state": "折签轻压左袖"},
        "start_state": {},
        "end_state": {},
        "spatial_blocking": {
            "actors": [
                {"name": "顾明昭", "start_3d": {"x": -2.44, "y": 0,
                                                   "z": -0.3},
                 "end_3d": {"x": -2.44, "y": 0, "z": -0.3},
                 "height_m": 1.22, "facing": "面向沈砚舟"},
                {"name": "沈砚舟", "start_3d": {"x": 2.44, "y": 0,
                                                   "z": 1.04},
                 "end_3d": {"x": 2.44, "y": 0, "z": 1.04},
                 "height_m": 1.68, "facing": "面向顾明昭"},
            ],
            "camera": {
                "start_3d": {"x": 1.02, "y": 1.43, "z": -2.23},
                "end_3d": {"x": -1.95, "y": 1.43, "z": -1.39},
                "target_start_3d": {"x": 0, "y": 1.12, "z": 0.37},
                "target_end_3d": {"x": 0, "y": 1.12, "z": 0.37},
                "target_3d": {"x": 0, "y": 1.12, "z": 0.37},
                "fov_degrees": 23.9,
                "movement": "环绕",
            },
            "dialogue_continuity": {
                "axis_id": "S02-P02-P03-A01",
                "screen_left_name": "顾明昭",
                "screen_right_name": "沈砚舟",
            },
        },
    }

    contract, prompt = compile_shot_prompt(
        shot, location="临江县衙验牒书房", mode="image")
    staging = contract["spatial_staging"]
    assert "空间裁决" in staging
    assert "沈砚舟固定成片屏幕左锚点" in staging["空间裁决"]
    assert "顾明昭固定成片屏幕右锚点" in staging["空间裁决"]
    assert "顾明昭在画面左侧" not in " ".join(staging.values())
    assert "沈砚舟在画面右侧" not in " ".join(staging.values())
    assert "【空间裁决】" in prompt


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


def test_authoritative_script_scene_wins_over_legacy_prompt_guess():
    shot = {"description": "现代书房闪回，青年查看银色笔记本电脑"}
    assert shot_local_scene(shot, "明代东宫寝殿") == "明代东宫寝殿"


def test_legacy_shot_without_structured_scene_still_uses_positive_hint():
    shot = {"description": "现代书房闪回，青年查看银色笔记本电脑"}
    assert shot_local_scene(shot) == "现代书房（闪回）"


def test_modern_hotel_exclusion_does_not_infer_ming_palace():
    shot = {
        "description": (
            "2078年现代高档酒店房间门口和走廊，"
            "不得出现明代宫殿、宫灯、烛火或其他古代元素"
        ),
    }
    assert shot_local_scene(shot) == "现代酒店"


def test_negative_scene_hints_do_not_create_a_scene_without_positive_fact():
    assert shot_local_scene({
        "prompt": "禁止出现明代宫殿、宫灯；无紫禁城元素",
    }) == "按场景基准图"


def test_positive_historical_scene_hint_remains_compatible():
    assert shot_local_scene({
        "description": "人物进入明代宫殿，停在正殿门内",
    }) == "明代宫殿内景"


def test_unrelated_no_text_rule_does_not_hide_later_scene_fact():
    assert shot_local_scene({
        "prompt": "无字幕、Logo、水印，画面是现代酒店走廊",
    }) == "现代酒店"


def test_modern_scene_filters_ancient_project_style_from_provider_prompt():
    shot = {
        "characters": [],
        "description": (
            "2078年现代高档酒店房间门口和走廊。"
            "不得出现明代宫殿、宫灯、烛火、明代烛台、书案、香炉、纱幕"
        ),
        "frame_target_policy": "legacy",
        "camera": "35mm俯拍斜侧固定中景",
    }
    style = (
        "鎏金柔雾写实古风；宫斗权谋；"
        "以殿内烛火、宫灯为光源动机，暖光与幽深暗部并置；"
        "明代烛台、书案、香炉、纱幕，电影级半写实3D材质"
    )

    contract, prompt = compile_shot_prompt(
        shot, location="2078年现代高档酒店走廊",
        style=style, mode="image")

    assert contract["scene"] == "2078年现代高档酒店走廊"
    assert "鎏金柔雾写实" in contract["style"]
    assert "暖光与幽深暗部并置" in contract["style"]
    assert "电影级半写实3D材质" in contract["style"]
    assert contract["era_object_constraints"] == []
    for forbidden in (
            "明代宫殿", "宫斗", "权谋", "殿内烛火", "宫灯",
            "明代烛台", "书案", "香炉", "纱幕"):
        assert forbidden not in contract["style"]
        assert forbidden not in contract["lighting"]
        assert forbidden not in prompt


def test_modern_scene_drops_whole_ancient_furnishing_clause_without_fragments():
    shot = {
        "characters": [],
        "description": "2078年现代高档酒店走廊，房门外可见远处电梯",
        "frame_target_policy": "legacy",
        "camera": "35mm俯拍斜侧固定中景",
    }
    style = (
        "电影级半写实3D真人漫剧；"
        "鎏金暖棕与冷灰蓝色板；"
        "柔和侧逆光、冷灰环境光；"
        "暖金中以古室、书案、香炉、纱幕、卷册构成场景陈设"
    )

    contract, prompt = compile_shot_prompt(
        shot, location="2078年现代高档酒店走廊",
        style=style, mode="image")

    # 媒介、色板和布光仍属于项目画风，可以安全继承到现代镜头。
    for retained in (
            "电影级半写实3D真人漫剧", "鎏金暖棕与冷灰蓝色板",
            "柔和侧逆光", "冷灰环境光"):
        assert retained in contract["style"]
        assert retained in prompt

    # 古代场景陈设是一整条地点设计，不可逐词删除后把残句送给 provider。
    for leaked in (
            "古室", "书案", "香炉", "纱幕", "卷册", "场景陈设",
            "暖金中以", "暖金中", "暖金中以、卷册"):
        assert leaked not in contract["style"]
        assert leaked not in prompt


def test_vehicle_interior_and_worn_seatbelt_get_complete_physical_contract():
    shot = {
        "characters": ["虞寻歌"],
        "description": "虞寻歌坐在现代轿车副驾驶，系好安全带",
        "frame_target": {
            "phase": "end",
            "state": "虞寻歌坐稳副驾驶并系好安全带",
            "fallback": False,
        },
    }

    contract, prompt = compile_shot_prompt(
        shot, location="现代轿车车内", mode="image")
    physical = "；".join(contract["physical"]["rules"])

    for structure in ("驾驶座", "副驾驶座", "头枕", "方向盘", "中控台"):
        assert structure in physical
    for belt_fact in ("外侧上方固定点", "斜跨胸口", "内侧锁扣", "横跨左右髋部"):
        assert belt_fact in physical
    assert "不穿过身体" in physical
    assert "现代乘用车车内结构完整" in prompt
    assert "三点式安全带路径" in prompt


def test_readable_handheld_screen_keeps_user_facing_geometry_with_other_gaze():
    shot = {
        "characters": ["虞寻歌"],
        "description": "虞寻歌右手持亮屏手机，目光望向床上的弟弟",
        "frame_target": {
            "phase": "end",
            "state": "虞寻歌持手机站在床边，目光落在弟弟身上",
            "fallback": False,
        },
        "readable_text": {
            "required": True,
            "carrier": "手机屏幕",
            "whitelist": ["SS级【盗神】"],
        },
    }

    contract, prompt = compile_shot_prompt(
        shot, location="现代卧室", mode="image")
    physical = "；".join(contract["physical"]["rules"])

    assert "屏幕正面必须朝向实际使用者" in physical
    assert "只可向摄影机小角度倾斜" in physical
    assert "禁止为了文字清晰把屏幕完全翻向镜头" in physical
    assert "视线只落在该对象" in physical
    assert "不得让双眼同时注视两个目标" in prompt


def test_explicit_camera_and_single_subject_over_shoulder_are_authoritative():
    shot = {
        "characters": ["朱慈烺"],
        "description": "朱慈烺背对镜头，肩后望向案上奏疏",
        "frame_target_policy": "legacy",
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


def test_prompt_contract_blocks_actor_local_wardrobe_conflict():
    shot = {
        "characters": ["沈砚", "陈允"],
        "description": (
            "沈砚身穿布旅装跪坐床前给陈允喂水，"
            "榻边搭着一件崭新青官袍"),
        "appearance_state_required": True,
        "start_state": {
            "沈砚": {"pose": "跪坐", "wardrobe": "宽松青官袍"},
            "陈允": {"pose": "卧床", "wardrobe": "中衣"},
        },
        "end_state": {
            "沈砚": {"pose": "跪坐", "wardrobe": "宽松青官袍"},
            "陈允": {"pose": "卧床", "wardrobe": "中衣"},
        },
    }

    contract, _ = compile_shot_prompt(
        shot, location="洪武二十四年清河驿馆")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is False
    assert any(
        "沈砚当前动作服装" in issue and "青官袍" in issue
        for issue in report["issues"])


def test_prompt_contract_blocks_one_worn_robe_staged_twice():
    shot = {
        "characters": ["沈砚"],
        "description": "沈砚穿青官袍立在床前，床边又搭着青官袍",
        "appearance_state_required": True,
        "start_state": {
            "沈砚": {"pose": "站立", "wardrobe": "青官袍"},
        },
        "end_state": {
            "沈砚": {"pose": "站立", "wardrobe": "青官袍"},
        },
    }

    contract, _ = compile_shot_prompt(
        shot, location="洪武二十四年清河驿馆")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is False
    assert any("又被当作独立物件" in issue for issue in report["issues"])


def test_garment_beside_actor_is_not_misread_as_worn():
    shot = {
        "characters": ["沈砚"],
        "description": "沈砚跪坐病榻前，身旁放着崭新青官袍",
        "frame_target_policy": "legacy",
        "appearance_state_required": True,
        "start_state": {
            "沈砚": {"pose": "跪坐", "wardrobe": "布旅装"},
        },
        "end_state": {
            "沈砚": {"pose": "跪坐", "wardrobe": "布旅装"},
        },
    }

    contract, _ = compile_shot_prompt(
        shot, location="洪武二十四年清河驿馆")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is True


def test_historical_oil_lamp_gets_executable_morphology_lock():
    shot = {
        "characters": [],
        "description": "驿馆病榻旁一盏油灯将尽",
        "frame_target_policy": "legacy",
        "era_context": "大明洪武二十四年",
    }

    contract, prompt = compile_shot_prompt(
        shot,
        location="明代清河驿馆",
        style="电影级历史漫剧",
    )

    assert any(
        "开放式浅盏" in rule and "玻璃灯罩" in rule
        for rule in contract["era_object_constraints"])
    assert "煤油灯筒" in prompt
    assert "灯油与棉芯可见" in prompt
    assert validate_shot_prompt_contract(contract)["passed"]


def test_dead_single_actor_cannot_receive_living_performance():
    shot = {
        "characters": ["陈允"],
        "description": "陈允已咽气，尸身静卧病榻",
        "performance": {"micro_expression": "眼神变化，保持微弱呼吸"},
        "start_state": {
            "陈允": {"pose": "尸身静卧", "injury": "已咽气"},
        },
        "end_state": {
            "陈允": {"pose": "尸身静卧", "injury": "已咽气"},
        },
    }

    contract, _ = compile_shot_prompt(
        shot, location="明代清河驿馆")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is False
    assert any(
        "陈允已死亡" in issue and "呼吸" in issue
        for issue in report["issues"])


def test_survivor_may_react_when_another_actor_dies():
    shot = {
        "characters": ["陈允", "沈砚"],
        "description": "陈允咽气，沈砚眼神骤然凝住",
        "frame_target_policy": "legacy",
        "performance": {"micro_expression": "沈砚呼吸一滞，眼神变化"},
        "start_state": {
            "陈允": {"pose": "卧床"},
            "沈砚": {"pose": "跪坐"},
        },
        "end_state": {
            "陈允": {"pose": "尸身静卧", "injury": "已咽气"},
            "沈砚": {"pose": "跪坐僵住"},
        },
    }

    contract, _ = compile_shot_prompt(
        shot, location="明代清河驿馆")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is True


def _lin_chuan_witness_shot():
    return {
        "shot_no": 21,
        "characters": ["林川"],
        "functional_figures": [
            {"label": "黑衣人", "count": 3},
            {"label": "书童尸体", "count": 1},
        ],
        "visible_figure_count": 5,
        "description": "林川循声走到院门，放下包袱，躲到门板后",
        "frame_target_policy": "legacy",
        "start_state": {
            "林川": {
                "position": "院外石阶",
                "pose": "背着包袱站立",
                "direction": "面向院门",
            },
        },
        "end_state": {
            "林川": {
                "position": "院门内侧门板后",
                "pose": "屏息藏身，包袱静置脚边",
                "direction": "望向院内",
            },
        },
        "spatial_relations": [
            {
                "subject": "林川",
                "relation": "藏在",
                "object": "院门内侧门板后",
            },
            {
                "subject": "黑衣人3名",
                "relation": "围住",
                "object": "书童尸体",
            },
        ],
    }


def _reference_scope(reference):
    scope = reference.get("inherit_scope") or reference.get("scope") or {}
    return {
        "include": scope.get("include") or scope.get("inherits") or [],
        "exclude": scope.get("exclude") or scope.get("excludes") or [],
    }


def _assert_single_freeze_section(prompt):
    assert prompt.count("【定格状态】") == 1
    assert "【首帧定格】" not in prompt
    assert "【终点定格】" not in prompt


def test_v21_registered_functional_and_visible_counts_are_distinct():
    contract, prompt = compile_shot_prompt(
        _lin_chuan_witness_shot(),
        location="夜间深宅院落",
        style="电影级半写实3D精品漫剧",
        mode="image",
    )

    assert contract["schema"] == PROMPT_CONTRACT_SCHEMA
    assert contract["subject"]["registered_count"] == 1
    assert contract["subject"]["functional_count"] == 4
    assert contract["subject"]["visible_count"] == 5
    assert [
        (item.get("name") or item.get("label"), item.get("count"))
        for item in contract["subject"]["functional_figures"]
    ] == [("黑衣人", 3), ("书童尸体", 1)]
    assert contract["population"]["counts"] == {
        "named_characters": 1,
        "functional_people": 4,
        "real_people_total": 5,
        "non_real_overlays": 0,
        "visible_entity_instances_total": 5,
    }
    assert contract["composition"]["expected_visible_figure_count"] == 5
    assert validate_shot_prompt_contract(contract)["passed"]
    assert (
        "总可见人形严格为5" in prompt
        or "画面可见真人严格共5人" in prompt)
    assert "黑衣人3名" in prompt
    assert "书童尸体1" in prompt


def test_folded_functional_person_and_static_prop_text_follow_target_phase():
    """Sequential driver beats stay one body; end frame ignores start phone."""
    shot = {
        "shot_no": 2,
        "characters": ["虞寻歌"],
        "functional_figures": [
            {"name": "小吴", "count": 1,
             "state": "双手控制方向盘", "function": "代驾司机"},
            {"name": "小吴", "count": 1,
             "state": "平稳提高车速", "function": "本镜说话者"},
            {"name": "小吴", "count": 1,
             "state": "车辆停稳后解开安全带", "function": "接受委托"},
            {"name": "小吴", "count": 1,
             "state": "站在驾驶侧车外", "function": "递交白酒"},
        ],
        # Legacy long-take folding stored the sequential sum.  Contract
        # compilation repairs that stale value instead of blocking the shot.
        "visible_figure_count": 5,
        "description": "虞寻歌坐副驾驶，小吴站在驾驶侧车外",
        "frame_target": {
            "phase": "end",
            "state": "虞寻歌坐副驾驶，小吴手中为空并站在车外",
            "fallback": False,
        },
        "readable_text": {"phases": {
            "start": {
                "required": True,
                "carrier": "手机锁屏",
                "whitelist": ["23:10"],
                "layout": "锁屏时间居中",
                "style": "白色系统数字",
                "perspective": "朝向使用者",
            },
            "end": {
                "required": False,
                "carrier": "手机锁屏",
                "whitelist": [],
                "layout": "",
                "style": "",
                "perspective": "",
            },
        }},
        "prop_registry": [{
            "prop_id": "prop_phone_01", "name": "虞寻歌的手机",
        }],
        "frame_props": [
            {
                "prop_id": "prop_phone_01", "phase": "start",
                "physical_state": "亮屏显示时间", "holder": "虞寻歌右手",
                "location": "副驾驶胸腹前", "support": "右手",
                "visibility": "visible", "representation": "physical",
            },
            {
                "prop_id": "prop_phone_01", "phase": "end",
                "physical_state": "锁屏收纳", "holder": "虞寻歌",
                "location": "风衣右袋", "support": "口袋内衬",
                "visibility": "hidden", "representation": "physical",
            },
        ],
    }

    contract, prompt = compile_shot_prompt(shot, mode="image")

    figures = contract["subject"]["functional_figures"]
    assert len(figures) == 1
    assert figures[0]["name"] == "小吴"
    assert figures[0]["count"] == 1
    assert figures[0]["state"] == (
        "双手控制方向盘；平稳提高车速；车辆停稳后解开安全带；站在驾驶侧车外")
    assert figures[0]["function"] == "代驾司机；本镜说话者；接受委托；递交白酒"
    assert contract["subject"]["functional_count"] == 1
    assert contract["subject"]["visible_count"] == 2
    assert contract["population"]["issues"] == []
    core = prompt.split("【核心画面】", 1)[1].split("\n", 1)[0]
    assert "亮屏显示时间" not in core
    assert "必须清晰画出:虞寻歌的手机" not in core
    assert "23:10" not in prompt
    assert contract["readable_text_current"]["required"] is False
    physical = "；".join(
        contract["physical"]["rules"] + contract["physical"]["objects"])
    assert "手持屏幕关系" not in physical
    assert "手持屏幕：使用者/观看者" not in physical
    assert "phase=end" in prompt
    assert "phase=start" not in prompt

    start_shot = dict(shot)
    start_shot["frame_target"] = {
        "phase": "start",
        "state": "虞寻歌右手持亮屏手机坐在副驾驶，锁屏时间可见",
        "fallback": False,
    }
    start_contract, start_prompt = compile_shot_prompt(
        start_shot, mode="image")
    start_physical = "；".join(
        start_contract["physical"]["rules"]
        + start_contract["physical"]["objects"])
    assert "23:10" in start_prompt
    assert "手机锁屏内文字只保持原样" in start_prompt
    assert "手持屏幕关系" in start_physical
    assert "电脑使用关系" not in start_physical
    assert "电脑屏幕必须打开" not in start_prompt


def test_three_phase_phone_text_isolated_per_still_and_ordered_in_video():
    base = {
        "shot_no": 4,
        "characters": ["虞寻歌"],
        "description": (
            "虞寻歌先看锁屏02:21:59，随后手机显示SS级盗神，"
            "最后放低手机看向床上的弟弟，屏幕已不可读"),
        "start_state": "虞寻歌坐在沙发上查看手机锁屏",
        "end_state": "虞寻歌放低手机，目光落在床上的弟弟",
        "prop_registry": [{
            "prop_id": "prop_phone_04", "name": "虞寻歌的手机",
        }],
        "frame_props": [
            {
                "prop_id": "prop_phone_04", "phase": "start",
                "physical_state": "亮屏锁屏", "holder": "虞寻歌双手",
                "location": "胸前", "visibility": "visible",
                "representation": "physical",
            },
            {
                "prop_id": "prop_phone_04", "phase": "freeze",
                "physical_state": "亮屏显示天赋卡", "holder": "虞寻歌双手",
                "location": "胸前", "visibility": "visible",
                "representation": "physical",
            },
            {
                "prop_id": "prop_phone_04", "phase": "end",
                "physical_state": "屏幕背向机位且不可读", "holder": "虞寻歌右手",
                "location": "膝侧", "visibility": "occluded",
                "representation": "physical",
            },
        ],
        "readable_text": {"phases": {
            "start": {
                "required": True, "carrier": "手机锁屏",
                "whitelist": ["02:21:59"], "layout": "时间居中",
                "style": "白色系统数字", "perspective": "朝向使用者",
            },
            "freeze": {
                "required": True, "carrier": "手机屏幕",
                "whitelist": ["SS", "盗神"], "layout": "卡面上下排版",
                "style": "紫红游戏卡面", "perspective": "朝向使用者",
            },
            "end": {
                "required": False, "carrier": "手机屏幕",
                "whitelist": [], "layout": "", "style": "",
                "perspective": "背向机位",
            },
        }},
    }

    prompts = {}
    contracts = {}
    states = {
        "start": "虞寻歌查看手机锁屏",
        "freeze": "虞寻歌查看手机上的天赋卡",
        "end": "虞寻歌放低手机并看向床上的弟弟",
    }
    for phase, state in states.items():
        shot = dict(base)
        shot["frame_target"] = {
            "phase": phase, "state": state, "fallback": False,
        }
        contracts[phase], prompts[phase] = compile_shot_prompt(
            shot, location="2078年现代别墅卧室", mode="image")

    assert "02:21:59" in prompts["start"]
    assert "盗神" not in prompts["start"]
    assert "SS" not in prompts["start"]
    assert "SS、盗神" in prompts["freeze"]
    assert "02:21:59" not in prompts["freeze"]
    assert "02:21:59" not in prompts["end"]
    assert "盗神" not in prompts["end"]
    assert contracts["end"]["readable_text_current"]["required"] is False
    end_physical = "；".join(
        contracts["end"]["physical"]["rules"]
        + contracts["end"]["physical"]["objects"])
    assert "手持屏幕关系" not in end_physical
    for prompt in prompts.values():
        assert "电脑屏幕必须打开" not in prompt
        assert "电脑使用关系" not in prompt

    _, video_prompt = compile_shot_prompt(
        base, location="2078年现代别墅卧室", mode="video")
    assert "文字时间线（各阶段按时间先后独立执行）" in video_prompt
    assert "起点:手机锁屏内文字只保持原样:02:21:59" in video_prompt
    assert "中间定格:手机屏幕内文字只保持原样:SS、盗神" in video_prompt
    assert "终点:手机屏幕在本阶段不要求可读" in video_prompt
    assert "禁止把不同阶段的文字、屏幕状态或版式同时塞进同一帧" in video_prompt
    assert "电脑屏幕必须打开" not in video_prompt


def test_hidden_phase_prop_overrides_stale_whole_take_device_description():
    shot = {
        "characters": ["虞寻歌"],
        "description": (
            "起点虞寻歌手持手机查看屏幕，随后把手机收入口袋，"
            "终点目光看向床上的弟弟"),
        "frame_target": {
            "phase": "end",
            "state": "虞寻歌目光看向床上的弟弟，手机已收好",
            "fallback": False,
        },
        "prop_registry": [{
            "prop_id": "prop_phone_hidden", "name": "虞寻歌的手机",
        }],
        "frame_props": [{
            "prop_id": "prop_phone_hidden", "phase": "end",
            "physical_state": "完全收纳", "holder": "虞寻歌",
            "location": "风衣口袋内", "visibility": "hidden",
            "representation": "physical",
        }],
    }

    contract, prompt = compile_shot_prompt(
        shot, location="2078年现代别墅卧室", mode="image")
    physical = "；".join(
        contract["physical"]["rules"] + contract["physical"]["objects"])

    assert "手持屏幕关系" not in physical
    assert "手持屏幕：使用者/观看者" not in physical
    assert "手持手机查看屏幕" not in prompt


def test_static_frame_character_subset_does_not_leak_whole_take_cast():
    shot = {
        "characters": ["虞寻歌", "柳争流"],
        "description": "虞寻歌进入房间，随后柳争流站在门口",
        "visible_figure_count": 2,
        "start_state": {
            "虞寻歌": {"pose": "独自走进房间"},
            "柳争流": {"pose": "尚未进入画面"},
        },
        "end_state": {
            "虞寻歌": {"pose": "回头"},
            "柳争流": {"pose": "站在门口"},
        },
        "physical_contract": {"rules": [
            "虞寻歌站在房间内并由地面稳定支撑",
            "柳争流站在门外并与虞寻歌隔门相望",
        ]},
        "spatial_blocking": {"actors": [
            {"name": "虞寻歌", "position": "房间内", "facing": "门口"},
            {"name": "柳争流", "position": "门外", "facing": "房间内"},
        ]},
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "虞寻歌独自走进房间",
                "characters": ["虞寻歌"], "fallback": False,
            },
            "keyframe": {
                "phase": "freeze", "state": "虞寻歌回头看见柳争流",
                "characters": ["虞寻歌", "柳争流"], "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "虞寻歌与柳争流隔门相望",
                "characters": ["虞寻歌", "柳争流"], "fallback": False,
            },
        },
    }

    first, first_prompt = compile_shot_prompt(
        shot, location="2078年现代酒店房间", mode="first_frame")
    keyframe, keyframe_prompt = compile_shot_prompt(
        shot, location="2078年现代酒店房间", mode="image")
    last, last_prompt = compile_shot_prompt(
        shot, location="2078年现代酒店房间", mode="last_frame")
    video, video_prompt = compile_shot_prompt(
        shot, location="2078年现代酒店房间", mode="video")

    assert first["subject"]["count"] == 1
    assert first["subject"]["visible_count"] == 1
    assert first["actor_names"] == ["虞寻歌"]
    assert set(first["character_conditions"]) == {"虞寻歌"}
    assert [item["character"] for item in first["composition"]["actors"]] == [
        "虞寻歌"]
    assert first["composition"]["expected_visible_figure_count"] == 1
    assert first["frame_target"]["characters"] == ["虞寻歌"]
    assert "柳争流" not in first_prompt

    for contract, prompt in ((keyframe, keyframe_prompt), (last, last_prompt)):
        assert contract["subject"]["count"] == 2
        assert contract["subject"]["visible_count"] == 2
        assert contract["actor_names"] == ["虞寻歌", "柳争流"]
        assert "柳争流" in prompt

    assert video["subject"]["count"] == 2
    assert video["actor_names"] == ["虞寻歌", "柳争流"]
    assert "虞寻歌" in video_prompt and "柳争流" in video_prompt


def test_static_frame_uses_declared_visible_sublocation_only_for_provider_scene():
    shot3 = {
        "characters": [],
        "description": "人物从门外走廊进入卧室",
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "停在卧室门外走廊",
                # scene_location is an accepted compatibility alias.
                "scene_location": "虞家别墅·卧室门外走廊",
                "fallback": False,
            },
            "keyframe": {
                "phase": "freeze", "state": "已经进入卧室",
                "location": "虞家别墅·虞寻欢卧室",
                "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "停在卧室床边",
                "location": "虞家别墅·虞寻欢卧室",
                "fallback": False,
            },
        },
    }
    authoritative3 = "虞家别墅·虞寻欢卧室"
    first3, first3_prompt = compile_shot_prompt(
        shot3, location=authoritative3, mode="first_frame")
    key3, _ = compile_shot_prompt(
        shot3, location=authoritative3, mode="image")
    last3, _ = compile_shot_prompt(
        shot3, location=authoritative3, mode="last_frame")
    video3, video3_prompt = compile_shot_prompt(
        shot3, location=authoritative3, mode="video")
    joint3, _ = compile_shot_prompt(
        {**shot3, "frame_kind": "frames"},
        location=authoritative3, mode="frames")

    assert first3["scene"] == "虞家别墅·卧室门外走廊"
    assert first3["frame_target"]["location"] == (
        "虞家别墅·卧室门外走廊")
    assert "【场景】虞家别墅·卧室门外走廊" in first3_prompt
    assert key3["scene"] == last3["scene"] == authoritative3
    # Motion and paired-frame containers retain the authoritative whole-take
    # scene; this static override never changes scene_model asset authority.
    assert video3["scene"] == joint3["scene"] == authoritative3
    assert "虞家别墅·卧室门外走廊" not in video3_prompt

    shot1 = {
        "characters": [],
        "description": "人物由酒店房间内走到门外走廊",
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "仍在酒店房间内",
                "location": "酒店房间内", "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "已经站在房间外走廊",
                "location": "酒店房间外·走廊", "fallback": False,
            },
        },
    }
    first1, _ = compile_shot_prompt(
        shot1, location="酒店房间", mode="first_frame")
    last1, _ = compile_shot_prompt(
        shot1, location="酒店房间", mode="last_frame")
    assert first1["scene"] == "酒店房间内"
    assert last1["scene"] == "酒店房间外·走廊"


def test_static_frame_drops_timeline_prop_transitions_and_offstage_references():
    shot3 = {
        "characters": ["虞寻歌", "虞寻欢"],
        "description": "虞寻歌进门后与虞寻欢碰杯饮酒，虞寻欢随后失衡",
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "虞寻歌独自站在门外走廊",
                "characters": ["虞寻歌"], "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "两人停在卧室床边",
                "characters": ["虞寻歌", "虞寻欢"], "fallback": False,
            },
        },
        "frame_props": [
            {"prop_id": "wine", "phase": "start",
             "physical_state": "酒杯尚未使用", "holder": "虞寻歌",
             "location": "右手", "visibility": "visible",
             "representation": "physical"},
            {"prop_id": "wine", "phase": "end",
             "physical_state": "饮酒后酒杯放下", "holder": "虞寻欢",
             "location": "床边柜", "visibility": "visible",
             "representation": "physical"},
        ],
        "prop_transitions": [{
            "prop_id": "wine", "from_phase": "start", "to_phase": "end",
            "action": "虞寻欢与虞寻歌碰杯、饮酒后失衡",
        }],
    }
    references = [
        {"index": 1, "role": "identity", "character": "虞寻歌",
         "name": "虞寻歌", "binding": "锁定虞寻歌身份"},
        {"index": 2, "kind": "character_identity", "name": "虞寻欢",
         "binding": "锁定虞寻欢身份"},
        # Non-person roles must remain even when their name is not a cast name.
        {"index": 3, "role": "scene", "name": "虞家别墅卧室",
         "binding": "锁定空间"},
    ]

    first, first_prompt = compile_shot_prompt(
        shot3, location="虞家别墅", references=references,
        mode="first_frame")
    assert first["prop_transitions"] == []
    assert first["physical"]["prop_transitions"] == []
    assert first["prop_transitions_audit"][0]["action"] == (
        "虞寻欢与虞寻歌碰杯、饮酒后失衡")
    assert [ref["role"] for ref in first["references"]] == [
        "identity", "scene"]
    assert first["references"][0]["character"] == "虞寻歌"
    for forbidden in ("虞寻欢", "碰杯", "饮酒", "失衡", "道具变化审计"):
        assert forbidden not in first_prompt
    assert "【道具定格】" in first_prompt

    video, video_prompt = compile_shot_prompt(
        shot3, location="虞家别墅", references=references, mode="video")
    assert len(video["prop_transitions"]) == 1
    assert len(video["references"]) == 3
    assert "【道具状态变化】" in video_prompt
    assert "碰杯、饮酒后失衡" in video_prompt

    paired, paired_prompt = compile_shot_prompt(
        {**shot3, "frame_kind": "frames"}, location="虞家别墅",
        references=references, mode="frames")
    assert len(paired["prop_transitions"]) == 1
    assert len(paired["references"]) == 3
    assert "碰杯、饮酒后失衡" in paired_prompt

    shot1 = {
        "characters": ["虞寻歌"],
        "description": "虞寻歌从枕边取手机后走出房门",
        "frame_targets": {"last_frame": {
            "phase": "end", "state": "虞寻歌已站在门外走廊",
            "characters": ["虞寻歌"], "fallback": False,
        }},
        "frame_props": [
            {"prop_id": "phone", "phase": "start",
             "physical_state": "放在枕边", "location": "枕边",
             "visibility": "visible", "representation": "physical"},
            {"prop_id": "phone", "phase": "end",
             "physical_state": "完全收入口袋", "holder": "虞寻歌",
             "location": "风衣口袋内", "visibility": "hidden",
             "representation": "physical"},
        ],
        "prop_transitions": [{
            "prop_id": "phone", "from_phase": "start", "to_phase": "end",
            "action": "虞寻歌从枕边取手机并收入口袋",
        }],
    }
    end1, end1_prompt = compile_shot_prompt(
        shot1, location="酒店房间外·走廊", mode="last_frame")
    assert end1["prop_transitions"] == []
    assert "枕边取手机" not in end1_prompt


def test_static_frame_provider_prompt_drops_routes_and_whole_take_physics():
    """A still consumes one observable phase, never the shot's motion plan."""
    shot = {
        "characters": ["虞寻歌", "虞寻欢"],
        "description": "虞寻歌从门外走进卧室，最终停在床边",
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "虞寻歌独自在门外走廊",
                "characters": ["虞寻歌"], "fallback": False,
            },
            "keyframe": {
                "phase": "freeze", "state": "两人在卧室门边短暂停住",
                "characters": ["虞寻歌", "虞寻欢"], "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "虞寻欢躺在床上，虞寻歌站在床边",
                "characters": ["虞寻歌", "虞寻欢"], "fallback": False,
            },
        },
        "frame_props": [
            {"prop_id": "phone", "phase": "start",
             "physical_state": "亮屏", "holder": "虞寻歌",
             "location": "右手", "visibility": "visible",
             "representation": "physical"},
            {"prop_id": "phone", "phase": "freeze",
             "physical_state": "熄屏", "location": "床边柜",
             "visibility": "visible", "representation": "physical"},
            {"prop_id": "phone", "phase": "end",
             "physical_state": "完全收纳", "holder": "虞寻歌",
             "location": "风衣口袋内", "visibility": "hidden",
             "representation": "physical"},
        ],
        "physical_contract": {"rules": [
            "start阶段：虞寻歌独自在走廊，手机在右手，视线看向房门",
            "freeze阶段：虞寻歌与虞寻欢停在门边，手机在床边柜，双方对视",
            "end阶段：虞寻欢躺在床上，虞寻歌看向床，手机完全收入口袋",
        ]},
        "spatial_blocking": {
            "camera": {
                "start_3d": {"x": 0.0, "y": 1.5, "z": 4.0},
                "end_3d": {"x": 0.0, "y": 1.5, "z": 3.0},
                "target_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
                "target_start_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
                "target_end_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
                "fov_degrees": 54.4, "movement": "缓推",
            },
            "actors": [
                {"name": "虞寻歌", "moving": True,
                 "start_3d": {"x": -1.2, "y": 0.0, "z": 0.0},
                 "end_3d": {"x": 1.0, "y": 0.0, "z": 2.0},
                 "pose_label_start": "站姿", "pose_label_end": "站姿",
                 "support_start": "双脚/地面", "support_end": "双脚/地面",
                 "facing_start": "面向房门", "facing_end": "面向虞寻欢"},
                {"name": "虞寻欢", "moving": False,
                 "start_3d": {"x": 1.0, "y": 0.0, "z": 1.2},
                 "end_3d": {"x": 1.0, "y": 0.0, "z": 1.2},
                 "pose_label_start": "站姿", "pose_label_end": "卧姿",
                 "support_start": "双脚/地面", "support_end": "身体/床垫",
                 "facing_start": "面向虞寻歌", "facing_end": "面向天花板"},
            ],
        },
    }

    cases = {
        "first_frame": {
            "present": (
                "虞寻歌独自在门外走廊", "physical_state=亮屏",
                "holder=虞寻歌", "location=右手"),
            "absent": ("手机在床边柜", "双方对视", "躺在床上", "完全收入口袋"),
        },
        "image": {
            "present": (
                "两人在卧室门边短暂停住", "physical_state=熄屏",
                "location=床边柜"),
            "absent": ("手机在右手", "躺在床上", "完全收入口袋"),
        },
        "last_frame": {
            "present": (
                "虞寻欢躺在床上，虞寻歌站在床边",
                "physical_state=完全收纳", "location=风衣口袋内"),
            "absent": ("手机在右手", "手机在床边柜", "双方对视"),
        },
    }
    for mode, expected in cases.items():
        contract, prompt = compile_shot_prompt(
            shot, location="虞家别墅·虞寻欢卧室", mode=mode)
        assert "行动路线" not in contract["spatial_staging"]
        assert "【行动路线】" not in prompt
        for text in expected["present"]:
            assert text in prompt
        for text in expected["absent"]:
            assert text not in prompt

    video, video_prompt = compile_shot_prompt(
        shot, location="虞家别墅·虞寻欢卧室", mode="video")
    assert "行动路线" in video["spatial_staging"]
    assert "【行动路线】" in video_prompt
    for timeline_fact in (
            "手机在右手", "手机在床边柜", "双方对视",
            "虞寻欢躺在床上", "手机完全收入口袋"):
        assert timeline_fact in video_prompt


def test_changed_frame_location_drops_unscoped_whole_scene_layout():
    """A bedroom model cannot place its bed in a hallway boundary frame."""
    whole_scene_layout = (
        "【固定场景坐标】双人床贴北墙；床头框位于床垫正上方；"
        "床边柜紧贴床右侧；衣柜位于西墙。")
    shot = {
        "characters": ["虞寻歌"],
        "description": "虞寻歌由门外走廊进入卧室",
        "location": "虞家别墅·虞寻欢卧室",
        "scene_layout": whole_scene_layout,
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "虞寻歌站在卧室门外走廊",
                "location": "虞家别墅·卧室门外走廊",
                "characters": ["虞寻歌"], "fallback": False,
            },
            "keyframe": {
                "phase": "freeze", "state": "虞寻歌站在卧室门边",
                "location": "虞家别墅·虞寻欢卧室",
                "characters": ["虞寻歌"], "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "虞寻歌停在卧室床边",
                "location": "虞家别墅·虞寻欢卧室",
                "characters": ["虞寻歌"], "fallback": False,
            },
        },
    }

    hallway, hallway_prompt = compile_shot_prompt(
        shot, location=shot["location"], mode="first_frame")
    assert hallway["scene"] == "虞家别墅·卧室门外走廊"
    assert hallway["scene_layout"] == ""
    for bedroom_fixture in ("双人床", "床头框", "床边柜", "衣柜"):
        assert bedroom_fixture not in hallway_prompt

    keyframe, keyframe_prompt = compile_shot_prompt(
        shot, location=shot["location"], mode="image")
    last, last_prompt = compile_shot_prompt(
        shot, location=shot["location"], mode="last_frame")
    for contract in (keyframe, last):
        for bedroom_fixture in ("双人床", "床头框", "床边柜", "衣柜"):
            assert bedroom_fixture in contract["scene_layout"]
    assert "床头框" in keyframe_prompt and "床头框" in last_prompt

    video, video_prompt = compile_shot_prompt(
        shot, location=shot["location"], mode="video")
    for bedroom_fixture in ("双人床", "床头框", "床边柜", "衣柜"):
        assert bedroom_fixture in video["scene_layout"]
    assert "床头框" in video_prompt


def test_static_frame_rejects_character_outside_whole_take_cast():
    shot = {
        "characters": ["虞寻歌"],
        "frame_targets": {"keyframe": {
            "phase": "freeze", "state": "虞寻歌独自在房间",
            "characters": ["陌生配角"], "fallback": False,
        }},
    }

    contract, _ = compile_shot_prompt(shot, mode="image")
    report = validate_shot_prompt_contract(contract)

    assert contract["subject"]["count"] == 0
    assert not report["passed"]
    assert any(
        "包含未登记人物「陌生配角」" in issue
        for issue in report["issues"])


def test_static_frame_functional_figure_uses_only_current_phase_state():
    shot = {
        "characters": ["虞寻歌"],
        "visible_figure_count": 5,
        "description": "小吴驾车、停车、下车并递交物品的完整长镜头",
        "functional_figures": [
            {"name": "小吴", "count": 1,
             "state": "双手控制方向盘", "function": "代驾司机"},
            {"name": "小吴", "count": 1,
             "state": "平稳提高车速", "function": "本镜说话者"},
            {"name": "小吴", "count": 1,
             "state": "车辆停稳后解开安全带", "function": "接受委托"},
            {"name": "小吴", "count": 1,
             "state": "站在驾驶侧车外", "function": "递交白酒"},
        ],
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "虞寻歌坐副驾驶，小吴驾车",
                "characters": ["虞寻歌"],
                "functional_figures": [{
                    "name": "小吴", "count": 1,
                    "state": "左前驾驶位双手握方向盘",
                    "function": "代驾司机",
                }],
                "fallback": False,
            },
            "last_frame": {
                "phase": "end", "state": "虞寻歌坐副驾驶，小吴已下车",
                "characters": ["虞寻歌"],
                "functional_figures": [{
                    "name": "小吴", "count": 1,
                    "state": "驾驶侧车外双手为空",
                    "function": "完成代驾",
                }],
                "fallback": False,
            },
        },
    }

    first, first_prompt = compile_shot_prompt(
        shot, location="2078年现代轿车车内", mode="first_frame")
    last, last_prompt = compile_shot_prompt(
        shot, location="2078年现代轿车车内", mode="last_frame")

    for contract in (first, last):
        assert contract["subject"]["count"] == 1
        assert contract["subject"]["functional_count"] == 1
        assert contract["subject"]["visible_count"] == 2
        assert contract["composition"]["expected_visible_figure_count"] == 2
        assert contract["population"]["counts"]["real_people_total"] == 2
        assert contract["population"]["issues"] == []
    assert first["subject"]["functional_figures"][0]["state"] == (
        "左前驾驶位双手握方向盘")
    assert "平稳提高车速" not in first_prompt
    assert "解开安全带" not in first_prompt
    assert "站在驾驶侧车外" not in first_prompt
    assert last["subject"]["functional_figures"][0]["state"] == (
        "驾驶侧车外双手为空")
    assert "双手控制方向盘" not in last_prompt
    assert "平稳提高车速" not in last_prompt
    assert "解开安全带" not in last_prompt


def test_v21_image_prompt_renders_only_frozen_end_state_not_motion_process():
    shot = _lin_chuan_witness_shot()
    contract, prompt = compile_shot_prompt(
        shot, location="夜间深宅院落", mode="image")

    assert contract["output"] == {
        "media": "image",
        "frame_phase": "end",
        "temporal_policy": "terminal_only",
    }
    assert isinstance(contract["frame_target"], dict)
    assert contract["frame_target_source"] == "end_state"
    _assert_single_freeze_section(prompt)
    assert "院门内侧门板后" in prompt
    assert "屏息藏身，包袱静置脚边" in prompt
    assert "【起点】" not in prompt
    assert "【单一主动作】" not in prompt
    assert "【终点】" not in prompt
    assert "循声走到" not in prompt
    assert "放下包袱" not in prompt
    assert "躲到" not in prompt


def test_v21_explicit_image_or_freeze_state_overrides_motion_end_state():
    shot = _lin_chuan_witness_shot()
    shot["image_state"] = "林川贴在门板后，包袱已落地，目光越过门缝"
    image_state_contract, image_state_prompt = compile_shot_prompt(
        shot, location="夜间深宅院落", mode="image")
    assert image_state_contract["frame_target_source"] == "image_state"
    _assert_single_freeze_section(image_state_prompt)
    assert "林川贴在门板后，包袱已落地，目光越过门缝" in image_state_prompt
    assert "屏息藏身，包袱静置脚边" not in image_state_prompt

    shot["freeze_state"] = "林川蹲在门板阴影里，包袱静置脚边"
    freeze_state_contract, freeze_state_prompt = compile_shot_prompt(
        shot, location="夜间深宅院落", mode="image")
    assert freeze_state_contract["frame_target_source"] == "freeze_state"
    _assert_single_freeze_section(freeze_state_prompt)
    assert "林川蹲在门板阴影里，包袱静置脚边" in freeze_state_prompt
    assert "目光越过门缝" not in freeze_state_prompt


def test_v21_video_prompt_keeps_start_action_end_progression():
    shot = _lin_chuan_witness_shot()
    contract, prompt = compile_shot_prompt(
        shot, location="夜间深宅院落", mode="video")

    assert contract["output"] == {
        "media": "video",
        "frame_phase": "timeline",
        "temporal_policy": "timeline",
    }
    assert prompt.index("【起点】") < prompt.index("【单一主动作】")
    assert prompt.index("【单一主动作】") < prompt.index("【终点】")
    assert "循声走到院门，放下包袱，躲到门板后" in prompt
    assert "【定格状态】" not in prompt
    assert "【首帧定格】" not in prompt
    assert "【终点定格】" not in prompt


def test_v21_vague_functional_figure_count_fails_before_generation():
    for vague_count in ("几名", "数名"):
        shot = _lin_chuan_witness_shot()
        shot["functional_figures"] = [
            {"label": "黑衣人", "count": vague_count},
            {"label": "书童尸体", "count": 1},
        ]
        contract, _ = compile_shot_prompt(shot, mode="image")
        report = validate_shot_prompt_contract(contract)

        assert report["passed"] is False
        assert any(
            "功能人物" in issue and "精确" in issue
            for issue in report["issues"])


def test_v22_exact_visible_count_overrides_vague_population_heuristic():
    shot = _shot()
    shot["visible_figure_count"] = 2
    shot["description"] += "；禁止出现一群围观者"
    shot["frame_targets"] = {
        "keyframe": {
            "phase": "freeze",
            "state": "林晚与白芷严格共2人，手机停在两人之间",
            "fallback": False,
        },
    }

    contract, _ = compile_shot_prompt(shot, mode="image")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is True
    assert contract["subject"]["visible_count"] == 2


def test_v22_vague_population_without_exact_count_still_fails():
    shot = _shot()
    shot["description"] += "；门外另有一群围观者"
    shot["frame_targets"] = {
        "keyframe": {
            "phase": "freeze",
            "state": "林晚与白芷停在手机两侧",
            "fallback": False,
        },
    }

    contract, _ = compile_shot_prompt(shot, mode="image")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is False
    assert any("模糊人数" in issue for issue in report["issues"])


def test_v21_functional_sum_must_match_visible_figure_count():
    shot = _lin_chuan_witness_shot()
    shot["visible_figure_count"] = 6
    contract, _ = compile_shot_prompt(shot, mode="image")
    report = validate_shot_prompt_contract(contract)

    assert report["passed"] is False
    assert any(
        ("人数" in issue or "population" in issue)
        and all(value in issue for value in ("1", "4", "6"))
        for issue in report["issues"])


def test_v21_identity_reference_default_scope_is_identity_only():
    contract, prompt = compile_shot_prompt(
        {
            "characters": ["林川"],
            "description": "林川立在门板阴影中",
            "frame_target_policy": "legacy",
            "end_state": {"林川": {"pose": "屏息立定"}},
        },
        references=[{
            "index": 1,
            "label": "林川身份定版",
            "kind": "identity",
            "character": "林川",
        }],
        mode="image",
    )
    reference = contract["references"][0]
    scope = _reference_scope(reference)

    assert scope["include"] == ["identity"]
    assert set(scope["exclude"]) >= {
        "wardrobe", "pose", "background", "props", "prop_position",
    }
    assert "identity" in prompt
    for excluded in (
            "wardrobe", "pose", "background", "props", "prop_position"):
        assert excluded in prompt
    assert validate_shot_prompt_contract(contract)["passed"]


def test_v21_explicit_identity_scope_cannot_leak_nonidentity_fields():
    contract, _ = compile_shot_prompt(
        {"characters": ["林川"], "description": "林川立在院门后",
         "frame_target_policy": "legacy"},
        references=[{
            "index": 1,
            "label": "林川身份定版",
            "kind": "identity",
            "character": "林川",
            "inherit_scope": {
                "include": ["identity"],
                "exclude": [
                    "wardrobe", "pose", "background", "props",
                    "prop_position",
                ],
            },
        }],
    )
    assert validate_shot_prompt_contract(contract)["passed"]


def test_v21_reference_scope_inherits_excludes_overlap_fails():
    contract, _ = compile_shot_prompt(
        {"characters": ["林川"], "description": "林川立在院门后"},
        references=[{
            "index": 1,
            "label": "林川身份定版",
            "kind": "identity",
            "character": "林川",
            "inherit_scope": {
                "include": ["identity", "wardrobe"],
                "exclude": ["wardrobe", "pose", "background"],
            },
        }],
    )
    report = validate_shot_prompt_contract(contract)
    assert report["passed"] is False
    assert any(
        (
            ("include" in issue and "exclude" in issue)
            or ("inherits" in issue and "excludes" in issue)
        )
        for issue in report["issues"])


def test_v21_identity_reference_cannot_also_bind_wardrobe():
    contract, _ = compile_shot_prompt(
        {"characters": ["林川"], "description": "林川立在院门后"},
        references=[{
            "index": 1,
            "label": "林川身份定版",
            "kind": "identity",
            "character": "林川",
            "bindings": ["identity", "wardrobe"],
        }],
    )
    report = validate_shot_prompt_contract(contract)
    assert report["passed"] is False
    assert any(
        ("身份" in issue or "identity" in issue)
        and ("服装" in issue or "wardrobe" in issue)
        and "binding" in issue
        for issue in report["issues"])


def test_v21_spatial_relations_are_preserved_rendered_and_validated():
    contract, prompt = compile_shot_prompt(
        _lin_chuan_witness_shot(),
        location="夜间深宅院落",
        mode="image",
    )
    assert contract["spatial_relations"] == [
        {
            "subject": "林川",
            "relation": "藏在",
            "object": "院门内侧门板后",
        },
        {
            "subject": "黑衣人3名",
            "relation": "围住",
            "object": "书童尸体",
        },
    ]
    assert "【空间关系】" in prompt
    assert "林川→藏在→院门内侧门板后" in prompt
    assert "黑衣人3名→围住→书童尸体" in prompt
    assert contract["physical"]["spatial_relations"] == (
        contract["spatial_relations"])
    assert validate_shot_prompt_contract(contract)["passed"]

    for missing_key in ("subject", "object"):
        shot = _lin_chuan_witness_shot()
        shot["spatial_relations"] = [{
            "subject": "林川",
            "relation": "藏在",
            "object": "门板后",
        }]
        shot["spatial_relations"][0].pop(missing_key)
        broken, _ = compile_shot_prompt(shot, mode="image")
        report = validate_shot_prompt_contract(broken)
        assert report["passed"] is False
        assert any(
            "空间关系" in issue and missing_key in issue
            for issue in report["issues"])


def test_v21_conflicting_2d_and_3d_medium_fails():
    contract, _ = compile_shot_prompt(
        {"characters": ["林川"], "description": "林川立在门后",
         "frame_target_policy": "legacy"},
        style="2D手绘平涂，同时使用3D写实渲染",
        mode="image",
    )
    report = validate_shot_prompt_contract(contract)
    assert report["passed"] is False
    assert any(
        "2D" in issue and "3D" in issue and "冲突" in issue
        for issue in report["issues"])


def test_v21_semi_realistic_3d_is_explicitly_not_live_action_photography():
    contract, prompt = compile_shot_prompt(
        {"characters": ["林川"], "description": "林川立在门后",
         "frame_target_policy": "legacy"},
        style="电影级半写实3D精品漫剧",
        mode="image",
    )
    assert contract["medium"]["dimension"] == "3D"
    assert contract["medium"]["live_action_photography"] is False
    assert "半写实3D" in prompt
    assert "非真人摄影" in prompt
    assert validate_shot_prompt_contract(contract)["passed"]


def test_v21_legacy_shot_without_functional_figures_keeps_registered_count():
    shot = _shot()
    shot.pop("functional_figures", None)
    shot.pop("visible_figure_count", None)
    contract, prompt = compile_shot_prompt(
        shot, location="直播办公室", mode="video")

    assert contract["schema"] == "aifos.shot-prompt/v2.2"
    assert contract["subject"]["count"] == 2
    assert contract["subject"]["registered_count"] == 2
    assert contract["subject"]["functional_count"] == 0
    assert contract["subject"]["visible_count"] == 2
    assert contract["population"]["counts"]["named_characters"] == 2
    assert contract["population"]["counts"]["functional_people"] == 0
    assert contract["population"]["counts"]["real_people_total"] == 2
    assert "严格共2人" in prompt
    assert validate_shot_prompt_contract(contract)["passed"]


def test_video_model_constraints_lock_count_scale_camera_and_idle_actors():
    """逐镜负向清单必须写出具体人数、锁死景别并按名字按住非主动作角色。

    2026-07-30 A/B 实测:同一首帧同一参数,只给正向描述时模型把「人物向树
    后缩」执行成镜头追脸推近,中景 3 名杀手与倒地书童被挤出画面,可见人形
    5→1、景别中景→大特写;补上本清单后 9 项合同约束全过。shot 无关的
    【硬约束】常量说不出这三件事,所以本测试锁的是"按镜生成"这一能力。
    """
    shot = {
        "characters": ["林川", "杀手甲", "杀手乙", "杀手丙", "书童"],
        "description": "林川屏息不动，极缓慢把身体向树干后收回半寸",
        "camera": "35mm 平视中景 侧面 固定",
        "start_state": "林川蹲在前景树干后侧向观察",
        "end_state": "林川仍在树干后，比起点更贴向树干",
    }
    contract, video_prompt = compile_shot_prompt(shot, mode="video")

    assert contract["actor_names"] == [
        "林川", "杀手甲", "杀手乙", "杀手丙", "书童"]
    constraints = build_model_constraints(contract, media="video")
    rendered = "；".join(constraints)

    # 人数上下界一起封:实测里丢人先于加人。
    assert "总可见人形严格为 5 人" in rendered
    assert "禁止出现第 6 人" in rendered
    # 景别锁的是执行值(5 人已把景别升档),不是导演随手写的值。
    assert f"景别锁定为{contract['camera']['景别']}" in rendered
    assert "不得推成更紧景别" in rendered
    # 固定机位必须显式否掉全部运动,这是 A 组崩溃的直接机制。
    assert "不推、不拉、不摇、不移、不升降、不环绕、不变焦" in rendered
    assert "不得越轴到对侧" in rendered
    # 名字不出现在主动作里的角色要被按住;主动作执行者不得被按住。
    assert "杀手甲、杀手乙、杀手丙、书童保持起点状态" in rendered
    assert "林川保持起点状态" not in rendered

    assert "【模型约束】" in video_prompt
    assert "总可见人形严格为 5 人" in video_prompt


def test_still_model_constraints_forbid_motion_and_skip_camera_movement():
    """静态帧只需禁止动作过程,不该出现运镜类否定句。"""
    shot = {
        "characters": ["林川"],
        "description": "林川独自站在官道旁",
        "camera": "近景 平视 正面 推",
    }
    contract, image_prompt = compile_shot_prompt(shot, mode="image")
    constraints = build_model_constraints(contract, media="image")
    rendered = "；".join(constraints)

    assert "只定格当前状态" in rendered
    assert "不推、不拉" not in rendered
    assert "保持起点状态" not in rendered
    assert "总可见人形严格为 1 人" in rendered
    assert "【模型约束】" in image_prompt


def test_still_camera_geometry_never_expands_video_movement():
    """A freeze may inherit framing, never the take's push-motion geometry."""
    shot = {
        "characters": ["虞寻歌"],
        "description": "虞寻歌背向镜头站在走廊尽端",
        "camera": "全景 俯拍 侧面",
        "shot_contract": {"运镜": "推"},
    }

    _, image_prompt = compile_shot_prompt(shot, mode="image")
    _, first_prompt = compile_shot_prompt(shot, mode="first_frame")
    _, last_prompt = compile_shot_prompt(shot, mode="last_frame")
    _, video_prompt = compile_shot_prompt(shot, mode="video")

    for prompt in (image_prompt, first_prompt, last_prompt):
        assert "推=摄影机沿视线方向" not in prompt
        assert "俯拍=摄影机高于人物视线" in prompt
        assert "侧面=人物呈正侧轮廓" in prompt
    assert "推=摄影机沿视线方向" in video_prompt


def test_static_end_phase_overrides_stale_dialogue_gaze_and_start_facing():
    """A departing tail frame cannot be pulled back into mutual eye contact."""
    shot = {
        "characters": ["虞寻歌", "柳争流"],
        "description": "两人在房门口交谈后，虞寻歌离场",
        "camera": "全景 俯拍 侧面 推",
        "frame_targets": {
            "last_frame": {
                "phase": "end",
                "state": (
                    "酒店走廊离场终点：虞寻歌背向镜头走向远处电梯，"
                    "柳争流留在房门外注视她的背影"),
                "characters": ["虞寻歌", "柳争流"],
                "visible_figure_count": 2,
                "location": "现代酒店走廊",
            },
        },
        "start_state": {
            "虞寻歌": {
                "position": "房门内", "direction": "面向柳争流",
                "pose": "站立"},
            "柳争流": {
                "position": "房门外", "direction": "面向虞寻歌",
                "pose": "站立"},
        },
        "end_state": {
            "虞寻歌": {
                "position": "走廊远端", "direction": "背向柳争流、面向电梯",
                "pose": "迈步离场"},
            "柳争流": {
                "position": "房门外", "direction": "注视虞寻歌背影",
                "pose": "原地站立"},
        },
        "spatial_blocking": {
            "camera": {"position": "侧面"},
            "actors": [
                {"name": "虞寻歌", "start": "房门内",
                 "end": "走廊远端", "facing_start": "面向柳争流",
                 "facing_end": "背向柳争流、面向电梯"},
                {"name": "柳争流", "start": "房门外",
                 "end": "房门外", "facing_start": "面向虞寻歌",
                 "facing_end": "注视虞寻歌背影"},
            ],
            "dialogue_continuity": {
                "axis_id": "S01-P01-P02-A01",
                "screen_left_name": "柳争流",
                "screen_right_name": "虞寻歌",
                "camera_side": "positive",
                "coverage": "双人建立镜头",
            },
        },
    }

    contract, prompt = compile_shot_prompt(
        shot, location="现代酒店走廊", mode="last_frame")
    physical = "；".join(contract["physical"]["rules"])

    assert "当前end静态相位" in prompt
    assert "朝向/视线=背向柳争流、面向电梯" in prompt
    assert "人物站位与朝向：虞寻歌:走廊远端，朝向背向柳争流、面向电梯" in physical
    assert "双方身体朝向彼此" not in prompt
    assert "视线精确落在对方双眼" not in prompt
    assert "视线落在对方身上" not in prompt
    assert "不得强制改成互看双眼" in prompt
    assert "推=摄影机沿视线方向" not in prompt


def test_static_subject_appearance_comes_from_selected_phase_not_shared_visual():
    """Joint-frame payloads cannot leak tail clothes into the first frame."""
    shot = {
        "characters": ["虞寻歌"],
        "character_background": {"虞寻歌": {
            "species": "人类", "gender": "女", "age_range": "25岁",
            "identity": "演员", "occupation": "演员",
            "costume": "浅卡其风衣、米色平底鞋",
        }},
        "character_visuals": {"虞寻歌": (
            "形态:人类,性别:女,发型:长卷发随步伐轻动,"
            "气质:表面镇定、步伐急迫,服装:浅卡其风衣、米色平底鞋")},
        "start_state": {"虞寻歌": {
            "wardrobe": "象牙白衬衫、深灰西裤，未穿风衣与鞋",
            "hair_makeup": "长卷发散在枕面",
            "emotion": "惊惧梦魇",
            "prop": "双手未持物",
        }},
        "end_state": {"虞寻歌": {
            "wardrobe": "浅卡其风衣、米色平底鞋",
            "hair_makeup": "长卷发随步伐轻动",
            "emotion": "表面镇定、步伐急迫",
            "prop": "双手空置",
        }},
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "虞寻歌仰躺在床上",
                "characters": ["虞寻歌"], "visible_figure_count": 1,
            },
            "last_frame": {
                "phase": "end", "state": "虞寻歌穿好衣服离开房间",
                "characters": ["虞寻歌"], "visible_figure_count": 1,
            },
        },
    }

    _, first = compile_shot_prompt(shot, mode="first_frame")
    _, last = compile_shot_prompt(shot, mode="last_frame")
    _, video = compile_shot_prompt(shot, mode="video")
    first_subject = first.split("【场景】", 1)[0]
    last_subject = last.split("【场景】", 1)[0]
    video_subject = video.split("【场景】", 1)[0]

    assert "未穿风衣与鞋" in first_subject
    assert "长卷发散在枕面" in first_subject
    assert "惊惧梦魇" in first_subject
    for tail_only in ("浅卡其风衣", "米色平底鞋", "步伐急迫", "随步伐轻动"):
        assert tail_only not in first_subject
    assert "浅卡其风衣" in last_subject
    assert "米色平底鞋" in last_subject
    assert "步伐急迫" in last_subject
    # Video subject is identity-only; start/end appearance remains in the
    # existing timeline state sections instead of pinning the whole take.
    assert "浅卡其风衣" not in video_subject
    assert "未穿风衣与鞋" not in video_subject


def test_latest_camera_compound_scale_and_lens_override_stale_nested_fields():
    """A repair camera is authoritative even when old nested fields remain."""
    shot = {
        "characters": ["沈砚舟", "顾明昭"],
        "description": "两人以右前左后错位定格，画面严格只有两名真人。",
        "camera": (
            "9:16，135mm平视严格侧面双人中近景，固定机位；"
            "右前左后纵深错位三分构图，不跟移、不越轴、不变焦。"),
        "shot_contract": {
            "景别": "近景", "角度": "俯拍", "焦段": "85mm",
            "机位": "正面", "运镜": "跟", "构图": "框中框",
        },
        "five_dimensions": {"camera_design": {
            "shot_scale": "近景", "angle": "俯拍", "lens": "85mm",
            "camera_position": "正面", "movement": "跟",
            "composition": "框中框",
        }},
    }

    contract, prompt = compile_shot_prompt(shot, mode="image")

    assert contract["camera"]["景别"] == "中近景"
    assert contract["camera"]["角度"] == "平视"
    assert contract["camera"]["焦段"] == "135mm"
    assert contract["camera"]["机位"] == "侧面"
    assert contract["camera"]["运镜"] == "固定"
    assert contract["camera"]["构图"] == "三分法"
    assert "景别锁定为中近景" in prompt
    assert "景别锁定为近景" not in prompt


def test_still_physical_contract_drops_timeline_and_camera_follow_clauses():
    shot = {
        "characters": ["沈砚舟", "顾明昭"],
        "description": (
            "仅定格静态动作终点：沈砚舟已后退半步站稳；"
            "顾明昭端坐并持纸包不动。"),
        "camera": "135mm平视侧面双人中近景，固定机位",
        "physical_logic": (
            "沈砚舟从书案南缘后退半步，双手离开案面；"
            "顾明昭坐北侧持纸包不动。"
            "摄影机只在轴线东侧短跟沈砚舟，不跨越对视轴。"),
    }

    physical = build_physical_contract(shot, media="image")
    rendered = "；".join(physical["rules"])
    _, prompt = compile_shot_prompt(shot, mode="image")

    assert "短跟" not in rendered
    assert "后退半步" not in rendered
    assert "顾明昭坐北侧持纸包不动" in rendered
    assert "短跟" not in prompt
    assert "景别锁定为中近景" in prompt


def test_repaired_camera_is_synchronized_into_all_executable_contracts():
    shot = {
        "characters": ["沈砚舟", "顾明昭"],
        "visible_figure_count": 2,
        "description": "沈砚舟已后退半步站稳，顾明昭端坐持纸包不动。",
        "camera": (
            "135mm平视严格侧面双人中近景，固定机位；"
            "右前左后纵深错位三分构图，不跟移、不越轴、不变焦。"),
        "shot_contract": {
            "景别": "近景", "角度": "俯拍", "焦段": "85mm",
            "机位": "正面", "运镜": "跟", "构图": "框中框",
            "风格镜头组合": "侧面跟拍",
        },
        "five_dimensions": {"camera_design": {
            "shot_scale": "近景", "angle": "俯拍", "lens": "85mm",
            "camera_position": "正面", "movement": "跟",
            "composition": "框中框",
        }, "aesthetics": {"shot_pattern": "侧面跟拍"}},
        "style_direction": {
            "shot_pattern": "侧面跟拍",
            "camera_contract": {
                "shot_scale": "近景", "angle": "俯拍", "lens": "85mm",
                "camera_position": "正面", "movement": "跟",
                "composition": "框中框",
            },
        },
        "physical_logic": (
            "沈砚舟从书案南缘后退半步，双手离开案面；"
            "顾明昭坐北侧持纸包不动。"
            "摄影机只在轴线东侧短跟沈砚舟，不跨越对视轴。"),
        "prompt": "100mm侧面近景，仅短跟。",
        "seedance_prompt": "只执行一次跟，近景俯拍。",
    }

    synchronize_shot_execution_contract(
        shot, location="临江县衙书房", style="超写实古装短剧")

    expected = {
        "景别": "中近景", "角度": "平视", "焦段": "135mm",
        "机位": "侧面", "运镜": "固定", "构图": "三分法",
    }
    assert all(shot["shot_contract"][key] == value
               for key, value in expected.items())
    design = shot["five_dimensions"]["camera_design"]
    assert design["shot_scale"] == "中近景"
    assert design["angle"] == "平视"
    assert design["movement"] == "固定"
    direction_camera = shot["style_direction"]["camera_contract"]
    assert direction_camera == {
        "shot_scale": "中近景", "angle": "平视", "lens": "135mm",
        "camera_position": "侧面", "movement": "固定",
        "composition": "三分法",
    }
    assert "短跟" not in shot["physical_logic"]
    assert "只在轴线东侧" not in shot["physical_logic"]
    assert "固定在当前执行机位" in shot["physical_logic"]
    assert "短跟" not in shot["seedance_prompt"]
    assert "景别锁定为近景" not in shot["seedance_prompt"]
    assert "景别锁定为中近景" in shot["seedance_prompt"]
    assert shot["prompt_contract"]["camera"]["运镜"] == "固定"
    assert shot["prompt_contract"]["style_direction"][
        "camera_contract"] == direction_camera

    first = shot["seedance_prompt"]
    synchronize_shot_execution_contract(
        shot, location="临江县衙书房", style="超写实古装短剧")
    assert shot["seedance_prompt"] == first


def test_static_image_repair_preserves_the_original_video_action():
    shot = {
        "characters": ["沈砚舟", "顾明昭"],
        "description": (
            "仅生成单一静态终态：沈砚舟站在左侧，顾明昭端坐右侧。"),
        "camera": "35mm平视双人中景，固定机位",
        "prompt_contract": {
            "action": "顾明昭旋转官凭一角，随后看向沈砚舟双手。",
        },
        "shot_contract": {},
        "five_dimensions": {"camera_design": {}},
    }

    synchronize_shot_execution_contract(shot)

    assert shot["video_action"] == (
        "顾明昭旋转官凭一角，随后看向沈砚舟双手。")
    assert shot["prompt_contract"]["action"] == shot["video_action"]
    assert "仅生成单一静态终态" not in shot["seedance_prompt"]


def test_repaired_camera_uses_leading_scale_not_later_depth_label():
    shot = {
        "characters": ["沈砚舟", "顾明昭", "赵典吏"],
        "visible_figure_count": 3,
        "description": "三人围绕书案完成验牒终态。",
        "camera": (
            "9:16平视28mm全景，固定机位。摄影机位于书案东南侧；"
            "沈砚舟在近景南侧、顾明昭在远景北侧，三人全身入画。"),
        "shot_contract": {
            "景别": "中景", "角度": "平视", "焦段": "35mm",
            "机位": "正面", "运镜": "固定", "构图": "黄金分割",
        },
        "five_dimensions": {"camera_design": {}},
    }

    synchronize_shot_execution_contract(shot)

    assert shot["shot_contract"]["景别"] == "全景"
    assert shot["shot_contract"]["焦段"] == "28mm"
    assert shot["prompt_contract"]["camera"]["景别"] == "全景"


def test_repaired_camera_ignores_negated_or_actor_lock_movement_tokens():
    shot = {
        "characters": ["沈砚舟", "顾明昭"],
        "visible_figure_count": 2,
        "description": "只呈现铜符落案后的单一终态。",
        "prompt_block_repair": {
            "repair_summary": "收敛为双人边缘侧脸、双手和案上铜符局部近景",
        },
        "frame_props": [{
            "prop_id": "copper_token", "visibility": "visible",
            "holder": "none",
        }],
        "camera": (
            "9:16竖幅，135mm微俯东南斜侧局部近景；"
            "顾明昭固定为屏幕右锚点；摄影机保持原定固定机位，"
            "不推、不拉、不摇、不移、不升降、不环绕、不变焦。"),
        "shot_contract": {
            "景别": "中景", "角度": "平视", "焦段": "35mm",
            "机位": "侧面", "运镜": "环绕", "构图": "荷兰角",
        },
        "five_dimensions": {"camera_design": {}},
    }

    synchronize_shot_execution_contract(shot)

    assert shot["shot_contract"]["景别"] == "近景"
    assert shot["shot_contract"]["角度"] == "俯拍"
    assert shot["shot_contract"]["焦段"] == "135mm"
    assert shot["shot_contract"]["机位"] == "侧面"
    assert shot["shot_contract"]["运镜"] == "固定"
    assert shot["prompt_contract"]["camera"]["运镜"] == "固定"


def test_mobile_viewing_policy_does_not_invent_an_in_scene_phone():
    physical = build_physical_contract({
        "characters": ["沈砚舟", "顾明昭"],
        "description": (
            "铜符平贴案面，尺寸仅略作手机端可读性调整；"
            "严格无其他人物、道具或文字。"),
        "shot_contract": {"画面内容描述": "铜符落案后的终点静帧"},
    }, media="image")

    rendered = "；".join(physical["rules"] + physical["objects"])
    assert "手持屏幕" not in rendered
    assert "屏幕正面" not in rendered


def test_model_constraints_skip_unresolved_camera_placeholders():
    """分镜没写景别时,_camera 回落到占位符「按分镜」。

    照抄进负向清单会得到"景别锁定为按分镜"——没有可执行画面标准的空话,
    正是原文批评的"镜头自然过渡"式写法,还会占掉提示词开头权重。
    宁可少一条,也不给模型假约束;运镜缺失时按"机位固定"兜底,因为让模型
    自由运镜的代价远高于一个不动的机位。
    """
    shot = {
        "characters": ["林川"],
        "description": "林川屏息不动",
        "camera": "35mm",
    }
    contract, video_prompt = compile_shot_prompt(shot, mode="video")
    rendered = "；".join(build_model_constraints(contract, media="video"))

    assert "按分镜" not in rendered
    assert "景别锁定为" not in rendered
    assert "机位固定,不推、不拉" in rendered
    assert "按分镜" not in video_prompt.split("【模型约束】")[1]


def test_all_unknown_character_conditions_are_dropped_from_prompt():
    """condition 四字段全缺时不渲染【人物状态合同】。

    2026-07-30 消融实测:5 人镜里该段占整条提示词 36.5%(最大的一块),而每个
    值都是 unknown;真正的镜头内容只占 5.5%。把提示词压到 40% 后九项硬约束
    仍全过,证明这部分冗余可安全削掉。死亡/昏迷/静止的真正执行条款在
    hard_state_lines,不受影响。
    """
    shot = {
        "characters": ["林川", "书童"],
        "description": "林川屏息不动",
        "camera": "35mm 平视中景 侧面 固定",
    }
    _, video_prompt = compile_shot_prompt(shot, mode="video")
    assert "【人物状态合同】" not in video_prompt
    assert "life=unknown" not in video_prompt

    # 有真实状态的角色仍必须保留该行,且死亡条款照常生效。
    shot_with_state = {
        **shot,
        "characters": ["林川", "书童"],
        "character_conditions": {
            "书童": {
                "start": {"life_state": "dead", "consciousness_state": "none"},
                "end": {"life_state": "dead", "consciousness_state": "none"},
            },
            "林川": {"start": {}, "end": {}},
        },
    }
    _, stated_prompt = compile_shot_prompt(shot_with_state, mode="video")
    assert "【人物状态合同】" in stated_prompt
    assert "书童:start(life=dead" in stated_prompt
    # 全 unknown 的林川不该混进这一行
    assert "林川:start(life=unknown" not in stated_prompt
