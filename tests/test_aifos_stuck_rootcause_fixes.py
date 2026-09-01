"""「反复卡住」根因修复的回归锁。

根因1:景别容量 < 人数合同 → 编译出同级互斥合同 → 审核必熔断。
根因4:重启遗留 generating 僵尸认领 + awaiting_human 门禁被自动重派绕过。
根因5:.png 文件装 JPEG 字节 → API 校验 400 → image_qc 阶梯断级。
"""

import json
from types import SimpleNamespace

import pytest

from aifos.app import App
from aifos.camera_language import (
    CAMERA_SCALE_CAPACITY,
    allows_partial_multi_subject_scale,
    enforce_scale_capacity,
    scale_capacity,
)
from aifos.prompt_contract import compile_shot_prompt, style_for_scene
from aifos.production.api_providers import sniff_image_media
from aifos.workflow import _camera_plan


# ---------- 根因1:景别容量可行性门禁 ----------

def test_scale_capacity_matches_geometry():
    # 容量与 SCALE_GEOMETRY 同源:特写肩线以下出画,装不下第二个人。
    # 覆盖镜头词汇扩展后的全部收紧景别(中近景/七分身/膝上景)。
    assert CAMERA_SCALE_CAPACITY == {
        "大特写": 1, "特写": 1, "近景": 2, "中近景": 2,
        "七分身": 3, "中景": 4, "膝上景": 4, "中全景": 6}
    assert scale_capacity("全景") > 100
    assert scale_capacity("中全景") == 6
    assert scale_capacity("") > 100  # 未知景别不设限,宁可漏修不误改


def test_enforce_scale_capacity_upgrades_only_when_needed():
    assert enforce_scale_capacity("特写", 1) == ("特写", "")
    scale, note = enforce_scale_capacity("特写", 3)
    assert scale == "中景" and "升档" in note
    scale, note = enforce_scale_capacity("近景", 7)
    assert scale == "全景" and "7人" in note
    # 人数未知/非法时绝不动景别。
    assert enforce_scale_capacity("特写", None) == ("特写", "")
    assert enforce_scale_capacity("特写", "abc") == ("特写", "")
    assert enforce_scale_capacity("特写", 0) == ("特写", "")


def test_partial_closeup_never_overrides_explicit_full_body_or_three_faces():
    assert not allows_partial_multi_subject_scale(
        "双人面部大特写，但二人全身都在画面中", 2)
    assert not allows_partial_multi_subject_scale(
        "三名人物均只以局部入画，头部躯干出画；"
        "同时要求三名人物完整面孔清晰同框", 3)
    # 否定式不是正向全身要求，明确的手腕局部仍应放行。
    assert allows_partial_multi_subject_scale(
        "双手大特写，不要求两人全身入画；"
        "两名人物均只以手腕局部入画，完整人形出画", 2)


def test_camera_plan_never_emits_infeasible_scale():
    # 盲轮换曾把 85mm 特写配给多人镜头 → 同级互斥 → 审核必熔断。
    for index in range(1, 9):
        plan = _camera_plan("", "dialogue", index, visible_count=3)
        assert scale_capacity(plan["shot_scale"]) >= 3, plan
        assert not (plan["lens"] == "85mm"
                    and plan["shot_scale"] in ("近景", "特写")
                    and scale_capacity(plan["shot_scale"]) < 3)
    # 显式特写 + 7 人:升档并留审计说明。
    plan = _camera_plan("特写", "dialogue", 1, visible_count=7)
    assert plan["shot_scale"] == "全景"
    assert "升档" in plan["capacity_note"]
    # 单人镜头不受影响,特写仍是特写。
    plan = _camera_plan("特写", "reaction", 1, visible_count=1)
    assert plan["shot_scale"] == "特写"
    assert "capacity_note" not in plan


def test_camera_plan_keeps_explicit_two_person_detail_closeup():
    plan = _camera_plan(
        "微俯双手大特写，135mm", "physical", 1,
        visible_count=2,
        framing_text="两名人物均只以手腕局部入画，完整人形出画")
    assert plan["shot_scale"] == "大特写"
    assert plan["lens"] == "135mm"
    assert "capacity_note" not in plan


