"""首尾帧合同去重：共用段只说一次。

真实病理（EP1 实测）：一条 frames 提示词 9153 字、35 段，两半各 17 段
结构完全相同，其中 13 段逐字节重复（主体 435、参考图职责 1468、
画风 464、道具定格 363…），合计 4348 字 = 全文 47.5% 是纯复制。
真正区分首尾帧的只有【核心画面】【定格状态】两段、不到 270 字。
模型要在两份几乎一样的合同里找那几处差别，差别反而被淹没。
"""
import unittest

from aifos.prompt_contract import merge_frame_compacts


FIRST = ("【核心画面】银铃仍在案上\n"
         "【主体】严格共1人：沈眉\n"
         "【画风】鎏金柔雾、超写实\n"
         "【定格状态】右拳尚未成形\n"
         "【参考图职责】图1=沈眉最终立绘，只锁脸\n"
         "【硬约束】只执行一个主动作")
LAST = ("【核心画面】银铃已握入掌心\n"
        "【主体】严格共1人：沈眉\n"
        "【画风】鎏金柔雾、超写实\n"
        "【定格状态】右手握拳，露一截红绳\n"
        "【参考图职责】图1=沈眉最终立绘，只锁脸\n"
        "【硬约束】只执行一个主动作")


class MergeFrameCompactsTest(unittest.TestCase):
    def test_shared_segments_appear_exactly_once(self):
        merged = merge_frame_compacts(FIRST, LAST)
        self.assertIsNotNone(merged)
        for shared in ("【主体】严格共1人：沈眉",
                       "【画风】鎏金柔雾、超写实",
                       "【参考图职责】图1=沈眉最终立绘，只锁脸",
                       "【硬约束】只执行一个主动作"):
            self.assertEqual(merged.count(shared), 1,
                             f"共用段应只出现一次: {shared}")

    def test_phase_specific_segments_are_labelled_by_owner(self):
        merged = merge_frame_compacts(FIRST, LAST)
        self.assertIn("【仅首帧】", merged)
        self.assertIn("【仅尾帧】", merged)
        first_part = merged.split("【仅首帧】", 1)[1].split("【仅尾帧】", 1)[0]
        last_part = merged.split("【仅尾帧】", 1)[1]
        self.assertIn("银铃仍在案上", first_part)
        self.assertIn("右拳尚未成形", first_part)
        self.assertIn("银铃已握入掌心", last_part)
        self.assertIn("露一截红绳", last_part)

    def test_no_fact_is_lost(self):
        merged = merge_frame_compacts(FIRST, LAST)
        for line in (FIRST + "\n" + LAST).split("\n"):
            self.assertIn(line.strip(), merged,
                          f"合并不得丢事实: {line}")

    def test_merged_is_shorter_than_naive_concat(self):
        naive = FIRST + "\n" + LAST
        merged = merge_frame_compacts(FIRST, LAST)
        self.assertLess(len(merged), len(naive) + 200)

    def test_identical_compacts_return_none_so_caller_can_fall_back(self):
        """两份完全一致说明相位没生效，这本身是缺陷，不能悄悄合并掉。"""
        self.assertIsNone(merge_frame_compacts(FIRST, FIRST))

    def test_unparseable_input_falls_back_to_none(self):
        self.assertIsNone(merge_frame_compacts("", LAST))
        self.assertIsNone(merge_frame_compacts(FIRST, ""))
        self.assertIsNone(merge_frame_compacts(None, None))


if __name__ == "__main__":
    unittest.main()
