"""场次「暂不生成」:单场跑通全流程的测试开关。

分镜/剧本文档永远保持完整;只有派生的生产视图(清单/出图/门禁/
质检/拆条)按 scene_plan.skipped_scenes 过滤。恢复场次后断点续写
自动补齐,不重生成已完成的镜头。
"""
import unittest

from aifos.director import Director


class _Slim:
    """只借用场次过滤方法,不初始化重型 Director。"""
    _scene_plan_doc = Director._scene_plan_doc
    _skipped_scenes = Director._skipped_scenes
    _active_shots = Director._active_shots
    _active_storyboard = Director._active_storyboard
    _active_scenes = Director._active_scenes


def _ctx(skipped):
    return {
        "scene_plan": {"skipped_scenes": skipped},
        "script": {"scenes": [
            {"scene_no": 1, "location": "青牛镇"},
            {"scene_no": 2, "location": "七玄门"},
            {"scene_no": 3, "location": "后山"},
        ]},
        "storyboard": {"shots": [
            {"shot_no": 1, "scene_no": 1},
            {"shot_no": 2, "scene_no": 2},
            {"shot_no": 3, "scene_no": 2},
            {"shot_no": 4, "scene_no": 3},
        ], "standard_fingerprint": "fp"},
        "episode": {"id": 1},
    }


class ScenePlanFilterTest(unittest.TestCase):
    def test_no_skip_returns_everything(self):
        d, ctx = _Slim(), _ctx([])
        self.assertEqual(len(d._active_shots(ctx)), 4)
        self.assertEqual(len(d._active_scenes(ctx)), 3)
        # 无跳过时直接返回原分镜对象,不做多余拷贝
        self.assertIs(d._active_storyboard(ctx), ctx["storyboard"])

    def test_skip_scene_filters_shots_and_scenes(self):
        d, ctx = _Slim(), _ctx([2, 3])
        self.assertEqual(
            [s["shot_no"] for s in d._active_shots(ctx)], [1])
        self.assertEqual(
            [s["scene_no"] for s in d._active_scenes(ctx)], [1])
        view = d._active_storyboard(ctx)
        self.assertEqual(len(view["shots"]), 1)
        self.assertEqual(view["standard_fingerprint"], "fp")
        # 铁律:分镜文档本体必须保持完整,过滤只发生在视图
        self.assertEqual(len(ctx["storyboard"]["shots"]), 4)

    def test_bad_values_ignored(self):
        d, ctx = _Slim(), _ctx(["2", "x", None])
        self.assertEqual(d._skipped_scenes(ctx), {2})
        self.assertEqual(
            [s["shot_no"] for s in d._active_shots(ctx)], [1, 4])



class CorePropUnionTest(unittest.TestCase):
    """道具母资产口径统一:候选生成必须覆盖登记表里的母资产类孤儿。

    实测(凡人修仙传 script v5):「韩立的旧麻布包裹」只在 prop_registry
    (identity_prop)不在 core_props——候选生成漏掉它,镜头引用却要母资产,
    必现「尚未人工锁定」熔断。"""

    def test_registry_orphan_master_props_included(self):
        from aifos.director import core_prop_definitions
        script = {
            "core_props": [{"name": "粗陶水壶", "visual_design": "灰褐陶"}],
            "prop_registry": [
                {"prop_id": "a", "name": "旧麻布包裹",
                 "kind": "identity_prop"},
                {"prop_id": "b", "name": "粗陶水壶", "kind": "core"},
                {"prop_id": "c", "name": "路边石子", "kind": "minor"},
            ],
        }
        names = [d["name"] for d in core_prop_definitions(script)]
        self.assertEqual(names, ["粗陶水壶", "旧麻布包裹"])

    def test_minor_kind_props_skip_master_demand(self):
        from aifos.director import master_prop_kind
        self.assertTrue(master_prop_kind("identity_prop"))
        self.assertTrue(master_prop_kind("core"))
        self.assertTrue(master_prop_kind(""))       # 旧数据留空=母资产
        self.assertFalse(master_prop_kind("minor"))
        self.assertFalse(master_prop_kind("one_off"))


if __name__ == "__main__":
    unittest.main()


class PropDesignDerivationTest(unittest.TestCase):
    """登记表孤儿道具缺设计卡:编剧从剧本推导补卡的提示词与校验。"""

    def test_prompt_carries_script_and_rules(self):
        from aifos.adapters.claude_script import build_prompt
        prompt = build_prompt("script", {
            "prop_design": True,
            "script": {"scenes": [{"scene_no": 1, "action": "握紧包裹绳结"}]},
            "style": "3D半写实",
            "props": [{"name": "旧麻布包裹", "kind": "identity_prop"}]})
        self.assertIn("旧麻布包裹", prompt)
        self.assertIn("握紧包裹绳结", prompt)
        self.assertIn("尺寸级别", prompt)
        self.assertIn("aifos.prop_design.v1", prompt)

    def test_validate_requires_all_requested_cards(self):
        from aifos.adapters.claude_script import validate_prop_design
        payload = {"props": [{"name": "旧麻布包裹"}, {"name": "密信"}]}
        ok = {"schema": "aifos.prop_design.v1", "props": [
            {"name": "旧麻布包裹", "story_function": "随身行李,以绳结手提",
             "visual_design": "粗麻方包袱,单手可提,绳结封口,磨白起毛边"},
            {"name": "密信", "story_function": "剧情关键信件,怀揣传递",
             "visual_design": "折叠桑皮纸信,掌心大小,火漆闭合,边缘磨损"},
        ]}
        self.assertIsNone(validate_prop_design(ok, payload))
        missing = {"schema": "aifos.prop_design.v1",
                   "props": ok["props"][:1]}
        self.assertIn("密信", validate_prop_design(missing, payload))
        lazy = {"schema": "aifos.prop_design.v1", "props": [
            {"name": "旧麻布包裹", "story_function": "包",
             "visual_design": "布包"}]}
        self.assertIn("敷衍", validate_prop_design(lazy, payload))