def test_shootability_repair_accepts_string_camera_over_old_dict(
        app, monkeypatch):
    """适配器改成一句精准机位时，不能因旧 camera 是 dict 而丢弃。"""
    shot = {
        "shot_no": 1,
        "scene_no": 1,
        "characters": ["甲", "乙"],
        "visible_figure_count": 2,
        "description": "两人站在房间中央。",
        "camera": {
            "shot_scale": "中景", "angle": "平视", "lens": "35mm",
            "camera_position": "正面", "movement": "固定",
            "composition": "三分法",
        },
        "shot_contract": {"景别": "中景", "焦段": "35mm"},
        "five_dimensions": {"camera_design": {"shot_scale": "中景"}},
        "style_direction": {
            "camera_contract": {"shot_scale": "中景", "lens": "35mm"}},
    }
    monkeypatch.setattr(
        app.director, "_call",
        lambda *_args, **_kwargs: SimpleNamespace(data={
            "camera": "9:16竖幅，135mm微俯斜侧局部近景，固定机位",
            "description": "两人仅以手部和侧脸局部入画。",
            "repair_summary": "收紧为可拍局部近景",
        }))
    ctx = {
        "project": {"title": "修复测试", "style": ""},
        "script": {"scenes": [{"scene_no": 1, "location": "房间"}]},
    }

    app.director._repair_shot_for_shootability(
        ctx, shot, "中景机位距离超出房间净深")

    assert isinstance(shot["camera"], str)
    assert shot["shot_contract"]["景别"] == "近景"
    assert shot["shot_contract"]["焦段"] == "135mm"
    assert shot["five_dimensions"]["camera_design"]["shot_scale"] == "近景"
    assert shot["style_direction"]["camera_contract"]["shot_scale"] == "近景"
    assert shot["prompt_contract"]["camera"]["景别"] == "近景"


def test_shootability_repair_does_not_reinject_ancient_style_into_modern_scene(
        app, monkeypatch):
    """Modern repair writers receive aesthetics, never ancient furnishings."""
    captured = {}

    def fake_call(_ctx, _stage, payload, _kind):
        captured.update(payload)
        return SimpleNamespace(data={
            "camera": "35mm平视中景，固定机位",
            "description": "现代别墅卧室内，人物站在床边。",
            "repair_summary": "保持现代场景并修正机位",
        })

    monkeypatch.setattr(app.director, "_call", fake_call)
    shot = {
        "shot_no": 1,
        "scene_no": 1,
        "characters": ["甲"],
        "description": "人物站在现代卧室床边。",
        "camera": "35mm平视中景，固定机位",
    }
    ctx = {
        "project": {
            "title": "时代隔离测试",
            "style": (
                "鎏金柔雾、超写实真人古风女频短剧；"
                "暖金古室中以书案、卷册、香炉和半垂纱幕构图；"
                "电影级半写实3D"),
        },
        "script": {
            "scenes": [{"scene_no": 1, "location": "现代豪宅卧室"}]},
    }

    app.director._repair_shot_for_shootability(
        ctx, shot, "修正机位距离")

    assert captured["location"] == "现代豪宅卧室"
    assert "鎏金柔雾" in captured["style"]
    assert "电影级半写实3D" in captured["style"]
    for forbidden in ("古风", "古室", "书案", "卷册", "香炉", "纱幕"):
        assert forbidden not in captured["style"]


def test_shootability_repair_keeps_ancient_style_in_ancient_scene(
        app, monkeypatch):
    """Time-travel/historical scenes keep their episode-authoritative style."""
    captured = {}

    def fake_call(_ctx, _stage, payload, _kind):
        captured.update(payload)
        return SimpleNamespace(data={
            "camera": "35mm平视中景，固定机位",
            "description": "明代宫殿内人物立于书案前。",
            "repair_summary": "修正机位",
        })

    monkeypatch.setattr(app.director, "_call", fake_call)
    shot = {
        "shot_no": 1,
        "scene_no": 1,
        "characters": ["甲"],
        "description": "明代宫殿内人物立于书案前。",
        "camera": "35mm平视中景，固定机位",
    }
    ancient_style = "鎏金柔雾古风；明代宫殿内以书案和香炉构图"
    ctx = {
        "project": {"title": "穿越测试", "style": ancient_style},
        "script": {
            "scenes": [{"scene_no": 1, "location": "明代宫殿内景"}]},
    }

    app.director._repair_shot_for_shootability(
        ctx, shot, "修正机位距离")

    assert captured["style"] == ancient_style


def test_scene_style_uses_authoritative_era_for_ambiguous_locations():
    """地点没写“现代”时也必须服从逐镜时代，不能靠有限地名猜。"""
    ancient_style = (
        "鎏金柔雾、电影级半写实3D；"
        "暖金古室中以书案、卷册、香炉和半垂纱幕构图")

    modern = style_for_scene(
        ancient_style, "高层内厅·夜",
        {"era_context": "2078年现代现实世界", "active_realm_id": "reality"})
    assert "鎏金柔雾" in modern
    assert "电影级半写实3D" in modern
    for forbidden in ("古室", "书案", "卷册", "香炉", "纱幕"):
        assert forbidden not in modern

    ancient = style_for_scene(
        ancient_style, "内室·夜",
        {"era_context": "明代崇祯年间", "active_realm_id": "ming"})
    assert ancient == ancient_style


def test_complete_shot_contract_keeps_rule_stack_audit_out_of_provider_prompt(
        app):
    payload = {
        "prompt": "【镜头合同v2.2】现代卧室内严格共1人",
        "prompt_compact": "【镜头合同v2.2】现代卧室内严格共1人",
        "prompt_contract_complete": True,
        "effective_rule_lines": [
            "world.forbidden_drift=[\"不得出现宫殿\",\"不得出现古装\"]",
            "story.high_value_events_must_expand=高价值事件必须展开",
        ],
    }

    app.director._append_generation_rules(
        payload, ["人物、镜头与道具的物理关系必须成立"])

    assert "物理关系必须成立" in payload["prompt_compact"]
    for forbidden in ("world.forbidden_drift", "宫殿", "古装", "高价值事件"):
        assert forbidden not in payload["prompt_compact"]
    # Governance remains complete and auditable outside the provider string.
    assert len(payload["effective_rule_lines"]) == 2


