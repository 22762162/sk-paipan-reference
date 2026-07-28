"""空间语言:3D 空间调度的世界坐标 → 可核验的画面文字合同。

2026-07-28 盘点:spatial_blocking 已算出完整三维调度(人物起终点世界
坐标、身高、姿态、朝向;摄影机三维位置、瞄准点、焦段、视场角),但
只渲染成示意图上传,一个数字都没进提示词——模型得"看懂图",我们还
要额外叮嘱"别把箭头画进成片"。用户要求:让 3D 空间图发挥更大作用,
图片视频参考更稳更一致,并让人物精准定位与行动路线可执行。
"""
import unittest

from aifos.prompt_contract import compile_shot_prompt
from aifos.spatial_language import (derive_movement_term, framing_conflict,
                                    framing_for_distance, motion_clause,
                                    project, screen_direction_clause,
                                    screen_zone, spatial_lines,
                                    staging_clause)


def _block(**overrides):
    """两人对话:摄影机在 z=+4 看向原点,甲在左、乙在右。"""
    block = {
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
        "actors": [
            {"name": "甲", "moving": False,
             "start_3d": {"x": -1.2, "y": 0.0, "z": 0.0},
             "end_3d": {"x": -1.2, "y": 0.0, "z": 0.0},
             "pose_label_start": "站姿", "support_start": "双脚/地面",
             "facing": "身体朝东，视线看乙"},
            {"name": "乙", "moving": False,
             "start_3d": {"x": 1.2, "y": 0.0, "z": 1.5},
             "end_3d": {"x": 1.2, "y": 0.0, "z": 1.5},
             "pose_label_start": "站姿", "support_start": "双脚/地面",
             "facing": "身体朝西，视线看甲"},
        ],
    }
    block.update(overrides)
    return block


class ProjectionTest(unittest.TestCase):
    def test_left_and_right_actors_land_on_correct_screen_side(self):
        block = _block()
        cam = block["camera"]
        left, _d = project(cam["start_3d"], cam["target_3d"],
                           block["actors"][0]["start_3d"],
                           cam["fov_degrees"])
        right, _d2 = project(cam["start_3d"], cam["target_3d"],
                             block["actors"][1]["start_3d"],
                             cam["fov_degrees"])
        self.assertLess(left, 0, "x 为负的人物应落在画面左侧")
        self.assertGreater(right, 0, "x 为正的人物应落在画面右侧")
        self.assertIn("左", screen_zone(left))
        self.assertIn("右", screen_zone(right))

    def test_missing_coordinates_return_none_not_a_guess(self):
        offset, distance = project(None, {"x": 0}, {"x": 1})
        self.assertIsNone(offset)
        self.assertIsNone(distance)

    def test_distance_maps_to_framing_band(self):
        self.assertEqual(framing_for_distance(0.8), "特写")
        self.assertEqual(framing_for_distance(2.8), "中景")
        self.assertEqual(framing_for_distance(9.0), "远景")


class StagingClauseTest(unittest.TestCase):
    def test_positions_depth_order_and_eyelines(self):
        clause = staging_clause(_block())
        self.assertIn("甲", clause)
        self.assertIn("距摄影机", clause)
        # 乙更近(z=1.5 vs 0.0,摄影机在 z=4) → 遮挡序里排前
        self.assertIn("由近及远的遮挡顺序:乙→甲", clause)
        self.assertIn("禁止把远处人物画在近处人物之前", clause)
        # 甲看乙 → 甲朝向画面右侧
        self.assertIn("甲朝向画面右侧", clause)
        self.assertIn("乙朝向画面左侧", clause)

    def test_no_actors_yields_no_clause(self):
        self.assertEqual(staging_clause({"actors": []}), "")


class MotionClauseTest(unittest.TestCase):
    def test_route_states_zones_and_distance_change(self):
        """用户点名:光说"向右走"没有约束力,要说清从哪个分区到哪个
        分区、离摄影机是近了还是远了。"""
        block = _block()
        block["actors"][0].update({
            "moving": True,
            "end_3d": {"x": 1.4, "y": 0.0, "z": 2.6},
            "pose_label_end": "跪姿",
        })
        clause = motion_clause(block)
        self.assertIn("甲:", clause)
        self.assertIn("移动到", clause)
        self.assertIn("走近摄影机", clause)
        self.assertIn("姿态由站姿变为跪姿", clause)
        self.assertIn("静态关键帧只定格所属相位", clause)

    def test_static_shot_has_no_route(self):
        self.assertEqual(motion_clause(_block()), "")


