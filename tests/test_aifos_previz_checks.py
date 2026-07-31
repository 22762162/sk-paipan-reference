"""动态预演时间维度校验:端点合法、过程出事的四类问题要在文本层暴露。"""
import unittest

from aifos.previz_checks import previz_report


def _shot(shot_no, scene_no, actors, camera=None):
    return {"shot_no": shot_no, "scene_no": scene_no,
            "actors": actors, "camera": camera or {}}


def _actor(name, start, end=None):
    end = end if end is not None else start
    return {"name": name,
            "start_3d": {"x": start[0], "y": 0.0, "z": start[1]},
            "end_3d": {"x": end[0], "y": 0.0, "z": end[1]}}


def _blocking(shots, locations=None):
    scenes = {}
    for shot in shots:
        scenes.setdefault(shot["scene_no"], []).append(shot)
    return {
        "shot_index": {str(s["shot_no"]): s for s in shots},
        "scenes": [
            {"scene_no": no, "location": (locations or {}).get(no, f"场{no}"),
             "shots": [{"shot_no": s["shot_no"]} for s in rows]}
            for no, rows in sorted(scenes.items())],
    }


def _model(objects):
    return {"objects": [
        {"name": name, "position_3d": {"x": x, "y": 0, "z": z},
         "width_m": w, "depth_m": d, "height_m": h,
         "rotation_y_deg": rot}
        for name, x, z, w, d, h, rot in objects]}


class TeleportTest(unittest.TestCase):
    def test_same_scene_position_jump_is_caught(self):
        blocking = _blocking([
            _shot(1, 1, [_actor("林川", (0.0, 0.0))]),
            _shot(2, 1, [_actor("林川", (3.0, 0.0))]),
        ])
        report = previz_report(blocking)
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["teleport"])
        self.assertIn("瞬移", report["issues"][0]["detail"])

    def test_cross_scene_jump_is_legitimate(self):
        blocking = _blocking([
            _shot(1, 1, [_actor("林川", (0.0, 0.0))]),
            _shot(2, 2, [_actor("林川", (5.0, 5.0))]),
        ])
        self.assertTrue(previz_report(blocking)["passed"])

    def test_covered_walk_is_legitimate(self):
        # 上一镜本人走到了 (2.9,0),下一镜从那里继续——不是传送
        blocking = _blocking([
            _shot(1, 1, [_actor("林川", (0.0, 0.0), (2.9, 0.0))]),
            _shot(2, 1, [_actor("林川", (3.0, 0.0))]),
        ])
        self.assertTrue(previz_report(blocking)["passed"])


class PathCollisionTest(unittest.TestCase):
    def test_walking_through_a_table_is_caught(self):
        blocking = _blocking([_shot(
            1, 1, [_actor("林川", (-2.0, 0.0), (2.0, 0.0))])],
            locations={1: "书房"})
        models = {"书房": _model([("长桌", 0, 0, 1.2, 0.8, 0.8, 0)])}
        report = previz_report(blocking, scene_models=models)
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["path_collision"])
        self.assertIn("长桌", report["issues"][0]["detail"])

    def test_low_rug_is_not_an_obstacle(self):
        blocking = _blocking([_shot(
            1, 1, [_actor("林川", (-2.0, 0.0), (2.0, 0.0))])],
            locations={1: "书房"})
        models = {"书房": _model([("地毯", 0, 0, 2.0, 1.5, 0.02, 0)])}
        self.assertTrue(
            previz_report(blocking, scene_models=models)["passed"])


class CrossingTest(unittest.TestCase):
    def test_two_actors_colliding_mid_shot_is_caught(self):
        blocking = _blocking([_shot(1, 1, [
            _actor("林川", (-2.0, 0.0), (2.0, 0.0)),
            _actor("阿砚", (2.0, 0.0), (-2.0, 0.0)),
        ])])
        report = previz_report(blocking)
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["crossing"])

    def test_parallel_walkers_are_fine(self):
        blocking = _blocking([_shot(1, 1, [
            _actor("林川", (-2.0, 0.0), (2.0, 0.0)),
            _actor("阿砚", (-2.0, 1.5), (2.0, 1.5)),
        ])])
        self.assertTrue(previz_report(blocking)["passed"])


class CameraTest(unittest.TestCase):
    @staticmethod
    def _camera(start, end, y=1.5):
        return {"start_3d": {"x": start[0], "y": y, "z": start[1]},
                "end_3d": {"x": end[0], "y": y, "z": end[1]}}

    def test_dolly_through_a_cabinet_is_caught(self):
        blocking = _blocking([_shot(
            1, 1, [_actor("林川", (3.0, 3.0))],
            camera=self._camera((-3.0, 0.0), (3.0, 0.0), y=1.2))],
            locations={1: "书房"})
        models = {"书房": _model([("立柜", 0, 0, 1.0, 0.6, 2.0, 0)])}
        report = previz_report(blocking, scene_models=models)
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["camera_through"])
        self.assertIn("立柜", report["issues"][0]["detail"])

    def test_crane_above_furniture_is_fine(self):
        blocking = _blocking([_shot(
            1, 1, [_actor("林川", (3.0, 3.0))],
            camera=self._camera((-3.0, 0.0), (3.0, 0.0), y=2.6))],
            locations={1: "书房"})
        models = {"书房": _model([("立柜", 0, 0, 1.0, 0.6, 2.0, 0)])}
        self.assertTrue(
            previz_report(blocking, scene_models=models)["passed"])


class ReportShapeTest(unittest.TestCase):
    def test_storyboard_order_and_counts(self):
        blocking = _blocking([
            _shot(2, 1, [_actor("林川", (3.0, 0.0))]),
            _shot(1, 1, [_actor("林川", (0.0, 0.0))]),
        ])
        storyboard = {"shots": [{"shot_no": 1}, {"shot_no": 2}]}
        report = previz_report(blocking, storyboard)
        self.assertEqual(report["shots"], 2)
        self.assertEqual(report["issues"][0]["kind"], "teleport")

    def test_empty_blocking_passes(self):
        report = previz_report({})
        self.assertTrue(report["passed"])
        self.assertEqual(report["issues"], [])


if __name__ == "__main__":
    unittest.main()


class RepairBridgeTest(unittest.TestCase):
    """previz 报告 → 就地修桥接:排序限量 + 人话理由。"""

    def test_shots_ranked_and_described(self):
        from aifos.previz_checks import (describe_for_repair,
                                         shots_with_issues)
        report = {"issues": [
            {"shot_no": 5, "kind": "teleport", "detail": "甲瞬移2米"},
            {"shot_no": 2, "kind": "path_collision", "detail": "乙穿过长桌"},
            {"shot_no": 5, "kind": "crossing", "detail": "甲乙路径交汇"},
        ]}
        self.assertEqual(shots_with_issues(report), [5, 2])
        text = describe_for_repair(report, 5)
        self.assertIn("瞬移", text)
        self.assertIn("交汇", text)
        self.assertEqual(describe_for_repair(report, 99), "")