def test_generation_rule_block_is_replaced_when_runtime_rules_change(app):
    payload = {
        "prompt": "【镜头合同v2.2】同一卧室内严格共1人",
        "prompt_compact": "【镜头合同v2.2】同一卧室内严格共1人",
        "prompt_contract_complete": True,
    }

    app.director._append_generation_rules(payload, ["旧规则A"])
    app.director._append_generation_rules(payload, ["新规则B"])

    assert payload["generation_quality_rules"] == ["新规则B"]
    assert "旧规则A" not in payload["prompt"]
    assert "旧规则A" not in payload["prompt_compact"]
    assert payload["prompt"].count("新规则B") == 1
    assert payload["prompt_compact"].count("新规则B") == 1

def test_inherently_modern_location_filters_ancient_furnishings_without_label():
    style = "写实3D；古室中以书案、香炉和纱幕构图"
    for location in ("直播间·夜", "高档套房·夜", "私人病房", "城市摄影棚"):
        filtered = style_for_scene(style, location)
        assert "写实3D" in filtered
        assert all(word not in filtered for word in ("古室", "书案", "香炉", "纱幕"))


def test_mixed_time_travel_world_never_overrides_current_ancient_scene():
    """全剧双时空只作背景；明确的当前古代场景/realm 必须保留古风。"""
    style = "鎏金柔雾古风；明代宫殿内以书案和香炉构图"

    palace = style_for_scene(
        style, "明代宫殿内景",
        {"era_context": "现代都市与明代崇祯双时空"})
    assert palace == style

    ming_room = style_for_scene(
        style, "内室·夜",
        {"era_context": "现代都市与明代崇祯双时空",
         "active_realm_id": "ming"})
    assert ming_room == style


def test_compile_shot_prompt_fixes_saved_infeasible_contract():
    """已保存的旧分镜(特写×3人全见)在编译期就地升档,不再送去熔断。"""
    shot = {
        "shot_no": 8, "scene_no": 1, "kind": "dialogue",
        "camera": "85mm特写,平视,正面",
        "characters": ["林川", "赵百户", "阿砚"],
        "description": "对峙",
        "action": "林川举手自证",
        "five_dimensions": {"camera_design": {
            "shot_scale": "特写", "lens": "85mm", "angle": "平视",
            "camera_position": "正面", "movement": "固定",
            "composition": "中心构图",
        }},
        "start_state": {}, "end_state": {},
    }
    contract, prompt = compile_shot_prompt(
        shot, location="废茶棚", style="明代历史", references=[],
        mode="image")
    camera = contract["camera"]
    assert camera["景别"] == "中景"
    assert "容量修正" in camera and "3人" in camera["容量修正"]
    # 85mm 是近景/特写绑定焦段,升档后必须一起改,否则再造一对矛盾。
    assert camera["焦段"] == "35mm"
    # 审计键不进提示词正文。
    assert "容量修正" not in prompt
    assert "特写" not in prompt.split("【镜头】")[1].split("。")[0]


def test_compile_keeps_feasible_closeup_untouched():
    shot = {
        "shot_no": 3, "scene_no": 1, "kind": "reaction",
        "camera": "特写", "characters": ["林川"],
        "description": "惊愕", "action": "瞳孔骤缩",
        "five_dimensions": {"camera_design": {
            "shot_scale": "特写", "lens": "85mm"}},
        "start_state": {}, "end_state": {},
    }
    contract, _prompt = compile_shot_prompt(
        shot, location="废茶棚", style="明代历史", references=[],
        mode="image")
    assert contract["camera"]["景别"] == "特写"
    assert contract["camera"]["焦段"] == "85mm"
    assert "容量修正" not in contract["camera"]


def test_compile_keeps_two_person_hand_detail_as_large_closeup():
    shot = {
        "shot_no": 4, "scene_no": 1, "kind": "physical",
        "camera": "微俯双手大特写，135mm，固定机位",
        "characters": ["虞寻歌", "虞寻欢"],
        "visible_figure_count": 2,
        "description": (
            "两名人物均只以手腕局部入画，不出现任何完整人形；"
            "头部、面部、躯干及其余身体明确出画"),
        "frame_targets": {"keyframe": {
            "phase": "end", "state": "两人的手腕保持接触",
            "fallback": False, "explicit": True}},
        "start_state": {}, "end_state": {},
    }
    contract, _prompt = compile_shot_prompt(
        shot, location="现代卧室", style="写实", references=[],
        mode="image")
    assert contract["camera"]["景别"] == "大特写"
    assert contract["camera"]["焦段"] == "135mm"
    assert "容量修正" not in contract["camera"]


