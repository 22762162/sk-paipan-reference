"""codex 适配器提示词瘦身:绑定不复注、状态不转储 dict。

实测病灶(EP1):参考图绑定文字在同一条提示词里被注入两次,合计占下发稿
约47%;start/end_state 以 Python dict repr 入词,单条 1022 字(20%),带英文
键名且与上文自然语言合同完全重复;审核压缩 58% 后适配器又原样加回。
"""
import unittest

from aifos.adapters.codex_image import _ref_line, _state_brief


MANIFEST = [{"index": 1, "uri": "/a.png", "label": "沈眉立绘",
             "binding": "只锁脸型五官"}]


class RefLineDedupTest(unittest.TestCase):
    def test_table_already_in_prompt_yields_pointer_only(self):
        line = _ref_line({"reference_manifest": MANIFEST},
                         "……。参考图对照表(共1张,按此顺序提交):图1=……")
        self.assertIn("已在上文", line)
        self.assertNotIn("/a.png", line, "不得把整表再展开一遍")
        # 「必须真实打开读取」的强制指令要保住
        self.assertIn("逐张真实打开读取", line)

    def test_no_table_in_prompt_renders_full_manifest(self):
        line = _ref_line({"reference_manifest": MANIFEST}, "画面内容……")
        self.assertIn("/a.png", line)
        self.assertIn("只锁脸型五官", line)

    def test_missing_prompt_text_keeps_legacy_behaviour(self):
        line = _ref_line({"reference_manifest": MANIFEST})
        self.assertIn("/a.png", line)


class StateBriefTest(unittest.TestCase):
    def test_dict_repr_never_leaks(self):
        brief = _state_brief({"沈眉": {"pose": "立于案右",
                                      "direction": "看向指尖",
                                      "wardrobe_state": {"k": "v"}}})
        self.assertNotIn("{", brief)
        self.assertNotIn("'", brief)
        self.assertIn("立于案右", brief)

    def test_empty_state_points_to_contract(self):
        self.assertEqual(_state_brief({}), "见上文合同")
        self.assertEqual(_state_brief(None), "见上文合同")

    def test_output_is_bounded(self):
        state = {f"人{i}": {"pose": "长" * 200} for i in range(9)}
        self.assertLessEqual(len(_state_brief(state)), 300)


if __name__ == "__main__":
    unittest.main()
