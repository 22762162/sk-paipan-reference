from aifos.prompt_contract import (
    PROMPT_CONTRACT_SCHEMA,
    build_composition_contract,
    build_model_constraints,
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
        {"characters": ["林川"], "description": "林川立在院门后"},
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
        {"characters": ["林川"], "description": "林川立在门后"},
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
        {"characters": ["林川"], "description": "林川立在门后"},
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