def test_partial_people_do_not_hide_distant_prop_anchor_conflict():
    shot = {
        "shot_no": 5, "scene_no": 1, "kind": "dialogue",
        "camera": "双人贴面大特写，135mm，固定机位",
        "characters": ["甲", "乙"], "visible_figure_count": 2,
        "description": "两张脸贴近对话，同时要求远处墙钟清晰入画",
        "frame_props": [{
            "prop_id": "wall_clock", "phase": "freeze",
            "visibility": "visible", "holder": "none",
        }],
        "frame_targets": {"keyframe": {
            "phase": "freeze", "state": "甲乙两张脸与远处墙钟同框",
            "fallback": False, "explicit": True}},
        "start_state": {}, "end_state": {},
    }
    contract, _prompt = compile_shot_prompt(
        shot, location="现代卧室", style="写实", references=[],
        mode="image")
    assert contract["camera"]["景别"] == "中景"
    assert contract["camera"]["焦段"] == "35mm"
    assert "空间锚点修正" in contract["camera"]["容量修正"]


def test_explicitly_off_frame_occluded_prop_does_not_widen_hand_closeup():
    """镜11实况:手机已明确出画，不得再把135mm手部特写升成中景。"""
    shot = {
        "shot_no": 11,
        "camera": "135mm微俯手部大特写，固定机位，中心对称构图",
        "characters": ["虞寻歌", "虞寻欢"],
        "visible_figure_count": 2,
        "description": (
            "画面只框入两人的手腕局部，不出现完整人形；"
            "床沿手机全部明确出画。"),
        "frame_targets": {"keyframe": {
            "phase": "end", "state": "两人的手腕保持接触，手机全部明确出画",
            "fallback": False, "explicit": True}},
        "frame_props": [{
            "prop_id": "prop_game_phone_01", "phase": "end",
            "visibility": "occluded", "representation": "physical",
            "holder": "none", "location": "床沿", "support": "床垫",
        }],
        "start_state": {}, "end_state": {},
    }

    contract, prompt = compile_shot_prompt(
        shot, location="现代卧室", style="写实", references=[],
        mode="image")

    assert contract["camera"]["景别"] == "大特写"
    assert contract["camera"]["焦段"] == "135mm"
    assert "容量修正" not in contract["camera"]
    assert contract["frame_props"] == []
    assert "prop_game_phone_01" not in prompt
    assert "手机" not in prompt


def test_out_of_frame_parser_respects_prohibited_out_language():
    for state in (
            "手机不得出画", "手机不能出画", "手机禁止出画",
            "手机必须不出画", "手机应保持画面内，不可出画"):
        shot = {
            "camera": "135mm手机与手部特写",
            "characters": ["甲"],
            "frame_targets": {"keyframe": {
                "phase": "freeze", "state": state,
                "fallback": False, "explicit": True}},
            "frame_props": [{
                "prop_id": "phone_main", "name": "手机", "phase": "freeze",
                "visibility": "visible", "holder": "甲",
            }],
        }
        contract, _prompt = compile_shot_prompt(shot, mode="image")
        assert contract["frame_props"], state


def test_out_of_frame_filter_is_bound_to_current_static_phase():
    shot = {
        "camera": "50mm近景",
        "characters": ["甲"],
        "frame_kind": "last_frame",
        "frame_targets": {
            "first_frame": {
                "phase": "start", "state": "手机保持画外",
                "fallback": False, "explicit": True},
            "last_frame": {
                "phase": "end", "state": "甲举起手机，屏幕保持可见",
                "fallback": False, "explicit": True},
        },
        "frame_props": [
            {"prop_id": "phone_main", "name": "手机", "phase": "start",
             "visibility": "absent", "holder": "none"},
            {"prop_id": "phone_main", "name": "手机", "phase": "end",
             "visibility": "visible", "holder": "甲"},
        ],
    }

    contract, prompt = compile_shot_prompt(shot, mode="last_frame")

    assert contract["frame_target"]["phase"] == "end"
    assert contract["frame_props"][0]["prop_id"] == "phone_main"
    assert "手机" in prompt


def test_absent_unheld_phone_does_not_trigger_handheld_screen_rule_in_video():
    shot = {
        "camera": "135mm手部大特写",
        "characters": ["甲", "乙"],
        "description": "甲握住乙的手腕，手机保持本镜画外",
        "frame_props": [
            {"prop_id": "phone_main", "name": "手机", "phase": phase,
             "visibility": "absent", "holder": "none"}
            for phase in ("start", "end")
        ],
        "physical_contract": {
            "rules": ["手持屏幕关系：屏幕正面朝向使用者"],
            "objects": ["手持屏幕：使用者↔屏幕正面"],
        },
    }

    contract, prompt = compile_shot_prompt(shot, mode="video")

    assert all("手持屏幕关系" not in rule
               for rule in contract["physical"]["rules"])
    assert "手持屏幕关系" not in prompt


