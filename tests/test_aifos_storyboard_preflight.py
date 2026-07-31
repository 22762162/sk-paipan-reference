"""分镜可拍性预检:把出图前才爆的矛盾前移到分镜/空间阶段。

用户指出的病理(2026-07-28):"前后顺序如果不对,很容易出现前后命令
要求不一样,然后又要核对"。查证后确认顺序本身是对的,真正的病是
**上游发明了下游做不到的事、而上游自己不知道**——特写装3人、声明
特写却把机位摆5米外、道具缺定格行、同场越轴,全都要等到出图前才爆,
中间隔着几十分钟和真金白银。空间调度是确定性计算、零成本,是最早
能算出这些矛盾的地方。
"""
import unittest

from aifos.storyboard_preflight import (describe_issues,
                                        preflight_storyboard,
                                        repairable_shots)


def _blocking(shot_no, actors, camera=None):
    return {"shot_index": {str(shot_no): {
        "shot_no": shot_no,
        "camera": camera or {
            "start_3d": {"x": 0.0, "y": 1.5, "z": 4.0},
            "end_3d": {"x": 0.0, "y": 1.5, "z": 4.0},
            "target_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
            "target_start_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
            "target_end_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
            "fov_degrees": 54.4,
        },
        "actors": actors,
    }}}


def _actor(name, x, z=0.0):
    return {"name": name,
            "start_3d": {"x": x, "y": 0.0, "z": z},
            "end_3d": {"x": x, "y": 0.0, "z": z},
            "pose_label_start": "站姿", "moving": False}


class CapacityTest(unittest.TestCase):
    def test_closeup_cannot_hold_three_people(self):
        """实测原型:85mm特写要求三人全部可见 → 出图必熔断。"""
        sb = {"shots": [{"shot_no": 1, "scene_no": 1, "camera": "85mm特写",
                         "characters": ["甲", "乙", "丙"]}]}
        report = preflight_storyboard({}, sb)
        self.assertFalse(report["passed"])
        issue = report["issues"][0]
        self.assertEqual(issue["kind"], "scale_capacity")
        self.assertIn("最多完整容纳1人", issue["detail"])
        self.assertIn("3人", issue["detail"])
        self.assertIn("放宽", issue["suggestion"])

    def test_functional_figures_count_toward_capacity(self):
        """实测:近景却要求9人全可见(3角色+6名功能人物)。"""
        sb = {"shots": [{"shot_no": 2, "scene_no": 1, "camera": "近景",
                         "characters": ["甲", "乙", "丙"],
                         "functional_figures": [{"name": "弟子队列",
                                                 "count": 6}]}]}
        report = preflight_storyboard({}, sb)
        self.assertIn("9人", report["issues"][0]["detail"])

    def test_wide_shot_passes(self):
        sb = {"shots": [{"shot_no": 3, "scene_no": 1, "camera": "全景",
                         "characters": ["甲", "乙", "丙"]}]}
        self.assertTrue(preflight_storyboard({}, sb)["passed"])


class PropPhaseTest(unittest.TestCase):
    def test_transition_without_freeze_is_caught(self):
        sb = {"shots": [{"shot_no": 1, "scene_no": 1, "camera": "中景",
                         "characters": ["甲"],
                         "frame_props": [],
                         "prop_transitions": [
                             {"prop_id": "p1", "from_phase": "start",
                              "to_phase": "end", "action": "静置"}]}]}
        report = preflight_storyboard({}, sb)
        kinds = [i["kind"] for i in report["issues"]]
        self.assertIn("prop_phase", kinds)
        self.assertIn("p1", report["issues"][0]["detail"])

    def test_declared_phase_row_satisfies_it(self):
        sb = {"shots": [{"shot_no": 1, "scene_no": 1, "camera": "中景",
                         "characters": ["甲"],
                         "frame_props": [{"prop_id": "p1",
                                          "phase": "freeze"}],
                         "prop_transitions": [
                             {"prop_id": "p1", "from_phase": "start",
                              "to_phase": "end", "action": "静置"}]}]}
        self.assertTrue(preflight_storyboard({}, sb)["passed"])


