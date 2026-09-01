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
                                        _scale_of,
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
    def test_medium_wide_is_an_independent_scale_before_wide(self):
        self.assertEqual(_scale_of({"camera": "35mm中全景·平视"}), "中全景")

    def test_medium_wide_holds_at_least_two_complete_people(self):
        sb = {"shots": [{
            "shot_no": 6, "scene_no": 1, "camera": "中全景",
            "characters": ["甲", "乙"], "visible_figure_count": 2,
        }]}
        self.assertNotIn("scale_capacity", {
            item["kind"] for item in preflight_storyboard({}, sb)["issues"]})

    def test_medium_wide_keeps_a_finite_group_capacity(self):
        sb = {"shots": [{
            "shot_no": 7, "scene_no": 1, "camera": "中全景",
            "characters": list("甲乙丙丁戊己庚"),
            "visible_figure_count": 7,
        }]}
        self.assertIn("scale_capacity", {
            item["kind"] for item in preflight_storyboard({}, sb)["issues"]})

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

    def test_two_people_as_cropped_hands_keep_detail_closeup(self):
        """可见真人=2 不等于两具完整人物；手腕局部不能被升成中景。"""
        sb = {"shots": [{
            "shot_no": 4, "scene_no": 1,
            "camera": "135mm微俯双手大特写，只框入接触点",
            "characters": ["甲", "乙"], "visible_figure_count": 2,
            "description": (
                "两名人物均只以手腕局部入画，不出现任何完整人形；"
                "头部、面部、躯干及其余身体明确出画"),
        }]}
        self.assertTrue(preflight_storyboard({}, sb)["passed"])

    def test_weak_partial_word_does_not_bypass_capacity(self):
        sb = {"shots": [{
            "shot_no": 5, "scene_no": 1, "camera": "85mm特写",
            "characters": ["甲", "乙", "丙"],
            "description": "三名人物完整面孔同时入画，局部焦点锐利",
        }]}
        report = preflight_storyboard({}, sb)
        self.assertIn("scale_capacity", {
            issue["kind"] for issue in report["issues"]})


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

    def test_partial_hand_insert_does_not_infer_axis_from_hidden_bodies(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][1].update({
            "camera": "135mm双手大特写",
            "description": (
                "甲乙均只以手腕局部入画，面部、躯干和完整人形出画；"
                "只表现两只手腕的接触关系，不声明可见左右位置"),
            "visible_figure_count": 2,
        })

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(axis, [])

    def test_partial_insert_still_catches_explicit_visible_axis_reversal(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][0]["camera"] = "甲在画面左侧，乙在画面右侧"
        sb["shots"][1].update({
            "camera": "135mm手部大特写，甲在画面右侧，乙在画面左侧",
            "description": "两人只露手腕局部，不出现完整人形",
            "visible_figure_count": 2,
        })

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(len(axis), 1)

    def test_unobservable_insert_does_not_erase_last_visible_axis(self):
        sb = {"shots": [
            {"shot_no": 1, "scene_no": 1, "camera": "双人中景",
             "characters": ["甲", "乙"]},
            {"shot_no": 2, "scene_no": 1, "camera": "135mm手部大特写",
             "characters": ["甲", "乙"], "visible_figure_count": 2,
             "description": "只框入两人手腕，面部、躯干和完整人形出画"},
            {"shot_no": 3, "scene_no": 1, "camera": "双人中景",
             "characters": ["甲", "乙"]},
        ]}
        blocking = {"shot_index": {}}
        for no, actors in (
                (1, [_actor("甲", -1.6), _actor("乙", 1.6)]),
                (2, [_actor("甲", -1.6), _actor("乙", 1.6)]),
                (3, [_actor("甲", 1.6), _actor("乙", -1.6)])):
            blocking["shot_index"].update(
                _blocking(no, actors)["shot_index"])

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual([item["shot_no"] for item in axis], [3])

    def test_two_face_closeup_remains_axis_observable(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][1].update({
            "camera": "85mm双人贴面特写",
            "description": "两张脸同时清楚入画，甲乙左右位置可辨",
            "visible_figure_count": 2,
        })

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(len(axis), 1)

    def test_explicit_zero_visible_people_never_uses_hidden_actor_centres(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][1].update({
            "visible_figure_count": 0,
            "description": "空镜，两人均在画外",
        })

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(axis, [])

    def test_adjacent_scene_numbers_in_same_room_keep_axis_continuity(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][1]["scene_no"] = 2
        script = {"scenes": [
            {"scene_no": 1, "location": "虞家别墅·卧室"},
            {"scene_no": 2, "location": "虞家别墅·卧室床侧"},
        ]}

        axis = [
            item for item in preflight_storyboard(
                script, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(len(axis), 1)

    def test_cut_compares_previous_end_to_current_start(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        previous = blocking["shot_index"]["1"]["actors"]
        previous[0]["end_3d"] = {"x": 1.6, "y": 0.0, "z": 0.0}
        previous[1]["end_3d"] = {"x": -1.6, "y": 0.0, "z": 0.0}

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(axis, [])

    def test_explicit_screen_side_lock_overrides_hidden_route_projection(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        sb["shots"][1]["camera"] = (
            "保持上一镜轴线同侧，甲固定在画面左侧，乙固定在画面右侧")

        axis = [
            item for item in preflight_storyboard({}, sb, blocking)["issues"]
            if item["kind"] == "axis_flip"]

        self.assertEqual(axis, [])

    def test_negated_screen_positions_do_not_manufacture_axis_flip(self):
        sb, blocking = self._two_shots(
            [_actor("甲", -1.6), _actor("乙", 1.6)])
        sb["shots"][0]["camera"] = "甲在画面左侧，乙在画面右侧"
        for wording in ("不在", "禁止在", "不得在", "不应在"):
            with self.subTest(wording=wording):
                sb["shots"][1]["camera"] = (
                    f"甲{wording}画面右侧，乙{wording}画面左侧")
                axis = [
                    item for item in preflight_storyboard(
                        {}, sb, blocking)["issues"]
                    if item["kind"] == "axis_flip"]
                self.assertEqual(axis, [])

    def test_realm_phase_or_era_change_resets_axis_in_same_room(self):
        sb, blocking = self._two_shots(
            [_actor("甲", 1.6), _actor("乙", -1.6)])
        script = {"scenes": [
            {"scene_no": 1, "location": "虞家别墅·卧室"},
        ]}
        cases = (
            ({"active_realm_id": "reality"},
             {"active_realm_id": "game"}),
            ({"story_phase": "present"},
             {"story_phase": "flashback"}),
            ({"era_context": "2078年现代"},
             {"era_context": "明代"}),
        )
        for before, after in cases:
            with self.subTest(before=before, after=after):
                sb["shots"][0].update(before)
                sb["shots"][1].update(after)
                axis = [
                    item for item in preflight_storyboard(
                        script, sb, blocking)["issues"]
                    if item["kind"] == "axis_flip"]
                self.assertEqual(axis, [])
                for key in set(before) | set(after):
                    sb["shots"][0].pop(key, None)
                    sb["shots"][1].pop(key, None)


class FramingDistanceTest(unittest.TestCase):
    def test_declared_scale_far_from_camera_distance_is_caught(self):
        sb = {"shots": [{"shot_no": 1, "scene_no": 1, "camera": "大特写",
                         "characters": ["甲"]}]}
        blocking = _blocking(1, [_actor("甲", 0.0, 0.0)])
        report = preflight_storyboard({}, sb, blocking)
        kinds = [i["kind"] for i in report["issues"]]
        self.assertIn("framing_distance", kinds)

    def test_end_frame_uses_end_geometry_for_framing(self):
        actor = _actor("甲", 0.0, -3.0)
        actor["moving"] = True
        actor["end_3d"] = {"x": 0.0, "y": 0.0, "z": 3.2}
        sb = {"shots": [{
            "shot_no": 1, "scene_no": 1, "camera": "特写",
            "characters": ["甲"],
            "frame_target": {"phase": "end", "state": "甲停在机位前"},
        }]}
        report = preflight_storyboard({}, sb, _blocking(1, [actor]))
        self.assertNotIn("framing_distance", {
            item["kind"] for item in report["issues"]})

    def test_freeze_blocking_phase_overrides_freeze_label(self):
        actor = _actor("甲", 0.0, -3.0)
        actor["moving"] = True
        actor["end_3d"] = {"x": 0.0, "y": 0.0, "z": 3.2}
        sb = {"shots": [{
            "shot_no": 1, "scene_no": 1, "camera": "特写",
            "characters": ["甲"],
            "frame_target": {
                "phase": "freeze", "blocking_phase": "end",
                "state": "甲停在机位前",
            },
        }]}
        report = preflight_storyboard({}, sb, _blocking(1, [actor]))
        self.assertNotIn("framing_distance", {
            item["kind"] for item in report["issues"]})

    def test_medium_wide_matches_spatial_blocking_3_8_metres(self):
        block = _blocking(1, [_actor("甲", 0.0)])
        block["shot_index"]["1"]["camera"]["director_camera"] = {
            "declared": {"shot_size": "中全景"},
            "distance_m": 3.8,
            "desired_distance_m": 3.8,
            "yaw_deg": 0.0,
        }
        sb = {"shots": [{
            "shot_no": 1, "scene_no": 1, "camera": "中全景",
            "characters": ["甲"],
        }]}
        report = preflight_storyboard({}, sb, block)
        self.assertNotIn("framing_distance", {
            item["kind"] for item in report["issues"]})

    def test_medium_wide_still_rejects_closeup_distance(self):
        block = _blocking(1, [_actor("甲", 0.0)])
        block["shot_index"]["1"]["camera"]["director_camera"] = {
            "declared": {"shot_size": "中全景"},
            "distance_m": 1.0,
            "desired_distance_m": 1.0,
            "yaw_deg": 0.0,
        }
        sb = {"shots": [{
            "shot_no": 1, "scene_no": 1, "camera": "中全景",
            "characters": ["甲"],
        }]}
        report = preflight_storyboard({}, sb, block)
        framing = [item for item in report["issues"]
                   if item["kind"] == "framing_distance"]
        self.assertEqual(len(framing), 1)
        self.assertIn("合同声明中全景", framing[0]["detail"])


class DirectorCameraClampTest(unittest.TestCase):
    @staticmethod
    def _report(**camera_flags):
        director_camera = {
            "declared": {"shot_size": "中景"},
            "yaw_deg": 0.0,
            "desired_distance_m": 4.2,
            "distance_m": 2.8,
            "movement": "拉",
            "movement_amount": "1.2米",
            **camera_flags,
        }
        blocking = {"shot_index": {"1": {
            "shot_no": 1, "scene_no": 1,
            "camera": {"director_camera": director_camera},
            "actors": [],
        }}}
        storyboard = {"shots": [{
            "shot_no": 1, "scene_no": 1, "camera": "中景",
            "characters": [],
        }]}
        return preflight_storyboard({}, storyboard, blocking)

    def test_wall_clamped_camera_is_a_preflight_issue(self):
        report = self._report(wall_clamped=True)
        self.assertIn("camera_wall_clamped", {
            item["kind"] for item in report["issues"]})

    def test_wall_clamped_movement_is_a_preflight_issue(self):
        report = self._report(movement_wall_clamped=True)
        self.assertIn("camera_movement_wall_clamped", {
            item["kind"] for item in report["issues"]})

    def test_other_camera_warnings_are_not_promoted_by_this_gate(self):
        report = self._report(wall_clamped=False,
                              movement_wall_clamped=False)
        self.assertNotIn("camera_wall_clamped", {
            item["kind"] for item in report["issues"]})
        self.assertNotIn("camera_movement_wall_clamped", {
            item["kind"] for item in report["issues"]})


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

    def test_long_take_profile_requires_eight_seconds_or_exception(self):
        storyboard = {
            "profile": {
                "long_take_policy": {
                    "enabled": True,
                    "preferred_seconds": [8, 15],
                    "temporal_phases_required": True,
                },
            },
            "shots": [self._shot(6)],
        }
        report = preflight_storyboard({}, storyboard)
        self.assertEqual(
            [item["kind"] for item in report["issues"]],
            ["duration_under_preferred"])

    def test_long_take_profile_accepts_three_phases(self):
        storyboard = {
            "profile": {
                "long_take_policy": {
                    "enabled": True,
                    "preferred_seconds": [8, 15],
                    "temporal_phases_required": True,
                },
            },
            "shots": [self._shot(10, temporal_beats=[
                {"phase": "setup"},
                {"phase": "main"},
                {"phase": "settle"},
            ])],
        }
        self.assertTrue(preflight_storyboard({}, storyboard)["passed"])


if __name__ == "__main__":
    unittest.main()