def test_ordinary_occluded_loose_prop_still_counts_as_spatial_anchor():
    """没有明确出画声明时，部分遮挡的离身道具仍参与构图关系。"""
    shot = {
        "camera": "135mm双手大特写",
        "characters": ["甲", "乙"], "visible_figure_count": 2,
        "description": "两人的手腕局部与桌边被手臂遮住一半的手机同框",
        "frame_targets": {"keyframe": {
            "phase": "end", "state": "双手和部分被挡手机同框",
            "fallback": False, "explicit": True}},
        "frame_props": [{
            "prop_id": "prop_game_phone_01", "phase": "end",
            "visibility": "occluded", "representation": "physical",
            "holder": "none", "location": "桌边", "support": "桌面",
        }],
        "start_state": {}, "end_state": {},
    }

    contract, _prompt = compile_shot_prompt(
        shot, location="现代卧室", mode="image")

    assert contract["camera"]["景别"] == "中景"
    assert "空间锚点修正" in contract["camera"]["容量修正"]
    assert contract["frame_props"][0]["visibility"] == "occluded"


def test_short_or_slow_move_camera_language_is_not_revived_as_fixed():
    for raw in ("35mm全景，短移", "50mm中景，缓移"):
        contract, _prompt = compile_shot_prompt({
            "camera": raw,
            "characters": ["甲"],
            "description": "人物保持原位，摄影机横向移动",
            "shot_contract": {"运镜": "固定"},
        }, mode="video")
        assert contract["camera"]["运镜"] == "移"


def test_hand_insert_does_not_reintroduce_face_or_full_body_geometry():
    shot = {
        "camera": "135mm微俯侧面手部大特写",
        "characters": ["甲", "乙"],
        "visible_figure_count": 2,
        "description": "仅框入甲的两指和乙的右腕，不出现完整人形",
        "frame_targets": {"keyframe": {
            "phase": "freeze", "state": "两指悬停在右腕上方",
            "fallback": False, "explicit": True}},
    }

    contract, prompt = compile_shot_prompt(shot, mode="image")

    assert contract["partial_body_framing"] is True
    assert "看见头顶" not in prompt
    assert "单侧眼睛" not in prompt
    assert "画面可见真人严格共2人" not in prompt
    assert "不生成完整真人" in prompt
    assert all(token in prompt for token in ("头部", "面孔", "躯干"))


def test_false_screen_carrier_without_text_does_not_create_computer_rule():
    shot = {
        "camera": "135mm手部大特写",
        "characters": ["甲"],
        "description": "只框手腕",
        "readable_text": {
            "required": False, "carrier": "屏幕", "whitelist": []},
        "frame_targets": {"keyframe": {
            "phase": "freeze", "state": "手腕保持静止",
            "fallback": False, "explicit": True}},
    }

    contract, prompt = compile_shot_prompt(shot, mode="image")

    assert all("电脑使用关系" not in rule
               for rule in contract["physical"]["rules"])
    assert "电脑使用关系" not in prompt


# ---------- 根因5:media_type 按真实字节 ----------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def test_sniff_image_media_trusts_bytes_over_suffix():
    assert sniff_image_media(PNG_BYTES) == "image/png"
    # 生产实证:.png 文件装 JPEG 字节 → 按后缀声明必 400。
    assert sniff_image_media(JPEG_BYTES, "image/png") == "image/jpeg"
    assert sniff_image_media(WEBP_BYTES, "image/png") == "image/webp"
    assert sniff_image_media(b"GIF89a" + b"\x00" * 10) == "image/gif"
    # 认不出的字节回落到后缀声明,不瞎猜。
    assert sniff_image_media(b"\x00" * 16, "image/webp") == "image/webp"
    assert sniff_image_media(b"", "image/png") == "image/png"


def test_qc_content_declares_actual_media_type(tmp_path):
    from aifos.production.api_providers import ClaudeApiProvider
    lying = tmp_path / "shot.keyframe.png"  # 后缀撒谎:装的是 JPEG
    lying.write_bytes(JPEG_BYTES)
    provider = ClaudeApiProvider.__new__(ClaudeApiProvider)
    content = provider._qc_content("检查这张图", {
        "image_uri": str(lying), "reference_manifest": []})
    image_blocks = [
        block for block in content if block.get("type") == "image"]
    assert image_blocks
    assert image_blocks[0]["source"]["media_type"] == "image/jpeg"


# ---------- 根因4:僵尸清扫 + 人工门禁 ----------

@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="卡住修复", number=1):
    app.director.produce(title, number, pause_for_confirm=True)
    summary = app.director.produce(title, number, pause_for_confirm=True)
    if summary["status"] == "awaiting_cast":
        project = app.projects.get_project(title)
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], number))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                title, number, character["name"], 1)
        app.director.produce(title, number, pause_for_confirm=True)
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    return project, episode


def _plan_path(app, project):
    return (app.workspace.artifacts_dir
            / f"p{project['id']:03d}" / "e001" / "render_plan.json")


