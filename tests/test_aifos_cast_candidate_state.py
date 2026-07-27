"""人物定妆母版:一次性剧情状态(湿/泥/伤/姿态)必须剥离。

定妆母版硬规定「全身正面自然站姿」。把场次里的失去支撑、跪地、
搀扶等动作姿态写进母版,会与该硬规则直接矛盾,提示词审核禁止
猜测取舍只能熔断,整批人物图卡死(《雨夜凶杀》cast 阶段两次熔断
的真实成因)。稳定人物设计(材质、磨旧、旧疤、配饰)必须完整保留。
"""

import pytest

from aifos.director import Director

clean = Director._clean_candidate_initial_state_text


@pytest.mark.parametrize("text, gone, kept", [
    # 真实熔断语料:审核报「姿态与遇害动作状态直接冲突」
    ("青灰粗布长衫，正面全身可见但正在失去支撑的护包姿态，怀中紧抱蓝布包袱",
     ("失去支撑", "护包", "紧抱"), ("青灰粗布长衫",)),
    ("土黄短打，跪地扶住伤口，面色苍白",
     ("跪地", "扶住", "伤口"), ("土黄短打",)),
    ("灰褐布衣被暴雨浸透，沾黄泥水，踉跄着俯身",
     ("浸透", "泥水", "踉跄", "俯身"), ("灰褐布衣",)),
    ("月白襦裙，蜷缩在墙角，肩头颤抖着",
     ("蜷缩", "颤抖"), ("月白襦裙",)),
    ("玄色劲装，转身伸手指向门外",
     ("转身", "伸手", "指向"), ("玄色劲装",)),
])
def test_transient_states_and_postures_are_stripped(text, gone, kept):
    result = clean(text)
    for token in gone:
        assert token not in result, f"{token!r} 未被剥离: {result}"
    for token in kept:
        assert token in result, f"稳定设计 {token!r} 被误删: {result}"


@pytest.mark.parametrize("text", [
    "藏青长衫，磨旧下摆，腰间旧革带",
    "赭石色圆领袍，肘部补丁，左眉旧疤",
    "靛蓝粗布短褐，草绳束腰，木簪挽发",
])
def test_stable_design_survives_untouched(text):
    """稳定人物设计不含一次性状态时必须一字不改。"""
    assert clean(text) == text


def test_empty_and_none_are_safe():
    assert clean(None) == ""
    assert clean("") == ""
    assert clean("，，；") == ""


# ---- 核心道具母版:同一段提示词不得自相矛盾 ----
def _prop_prompt(prop):
    director = Director.__new__(Director)
    return director._prop_candidate_prompt(
        prop, "写实历史", {"variant_label": "方案1",
                           "variant_focus": "轮廓与工艺"})


def test_prop_master_drops_scene_continuity_states():
    """连续性状态是场次事实,写进母版会与「未包装」约束互斥并熔断。"""
    prompt = _prop_prompt({
        "name": "吏部札付", "story_function": "冒名入仕的凭证",
        "visual_design": "折叠公文，骑缝印",
        "era_material": "明代桑皮纸、朱印", "owner": "林川",
        "continuity_states": "初始油纸外包→雨中拆开→沾血"})
    for token in ("油纸外包", "拆开", "沾血"):
        assert token not in prompt, f"场次状态 {token!r} 混入母版"
    assert "未包装" in prompt and "未拆封" in prompt
    # 稳定道具事实必须保留
    assert "骑缝印" in prompt and "桑皮纸" in prompt


def test_text_bearing_prop_never_invents_document_text():
    """文书类母版不得编造正文——正文由「锁文字」阶段统一定版。"""
    prompt = _prop_prompt({
        "name": "吏部札付", "visual_design": "折叠公文",
        "era_material": "明代桑皮纸"})
    assert "不可辨读" in prompt
    assert "禁止编造" in prompt
    # 旧的「无新增文字」与文书本体矛盾,不应再出现
    assert "无新增文字" not in prompt


def test_plain_prop_keeps_no_text_rule():
    """非文字载体道具仍禁止任何画面文字,且不套用文书规则。"""
    prompt = _prop_prompt({
        "name": "铜匕首", "visual_design": "短刃、缠绳柄",
        "era_material": "青铜"})
    assert "无新增文字" in prompt
    assert "不可辨读" not in prompt
