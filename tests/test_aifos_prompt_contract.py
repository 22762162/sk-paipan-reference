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
