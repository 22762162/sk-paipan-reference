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

    def test_contract_repair_uses_replacement_not_amendment(self):
        """失败合同必须从运行时提示词删除，不能靠高优先级后缀遮盖。"""
        self.assertIn("_replace_repair_static_contract(", self.src)
        self.assertNotIn("【Codex合同修订·必须执行】", self.src)
        self.assertIn("auto_contract_repair_replacement", self.src)

    def test_manifest_refresh_does_not_recompile_failed_contract(self):
        method = inspect.getsource(director.Director._attach_reference_manifest)
        idx = method.find('if payload.get("_repair_static_contract_replaced")')
        window = method[idx:idx + 1700]
        self.assertIn('payload["prompt_compact"] = base_prompt', window)
        self.assertIn("return", window)

    def test_escalation_report_carries_the_flag(self):
        self.assertIn('"auto_contract_repair": auto_contract_repair', self.src)


if __name__ == "__main__":
    unittest.main()
