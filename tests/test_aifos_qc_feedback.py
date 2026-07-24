"""视觉质检原因到可执行返工提示词的编译器。"""

from aifos.qc_feedback import optimize_qc_feedback


def test_image_feedback_is_actionable_and_keeps_original_reasons():
    result = optimize_qc_feedback(
        ["小鹿被画成了动物", "多出一名人物", "字幕乱码"], mode="image")

    assert result["issues"] == ["小鹿被画成了动物", "多出一名人物", "字幕乱码"]
    assert {"species", "count", "text"} <= set(result["categories"])
    assert "【质检原因】" in result["text"]
    assert "【自动优化修订】" in result["text"]
    assert "严格按剧本声明的物种与身份生成" in result["text"]
    assert "删除多余人物" in result["text"]
    assert "删除字幕" in result["text"]
    assert "未提及内容保持不变" in result["text"]


def test_video_feedback_locks_first_and_last_frame_boundary():
    result = optimize_qc_feedback(["动作与尾帧不一致"], mode="video")

    assert result["mode"] == "video"
    assert result["categories"] == ["continuity"]
    assert "首帧/尾帧" in result["text"]
    assert "只让修订后的画面动起来" in result["text"]


def test_back_view_identity_feedback_does_not_force_a_front_face():
    result = optimize_qc_feedback(
        ["李继周仅以背面过肩角度入镜，正脸五官不可见，无法核实身份"],
        mode="image",
    )

    assert result["categories"] == ["nonface_visibility"]
    assert "禁止强迫背影人物转成正脸" in result["text"]
    assert "肩背体型" in result["text"]


def test_blank_laptop_screen_gets_explicit_readable_page_repair():
    result = optimize_qc_feedback(
        ["银色笔记本电脑屏幕呈空白冷白画面，未显示人物设定要求的《明季北略》崇祯页面。"],
        mode="image",
        readable_text={"required": True, "whitelist": ["明季北略", "崇祯"]},
    )

    assert result["categories"] == ["screen_text"]
    assert "TEXT ASSET HARD GATE" in result["text"]
    assert "明季北略、崇祯" in result["text"]
    assert "禁止空白冷白屏" in result["text"]


def test_structured_diagnosis_uses_short_patch_without_dumping_analysis():
    diagnostics = {
        "pass": False,
        "image_error": {
            "summary": "主角服装错误",
            "categories": ["wardrobe"],
            "evidence": ["画面中的长袍颜色错误"],
        },
        "prompt_diagnosis": {
            "status": "needs_patch",
            "issues": ["服装颜色没有明确"],
            "irrelevant_or_conflicting_sections": [
                "这是一段很长的整集背景分析，不应交给图片模型"],
        },
        "reference_diagnosis": {
            "status": "correct", "issues": [], "missing_roles": []},
        "targeted_prompt_patch": {
            "instructions": ["只把主角长袍改为深青色"],
            "preserve": ["人物脸", "构图", "场景"],
        },
        "reference_adjustments": [],
    }
    result = optimize_qc_feedback(
        ["主角服装颜色错误"], mode="image", diagnostics=diagnostics)

    assert result["diagnosis_complete"] is True
    assert result["text"] == result["targeted_prompt_patch"]
    assert "只把主角长袍改为深青色" in result["text"]
    assert "只修改当前镜头" in result["text"]
    assert "很长的整集背景分析" not in result["text"]
    assert result["reference_adjustments"] == []
