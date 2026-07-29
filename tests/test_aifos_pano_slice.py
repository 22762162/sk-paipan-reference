"""全景机位切片:blocking 3D → v360 确定性背景投影。"""
import math
import subprocess
import unittest
from pathlib import Path

from aifos.pano_slice import (find_ffmpeg, slice_for_block, slice_panorama,
                              view_params_from_block)


def _block(cam=(0, 1.55, -2.6), target=(0, 1.2, 0), fov=46):
    return {"camera": {
        "start_3d": {"x": cam[0], "y": cam[1], "z": cam[2]},
        "target_3d": {"x": target[0], "y": target[1], "z": target[2]},
        "fov_degrees": fov}}


class ViewParamsTest(unittest.TestCase):
    def test_north_facing_camera_maps_to_yaw_zero(self):
        yaw, pitch, hfov = view_params_from_block(_block())
        self.assertAlmostEqual(yaw, 0.0)
        self.assertLess(pitch, 0, "镜高于瞄点应为俯视(负pitch)")
        self.assertAlmostEqual(hfov, 46.0)

    def test_eastward_target_yields_positive_yaw(self):
        yaw, _p, _f = view_params_from_block(
            _block(cam=(0, 1.5, 0), target=(2, 1.5, 2)))
        self.assertAlmostEqual(yaw, 45.0, places=1)

    def test_bad_fov_falls_back_to_medium(self):
        _y, _p, hfov = view_params_from_block(_block(fov=999))
        self.assertEqual(hfov, 46.0)

    def test_missing_coords_return_none(self):
        self.assertIsNone(view_params_from_block({"camera": {}}))
        self.assertIsNone(view_params_from_block(None))

    def test_extreme_pitch_is_clamped(self):
        _y, pitch, _f = view_params_from_block(
            _block(cam=(0, 10, -0.5), target=(0, 0, 0)))
        self.assertGreaterEqual(pitch, -40.0)


@unittest.skipUnless(find_ffmpeg(), "本机无 ffmpeg")
class SliceTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.pano = self.dir / "pano.png"
        subprocess.run(
            [find_ffmpeg(), "-y", "-f", "lavfi",
             "-i", "color=c=orange:s=128x64", "-frames:v", "1",
             str(self.pano)],
            check=True, capture_output=True, timeout=60)

    def test_slice_produces_image_and_caches(self):
        out1 = slice_panorama(self.pano, self.dir / "s", 0, 2, 46,
                              size=(90, 160))
        self.assertTrue(out1 and Path(out1).exists())
        mtime = Path(out1).stat().st_mtime_ns
        out2 = slice_panorama(self.pano, self.dir / "s", 0, 2, 46,
                              size=(90, 160))
        self.assertEqual(out1, out2)
        self.assertEqual(Path(out2).stat().st_mtime_ns, mtime,
                         "同参数必须走缓存,不重切")

    def test_slice_for_block_end_to_end(self):
        out = slice_for_block(self.pano, self.dir / "s2", _block())
        self.assertTrue(out and Path(out).exists())

    def test_missing_pano_returns_empty(self):
        self.assertEqual(
            slice_panorama(self.dir / "nope.png", self.dir, 0, 0, 46), "")

    def test_bad_block_returns_empty(self):
        self.assertEqual(slice_for_block(self.pano, self.dir, {}), "")


class WiringTest(unittest.TestCase):
    def test_art_refs_prefers_slice_then_falls_back(self):
        import inspect

        from aifos import director

        src = inspect.getsource(director)
        self.assertIn("_scene_slice_for_shot(ctx, location, shot_no)", src)
        idx = src.find("_scene_slice_for_shot(ctx, location, shot_no)")
        window = src[idx:idx + 1200]
        self.assertIn("_scene_view_reference", window,
                      "切片失败必须回退到静态视角母版")

    def test_storyboard_stage_ensures_scene_masters_first(self):
        import inspect

        from aifos import director

        src = inspect.getsource(director.Director._stage_storyboard)
        self.assertIn("_ensure_space_first_scenes", src.split("\n")[1],
                      "空间前置必须是分镜阶段的第一步")


if __name__ == "__main__":
    unittest.main()
