from aifos.app import App
from aifos.story_analysis import apply_story_analysis, build_story_analysis
from aifos.style_director import (
    compile_director_style,
    director_ready,
    extract_director_knowledge,
    select_shot_direction,
    strip_director_knowledge,
)
from aifos.workflow import (
    _camera_plan,
    build_continuity_bible,
    enrich_storyboard,
    production_profile,
)


KNOWLEDGE = {
    "shot_language": {
        "shot_patterns": ["过肩近景缓推", "手部大特写后轻拉"],
        "shot_scales": ["近景", "特写", "大特写"],
        "camera_angles": ["平视", "俯拍"],
        "camera_positions": ["过肩", "斜侧"],
        "lenses": ["85mm", "135mm"],
        "camera_moves": ["推", "拉", "固定"],
        "compositions": ["前景遮挡", "三分法"],
        "transitions": ["视线匹配硬切"],
        "rhythm": ["一镜一个微动作"],
        "forbidden": ["高速甩镜"],
    },
    "visual_effects": {
        "lighting": ["45度暖金侧光"],
        "atmosphere": ["沉香烟雾"],
        "optical": ["二分之一黑柔", "极浅景深"],
        "color_grade": ["低饱和鎏金调"],
        "materials": ["旧银与绣纹布独立高光"],
        "particles": ["细碎金色光尘"],
        "post_process": ["暖色高光晕散"],
        "forbidden": ["特效遮脸"],
    },
    "selection_rules": [{
        "when": "暧昧对白",
        "shots": ["过肩近景缓推"],
        "effects": ["45度暖金侧光", "沉香烟雾"],
        "purpose": "缩短人物关系距离",
    }],
}


def _script():
    return {
        "project_title": "风格导演测试",
        "episode_number": 1,
        "logline": "两名成年人在古室试探彼此",
        "characters": [
            {"name": "沈昭璃", "role": "主角", "gender": "女",
             "age_range": "约26岁成年女性"},
            {"name": "谢观澜", "role": "重要配角", "gender": "男",
             "age_range": "约30岁成年男性"},
        ],
        "scenes": [{
            "scene_no": 1,
            "location": "暖金古室",
            "characters": ["沈昭璃", "谢观澜"],
            "action": "两人隔案试探",
            "lines": [],
        }],
    }


def test_director_knowledge_round_trip_and_story_analysis_separation():
    payload = compile_director_style("鎏金柔雾写实古风", KNOWLEDGE)

    assert director_ready(extract_director_knowledge(payload))
    assert strip_director_knowledge(payload) == "鎏金柔雾写实古风"

    analysis = build_story_analysis(_script(), payload)
    assert analysis["visual"]["user_style_constraint"] == "鎏金柔雾写实古风"
    assert analysis["visual"]["director_knowledge"]["schema"] == (
        "firefire.director-style/v1")
    assert "过肩近景缓推" in analysis["visual"]["camera_language"]
    assert "沉香烟雾" in analysis["visual"]["visual_effect_language"]
    assert "AIFOS_STYLE_DIRECTOR" not in analysis["prompt_bible"][
        "global_image_prefix"]

    restyled = build_story_analysis(
        _script(), "玄曜东方3D CG", raw=analysis)
    assert restyled["visual"]["director_knowledge"][
        "shot_language"]["shot_patterns"] == []
    assert "过肩近景缓推" not in restyled["visual"]["camera_language"]
    assert "沉香烟雾" not in restyled["visual"]["visual_effect_language"]


def test_camera_plan_uses_selected_styles_own_camera_library():
    plan = _camera_plan(
        "", "dialogue", 1, rules={},
        visible_count=2, director_knowledge=KNOWLEDGE)

    assert plan["shot_scale"] in {"近景", "特写", "大特写"}
    assert plan["camera_position"] in {"过肩", "斜侧"}
    assert plan["lens"] in {"85mm", "135mm"}
    assert plan["movement"] in {"推", "拉", "固定"}
    assert plan["composition"] in {"前景遮挡", "三分法"}


def test_shot_direction_uses_matching_style_rule_instead_of_cycling_library():
    direction = select_shot_direction(
        KNOWLEDGE, 2,
        raw={"kind": "dialogue", "description": "两名成年人暧昧试探"},
        kind="dialogue")

    assert direction["shot_pattern"] == "过肩近景缓推"
    assert direction["visual_effects"] == ["45度暖金侧光", "沉香烟雾"]
    assert direction["selection_reason"] == "缩短人物关系距离"


def test_style_director_reaches_five_dimension_and_seedance_prompts(tmp_path):
    script = _script()
    analysis = build_story_analysis(
        script, compile_director_style("鎏金柔雾写实古风", KNOWLEDGE))
    apply_story_analysis(script, analysis)
    app = App(tmp_path / "ws")
    try:
        profile = production_profile(app.config, app.standards.active())
    finally:
        app.close()
    continuity = build_continuity_bible(
        {"title": "风格导演测试", "style": "鎏金柔雾写实古风"},
        script, profile)
    board = enrich_storyboard(script, {"shots": [{
        "scene_no": 1,
        "kind": "dialogue",
        "description": "沈昭璃与谢观澜隔案暧昧试探",
        "characters": ["沈昭璃", "谢观澜"],
        "dialogue": {"character": "沈昭璃", "dialogue": "你在躲我？"},
    }]}, continuity, profile, style="鎏金柔雾写实古风")
    shot = board["shots"][0]

    assert board["style_director_schema"] == "firefire.director-style/v1"
    assert shot["style_direction"]["shot_pattern"] == "过肩近景缓推"
    assert shot["style_direction"]["visual_effects"] == [
        "45度暖金侧光", "沉香烟雾"]
    assert shot["five_dimensions"]["camera_design"]["lens"] in {
        "85mm", "135mm"}
    assert "【风格镜头】过肩近景缓推" in shot["seedance_prompt"]
    assert "【风格导演执行】" in shot["seedance_prompt_compact"]
