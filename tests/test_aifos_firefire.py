from aifos.app import App
from aifos.errors import AifosError


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
            negative_prompt="字幕，logo，水印")
        assert style["status"] == "draft"
        published = app.firefire.publish_style(style["id"], approved_by="tester")
        assert published["status"] == "approved"
        assert published["validation"]["human_confirmed"] is True
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
    finally:
        app.close()
