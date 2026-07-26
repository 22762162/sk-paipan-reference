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


def test_merge_patch_supersedes_single_voice_sections():
    """定向修正必须落进镜头/文字/主动作段并替换旧口径,不得新旧并存。

    回归背景:修正原来只贴在提示词尾部,合同段里的旧"顶拍"和补丁里的
    "不要再写顶拍"同时发给模型,违反规则治理的唯一优先级原则。"""
    from aifos.qc_feedback import merge_patch_into_prompt, render_patch_text

    prompt = (
        "【镜头合同v2】只执行下列事实，不自行补剧情。\n"
        "【主体】严格共1人：P01=林川（……）（均为真实人物）。\n"
        "【单一主动作】黑场延续半拍以远处车轮声过渡；林川猛然惊醒坐起。\n"
        "【物理/空间逻辑】林川平躺于木质床榻；空间调度机位：正面。\n"
        "【镜头】近景；顶拍；85mm；正面；静态关键帧只定格最终可见机位与构图；构图中心。\n"
        "【终点】林川:驿舍床榻坐起，情绪错愕惊疑。\n"
        "【文字】无画面文字、无字幕、无Logo、无水印。\n"
        "【硬约束】只执行一个主动作和一个运镜。")
    instructions = [
        "静态关键帧只定格终点：林川已坐起，一手撑床板，低头看官袍。",
        "机位唯一设为床榻侧前方高位俯拍的正面偏四分之三近景，85mm；"
        "不要再写垂直顶拍。",
        "官府任命文书仅1份，平放床榻旁靠枕一侧地面并由地面自然承托，"
        "禁止放到人物手中。",
    ]
    merged, leftover, superseded = merge_patch_into_prompt(
        prompt, instructions)
    # 镜头/主动作整段替换:旧口径消失,新口径成为该段唯一内容
    assert "顶拍；85mm；正面" not in merged
    assert "【镜头】机位唯一设为床榻侧前方高位俯拍" in merged
    assert "黑场延续半拍" not in merged
    assert "【单一主动作】静态关键帧只定格终点" in merged
    assert superseded == ["单一主动作", "镜头"]
    # 多事实段(物理/道具)不整段替换,指令保守留在尾部补丁
    assert "林川平躺于木质床榻" in merged
    assert leftover == [instructions[2]]
    tail = render_patch_text(leftover, ["林川身份"])
    assert "【本镜定向修正】官府任命文书仅1份" in tail
    assert "【保持不变】林川身份" in tail
    assert "【范围】只修改当前镜头" in tail


def test_merge_patch_is_conservative_on_ambiguous_instructions():
    from aifos.qc_feedback import merge_patch_into_prompt

    prompt = "【镜头】近景；顶拍。\n【文字】无画面文字。"
    # 镜头与文字命中数打平的指令不落段;无段标记的提示词也不落段
    ambiguous = ["改为俯拍构图，同时删除画面文字和水印"]
    merged, leftover, superseded = merge_patch_into_prompt(prompt, ambiguous)
    assert merged == prompt and leftover == ambiguous and superseded == []
    merged, leftover, superseded = merge_patch_into_prompt(
        "没有任何段标记的普通提示词", ["机位唯一设为俯拍近景"])
    assert merged == "没有任何段标记的普通提示词"
    assert leftover == ["机位唯一设为俯拍近景"] and superseded == []


def test_auto_feedback_supersedes_previous_auto_patch():
    """重画 feedback:新自动补丁取代旧自动补丁,人工手写意见保留。

    回归背景:旧实现无差别拼接,上一轮"改成A"与这一轮"改成B"会在
    尾部同时在场;硬截断还会把指令拦腰切断。"""
    from aifos.director import Director

    old = ("人工备注:官袍颜色再灰一点\n"
           "【本镜定向修正】机位改为顶拍\n【保持不变】人物身份\n"
           "【范围】只修改当前镜头")
    patch = "【本镜定向修正】机位改为侧前方高位俯拍\n【范围】只修改当前镜头"
    merged = Director._merge_auto_feedback(old, patch)
    assert "机位改为顶拍" not in merged
    assert "机位改为侧前方高位俯拍" in merged
    assert "人工备注:官袍颜色再灰一点" in merged
    # 截断退回句边界,不把指令拦腰切断
    long_patch = "【本镜定向修正】" + "修正甲；" * 400
    truncated = Director._merge_auto_feedback("", long_patch, limit=100)
    assert len(truncated) <= 100 and truncated.endswith("；")


def test_generation_seed_is_stable_per_asset_version():
    """seed 只随资产身份+版本变化,不随提示词措辞变化。"""
    from aifos.director import Director

    base = {"shot_no": 7, "revision": 1, "prompt": "版本A提示词"}
    reworded = {"shot_no": 7, "revision": 1, "prompt": "措辞完全不同的版本B"}
    assert (Director._derive_generation_seed(base)
            == Director._derive_generation_seed(reworded))
    assert (Director._derive_generation_seed(base)
            != Director._derive_generation_seed(
                {"shot_no": 7, "revision": 2}))
    assert 0 <= Director._derive_generation_seed(base) < 2147483647
