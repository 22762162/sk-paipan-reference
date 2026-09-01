"""闸门 I:相邻帧对比 QC——场景漂移从人眼兜底变机器拦截。

真实病理(2026-07-29 实测):单帧重画后凭空多出一盏灯笼、书架换成满架书、
纱幕变厚重暖帘、色温整体偏橙——与前后帧完全剪不到一起。QC 其实早就随
请求收到了 role=continuity 的对照图(收集序列含 chain_first_uri),
但判据里从没要求对照它查场景,漂移全靠人眼发现。
"""
import unittest

from aifos.adapters.claude_script import build_qc_prompt


def _payload(manifest):
    return {"image_uri": "/tmp/x.png", "characters": ["沈眉"],
            "count": 1, "reference_manifest": manifest}


CONTINUITY = {"uri": "/tmp/prev.png", "label": "上一镜结尾画面",
              "role": "continuity"}
IDENTITY = {"uri": "/tmp/id.png", "label": "沈眉最终立绘",
            "role": "identity"}


class SceneContinuityQcTest(unittest.TestCase):
    def test_continuity_ref_activates_comparison_criteria(self):
        prompt = build_qc_prompt(_payload([CONTINUITY, IDENTITY]))
        self.assertIn("上一镜结尾画面", prompt)
        self.assertIn("同场对照基准", prompt)
        # 判据必须点名实测漂移形态,而不是一句空泛的「保持一致」
        self.assertIn("对照图中不存在的", prompt)
        self.assertIn("色温明显偏移", prompt)

    def test_drift_is_a_visual_failure_not_a_suggestion(self):
        prompt = build_qc_prompt(_payload([CONTINUITY]))
        idx = prompt.find("同场对照基准")
        window = prompt[idx:idx + 400]
        self.assertIn("visual_pass 必须为 false", window)

    def test_actor_motion_is_exempt_from_scene_comparison(self):
        """人物动作/视线按合同变化是本镜的职责,不得被对比误伤。"""
        prompt = build_qc_prompt(_payload([CONTINUITY]))
        self.assertIn("不参与本项对比", prompt)

    def test_no_continuity_ref_skips_cleanly(self):
        prompt = build_qc_prompt(_payload([IDENTITY]))
        self.assertIn("无同场对照帧", prompt)
        self.assertNotIn("同场对照基准", prompt)

    def test_identity_refs_never_treated_as_scene_baseline(self):
        """立绘是棚拍图,拿它当场景基准会把所有真实场景判成漂移。"""
        prompt = build_qc_prompt(_payload([IDENTITY]))
        self.assertNotIn("沈眉最终立绘」为", prompt)


if __name__ == "__main__":
    unittest.main()