class ScreenDirectionTest(unittest.TestCase):
    def test_axis_lock_declared_for_two_actors(self):
        clause = screen_direction_clause(_block())
        self.assertIn("甲在画面左侧", clause)
        self.assertIn("乙在画面右侧", clause)
        self.assertIn("180度轴线法则", clause)

    def test_same_zone_actors_get_no_false_lock(self):
        """两人都在中央时锁"一左一右"只会造出互斥合同(实测 ep24)。"""
        block = _block()
        block["actors"][1]["start_3d"] = {"x": -1.15, "y": 0.0, "z": 0.0}
        self.assertEqual(screen_direction_clause(block), "")

    def test_phase_is_honoured(self):
        """必须与空间站位同相位,否则站位说中央、方向说一左一右。"""
        block = _block()
        for actor in block["actors"]:
            actor["end_3d"] = {"x": 0.0, "y": 0.0, "z": 0.5}
        self.assertEqual(screen_direction_clause(block, phase="end"), "")


class MovementAndConflictTest(unittest.TestCase):
    def test_camera_path_derives_movement_term(self):
        block = _block()
        self.assertEqual(derive_movement_term(block["camera"]), "固定")
        pushed = dict(block["camera"], end_3d={"x": 0.0, "y": 1.5, "z": 2.0})
        self.assertEqual(derive_movement_term(pushed), "推")
        pulled = dict(block["camera"], end_3d={"x": 0.0, "y": 1.5, "z": 6.5})
        self.assertEqual(derive_movement_term(pulled), "拉")
        lifted = dict(block["camera"], end_3d={"x": 0.0, "y": 3.2, "z": 4.0})
        self.assertEqual(derive_movement_term(lifted), "升降")

    def test_framing_conflict_only_fires_on_real_mismatch(self):
        block = _block()   # 最近人物约 2.5 米 = 中景带
        self.assertEqual(framing_conflict(block, "中景"), "")
        self.assertEqual(framing_conflict(block, "近景"), "",
                         "相邻档不报,避免噪音")
        conflict = framing_conflict(block, "大特写")
        self.assertIn("景别与空间调度不一致", conflict)
        self.assertIn("请调整机位距离或改写景别", conflict)


class ContractInjectionTest(unittest.TestCase):
    def test_shot_contract_and_prompt_carry_spatial_lines(self):
        shot = {"shot_no": 1, "scene_no": 1, "kind": "dialogue",
                "duration": 3.0, "characters": ["甲", "乙"],
                "dialogue": None, "prompt": "p",
                "camera": "中景·平视·正面",
                "description": "两人对峙",
                "spatial_blocking": _block()}
        contract, compact = compile_shot_prompt(
            shot, location="废茶棚", style="写实古装悬疑", mode="image")
        staging = contract.get("spatial_staging") or {}
        self.assertIn("空间站位", staging)
        self.assertIn("屏幕方向", staging)
        self.assertIn("【空间站位】", compact)
        self.assertIn("【屏幕方向】", compact)

    def test_missing_blocking_is_silent(self):
        shot = {"shot_no": 1, "scene_no": 1, "kind": "environment",
                "duration": 3.0, "characters": [], "dialogue": None,
                "prompt": "p", "camera": "远景", "description": "空镜"}
        contract, compact = compile_shot_prompt(
            shot, location="废茶棚", style="写实", mode="image")
        self.assertEqual(contract.get("spatial_staging"), {})
        self.assertNotIn("【空间站位】", compact)

    def test_qc_prompt_shares_the_same_spatial_facts(self):
        from aifos.adapters.claude_script import build_qc_prompt
        prompt = build_qc_prompt({
            "image_uri": "/tmp/x.png", "characters": ["甲"],
            "identity_references": [], "camera": "中景",
            "spatial_staging": {
                "空间站位": "甲在画面左三分之一,距摄影机2.4米",
                "行动路线": "自画面左侧走到中央"}})
        self.assertIn("空间调度核验", prompt)
        self.assertIn("画面左三分之一", prompt)


if __name__ == "__main__":
    unittest.main()
