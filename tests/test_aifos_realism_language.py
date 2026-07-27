"""真实感层(sk-zhengren 移植):画风门控与三个人物面注入。"""

from aifos.director import Director
from aifos.realism_language import (
    NEGATIVE_CONTROLS, REALISM_CLAUSE, realism_applicable, realism_lines)


def test_style_gating():
    assert realism_applicable("电影级半写实3D渲染质感明初古风")
    assert realism_applicable("写实悬疑")
    assert realism_applicable("photorealistic cinematic")
    assert not realism_applicable("Q版二次元卡通")
    assert not realism_applicable("国风水墨手绘")
    assert not realism_applicable("")            # 未指定画风不强加
    assert realism_lines("Q版") == []
    assert realism_lines("写实") == [REALISM_CLAUSE, NEGATIVE_CONTROLS]


def test_realism_layer_on_character_surfaces():
    director = Director.__new__(Director)
    style = "写实悬疑"
    candidate = director._candidate_portrait_prompt(
        "林川", "主角", style, None,
        {"variant_label": "基准", "variant_focus": "x",
         "look_variant": {}, "story_variant": {}})
    portrait = director._portrait_prompt("林川", "主角", style, None)
    sheet = director._sheet_prompt(
        "林川", "主角", style, "正面", "desc", key="front")
    for prompt in (candidate, portrait, sheet):
        assert "【真实感】" in prompt
        assert "【真实感负面】" in prompt
        assert "瓷白眼白" in prompt              # 死眼判据在场
    q_portrait = director._portrait_prompt("小鹿", "主角", "Q版卡通", None)
    assert "【真实感】" not in q_portrait          # 非写实零污染
