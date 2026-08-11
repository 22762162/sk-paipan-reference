"""材质/灯光视觉标注必须可验证，且外观补齐不允许改三维几何。"""

from aifos.adapters.claude_script import (
    SCENE_APPEARANCE_PROMPT,
    validate_scene_annotation,
)


def _appearance(names=("书案", "木椅")):
    return {
        "objects": [{
            "name": name,
            "material": {
                "verified": True,
                "name": "深褐色木材",
                "base_color": "#5A3C29",
                "roughness": 0.7,
                "metalness": 0.02,
                "emissive_intensity": 0,
                "evidence": "可见木纹与暖色高光",
            },
        } for name in names],
        "lighting": {
            "verified": True,
            "ambient_intensity": 0.4,
            "key_intensity": 1.2,
            "color_temperature_k": 4300,
            "direction": {"x": -0.7, "y": -0.5, "z": 0.3},
            "evidence": "窗光投影方向与高光一致",
        },
    }


def test_appearance_only_contract_accepts_evidence_without_geometry():
    data = _appearance()

    assert validate_scene_annotation(
        data, appearance_only=True,
        expected_object_names=["书案", "木椅"]) == ""
    assert "严禁修改、增删或重命名" in SCENE_APPEARANCE_PROMPT


def test_appearance_only_contract_rejects_renaming_and_fake_direction():
    renamed = _appearance(("书案", "新椅子"))
    assert "名称、顺序和数量" in validate_scene_annotation(
        renamed, appearance_only=True,
        expected_object_names=["书案", "木椅"])

    zero_light = _appearance()
    zero_light["lighting"]["direction"] = {"x": 0, "y": 0, "z": 0}
    assert "不能全为0" in validate_scene_annotation(
        zero_light, appearance_only=True,
        expected_object_names=["书案", "木椅"])

