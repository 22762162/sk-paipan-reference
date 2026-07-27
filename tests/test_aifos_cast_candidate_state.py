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
    assert "出厂/初始的完整形态" in prompt
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


def test_prop_master_declares_precedence_over_fact_source():
    """事实源把外包装/破损写进视觉结构时,母版必须给出确定裁决。

    审核规则禁止猜测:只有明确宣告"母版规则优先于事实源相应描述",
    才不会因「油纸外包 vs 无包装」判定冲突而熔断整批道具图。
    """
    prompt = _prop_prompt({
        "name": "江浦县主簿吏部札付原件",
        "story_function": "把林川与赴任官员身份强行绑定",
        "visual_design": "米黄色桑皮纸札付，纵向折为三折，正文墨书楷体，"
                          "落有吏部朱印；外包一层边角发白的半透明油纸，"
                          "右下角有一道旧折裂。",
        "era_material": "手工桑皮纸、松烟墨"})
    assert "优先级最高" in prompt
    assert "不需要在两者之间做取舍" in prompt
    for token in ("外包装", "污渍", "血迹", "破损", "折裂"):
        assert token in prompt, f"母版未点名排除 {token!r}"
    # 稳定工艺事实仍在
    assert "桑皮纸" in prompt and "朱印" in prompt


def test_character_master_declares_precedence_over_fact_source():
    """人物母版同理:泥渍/血迹/姿态冲突必须由优先级条款裁决。"""
    from aifos.director import CHARACTER_CANDIDATE_PROMPT_SCHEMA
    # schema 版本必须随裁决条款升级,否则旧提示词会被继续复用
    assert "v4" in CHARACTER_CANDIDATE_PROMPT_SCHEMA


def test_scene_master_context_excludes_drama_facts():
    """空镜合同只带环境事实;场次人物/尸体/动作绝不入合同。

    《雨夜凶杀》独立资产批次熔断:「镜头局部合同中的人物与尸体出镜
    要求,和同属镜头局部合同的空镜要求相互排斥」——正是 action 被
    装进了空镜合同。
    """
    director = Director.__new__(Director)
    scene = {"location": "雨夜公寓单元房", "time_of_day": "深夜暴雨",
             "action": "林川推门发现书童尸体倒在血泊中,黑衣人跃出",
             "production_design": {"environment": "老式单元房,昏黄吸顶灯",
                                    "lighting": "单一顶光,窗外闪电"}}
    context = director._scene_art_review_context(
        "雨夜公寓单元房", "写实悬疑", scene)
    blob = str(context)
    # 禁止条款里合法出现裸词「尸体」("画面绝不出现…尸体");
    # 只断言戏剧原文短语没有混入。
    for drama in ("尸体倒在", "血泊", "黑衣人", "推门", "跃出"):
        assert drama not in blob, f"戏剧事实 {drama!r} 混入空镜合同"
    assert "昏黄吸顶灯" in blob            # 环境事实保留
    assert "优先级最高" in context["master_state_precedence"]
    assert context["time_and_weather"] == "深夜暴雨"
