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


def test_physical_logic_feedback_preserves_shot_boundary():
    result = optimize_qc_feedback(
        ["人物坐在笔记本屏幕后方却看到屏幕正面，电脑使用方向反了"],
        mode="image",
    )
    assert result["categories"] == ["physics"]
    assert "电脑/手机屏幕" in result["text"]
    assert "同一空间坐标" in result["text"]


def test_revision_is_throttled_and_ranked_by_decisiveness():
    """一次塞十条改图意见会顾此失彼:实测镜头21 四轮 5→7→10 条,
    还撞出全新的身份漂移。改为按决定性程度排序、每轮只下发前 N 条。"""
    from aifos.qc_feedback import (MAX_REVISION_ISSUES, issue_severity,
                                   optimize_qc_feedback, rank_issues)
    issues = [
        "屏幕中提灯归属含混：观众难以判断是两名弓兵还是一人兼任",
        "画面可见真人只有6人，少了1名弓兵",
        "景别宽于合同：成片把靴子与地面全部收进",
        "输入合同自相冲突：构图合同写 林川=back，同一份提示词又要求正面",
        "阿砚左前胸的致命伤完全不可见，没有任何血迹",
        "阿砚被画成明显的少女",
    ]
    focus, deferred = rank_issues(issues)
    assert len(focus) == MAX_REVISION_ISSUES
    # 人数/性别是决定性事实,必须排在景别、构图这类瑕疵前面
    assert "少了1名弓兵" in focus[0]
    assert any("少女" in item for item in focus)
    assert all("景别" not in item for item in focus)
    # 输入自相矛盾改图治不好,不占修订名额
    assert all("自相冲突" not in item for item in focus)
    assert issue_severity("画面可见真人只有6人") < issue_severity("景别偏大")

    result = optimize_qc_feedback(issues, mode="image")
    assert len(result["issues"]) == MAX_REVISION_ISSUES
    assert result["deferred_issues"] == deferred
    assert "本轮不修" in result["text"]
    assert result["input_level_issues"]
    assert result["all_issues"] == issues
    # 限流可关闭(需要全量清单的调用方不受影响)
    assert len(optimize_qc_feedback(issues, limit=0)["issues"]) == len(issues)


def test_taxonomy_understands_varied_count_phrasings():
    """判官每轮措辞都不同,分类器过度字面化会让台账与跨轮比对全失真。"""
    from aifos.qc_stats import classify_issue
    for text in ("画面可见真人只有6人，少了1名弓兵",
                 "人数少一人：巡检弓兵只有3人",
                 "缺少『持木棍立于林川数步外』的第4名弓兵",
                 "多出一名人物"):
        assert classify_issue(text) == "figure_count", text
    assert classify_issue("阿砚被画成明显的少女") == "gender"
    assert classify_issue("小鹿被画成了动物") == "species"
    assert classify_issue("缺少道具描述") == "props"
