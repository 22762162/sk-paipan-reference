"""道具画面内尺度：缺 scale_reference 时必须给兜底禁令。

真实病理(EP1 旧银铃被画成 3-4 倍大)：
道具参考图的 binding 写「尺寸服从当前镜头合同」→ 合同从 prop_registry 取事实
→ prop_registry 没有任何尺度字段 → 没有东西约束大小 → 模型退回去继承道具母图
→ 母图是占满画面的棚拍特写 → 「这东西很大」被当成道具事实继承。
"""

from aifos.prompt_contract import REFERENCE_SCOPE_DEFAULTS, compile_shot_prompt
from aifos.story_logic import audit_prop_contract


def _shot(scale=None):
    entry = {
        "prop_id": "prop_bell_001", "name": "旧银铃", "kind": "core",
        "instance_count": 1,
        "availability_start_event": {"event_id": "ep01-s01", "phase": "start"},
        "disclosure_policy": "explicit_frame_only",
    }
    if scale:
        entry["scale_reference"] = scale
    return {
        "shot_no": 1, "scene_no": 1,
        "action": "沈眉低头看案上的旧银铃",
        "characters": ["沈眉"],
        "prop_registry": [entry],
        "frame_props": [{
            "prop_id": "prop_bell_001", "name": "旧银铃",
            "visibility": "visible", "representation": "physical",
            "phase": "freeze", "holder": "", "physical_state": "静置案上",
        }],
    }


def _text_of(shot):
    contract, compact = compile_shot_prompt(
        shot, location="书阁", style="写实", references=[], mode="image")
    return str(contract) + str(compact)


def test_scale_reference_is_rendered_when_declared():
    body = _text_of(_shot("比一册线装书还薄，两指指尖即可捏住"))
    assert "两指指尖即可捏住" in body
    # 已声明尺度就不该再挂兜底禁令
    assert "【道具尺度】" not in body


def test_missing_scale_reference_injects_fallback_prohibition():
    """存量剧本没有这个字段，兜底必须自动生效。"""
    body = _text_of(_shot())
    assert "【道具尺度】" in body
    assert "严禁参照其母资产图在画面中的占比" in body
    assert "宁小勿大" in body


def test_prop_scope_excludes_frame_share():
    """作用域表必须显式排除画面占比，否则它会被当作可继承的道具属性。"""
    assert "frame_share" in REFERENCE_SCOPE_DEFAULTS["prop"]["exclude"]


def test_audit_warns_but_does_not_block_legacy_registry():
    """做成阻断级会让所有存量项目一夜不合格，还会短路下游既有修复。"""
    report = audit_prop_contract(_shot())
    assert report["passed"] is True, "缺尺度不能阻断"
    assert any("scale_reference" in w for w in report["warnings"])


def test_audit_warns_on_absolute_size_only():
    """实测：写「直径1.5厘米」无效，铃铛仍然过大；只有参照物有效。"""
    report = audit_prop_contract(_shot("直径1.5厘米"))
    assert report["passed"] is True
    assert any("绝对尺寸" in w for w in report["warnings"])


def test_audit_is_quiet_when_scale_uses_comparison_objects():
    report = audit_prop_contract(_shot("两指指尖即可捏住，掌心可完全藏住"))
    assert not [w for w in report["warnings"] if "scale_reference" in w]
