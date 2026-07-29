"""视线必须分相位：首帧不能拿到尾帧视线。

真实病理（2026-07-29《长夏记事》EP1 实测，7/8 镜 100% 复现）：
`spatial_blocking` 只写一个 `facing`，取的是 `state_end.direction`。
于是镜6 的**首帧**合同同时下发两条互斥指令——
  【定格状态】银铃仍由木面承托、右拳尚未成形（= 起点）
  【空间站位】视线仍落在右拳（= 终点）
要求模型注视一个此刻还不存在的东西。首帧图带着尾帧视线出图，
首尾帧驱动的视频段起点就是「已经转过头」的画面，中段没有可演的
转头过程，模型只能自行编造——这就是观众看到的「相邻帧互相打架」。
"""
import unittest

from aifos.spatial_language import staging_clause


def _block(facing_start=None, facing_end=None, facing=None):
    """单人镜：摄影机在 z=+4 看向原点，人物在画面中央。"""
    actor = {
        "name": "沈眉", "moving": False,
        "start_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
        "end_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
        "pose_label_start": "站姿", "pose_label_end": "站姿",
        "support_start": "双脚/地面", "support_end": "双脚/地面",
    }
    if facing_start is not None:
        actor["facing_start"] = facing_start
    if facing_end is not None:
        actor["facing_end"] = facing_end
    if facing is not None:
        actor["facing"] = facing
    return {
        "shot_no": 1,
        "camera": {
            "start_3d": {"x": 0.0, "y": 1.5, "z": 4.0},
            "end_3d": {"x": 0.0, "y": 1.5, "z": 4.0},
            "target_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
            "target_start_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
            "target_end_3d": {"x": 0.0, "y": 1.2, "z": 0.0},
            "fov_degrees": 54.4,
            "movement": "固定",
        },
        "actors": [actor],
    }


class GazePhaseTest(unittest.TestCase):
    def test_start_phase_uses_start_facing_not_end_facing(self):
        block = _block(facing_start="视线落在案上的银铃",
                       facing_end="视线转向左侧纱幕",
                       facing="视线转向左侧纱幕")
        start = staging_clause(block, phase="start")
        self.assertIn("银铃", start)
        self.assertNotIn("纱幕", start, "首帧不得携带尾帧视线")

    def test_end_phase_uses_end_facing(self):
        block = _block(facing_start="视线落在案上的银铃",
                       facing_end="视线转向左侧纱幕",
                       facing="视线转向左侧纱幕")
        end = staging_clause(block, phase="end")
        self.assertIn("纱幕", end)
        self.assertNotIn("银铃", end)

    def test_start_and_end_differ_when_direction_changes(self):
        block = _block(facing_start="视线落在案上的银铃",
                       facing_end="视线转向左侧纱幕",
                       facing="视线转向左侧纱幕")
        self.assertNotEqual(staging_clause(block, phase="start"),
                            staging_clause(block, phase="end"))

    def test_legacy_block_with_only_facing_renders_unchanged(self):
        """存量 blocking 文档只有 facing 键，渲染结果必须与改动前一致。"""
        legacy = _block(facing="视线转向左侧纱幕")
        start = staging_clause(legacy, phase="start")
        end = staging_clause(legacy, phase="end")
        self.assertEqual(start, end,
                         "无分相位数据时两相位应回落到同一个 facing")
        self.assertIn("纱幕", start)


class DialogueAxisTest(unittest.TestCase):
    def test_axis_override_writes_all_three_facing_keys(self):
        """双人镜刻意覆写朝向锁 180° 轴线；只改 facing 会被新键绕过。"""
        import inspect

        from aifos import spatial_blocking

        src = inspect.getsource(spatial_blocking)
        marker = 'actor["facing"] = f"面向{other[\'name\']}"'
        self.assertIn(marker, src)
        tail = src.split(marker, 1)[1][:400]
        self.assertIn('facing_start', tail,
                      "轴线覆写必须同时写 facing_start")
        self.assertIn('facing_end', tail,
                      "轴线覆写必须同时写 facing_end")


class BeatDirectionTest(unittest.TestCase):
    def test_beat_fallback_does_not_inject_template_direction(self):
        """beat/reaction 兜底不得用模板朝向覆盖继承来的具体朝向。

        「面向本镜主体，视线不越轴」在单人镜里自指，且「不越轴」是给
        摄影指导看的规则而非可画的画面事实——对图像模型等价于随便画。
        """
        import inspect

        from aifos import workflow

        src = inspect.getsource(workflow)
        self.assertIn("_STATE_DEFAULT_DIRECTION", src)
        # 兜底分支之后必须有「继承起点朝向 / 否则删掉该键」的处理
        idx = src.find('pose="保持原位，完成眼神与呼吸变化"')
        self.assertGreater(idx, 0)
        tail = src[idx:idx + 900]
        self.assertIn("inherited_direction", tail)
        self.assertIn('explicit_state.pop("direction"', tail)


class GazeInstructionTest(unittest.TestCase):
    def test_gaze_is_derived_not_a_hardcoded_option_menu(self):
        """旧值「凝视/瞥向对手或核心物件」是未决选项，不是指令。

        它对全片全角色恒等，对视频模型是零信息量噪声；首尾帧图片里
        视线已被【空间站位】定死，视频提示词却从不复述，模型于是在
        中段拥有完全的视线自由裁量权。
        """
        import inspect

        from aifos import workflow

        src = inspect.getsource(workflow)
        self.assertNotIn('actor_gaze = "凝视/瞥向对手或核心物件"', src,
                         "硬编码的选项菜单必须被派生指令取代")
        self.assertIn("中段视线只在这两者之间连续过渡", src)
        self.assertIn("不得看镜头", src)


if __name__ == "__main__":
    unittest.main()
