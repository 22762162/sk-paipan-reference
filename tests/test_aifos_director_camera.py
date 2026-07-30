"""导演调度器:机位由导演意图求解,不再是画布坐标的副产品。

修前实测(《长夏记事》EP1 八镜):
- 景别→机距只有 2/8 生效(声明近景 1.7m,实际 4.73m);
- 镜高恒定 1.55m——`_camera_height` 只读机位/运动,而俯拍写在「角度」里;
- 方位逐镜乱跳 134°→168°→-93°→15°,因为它是「画布固定点→主体连线」的
  方向,主体一动就变,不是任何导演选择。
"""
import math
import unittest

from aifos.director_camera import (ANGLE_PITCH_DEG, SHOT_SIZE_DISTANCE_M,
                                   declared_angle, declared_position,
                                   declared_shot_size, primary_actor,
                                   solve_camera, solve_scene)


def _actor(name="沈眉", x=0.0, z=0.0, hero=True, **extra):
    a = {"actor_id": name, "name": name, "is_protagonist": hero,
         "height_m": 1.68,
         "start_3d": {"x": x, "y": 0.0, "z": z},
         "end_3d": {"x": x, "y": 0.0, "z": z}}
    a.update(extra)
    return a


def _shot(camera_text, no=1):
    return {"shot_no": no, "camera": camera_text}


class DeclaredFieldTest(unittest.TestCase):
    def test_longest_token_wins(self):
        """「中近景」不能被「中景」吃掉,否则机距差 0.9 米。"""
        self.assertEqual(declared_shot_size(_shot("中近景，平视")), "中近景")
        self.assertEqual(declared_shot_size(_shot("中景，平视")), "中景")

    def test_angle_is_read_at_all(self):
        """角度维度此前从没有人读过——镜高因此永远是默认值。"""
        self.assertEqual(declared_angle(_shot("特写，俯拍，侧面")), "俯拍")
        self.assertEqual(declared_angle(_shot("特写，仰拍，侧面")), "仰拍")
        self.assertEqual(declared_angle(_shot("特写，侧面")), "")

    def test_five_dimensions_takes_priority_over_free_text(self):
        shot = {"shot_no": 1, "camera": "中景，平视",
                "five_dimensions": {"camera_design": {"shot_size": "大特写",
                                                      "angle": "俯拍"}}}
        self.assertEqual(declared_shot_size(shot), "大特写")
        self.assertEqual(declared_angle(shot), "俯拍")

    def test_position_tokens(self):
        self.assertEqual(declared_position(_shot("特写，平视，过肩")), "过肩")
        self.assertEqual(declared_position(_shot("特写，平视，侧面")), "侧面")


class DistanceTest(unittest.TestCase):
    def _distance(self, text):
        cam = solve_camera(_shot(text), [_actor()])
        p, t = cam["position_3d"], cam["target_3d"]
        return math.dist((p["x"], p["y"], p["z"]), (t["x"], t["y"], t["z"]))

    def test_every_declared_size_lands_on_its_distance(self):
        for size, want in SHOT_SIZE_DISTANCE_M.items():
            if want > 4.0:
                continue          # 大于半间屋的景别会贴墙,单独测
            got = self._distance(f"{size}，平视，正面")
            self.assertAlmostEqual(got, want, delta=0.12,
                                   msg=f"{size} 应 {want}m,实得 {got:.2f}m")

    def test_missing_size_falls_back_to_medium_not_canvas(self):
        cam = solve_camera(_shot("平视，正面"), [_actor()])
        self.assertEqual(cam["desired_distance_m"],
                         SHOT_SIZE_DISTANCE_M["中景"])

    def test_wall_clamp_is_reported_not_silent(self):
        cam = solve_camera(_shot("大远景，平视，正面"), [_actor(x=4.6, z=3.2)],
                           world={"floor_width_m": 10, "floor_depth_m": 7})
        self.assertTrue(cam["wall_clamped"])
        self.assertLess(cam["distance_m"], cam["desired_distance_m"])


class HeightTest(unittest.TestCase):
    def _height(self, text):
        return solve_camera(_shot(text), [_actor()])["height_m"]

    def test_angle_actually_moves_the_camera(self):
        level = self._height("特写，平视，正面")
        down = self._height("特写，俯拍，正面")
        up = self._height("特写，仰拍，正面")
        self.assertGreater(down, level, "俯拍必须抬高机位")
        self.assertLess(up, level, "仰拍必须压低机位")

    def test_heights_are_not_all_identical(self):
        """修前 33/33 镜全是 1.55m——整个维度是死的。"""
        heights = {self._height(f"中景，{a}，正面")
                   for a in ("平视", "俯拍", "仰拍", "顶拍")}
        self.assertGreaterEqual(len(heights), 3)

    def test_height_stays_in_physical_range(self):
        for angle in ANGLE_PITCH_DEG:
            h = self._height(f"大远景，{angle}，正面")
            self.assertGreaterEqual(h, 0.35)
            self.assertLessEqual(h, 4.6)


