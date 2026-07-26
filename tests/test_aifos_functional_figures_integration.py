from aifos.adapters.claude_script import validate_storyboard
from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.workflow import (
    build_continuity_bible,
    enrich_storyboard,
    production_profile,
)


def _shot(**overrides):
    return {
        "scene_no": 1,
        "duration": 3,
        "prompt": "顾青回头，门边倒着两具尸体",
        "characters": ["顾青"],
        **overrides,
    }


def test_storyboard_validation_computes_real_people_without_overlays():
    board = {"shots": [_shot(
        functional_figures=[
            {"name": "倒地尸体", "count": 2, "state": "静止倒地"},
            {"label": "蒙面杀手", "count": 3, "function": "封锁出口"},
        ],
        narrative_overlays=[{
            "kind": "inner_persona_chibi", "name": "内心顾青",
        }],
    )]}

    assert validate_storyboard(board) is None
    assert board["shots"][0]["visible_figure_count"] == 6

    legacy = {"shots": [_shot()]}
    assert validate_storyboard(legacy) is None
    assert legacy["shots"][0]["functional_figures"] == []
    assert legacy["shots"][0]["visible_figure_count"] == 1

    fuzzy = {"shots": [_shot(functional_figures=[
        {"name": "一群杀手", "count": 3},
    ])]}
    assert "禁止模糊数量词" in validate_storyboard(fuzzy)


def test_workflow_preserves_functional_figures_and_video_mode(tmp_path):
    script = {
        "project_title": "功能人物集成",
        "episode_number": 1,
        "episode_title": "封门",
        "logline": "顾青发现出口被封。",
        "characters": [{"name": "顾青", "role": "主角"}],
        "scenes": [{
            "scene_no": 1,
            "location": "夜间仓库",
            "characters": ["顾青"],
            "action": "顾青回头看见杀手封门",
            "lines": [],
        }],
    }
    app = App(tmp_path / "ws")
    try:
        profile = production_profile(app.config, app.standards.active())
    finally:
        app.close()
    continuity = build_continuity_bible(
        {"title": "功能人物集成", "style": ""}, script, profile)
    board = enrich_storyboard(script, {"shots": [_shot(
        kind="physical",
        description="顾青回头，两名蒙面杀手封住仓库出口",
        functional_figures=[{
            "name": "蒙面杀手", "count": 2, "function": "封锁出口",
        }],
    )]}, continuity, profile)

    shot = board["shots"][0]
    assert shot["functional_figures"] == [{
        "name": "蒙面杀手", "count": 2, "function": "封锁出口",
    }]
    assert shot["character_count"] == 1
    assert shot["visible_figure_count"] == 3
    assert "登记角色1人" in shot["seedance_prompt"]
    assert "功能人物2人" in shot["seedance_prompt"]
    assert "实际可见真人严格共3人" in shot["seedance_prompt"]
    assert "图1是唯一动作起点" in shot["seedance_prompt_compact"]


def test_codex_keyframe_and_qc_use_total_real_people(tmp_path):
    population = {
        "counts": {
            "named_characters": 1,
            "functional_people": 2,
            "real_people_total": 3,
            "non_real_overlays": 1,
            "visible_entity_instances_total": 4,
        },
    }
    payload = {
        "shot_no": 7,
        "prompt_compact": "顾青回头，两名蒙面杀手封住出口",
        "prompt_contract_complete": True,
        "prompt_contract": {
            "population": population,
            "subject": {
                "registered_count": 1,
                "functional_count": 2,
                "visible_count": 3,
            },
        },
        "characters": ["顾青"],
        "character_count": 1,
        "functional_figures": [{
            "name": "蒙面杀手", "count": 2, "function": "封锁出口",
        }],
        "visible_figure_count": 3,
        "narrative_overlays": [{
            "kind": "inner_persona_chibi", "name": "内心顾青",
        }],
    }

    instruction, _, _ = build_instruction("image", payload, tmp_path)
    assert "登记角色1人" in instruction
    assert "功能人物2人" in instruction
    assert "画面可见真人严格共3人" in instruction
    assert "严格共1人" not in instruction

    app = App(tmp_path / "qc-ws")
    try:
        project, _ = app.projects.get_or_create_project("QC功能人物")
        spec = app.director._qc_spec(
            project["id"], ["顾青"], require_identity=False,
            expected_characters=["顾青"], expected_count=3,
            functional_figures=payload["functional_figures"])
    finally:
        app.close()
    assert spec["identity_characters"] == ["顾青"]
    assert spec["count"] == 3
    assert spec["functional_figures"] == payload["functional_figures"]

    qc_instruction, _, _ = build_instruction(
        "image_qc", {
            **spec,
            "image_uri": "/tmp/shot.png",
            "composition_contract": {
                "expected_visible_figure_count": 3,
            },
        }, tmp_path)
    assert "画面可见真人严格共3人" in qc_instruction
    assert "功能人物2人" in qc_instruction
    assert "身份与最终立绘核验只覆盖登记角色" in qc_instruction
