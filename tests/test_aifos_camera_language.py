"""镜头语言具象化:术语词典与镜头合同渲染注入。"""

from aifos.camera_language import camera_geometry_clause
from aifos.prompt_contract import compile_shot_prompt


def test_geometry_clause_translates_known_terms():
    clause = camera_geometry_clause(
        {"景别": "全景", "角度": "俯拍", "机位": "背面"})
    assert "按可见特征执行并核验" in clause
    assert "头顶到脚底完整入画" in clause          # 景别边界
    assert "头顶与双肩上表面" in clause            # 俯仰透视
    assert "不出现眉、眼、鼻、嘴" in clause        # 背面身份判据


def test_geometry_clause_skips_placeholders_and_unknown():
    # 「按分镜」「保持轴线」等默认占位不产出条款
    assert camera_geometry_clause(
        {"景别": "按分镜", "角度": "保持轴线", "机位": ""}) == ""
    assert camera_geometry_clause(None) == ""
    assert camera_geometry_clause({"角度": "荷兰角"}) == ""


def test_compiled_shot_prompt_carries_geometry_for_image_and_video():
    shot = {
        "shot_no": 1, "scene_no": 1, "kind": "dialogue",
        "description": "林川抬头看向房梁", "camera": "特写·仰拍",
        "duration": 2.5, "characters": ["林川"], "dialogue": None,
        "prompt": "p",
    }
    _contract, image_prompt = compile_shot_prompt(
        shot, location="驿馆内室", mode="image")
    assert "肩线以下出画" in image_prompt        # 特写边界
    assert "下颌底面与鼻底" in image_prompt      # 仰拍透视
    _contract, video_prompt = compile_shot_prompt(
        shot, location="驿馆内室", mode="video")
    assert "下颌底面与鼻底" in video_prompt      # 视频同一标准


def test_qc_feedback_camera_rule_references_visible_features():
    from aifos.qc_feedback import optimize_qc_feedback
    revision = optimize_qc_feedback(
        ["视角接近后上方俯视，不符合合同要求的仰拍"])
    assert "camera" in revision["categories"]
    assert "可见特征执行并核验" in revision["text"]


# ---- 场景多视角:机位映射与母版一致性合同 ----
def test_scene_view_mapping_from_camera():
    from aifos.camera_language import scene_view_for_camera
    assert scene_view_for_camera({"机位": "过肩"}) == "reverse"
    assert scene_view_for_camera({"机位": "背面"}) == "reverse"
    assert scene_view_for_camera("中景·背面跟拍") == "reverse"
    assert scene_view_for_camera({"机位": "侧面"}) == "side"
    assert scene_view_for_camera("全景·俯拍·推") == "main"
    assert scene_view_for_camera(None) == "main"      # 永不阻断


def test_scene_view_prompt_and_contract():
    from aifos.director import Director
    director = Director.__new__(Director)
    scene = {"location": "雨夜公寓单元房", "time_of_day": "深夜暴雨",
             "production_design": {"environment": "老式单元房,昏黄吸顶灯"}}
    prompt = director._scene_view_prompt(
        "雨夜公寓单元房", "写实悬疑", scene,
        "雨夜公寓单元房·反打视角", "反打视角",
        "从主视角正对面的机位回看同一空间")
    # 派发合同逐字校验对象名:提示词必须写出 art_name
    assert "【本图对象】雨夜公寓单元房·反打视角" in prompt
    assert "逐项一致" in prompt and "只允许摄影机位改变" in prompt
    context = director._scene_view_review_context(
        "雨夜公寓单元房", "写实悬疑", scene, "反打视角")
    assert "不构成需要裁决的冲突" in context["view_consistency_precedence"]
    assert "master_state_precedence" in context     # 空镜条款仍然在场


def test_shot_contract_declares_camera_precedence():
    """镜头合同必须带 camera_precedence 显式裁决:多套机位并存时
    以融合后的 camera 字段为唯一执行值(关键帧首熔断的点名要求)。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 7, "scene_no": 1, "kind": "action",
            "camera": "近景·俯拍·严格侧面", "description": "林川侧身窥视",
            "duration": 2.5, "characters": ["林川"], "dialogue": None,
            "prompt": "p"}
    contract, prompt = compile_shot_prompt(
        shot, location="屋檐下", mode="image")
    assert "唯一执行镜位" in contract["camera_precedence"]
    assert "不构成需要裁决的冲突" in contract["camera_precedence"]
    assert "唯一执行镜位(camera_precedence)" in prompt
