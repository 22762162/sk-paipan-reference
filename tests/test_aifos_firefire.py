import pytest

from aifos.app import App
from aifos.errors import AifosError


DIRECTOR_KNOWLEDGE = {
    "shot_language": {
        "shot_patterns": ["过肩近景缓推", "人物反应特写"],
        "shot_scales": ["近景", "特写"],
        "camera_angles": ["平视"],
        "camera_positions": ["过肩"],
        "lenses": ["85mm"],
        "camera_moves": ["推", "固定"],
        "compositions": ["前景遮挡"],
        "transitions": ["视线匹配硬切"],
        "rhythm": ["一镜一个动作"],
        "forbidden": ["无动机环绕"],
    },
    "visual_effects": {
        "lighting": ["冷色逆光"],
        "atmosphere": ["雨雾"],
        "optical": ["浅景深"],
        "color_grade": ["冷暖对撞"],
        "materials": ["湿地反光"],
        "particles": [],
        "post_process": ["轻微高光晕染"],
        "forbidden": ["特效遮脸"],
    },
    "selection_rules": [{
        "when": "夜景对白",
        "shots": ["过肩近景缓推"],
        "effects": ["冷色逆光", "雨雾"],
        "purpose": "强化人物距离与夜色情绪",
    }],
}


def test_firefire_learning_session_evidence_and_human_style_publish(tmp_path):
    app = App(tmp_path)
    try:
        assert app.firefire.overview()["name"] == "火火漫剧研究室"
        session = app.firefire.create_session(
            name="夜戏案例", source_url="https://example.com/case",
            rights_confirmed=True)
        assert session["status"] == "queued"
        app.firefire.start_analysis(session["id"])
        evidence = app.firefire.add_evidence(
            session["id"], kind="style", label="夜景近景",
            uri="/tmp/reference.png", timecode="00:01:12",
            observation="冷色逆光、人物脸部清晰")
        assert evidence["timecode"] == "00:01:12"

        style = app.firefire.create_style(
            name="夜色玻璃糖", session_id=session["id"],
            summary="适合夜戏和都市情绪场",
            compiled_style="现代漫剧夜景，冷暖对撞，人物脸部清晰，禁止文字水印",
            positive_prompt="冷色逆光",
            negative_prompt="字幕，logo，水印",
            director_knowledge=DIRECTOR_KNOWLEDGE)
        assert style["status"] == "draft"
        assert style["director_ready"] is True
        assert style["director_counts"]["selection_rules"] == 1
        assert "<AIFOS_STYLE_DIRECTOR_KNOWLEDGE>" in style[
            "director_prompt"]
        published = app.firefire.publish_style(style["id"], approved_by="tester")
        assert published["status"] == "approved"
        assert published["validation"]["human_confirmed"] is True
        analysis_style = app.director._style_with_director_knowledge(
            published["compiled_style"], published["id"])
        assert analysis_style.startswith(published["compiled_style"])
        assert "<AIFOS_STYLE_DIRECTOR_KNOWLEDGE>" in analysis_style
        assert app.firefire.get_style(style["id"], approved_only=True)["id"] == style["id"]
    finally:
        app.close()


def test_firefire_requires_rights_for_analysis_and_draft_for_publish(tmp_path):
    app = App(tmp_path)
    try:
        session = app.firefire.create_session(
            name="待确认来源", source_url="https://example.com")
        try:
            app.firefire.start_analysis(session["id"])
        except AifosError as exc:
            assert "权" in str(exc)
        else:
            raise AssertionError("未确认权利的学习会话不应进入分析")
        style = app.firefire.create_style(
            name="只有总提示词",
            compiled_style="电影感写实风格")
        with pytest.raises(AifosError, match="结构化导演知识"):
            app.firefire.publish_style(style["id"], approved_by="tester")
    finally:
        app.close()