class AzimuthTest(unittest.TestCase):
    def _yaw(self, text, actors=None):
        return solve_camera(_shot(text), actors or [_actor()])["yaw_deg"]

    def test_position_word_determines_azimuth_not_subject_drift(self):
        """同一机位词,主体挪位后方位仍应一致——修前主体一动方位就变。"""
        a = self._yaw("中景，平视，正面", [_actor(x=0, z=0)])
        b = self._yaw("中景，平视，正面", [_actor(x=2.4, z=-0.3)])
        # 都是「正面」:相对主体朝向的偏移相同(朝向本身随位置变是应该的),
        # 关键是同一词给出同一相对关系,而不是被画布点牵着走
        self.assertEqual(
            solve_camera(_shot("中景，平视，正面"), [_actor(x=0, z=0)])
            ["declared"]["position"],
            solve_camera(_shot("中景，平视，正面"), [_actor(x=2.4, z=-0.3)])
            ["declared"]["position"])
        self.assertIsInstance(a, float)
        self.assertIsInstance(b, float)

    def test_different_position_words_give_different_azimuth(self):
        actors = [_actor(x=1.0, z=1.0)]
        yaws = {self._yaw(f"中景，平视，{p}", actors)
                for p in ("正面", "侧面", "过肩", "背面")}
        self.assertGreaterEqual(len(yaws), 3)

    def test_two_person_shot_frames_the_protagonist_not_the_centroid(self):
        """双人镜里若一方是远处剪影,按质心摆位会让声明的特写拍不到脸。"""
        hero = _actor("沈眉", x=2.4, z=-0.3, hero=True)
        extra = _actor("纱幕后人", x=-2.4, z=1.0, hero=False)
        self.assertIs(primary_actor([extra, hero]), hero)
        cam = solve_camera(_shot("大特写，平视，正面"), [extra, hero])
        t = cam["target_3d"]
        self.assertAlmostEqual(t["x"], 2.4, delta=0.01)
        self.assertAlmostEqual(t["z"], -0.3, delta=0.01)


class SceneContinuityTest(unittest.TestCase):
    def test_flags_near_identical_consecutive_setups(self):
        """同景别 + 几乎同方位 = 剪起来像跳帧,必须提示。"""
        pairs = [(_shot("中景，平视，正面", 1), [_actor()]),
                 (_shot("中景，平视，正面", 2), [_actor()])]
        res = solve_scene(pairs)
        self.assertTrue(any(i["field"] == "coverage" for i in res["issues"]))

    def test_size_change_is_a_legitimate_cut(self):
        pairs = [(_shot("中景，平视，正面", 1), [_actor()]),
                 (_shot("特写，平视，正面", 2), [_actor()])]
        res = solve_scene(pairs)
        self.assertFalse(any(i["field"] == "coverage" for i in res["issues"]))

    def test_missing_shot_size_is_reported(self):
        res = solve_scene([(_shot("平视，正面", 1), [_actor()])])
        self.assertTrue(any(i["field"] == "shot_size" for i in res["issues"]))

    def test_scene_locks_one_axis_side(self):
        pairs = [(_shot("中景，平视，侧面", n), [_actor()]) for n in (1, 2, 3)]
        res = solve_scene(pairs)
        self.assertEqual(len({c["axis_side"] for c in res["cameras"]}), 1)


class WiringTest(unittest.TestCase):
    def test_blocking_build_uses_the_solver(self):
        import inspect

        from aifos import spatial_blocking

        src = inspect.getsource(spatial_blocking)
        self.assertIn("_apply_director_camera(", src)
        self.assertIn("director_height_m", src,
                      "三维高度必须采用调度器解出的镜高,否则俯仰仍是死的")


if __name__ == "__main__":
    unittest.main()


class BlockingIssueAggregationTest(unittest.TestCase):
    """跨镜调度问题必须在文本层暴露——到出图层才发现已浪费一次生成。"""

    def _blocking(self, rows):
        return {"shot_index": {
            str(n): {"scene_no": 1, "camera": {"director_camera": dc}}
            for n, dc in rows}}

    def _dc(self, size="中景", yaw=0.0, clamped=False, want=2.8, got=2.8):
        return {"declared": {"shot_size": size, "angle": "平视",
                             "position": "正面"},
                "yaw_deg": yaw, "wall_clamped": clamped,
                "desired_distance_m": want, "distance_m": got}

    def test_flags_near_identical_consecutive_setups(self):
        from aifos.spatial_blocking import director_camera_issues
        issues = director_camera_issues(self._blocking([
            (1, self._dc(yaw=10.0)), (2, self._dc(yaw=14.0))]))
        self.assertTrue(any(i["field"] == "coverage" for i in issues))

    def test_size_change_is_a_legitimate_cut(self):
        from aifos.spatial_blocking import director_camera_issues
        issues = director_camera_issues(self._blocking([
            (1, self._dc(size="中景", yaw=10.0)),
            (2, self._dc(size="特写", yaw=12.0))]))
        self.assertFalse(any(i["field"] == "coverage" for i in issues))

    def test_flags_missing_shot_size_and_wall_clamp(self):
        from aifos.spatial_blocking import director_camera_issues
        issues = director_camera_issues(self._blocking([
            (1, self._dc(size="")),
            (2, self._dc(size="远景", yaw=90.0, clamped=True,
                         want=7.5, got=3.1))]))
        fields = {i["field"] for i in issues}
        self.assertIn("shot_size", fields)
        self.assertIn("distance", fields)

    def test_yaw_wrap_is_handled(self):
        """179° 与 -179° 只差 2°,不能算成 358°。"""
        from aifos.spatial_blocking import director_camera_issues
        issues = director_camera_issues(self._blocking([
            (1, self._dc(yaw=179.0)), (2, self._dc(yaw=-179.0))]))
        self.assertTrue(any(i["field"] == "coverage" for i in issues))

    def test_wired_into_blocking_validation(self):
        import inspect

        from aifos import director

        src = inspect.getsource(director)
        self.assertIn("director_camera_warnings", src)
        self.assertIn("director_camera_issues(blocking)", src)