def test_reconcile_resets_stale_generating_claims(app):
    """重启前被认领的 generating 条目必须在下一轮生产入口重置。"""
    project, episode = _preproduce(app)
    plan_path = _plan_path(app, project)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stale_ids = []
    for item in plan["items"]:
        if item["category"] == "shot_image" and len(stale_ids) < 2:
            item["status"] = "generating"
            stale_ids.append(item["id"])
        elif item["category"] == "frames" and len(stale_ids) < 3:
            item["status"] = "retrying"
            stale_ids.append(item["id"])
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "storyboard": storyboard}
    result = app.director.reconcile_completed_shot_images(ctx)

    assert result["stale_reset"] == len(stale_ids)
    refreshed = json.loads(plan_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in refreshed["items"]}
    for item_id in stale_ids:
        assert by_id[item_id]["status"] == "pending"
        reset = by_id[item_id]["stale_reset"]
        assert reset["previous_status"] in ("generating", "retrying")
        assert "中断遗留" in reset["reason"]


def test_reconcile_migrates_recoverable_awaiting_human_to_auto_retry(app):
    """有产物的 awaiting_human 迁回 pending 自动重排——不再占人工。

    这是新产线的契约:人工确认点不再由断点对账创建,失败稿只要文件还在
    就自动重新排队。旧契约(原样上报供人工)已被取代。默认选片模式关闭
    阻断式图片内容质检,因此旧红牌改成风险记录而不是继续计作失败。
    """
    project, episode = _preproduce(app)
    plan_path = _plan_path(app, project)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    gated = []
    for item in plan["items"]:
        if (item["category"] == "shot_image" and len(gated) < 2
                and item.get("output_uri")):
            item["status"] = "awaiting_human"
            item["qc"] = {"passed": False, "issues": ["测试失败原因"]}
            gated.append(int(item["id"].split(":")[1]))
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "storyboard": storyboard}
    result = app.director.reconcile_completed_shot_images(ctx)

    assert result["autonomous_retry"] == 2
    assert result["awaiting_human"] == 0
    assert result["awaiting_human_shots"] == []
    refreshed = json.loads(plan_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in refreshed["items"]}
    for shot_no in gated:
        item = by_id[f"shot:{shot_no}"]
        assert item["status"] == "pending"
        assert item["qc"]["awaiting_human"] is False
        assert item["qc"]["advisory_only"] is True
        assert item["qc"]["blocking"] is False
        assert item["content_qc_migration"]["previous_status"] \
            == "awaiting_human"


def test_reconcile_requeues_missing_awaiting_human_without_mobile_gate(app):
    """历史产物丢失也迁回 pending，由生成阶段自动补齐而非等待手机。

    断点对账本身不把“待重生”误报成 technical_incomplete；只有生成结束后
    确认 0 张技术可用，候选组才进入 technical_incomplete（另有候选组回归
    测试覆盖），同时其他镜头继续生产。
    """
    project, episode = _preproduce(app)
    plan_path = _plan_path(app, project)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    gated = []
    for item in plan["items"]:
        if item["category"] == "shot_image" and len(gated) < 2:
            item["status"] = "awaiting_human"
            item["output_uri"] = ""          # 产物已丢失:无法自动重排
            item["qc"] = {"passed": False, "issues": ["历史待人工"]}
            gated.append(int(item["id"].split(":")[1]))
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "storyboard": storyboard}
    result = app.director.reconcile_completed_shot_images(ctx)
    assert result["awaiting_human_shots"] == []
    assert result["awaiting_human"] == 0
    refreshed = json.loads(plan_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in refreshed["items"]}
    for shot_no in gated:
        item = by_id[f"shot:{shot_no}"]
        assert item["status"] == "pending"
        assert item["output_uri"] == ""
        assert item["qc"]["awaiting_human"] is False
        assert item["qc"]["blocking"] is False
        assert item["content_qc_migration"]["previous_status"] \
            == "awaiting_human"

def test_character_face_hidden_detection():
    from aifos.director import Director
    hidden_dict = {"visual_dna": {"face_structure": "背光剪影，面部处于阴影中不可见"}}
    hidden_str = {"visual_dna": "{'face_structure': '纱幕遮挡，不可见（剪影无细节）', 'hair': 'x'}"}
    hidden_prompt = {"image_prompt": "定妆母图：某人；脸部骨相：因背光剪影而完全不可见；发型轮廓：束发"}
    # 普通遮挡描述(刘海遮额头)绝不能误判成身份隐藏。
    normal = {"visual_dna": {"face_structure": "鹅蛋脸，刘海遮挡额头，双眼清晰"},
              "image_prompt": "定妆母图：沈眉；脸部骨相：鹅蛋脸颧骨柔和；"}
    assert Director.character_face_hidden(hidden_dict) is True
    assert Director.character_face_hidden(hidden_str) is True
    assert Director.character_face_hidden(hidden_prompt) is True
    assert Director.character_face_hidden(normal) is False
    assert Director.character_face_hidden(None) is False
    assert Director.character_face_hidden({}) is False


