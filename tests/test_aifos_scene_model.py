"""全景 → 真实三维场景:落地点射线求交是精确解,不是深度估计。

此前全景只被当贴图用——看着像那间屋,但平台并不知道「书案在哪、多大、
人能不能站进去」。人物位置只能靠文字,物理逻辑(遮挡/接触/可达/碰撞)
无从校验。
"""
import math
import unittest

from aifos.scene_model import (DEFAULT_CAPTURE_HEIGHT_M, actor_placement_issues,
                               build_object, build_scene_model,
                               direction_from_equirect,
                               equirect_from_direction, find_object,
                               floor_point, height_at, overlap_issues)


ROOM = {"floor_width_m": 10.0, "floor_depth_m": 7.0}


class ProjectionTest(unittest.TestCase):
    def test_center_maps_to_plus_z(self):
        """u=0.5 必须对应 +Z——与 pano_slice / scene3d 着色器同一约定。"""
        dx, dy, dz = direction_from_equirect(0.5, 0.5)
        self.assertAlmostEqual(dx, 0.0, places=6)
        self.assertAlmostEqual(dy, 0.0, places=6)
        self.assertAlmostEqual(dz, 1.0, places=6)

    def test_quarter_right_maps_to_plus_x(self):
        dx, _dy, dz = direction_from_equirect(0.75, 0.5)
        self.assertAlmostEqual(dx, 1.0, places=6)
        self.assertAlmostEqual(dz, 0.0, places=6)

    def test_roundtrip_is_stable(self):
        for u, v in ((0.1, 0.3), (0.5, 0.5), (0.9, 0.72), (0.33, 0.61)):
            d = direction_from_equirect(u, v)
            back = equirect_from_direction(*d)
            self.assertAlmostEqual(back[0], u, places=5)
            self.assertAlmostEqual(back[1], v, places=5)


class FloorRayTest(unittest.TestCase):
    def test_straight_down_lands_at_capture_footprint(self):
        self.assertEqual(floor_point(0.5, 1.0), (0.0, 0.0))

    def test_known_geometry_is_exact_not_estimated(self):
        """已知点回推:先由目标点算出它在全景里的像素,再解回来必须重合。"""
        for target in ((2.4, -0.3), (-1.2, 2.8), (3.0, 3.0)):
            dx = target[0] - 0.0
            dz = target[1] - 0.0
            dy = -DEFAULT_CAPTURE_HEIGHT_M
            u, v = equirect_from_direction(dx, dy, dz)
            got = floor_point(u, v)
            self.assertIsNotNone(got)
            self.assertAlmostEqual(got[0], target[0], places=2)
            self.assertAlmostEqual(got[1], target[1], places=2)

    def test_horizon_and_upward_have_no_floor_hit(self):
        self.assertIsNone(floor_point(0.5, 0.5))     # 正好水平
        self.assertIsNone(floor_point(0.5, 0.2))     # 朝上

    def test_far_grazing_ray_is_rejected_not_absurd(self):
        """接近水平的视线会把交点推到无穷远,必须拒绝而不是给荒谬坐标。"""
        self.assertIsNone(floor_point(0.5, 0.5 + 1e-4))


class HeightTest(unittest.TestCase):
    def test_height_of_known_object(self):
        """1.2m 高、距拍摄点 3m 的物体顶点回推,应解回 1.2m。"""
        x, z, h = 0.0, 3.0, 1.2
        u, v = equirect_from_direction(x, h - DEFAULT_CAPTURE_HEIGHT_M, z)
        self.assertAlmostEqual(height_at(u, v, x, z), h, places=2)

    def test_object_taller_than_camera(self):
        x, z, h = 2.0, 0.0, 2.6
        u, v = equirect_from_direction(x, h - DEFAULT_CAPTURE_HEIGHT_M, z)
        self.assertAlmostEqual(height_at(u, v, x, z), h, places=2)


