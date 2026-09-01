"""「迟到的发现」穿帮检测:信息状态 × 空间事实交叉校验。

v7 实证:全景重排把纱幔窗挪到人物正前方,人影从镜1就与她近距同场,
她镜7才「惊疑发现」——发现拍点穿帮。空间一变,「谁能看见谁」必须重推。
"""
import unittest

from aifos.spatial_blocking import awareness_sightline_issues


def _actor(name, x, z):
    return {"name": name, "start_3d": {"x": x, "y": 0, "z": z},
            "end_3d": {"x": x, "y": 0, "z": z}}


def _setup(distance=2.0, discovery_shot=7, early_shot=1, scene=1):
    storyboard = {"shots": [
        {"shot_no": early_shot, "scene_no": scene,
         "action": "沈眉低头整理旧卷"},
        {"shot_no": discovery_shot, "scene_no": scene,
         "action": "沈眉抬眸望向纱幔,惊疑地发现幕后人影"},
    ]}
    blocking = {"shot_index": {
        str(early_shot): {"actors": [
            _actor("沈眉", 0, 0), _actor("纱幕后人", 0, distance)]},
        str(discovery_shot): {"actors": [
            _actor("沈眉", 0, 0), _actor("纱幕后人", 0, distance)]},
    }}
    return storyboard, blocking


class AwarenessSightlineTest(unittest.TestCase):
    def test_late_discovery_with_early_copresence_warns(self):
        issues = awareness_sightline_issues(*_setup(distance=2.0))
        self.assertEqual(len(issues), 1)
        item = issues[0]
        self.assertEqual(item["shot_no"], 7)
        self.assertEqual(item["earlier_shot_no"], 1)
        self.assertEqual(item["severity"], "warning",
                         "只警示不阻断:近距同场可以有合法理由,但必须是有意选择")
        self.assertIn("穿帮", item["message"])

    def test_far_copresence_is_fine(self):
        self.assertEqual(
            awareness_sightline_issues(*_setup(distance=9.0)), [])

    def test_different_scene_is_fine(self):
        storyboard, blocking = _setup()
        storyboard["shots"][0]["scene_no"] = 2
        self.assertEqual(
            awareness_sightline_issues(storyboard, blocking), [])

    def test_no_discovery_cue_is_fine(self):
        storyboard, blocking = _setup()
        storyboard["shots"][1]["action"] = "沈眉望向窗外看天色"
        self.assertEqual(
            awareness_sightline_issues(storyboard, blocking), [])

    def test_target_absent_earlier_is_fine(self):
        """对方更晚才出现——正确的悬念结构,不得误报。"""
        storyboard, blocking = _setup()
        blocking["shot_index"]["1"]["actors"] = [_actor("沈眉", 0, 0)]
        self.assertEqual(
            awareness_sightline_issues(storyboard, blocking), [])


if __name__ == "__main__":
    unittest.main()
