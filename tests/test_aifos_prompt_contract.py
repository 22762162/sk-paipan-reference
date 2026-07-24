from aifos.prompt_contract import (
    PROMPT_CONTRACT_SCHEMA,
    build_composition_contract,
    compile_shot_prompt,
    readable_text_required,
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
