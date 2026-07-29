"""repair_contract 从死路变为一次自动修复机会。

病灶:Codex 升级链下达「修合同」指令(instruction_to_aifos),但全仓库没有
任何代码执行它——escalation_redraw_block 等着「合同真的改了(输入哈希
变化)就放行」,而没有人去改,合同类失败只能人工介入。
"""
import inspect
import unittest

from aifos import director


class RepairContractExecTest(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(director)

    def test_first_failure_repair_contract_allows_one_auto_repair(self):
        self.assertIn("auto_contract_repair = bool(", self.src)
        idx = self.src.find("auto_contract_repair = bool(")
        window = self.src[idx:idx + 300]
        self.assertIn('action == "repair_contract"', window)
        self.assertIn("instruction", window)
        # redraw_now 必须同时接受两种可执行动作
        self.assertIn("action == \"targeted_redraw\" or auto_contract_repair",
                      self.src)

    def test_amendment_lands_on_prompt_base_not_feedback(self):
        """修订必须落在 _reference_prompt_base:落 feedback 会被审核清空,
        落 prompt 会在下一轮被 attach_reference_manifest 重建回原始稿。"""
        self.assertIn("【Codex合同修订·必须执行】", self.src)
        idx = self.src.find("【Codex合同修订·必须执行】")
        window = self.src[idx - 800:idx + 900]
        self.assertIn('_reference_prompt_base', window)
        self.assertIn('auto_contract_repair', window)

    def test_amendment_is_idempotent(self):
        idx = self.src.find("【Codex合同修订·必须执行】")
        window = self.src[idx:idx + 700]
        self.assertIn("if amendment not in base", window,
                      "重复进入不得叠加同一条修订")

    def test_escalation_report_carries_the_flag(self):
        self.assertIn('"auto_contract_repair": auto_contract_repair', self.src)


if __name__ == "__main__":
    unittest.main()
