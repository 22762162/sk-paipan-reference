"""人物母资产状态净化与相位声明:12星座 cast 熔断三缺陷的回归测试。

缺陷1:净化器把「鞋面无泥污」洗成截断残句「鞋面无;」
缺陷2:相位条件态(腕带光色)无相位上下文,审核无法裁决
缺陷3:妆容板(正面半身)不含服装字段,三方规则互锁
"""

import pytest

from aifos.app import App
from aifos.director import Director
from aifos.production.router import ProviderRouter

# 12星座·林未 v2 设计资产中的真实文本(曾触发 j49 熔断)
REAL_COSTUME_DETAIL = (
    "旧羊毛外套肘部轻微磨亮，前襟用暗色小金属扣，内侧可见整齐手工补针和"
    "反复熨烫留下的浅折痕；衣领内侧缝有未闭合的银灰断线星轨，宽腿裤为"
    "耐磨棉混纺，靴底防滑且鞋面无泥污。"
)
REAL_PALETTE = (
    "主色为暗蓝、炭灰和旧黑，体现旧港劳动生活；点缀为衣领内侧银灰缝线与"
    "腕带状态光，认证失败时窄幅红色，共鸣后才转为低饱和银白。"
)


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _design():
    return {
        "species": "人类", "gender": "女", "age_range": "18岁",
        "appearance": "短而略方的下颌，清晰眉骨",
        "hair": "齐颌黑色短发，后颈收薄",
        "eyes": "黑褐瞳色", "makeup": "淡妆",
        "temperament": "克制、警觉",
        "costume": "短外套叠无袖针织内层，高腰宽腿裤形成上短下长轮廓",
        "costume_detail": REAL_COSTUME_DETAIL,
        "palette": REAL_PALETTE,
        "era_setting": "近未来东亚海港都市曜港",
    }


# ---- 缺陷1:否定排除整段剔除,不留截断残句 ----

def test_negated_state_exclusion_removed_as_whole():
    text = Director._sheet_stable_text(
        "宽腿裤为耐磨棉混纺，靴底防滑且鞋面无泥污")
    assert "靴底防滑" in text
    assert "鞋面无" not in text      # 不得留下截断残句
    assert "泥污" not in text
    assert "耐磨棉混纺" in text       # 前段稳定事实不受牵连


def test_other_negated_exclusions_removed_cleanly():
    text = Director._sheet_stable_text(
        "袖口同色修补线，无伤口、无包扎痕迹；发质干燥")
    assert "修补线" in text
    assert "无伤口" not in text and "无包扎" not in text
    assert "发质干燥" in text


def test_stable_negation_facts_survive():
    """「无纹样」「未闭合」是稳定外观事实,不得误删。"""
    text = Director._sheet_stable_text(
        "粗布长衫无纹样；衣领内侧缝有未闭合的银灰断线星轨")
    assert "无纹样" in text
    assert "未闭合的银灰断线星轨" in text


# ---- 缺陷2:相位条件态剔除 + 相位声明 ----

def test_phase_conditional_accessory_state_stripped():
    text = Director._sheet_stable_text(REAL_PALETTE)
    assert "主色为暗蓝" in text
    assert "腕带状态光" in text        # 配饰本体是稳定事实,保留
    assert "认证失败" not in text      # 条件段剔除
    assert "共鸣" not in text
    assert "窄幅红色" not in text
    assert "低饱和银白" not in text


def test_real_costume_detail_compiles_without_truncation():
    line = Director._sheet_design_line(
        _design(), "costume_detail", locked_look=None)
    assert "靴底防滑" in line
    assert "鞋面无" not in line
    assert "星轨" in line


def test_sheet_prompt_declares_base_phase(app):
    prompt = app.director._sheet_prompt(
        "林未", "主角", "电影级写实", "服装细节", "legacy",
        key="costume_detail", design=_design(), locked_look=None)
    assert "首次登场基础定妆" in prompt
    assert "未激活" in prompt and "未亮" in prompt
    assert "鞋面无" not in prompt
    assert "认证失败" not in prompt


# ---- 缺陷3:妆容板携带锁定服装 ----

def test_makeup_sheet_carries_locked_costume(app):
    prompt = app.director._sheet_prompt(
        "林未", "主角", "电影级写实", "妆容板", "legacy",
        key="makeup", design=_design(),
        locked_look={"costume": _design()["costume"]})
    assert "服装" in prompt
    assert "短外套叠无袖针织内层" in prompt
    # 妆容板本职字段仍在
    assert "底妆" in prompt or "淡妆" in prompt


# ---- 审核上下文透传相位 ----

def test_review_context_exposes_story_phase_and_base_state():
    payload = {
        "prompt_contract_complete": True,
        "prompt_contract": {"schema": "aifos.character-sheet/v2"},
        "characters": ["林未"],
        "story_phase": "首次登场基础定妆",
        "initial_character_state": "母版基准状态：无伤无污",
    }
    context = ProviderRouter._prompt_review_context("image", payload)
    assert context["story_phase"] == "首次登场基础定妆"
    assert context["initial_character_state"] == "母版基准状态：无伤无污"


def test_review_context_fallback_path_exposes_story_phase():
    payload = {
        "characters": ["林未"],
        "story_phase": "首次登场基础定妆",
        "initial_character_state": "母版基准状态：无伤无污",
    }
    context = ProviderRouter._prompt_review_context("image", payload)
    assert context["story_phase"] == "首次登场基础定妆"
    assert context["initial_character_state"] == "母版基准状态：无伤无污"