class BuildObjectTest(unittest.TestCase):
    def _annot(self, x, z, **extra):
        u, v = equirect_from_direction(x, -DEFAULT_CAPTURE_HEIGHT_M, z)
        a = {"name": "书案", "category": "furniture", "base_u": u, "base_v": v}
        a.update(extra)
        return a

    def test_position_is_solved_from_base_pixel(self):
        obj = build_object(self._annot(1.5, 2.0), room=ROOM)
        self.assertAlmostEqual(obj["position_3d"]["x"], 1.5, places=1)
        self.assertAlmostEqual(obj["position_3d"]["z"], 2.0, places=1)
        self.assertTrue(obj["inside_room"])

    def test_missing_base_yields_nothing(self):
        self.assertIsNone(build_object({"name": "书案"}))
        self.assertIsNone(build_object({"base_u": 0.5, "base_v": 0.9}))

    def test_out_of_room_is_flagged_and_clamped(self):
        obj = build_object(self._annot(20.0, 0.0), room=ROOM)
        self.assertFalse(obj["inside_room"])
        self.assertLessEqual(abs(obj["position_3d"]["x"]), 5.0)


class SceneModelTest(unittest.TestCase):
    def _annot(self, name, x, z, **extra):
        u, v = equirect_from_direction(x, -DEFAULT_CAPTURE_HEIGHT_M, z)
        a = {"name": name, "category": "furniture", "base_u": u, "base_v": v}
        a.update(extra)
        return a

    def test_builds_objects_and_keeps_capture_point(self):
        model = build_scene_model(
            [self._annot("书案", 0.0, 0.6), self._annot("书架", -3.0, 2.0)],
            location="书阁内·暖金古室", room=ROOM)
        self.assertEqual(len(model["objects"]), 2)
        self.assertEqual(model["capture"]["y"], DEFAULT_CAPTURE_HEIGHT_M)
        self.assertIsNotNone(find_object(model, "书案"))

    def test_unparseable_annotation_is_reported_not_silently_dropped(self):
        model = build_scene_model([{"name": "香炉"}], room=ROOM)
        self.assertEqual(model["objects"], [])
        self.assertTrue(any(i["field"] == "annotation"
                            for i in model["issues"]))

    def test_overlapping_objects_are_flagged(self):
        model = build_scene_model(
            [self._annot("书案", 1.0, 1.0), self._annot("香案", 1.05, 1.0)],
            room=ROOM)
        self.assertTrue(any(i["field"] == "overlap" for i in model["issues"]))


class ActorPlacementTest(unittest.TestCase):
    """真实三维场景的兑现点:人物位置从此可被物理校验。"""

    def _model(self):
        u, v = equirect_from_direction(0.0, -DEFAULT_CAPTURE_HEIGHT_M, 0.6)
        return build_scene_model(
            [{"name": "书案", "category": "furniture",
              "base_u": u, "base_v": v, "width_u": 0.12}], room=ROOM)

    def test_actor_standing_inside_furniture_is_flagged(self):
        issues = actor_placement_issues(
            self._model(),
            [{"name": "沈眉", "start_3d": {"x": 0.0, "y": 0, "z": 0.6}}])
        self.assertTrue(any(i["field"] == "actor_furniture" for i in issues))

    def test_actor_with_clearance_is_fine(self):
        issues = actor_placement_issues(
            self._model(),
            [{"name": "沈眉", "start_3d": {"x": 3.5, "y": 0, "z": -2.0}}])
        self.assertFalse(any(i["field"] == "actor_furniture" for i in issues))

    def test_actor_outside_room_is_a_block(self):
        issues = actor_placement_issues(
            self._model(),
            [{"name": "沈眉", "start_3d": {"x": 12.0, "y": 0, "z": 0.0}}])
        self.assertTrue(any(i["severity"] == "block"
                            and i["field"] == "actor_bounds" for i in issues))


if __name__ == "__main__":
    unittest.main()