def test_sheet_suite_pruned_for_hidden_face(app):
    from aifos.director import (
        CHARACTER_SHEETS, FACE_DEPENDENT_SHEET_KEYS)
    d = app.director
    hidden = {"visual_dna": {"face_structure": "剪影，不可见"}}
    pruned = d._sheet_suite_for("纱幕后人", hidden, CHARACTER_SHEETS)
    kept = {row[0] for row in pruned}
    assert kept.isdisjoint(FACE_DEPENDENT_SHEET_KEYS)
    # 轮廓/服装类母资产必须保留——身份稳定仍然需要。
    assert {"front", "profile", "back", "costume"} <= kept
    # 可见面部角色套件原样不动。
    normal = {"visual_dna": {"face_structure": "鹅蛋脸，双眼清晰"}}
    assert d._sheet_suite_for("沈眉", normal, CHARACTER_SHEETS) \
        == CHARACTER_SHEETS


# ---------- 排除性约束必须逐字幸存审词 ----------

def test_exclusion_clauses_are_required_verbatim():
    """「严禁用实体遮蔽物」类条款被审词删掉 = 画面事实被放走。"""
    from aifos.production.router import ProviderRouter
    source = (
        "单人角色定妆母图：纱幕后人；面部处于阴影中不可见。"
        "严禁用任何实体遮蔽物实现：不得有面纱、头纱、帷帽、兜帽覆盖头面部；"
        "全身正面自然站姿，纯净中性深色棚拍背景。")
    tokens = ProviderRouter._prompt_review_required_tokens(
        source, {"characters": ["纱幕后人"]})
    joined = "；".join(tokens)
    assert "纱幕后人" in tokens
    assert "严禁用任何实体遮蔽物实现" in joined
    assert "不得有面纱、头纱、帷帽、兜帽覆盖头面部" in joined


def test_exclusion_capture_does_not_swallow_whole_prompt():
    """只收句子级排除条款,不把整篇写死(否则审词无法做任何优化)。"""
    from aifos.production.router import ProviderRouter
    source = "人物站立。不得漂浮。严禁出现现代装备与电子设备在画面里。"
    tokens = ProviderRouter._prompt_review_required_tokens(source, {})
    # 「不得漂浮」太短(<6字)不收,长条款收。
    assert not any(t == "不得漂浮" for t in tokens)
    assert any("严禁出现现代装备" in t for t in tokens)
    assert all(len(t) <= 90 for t in tokens)


# ---------- 景别 vs 构图 / 空间锚点 ----------

def test_environment_composition_dropped_on_tight_scale():
    """大特写装不下框中框:让构图,不动景别(景别承载导演意图)。"""
    from aifos.camera_language import enforce_composition_scale
    comp, note = enforce_composition_scale("大特写", "框中框")
    assert comp == "三分法" and "框中框" in note and "装不下" in note
    comp, note = enforce_composition_scale("特写", "引导线")
    assert comp == "三分法" and note
    # 中景及更宽装得下,原样不动。
    assert enforce_composition_scale("中景", "框中框") == ("框中框", "")
    assert enforce_composition_scale("全景", "引导线") == ("引导线", "")
    # 非环境类构图任何景别都成立。
    assert enforce_composition_scale("大特写", "留白") == ("留白", "")
    assert enforce_composition_scale("大特写", "前景遮挡") == ("前景遮挡", "")
    # 景别未知时不猜。
    assert enforce_composition_scale("", "框中框") == ("框中框", "")


def test_spatial_anchor_scale_upgrades_for_off_body_prop():
    from aifos.camera_language import enforce_spatial_anchor_scale
    scale, note = enforce_spatial_anchor_scale("大特写", 2)
    assert scale == "中景" and "空间锚点" in note
    # 单锚点(只有人物)不动。
    assert enforce_spatial_anchor_scale("大特写", 1) == ("大特写", "")
    assert enforce_spatial_anchor_scale("大特写", None) == ("大特写", "")
    # 已经够宽的不动。
    assert enforce_spatial_anchor_scale("全景", 3) == ("全景", "")


def test_spatial_anchor_count_ignores_held_props():
    """握在手里的道具随人入画,不额外占取景位;离身道具才占。"""
    from aifos.prompt_contract import _spatial_anchor_count
    held = {
        "characters": ["沈眉"],
        "frame_props": [{"prop_id": "p1", "visibility": "visible",
                         "holder": "沈眉"}],
    }
    assert _spatial_anchor_count(held) == 1
    off_body = {
        "characters": ["沈眉"],
        "frame_props": [{"prop_id": "p1", "visibility": "visible",
                         "holder": "none"}],
    }
    assert _spatial_anchor_count(off_body) == 2
    # 隐藏道具不占取景位。
    hidden = {
        "characters": ["沈眉"],
        "frame_props": [{"prop_id": "p1", "visibility": "hidden",
                         "holder": "none"}],
    }
    assert _spatial_anchor_count(hidden) == 1
    # 同一道具多行只算一次。
    duplicated = {
        "characters": ["沈眉"],
        "frame_props": [
            {"prop_id": "p1", "visibility": "visible", "holder": "none"},
            {"prop_id": "p1", "visibility": "occluded", "holder": "none"},
        ],
    }
    assert _spatial_anchor_count(duplicated) == 2


