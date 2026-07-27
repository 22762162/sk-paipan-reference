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


if __name__ == "__main__":
    unittest.main()
