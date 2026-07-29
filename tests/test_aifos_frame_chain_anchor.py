"""帧链锚点必须来自紧邻的上一镜，不能是几镜之前的旧尾帧。

真实病理：`last_by_scene` 只在成功时写入、从不删除，而旧门禁只问
「这一场有没有过尾帧」。镜3 失败后字典里还留着镜2 的尾帧，镜4 就把
镜2 的尾帧当成自己的首帧，静默跳过镜3——合同声称「上一镜尾帧=本镜
首帧」，实际接的是两镜之前的画面，且全程不报错。
"""
import inspect
import re
import unittest

from aifos import director


class FrameChainAnchorTest(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(director)

    def test_anchor_records_its_source_shot(self):
        """两个写入点都必须记下锚点属于哪一镜，否则无从判断是否紧邻。"""
        # 两个写入点的下标写法不同(scene_no 与 task["scene"]),
        # 嵌套方括号不能用 [^\]]+ 匹配。
        writes = re.findall(
            r"last_by_scene\[.+?\]\s*=\s*\{(.{0,240}?)\}",
            self.src, re.S)
        self.assertGreaterEqual(len(writes), 2,
                                "复用与新生成两条路径都要写锚点")
        for body in writes:
            self.assertIn("shot_no", body,
                          "锚点必须带 shot_no，否则无法校验是否紧邻")

    def test_guard_compares_anchor_against_immediately_previous_shot(self):
        self.assertIn("expected_prev", self.src)
        idx = self.src.find("expected_prev")
        window = self.src[idx:idx + 900]
        self.assertIn("chain[round_no - 1]", window,
                      "期望锚点应取自同场链上紧邻的上一镜")
        self.assertIn('anchor.get("shot_no") != expected_prev', window,
                      "锚点镜号与期望不符时必须拦下，不能沿用")

    def test_stale_anchor_is_reported_not_silently_used(self):
        """静默使用旧锚点是最难发现的断链形式，必须把镜号写进错误里。"""
        idx = self.src.find("expected_prev")
        window = self.src[idx:idx + 900]
        self.assertIn("不是紧邻的上一镜", window)
        self.assertIn("_plan_mark", window)


if __name__ == "__main__":
    unittest.main()
