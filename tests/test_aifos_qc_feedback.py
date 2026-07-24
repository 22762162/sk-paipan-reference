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


def test_physical_logic_feedback_preserves_shot_boundary():
    result = optimize_qc_feedback(
        ["人物坐在笔记本屏幕后方却看到屏幕正面，电脑使用方向反了"],
        mode="image",
    )
    assert result["categories"] == ["physics"]
    assert "电脑/手机屏幕" in result["text"]
    assert "同一空间坐标" in result["text"]
