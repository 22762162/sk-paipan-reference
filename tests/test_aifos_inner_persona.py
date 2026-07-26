from aifos.app import App
from aifos.inner_persona import normalize_inner_persona_policy
from aifos.prompt_contract import compile_shot_prompt
from aifos.workflow import (
    build_continuity_bible,
    enrich_storyboard,
    production_profile,
)


SOURCE = "穿越者（现代之魂/朱慈烺前世）"
HOST = "朱慈烺"


def _script(asset_uri="/tmp/chibi-a.png"):
    return {
        "project_title": "大明",
        "episode_number": 2,
        "characters": [
            {
                "name": SOURCE,
                "role": "现代之魂",
                "introduction": (
                    "穿越前以现代真人出现；穿越后成为朱慈烺的内在意识，"
                    "不再拥有独立肉身。"),
            },
            {"name": "李继周", "role": "内侍"},
            {"name": HOST, "role": "太子"},
        ],
        "inner_persona_policy": {
            "enabled": True,
            "source_character": SOURCE,
            "host_character": HOST,
            "asset_name": f"{SOURCE}:chibi-a",
            "asset_uri": asset_uri,
        },
        "scenes": [{
            "scene_no": 1,
            "location": "现代书房→明代东宫寝殿",
            "characters": [SOURCE, "李继周", HOST],
            "action": "现代灵魂穿越后在东宫醒来",
            "lines": [
                {"character": SOURCE, "dialogue": "这盒子到底是什么？"},
                {"character": HOST, "dialogue": "先别声张。"},
            ],
        }],
    }


def _profile(tmp_path):
    app = App(tmp_path / "ws")
    try:
        return production_profile(app.config, app.standards.active())
    finally:
        app.close()


def test_inner_persona_is_non_physical_and_exaggerated(tmp_path):
    script = _script()
    profile = _profile(tmp_path)
    continuity = build_continuity_bible(
        {"title": "大明", "style": "3D重工国风"}, script, profile)
    raw = {"shots": [
        {
            "scene_no": 1,
            "kind": "dialogue",
            "description": "现代书房，穿越前的现代真人对着盒子自言自语",
            "characters": [SOURCE],
            "dialogue": {
                "character": SOURCE,
                "dialogue": "这盒子到底是什么？",
            },
        },
        {
            "scene_no": 1,
            "kind": "dialogue",
            "description": "穿越后，明代东宫寝殿内朱慈烺低声吩咐李继周",
            "characters": [HOST, "李继周"],
            "dialogue": {"character": HOST, "dialogue": "先别声张。"},
        },
        {
            "scene_no": 1,
            "kind": "reaction",
            "description": "现代之魂在内心夸张吐槽，气得原地跺脚",
            "characters": [SOURCE, HOST],
            "dialogue": None,
        },
    ]}
    storyboard = enrich_storyboard(
        script, raw, continuity, profile, style="3D重工国风")

    pre = next(
        shot for shot in storyboard["shots"]
        if (shot.get("dialogue") or {}).get("character") == SOURCE)
    assert pre["timeline_state"] == "pre_transition"
    assert pre["characters"] == [SOURCE]
    assert pre["narrative_overlays"] == []

    post = [
        shot for shot in storyboard["shots"]
        if shot.get("timeline_state") == "post_transition"]
    assert post
    assert all(SOURCE not in shot["characters"] for shot in post)
    # Ordinary listener reactions remain physical acting and do not receive a
    # Q insert after every line.
    ordinary_reactions = [
        shot for shot in post
        if shot.get("kind") == "reaction"
        and not shot.get("narrative_overlays")]
    assert ordinary_reactions
    assert ordinary_reactions[0]["characters"] == ["李继周"]

    inner = next(
        shot for shot in post if shot.get("narrative_overlays"))
    overlay = inner["narrative_overlays"][0]
    assert inner["characters"] == [HOST]
    assert inner["character_count"] == 1
    assert inner["visible_figure_count"] == 1
    assert len(inner["narrative_overlays"]) == 1  # Q版叠层另计，不计真人
    assert overlay["counts_as_real_character"] is False
    assert overlay["included_in_spatial_blocking"] is False
    assert overlay["visible_to"] == "host_only"
    assert overlay["historical_characters_may_react"] is False
    assert overlay["inherit_signature_props"] is False
    assert overlay["host_mouth_closed"] is True
    assert overlay["expression_style"] == "exaggerated"
    assert overlay["proportion_style"] == "oversized_head_tiny_body"
    assert overlay["total_height_in_heads"] == 1.8
    assert overlay["head_height_ratio"] == 0.58
    assert overlay["body_smaller_than_head"] is True
    assert overlay["asset_uri"] == "/tmp/chibi-a.png"
    assert SOURCE not in inner["start_state"]
    assert SOURCE not in {
        item.get("name")
        for item in inner["character_number_map"].values()}
    assert "非现实内心Q版叠层" in inner["seedance_prompt_compact"]
    assert "不计入上述1名真实主体" in inner["seedance_prompt_compact"]
    assert "表情和动作" in inner["seedance_prompt"]
    assert "约1.8头身" in inner["seedance_prompt"]
    assert "头占总高约58%" in inner["seedance_prompt"]
    assert "不继承任何默认道具" in inner["seedance_prompt"]
    assert "宿主闭口" in inner["seedance_prompt"]


def test_inner_persona_prompt_and_reference_role_are_explicit():
    policy = normalize_inner_persona_policy(_script())
    shot = {
        "characters": [HOST],
        "description": "朱慈烺表面镇定，内心疯狂吐槽",
        "narrative_overlays": [{
            "name": SOURCE,
            "host_character": HOST,
            "function": "inner_commentary",
            "expression": "抱头瞪眼，嘴形夸张",
            "action": "跳到宿主肩旁跺脚",
            "dialogue": "这也太离谱了！",
        }],
    }
    contract, prompt = compile_shot_prompt(
        shot, location="明代东宫寝殿",
        references=[{
            "index": 2,
            "label": "内心Q版母资产",
            "role": "inner_persona",
        }])

    assert policy["mode"] == "chibi_overlay"
    assert policy["auto_after_every_dialogue"] is False
    assert policy["expression_style"] == "exaggerated"
    assert policy["proportion_style"] == "oversized_head_tiny_body"
    assert policy["body_smaller_than_head"] is True
    assert contract["subject"]["count"] == 1
    assert len(contract["narrative_overlays"]) == 1
    assert "严格共1人" in prompt
    assert "非现实内心Q版叠层" in prompt
    assert "其他人物不得看见、回应、触碰、对视" in prompt
    assert "不继承默认道具" in prompt
    assert "约1.8头身" in prompt
    assert "图2=内心Q版母资产(内心Q版：" in prompt


def test_default_standard_locks_inner_persona_as_overlay(tmp_path):
    app = App(tmp_path / "ws")
    try:
        rules = app.standards.active()["content"]["rules"]["inner_persona"]
        assert rules["physical_presence"] is False
        assert rules["counts_as_real_character"] is False
        assert rules["included_in_spatial_blocking"] is False
        assert rules["historical_characters_may_react"] is False
        assert rules["auto_after_every_dialogue"] is False
        assert rules["expression_style"] == "exaggerated"
        assert rules["proportion_style"] == "oversized_head_tiny_body"
        assert rules["total_height_in_heads"] == 1.8
        assert rules["head_height_ratio"] == 0.58
        assert rules["body_smaller_than_head"] is True
        assert rules["host_mouth_closed_for_inner_voice"] is True
    finally:
        app.close()