def test_compile_upgrades_scale_and_drops_long_lens_for_anchors():
    """《长夏记事》镜头3 实况:大特写×框中框×135mm + 桌上银铃。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {
        "shot_no": 3, "scene_no": 1, "kind": "beat",
        "camera": "大特写,俯拍,侧面",
        "characters": ["沈眉"],
        "description": "银铃脱离衣襟静止在书案上",
        "action": "沈眉视线下落至书案银铃",
        "frame_props": [{
            "prop_id": "prop_bell_001", "phase": "freeze",
            "visibility": "visible", "representation": "physical",
            "holder": "none", "location": "书案中心偏右",
            "support": "桌面", "physical_state": "完好"}],
        "five_dimensions": {"camera_design": {
            "shot_scale": "大特写", "lens": "135mm", "angle": "俯拍",
            "camera_position": "侧面", "composition": "框中框",
            "movement": "固定"}},
        "start_state": {}, "end_state": {},
    }
    contract, prompt = compile_shot_prompt(
        shot, location="书阁", style="古风", references=[], mode="image")
    camera = contract["camera"]
    assert camera["景别"] == "中景"
    assert camera["焦段"] == "35mm"       # 长焦不得残留
    assert camera["构图"] == "框中框"      # 中景装得下,构图保留
    assert "空间锚点" in camera["容量修正"]
    assert "容量修正" not in prompt        # 审计键不进提示词正文


def test_over_shoulder_needs_two_actors():
    """过肩=前景一人后脑肩背+远端另一人;独角戏构不成这种关系。"""
    from aifos.camera_language import enforce_position_capacity
    pos, note = enforce_position_capacity("过肩", 1)
    assert pos == "斜侧" and "构不成" in note
    pos, note = enforce_position_capacity("反打", 1)
    assert pos == "斜侧" and note
    # 两人及以上成立,原样不动。
    assert enforce_position_capacity("过肩", 2) == ("过肩", "")
    assert enforce_position_capacity("反打", 3) == ("反打", "")
    # 单人本就成立的机位不受影响。
    assert enforce_position_capacity("侧面", 1) == ("侧面", "")
    assert enforce_position_capacity("背面", 1) == ("背面", "")
    # 人数未知时不猜。
    assert enforce_position_capacity("过肩", None) == ("过肩", "")


def test_compile_swaps_over_shoulder_for_solo_shot():
    """《长夏记事》镜2 实况:1 人却被盲轮换配到过肩。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {
        "shot_no": 2, "scene_no": 1, "kind": "reaction",
        "camera": "过肩,俯拍", "characters": ["沈眉"],
        "description": "指尖触到别针，手指顿住",
        "action": "沈眉指尖停住",
        "five_dimensions": {"camera_design": {
            "shot_scale": "特写", "lens": "100mm", "angle": "俯拍",
            "camera_position": "过肩", "composition": "三分法",
            "movement": "固定"}},
        "start_state": {}, "end_state": {},
    }
    contract, _prompt = compile_shot_prompt(
        shot, location="书阁", style="古风", references=[], mode="image")
    assert contract["camera"]["机位"] == "斜侧"
    assert "机位容量修正" in contract["camera"]["容量修正"]


# ---------- 合同不一致不是硬伤:人工必须能放行 ----------

def _qc_verdict(*, image_pass=True, prompt_status="conflicting"):
    """画面全部达标,只有合同一致性不过。"""
    return {
        "pass": image_pass,
        "visual_pass": image_pass,
        "input_contract_pass": False,
        "identity_checked": True, "identity_match": True,
        "gender_checked": True, "gender_match": True,
        "count_checked": True, "count_match": True,
        "wardrobe_checked": True, "wardrobe_match": True,
        "physical_logic_checked": True, "physical_logic_match": True,
        "spatial_logic_checked": True, "spatial_logic_match": True,
        "issues": ["景别与合同不符：合同要求大特写，成片为中近景"],
        "image_error": {"summary": "画面本身达到放行阈值",
                        "categories": ["camera"], "evidence": []},
        "prompt_diagnosis": {"status": prompt_status, "issues": ["焦段互斥"]},
        "reference_diagnosis": {"status": "correct", "issues": []},
        "targeted_prompt_patch": {"instructions": [], "preserve": [],
                                  "max_scope": "current_shot_only"},
        "reference_adjustments": [],
    }


def test_contract_mismatch_alone_is_not_a_hard_failure(app):
    """画面达标、只有合同对不上时,不得判硬伤——否则错合同一票否决。"""
    report = app.director._assess_image_qc(
        {"characters": ["沈眉"], "count": 1},
        _qc_verdict(image_pass=True), 1)
    assert report["visual_pass"] is True
    assert report["input_contract_pass"] is False
    assert report["contract_hard_failure"] is True
    # 关键:人工仍然可以放行。
    assert report["hard_failure"] is False


def test_contract_mismatch_with_bad_image_stays_hard(app):
    """画面自身也没过时,合同不一致并入硬伤——那才是真要重画。"""
    report = app.director._assess_image_qc(
        {"characters": ["沈眉"], "count": 1},
        _qc_verdict(image_pass=False), 1)
    assert report["visual_pass"] is False
    assert report["hard_failure"] is True
