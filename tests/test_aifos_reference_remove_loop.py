"""QC 参考图移除指令不生效的回归测试(12星座 shot 8 修复轮空事故)。

事故形态:QC 连续多轮下达「彻底移除 index 3 干净机位空间图」指令,
平台每轮跳过,下一轮原样重发 → 同一冲突反复熔断。三个叠加缺陷:
1. 文字指令解析器只认「图3」,不认 Codex 常用的「index 3/index=3」;
2. 结构化动作与文字动作去重时丢失 _codex_explicit 显式标记;
3. 诊断冲突豁免的角色表只认整词 prop/spatial/scene,
   漏掉 spatial_scene_clean 等空间族变体。
"""

import pytest

from aifos.app import App


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _payload():
    return {
        "reference_manifest": [
            {"index": 1, "role": "identity", "character": "林未",
             "uri": "/ws/cast/portrait_林未.png", "asset_id": 8649},
            {"index": 2, "role": "spatial",
             "uri": "/ws/blocking/shot8.png", "asset_id": 8764},
            {"index": 3, "role": "spatial_scene_clean",
             "uri": "/ws/blocking/shot8_clean.png", "asset_id": 8765},
            {"index": 4, "role": "prop",
             "uri": "/ws/props/wristband.png", "asset_id": 8655},
        ],
        "reference_images": [
            "/ws/cast/portrait_林未.png", "/ws/blocking/shot8.png",
            "/ws/blocking/shot8_clean.png", "/ws/props/wristband.png",
        ],
        "asset_matches": [
            {"uri": "/ws/cast/portrait_林未.png", "reference_role": "identity"},
            {"uri": "/ws/blocking/shot8.png", "reference_role": "spatial"},
            {"uri": "/ws/blocking/shot8_clean.png",
             "reference_role": "spatial"},
            {"uri": "/ws/props/wristband.png", "reference_role": "prop"},
        ],
    }


def _diagnostics(status="conflicting"):
    return {
        "schema": "aifos.generation-diagnostics/v1",
        "reference_diagnosis": {"status": status},
        "reference_adjustments": [
            {"action": "keep", "target_index": 1},
            {"action": "remove", "target_index": 3,
             "role": "spatial_scene_clean",
             "reason": "返工合同要求从参考输入与 mandatory 集合移除 index 3"},
        ],
    }


REAL_INSTRUCTION = (
    "从本轮参考输入数组及mandatory集合中彻底移除index 3「本镜干净机位空间图」"
    "，不得添加替代参考；仅保留index 1、2、4、5及其原用途绑定。")


def test_instruction_parser_understands_index_phrasing(app):
    actions = app.director._codex_instruction_reference_actions(
        REAL_INSTRUCTION, _payload())
    removes = [a for a in actions if a["action"] == "remove"]
    assert [a["target_index"] for a in removes] == [3]
    assert removes[0]["_codex_explicit"] is True

    variant = app.director._codex_instruction_reference_actions(
        "从实际参考输入列表中删除index=3及其mandatory绑定，不读取该图任何内容。",
        _payload())
    assert [a["target_index"] for a in variant
            if a["action"] == "remove"] == [3]


def test_structured_remove_applies_via_diagnosed_conflict(app):
    """结构化 remove + 诊断冲突:角色词根归一后落在豁免内。"""
    payload = _payload()
    changes = app.director._apply_image_reference_adjustments(
        payload, {}, _diagnostics(status="conflicting"))
    assert [a["target_index"] for a in changes["applied"]] == [3]
    assert changes["skipped"] == []
    assert "/ws/blocking/shot8_clean.png" not in payload["reference_images"]
    assert all(m["uri"] != "/ws/blocking/shot8_clean.png"
               for m in payload["asset_matches"])
    # 其余参考不受影响
    assert len(payload["reference_images"]) == 3


def test_explicit_instruction_merges_flag_onto_structured_action(app):
    """无诊断状态兜底:文字指令的显式标记合并到结构化动作上。"""
    payload = _payload()
    changes = app.director._apply_image_reference_adjustments(
        payload, {}, _diagnostics(status=""),
        instruction=REAL_INSTRUCTION)
    assert [a["target_index"] for a in changes["applied"]] == [3]
    assert changes["applied"][0]["source"] == "codex_instruction"
    assert "/ws/blocking/shot8_clean.png" not in payload["reference_images"]


def test_protected_identity_survives_even_explicit_instruction(app):
    """锁定身份参考图即使是 Codex 文字点名也不得自动移除。"""
    payload = _payload()
    diagnostics = _diagnostics(status="conflicting")
    diagnostics["reference_adjustments"] = [
        {"action": "remove", "target_index": 1, "role": "identity"},
    ]
    changes = app.director._apply_image_reference_adjustments(
        payload, {}, diagnostics,
        instruction="移除index 1,改用新脸。")
    assert changes["applied"] == []
    assert changes["skipped"][0]["target_index"] == 1
    assert "/ws/cast/portrait_林未.png" in payload["reference_images"]


def test_unjustified_weak_reference_still_skipped(app):
    """没有诊断冲突也没有文字指令时,非弱引用表角色依然拒绝移除。"""
    payload = _payload()
    diagnostics = _diagnostics(status="ok")
    changes = app.director._apply_image_reference_adjustments(
        payload, {}, diagnostics)
    assert changes["applied"] == []
    assert changes["skipped"][0]["target_index"] == 3
    assert "/ws/blocking/shot8_clean.png" in payload["reference_images"]