class AxisFlipTest(unittest.TestCase):
    def _two_shots(self, second_actors):
        sb = {"shots": [
            {"shot_no": 1, "scene_no": 1, "camera": "中景",
             "characters": ["甲", "乙"]},
            {"shot_no": 2, "scene_no": 1, "camera": "中景",
             "characters": ["甲", "乙"]},
        ]}
        blocking = {"shot_index": {}}
        blocking["shot_index"].update(
            _blocking(1, [_actor("甲", -1.6), _actor("乙", 1.6)])[
                "shot_index"])
        blocking["shot_index"].update(
            _blocking(2, second_actors)["shot_index"])
        return sb, blocking

    def test_left_right_flip_across_cut_is_caught(self):
        """越轴:上一镜甲在左乙在右,本镜反过来——观众看不懂。"""
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        report = preflight_storyboard({}, sb, blocking)
        axis = [i for i in report["issues"] if i["kind"] == "axis_flip"]
        self.assertEqual(len(axis), 1)
        self.assertIn("越轴", axis[0]["detail"])
        self.assertIn("轴线同一侧", axis[0]["suggestion"])

    def test_same_order_is_fine(self):
        sb, blocking = self._two_shots(
            [_actor("甲", -1.2), _actor("乙", 1.2)])
        axis = [i for i in preflight_storyboard({}, sb, blocking)["issues"]
                if i["kind"] == "axis_flip"]
        self.assertEqual(axis, [])

    def test_different_scene_is_not_an_axis_violation(self):
        """换场是硬切,不受同一条轴线约束。"""
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][1]["scene_no"] = 2
        axis = [i for i in preflight_storyboard({}, sb, blocking)["issues"]
                if i["kind"] == "axis_flip"]
        self.assertEqual(axis, [])

    def test_single_actor_shot_never_flags_axis(self):
        sb, blocking = self._two_shots([_actor("甲", 0.0)])
        axis = [i for i in preflight_storyboard({}, sb, blocking)["issues"]
                if i["kind"] == "axis_flip"]
        self.assertEqual(axis, [])


class FramingDistanceTest(unittest.TestCase):
    def test_declared_scale_far_from_camera_distance_is_caught(self):
        sb = {"shots": [{"shot_no": 1, "scene_no": 1, "camera": "大特写",
                         "characters": ["甲"]}]}
        blocking = _blocking(1, [_actor("甲", 0.0, 0.0)])
        report = preflight_storyboard({}, sb, blocking)
        kinds = [i["kind"] for i in report["issues"]]
        self.assertIn("framing_distance", kinds)


class ReportShapeTest(unittest.TestCase):
    def test_repair_targets_ranked_and_described(self):
        sb = {"shots": [
            {"shot_no": 1, "scene_no": 1, "camera": "特写",
             "characters": ["甲", "乙", "丙"],
             "frame_props": [],
             "prop_transitions": [{"prop_id": "p1", "from_phase": "start",
                                   "to_phase": "end", "action": "x"}]},
            {"shot_no": 2, "scene_no": 1, "camera": "近景",
             "characters": ["甲", "乙", "丙"]},
        ]}
        report = preflight_storyboard({}, sb)
        # 镜头1 有两类问题 → 排在前面
        self.assertEqual(repairable_shots(report)[0], 1)
        text = describe_issues(report, 1)
        self.assertIn("建议", text)
        self.assertIn("最多完整容纳", text)
        self.assertEqual(describe_issues(report, 99), "")

    def test_clean_storyboard_reports_passed(self):
        sb = {"shots": [{"shot_no": 1, "scene_no": 1, "camera": "全景",
                         "characters": ["甲"]}]}
        report = preflight_storyboard({}, sb)
        self.assertTrue(report["passed"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["shots"], 1)


class DurationTest(unittest.TestCase):
    """时长越界是分镜层就能算出的废镜:短于4秒提交必拒(真实案例:
    mock 产线写 2.5 秒镜头,一路混到视频提交才被拒),超上限禁止静默截短。"""

    @staticmethod
    def _shot(duration, **extra):
        return {"shot_no": 1, "scene_no": 1, "camera": "全景",
                "characters": ["甲"], "duration": duration, **extra}

    def test_below_four_seconds_is_caught(self):
        report = preflight_storyboard({}, {"shots": [self._shot(2.5)]})
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["duration_short"])
        self.assertIn("4秒", report["issues"][0]["detail"])

    def test_over_fifteen_without_upgrade_is_caught(self):
        report = preflight_storyboard({}, {"shots": [self._shot(20)]})
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["duration_long"])
        self.assertIn("seedance2_5", report["issues"][0]["suggestion"])

    def test_upgrade_tier_shot_may_run_16_to_30_seconds(self):
        report = preflight_storyboard({}, {"shots": [
            self._shot(24, video_model_tier="seedance2_5")]})
        self.assertTrue(report["passed"])

    def test_upgrade_tier_still_capped_at_thirty(self):
        report = preflight_storyboard({}, {"shots": [
            self._shot(45, video_model_tier="seedance2_5")]})
        kinds = [item["kind"] for item in report["issues"]]
        self.assertEqual(kinds, ["duration_long"])
        self.assertIn("30秒", report["issues"][0]["detail"])

    def test_normal_band_and_missing_duration_pass(self):
        report = preflight_storyboard({}, {"shots": [
            self._shot(5.5),
            {"shot_no": 2, "scene_no": 1, "camera": "全景",
             "characters": ["甲"]},
        ]})
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
