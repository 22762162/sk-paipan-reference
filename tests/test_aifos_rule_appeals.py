"""规则上诉庭:死规则初审 → AI 仲裁复核 → 误杀放行入台账。

用户拍板(2026-07-28):规则是死的、剧情是活的。今晚五起熔断全是
字面化误杀(25-30 没写"岁"、「共7名可见真人」不认、繁简异体、
include/exclude 交集、freeze 可推导)。规则继续做零成本初审,
判败的上诉;撤销必须逐字举证,防 LLM 空口翻案。
"""
import json
import tempfile
import unittest
from pathlib import Path

from aifos.adapters.claude_script import (build_prompt,
                                          validate_rule_appeal)
from aifos.rule_appeals import (FIX_RULE_THRESHOLD, format_appeal_table,
                                record_appeal, rules_needing_fix,
                                summarize_appeals)


SUBJECT = "画面严格共7名可见真人：3名登记角色林川、赵百户、阿砚，加4名巡检弓兵"


class AppealPromptTest(unittest.TestCase):
    def test_prompt_carries_rule_subject_and_evidence_duty(self):
        prompt = build_prompt("script", {
            "rule_appeal": True,
            "rule_id": "prompt_review.immutable_facts",
            "rule_reason": "优化稿删除了不可变事实：人物总数",
            "subject": SUBJECT,
            "context": {"expected_character_count": 3},
            "adjudication": {"policy": "六级优先级"}})
        self.assertIn("prompt_review.immutable_facts", prompt)
        self.assertIn("严格共7名可见真人", prompt)
        self.assertIn("逐字引用", prompt)
        self.assertIn("aifos.rule_appeal.v1", prompt)
        # 不确定时必须维持原判(宁可多修一次,不可放行错图)
        self.assertIn("不确定时维持原判", prompt)


class AppealValidateTest(unittest.TestCase):
    def test_overturn_with_real_quote_passes(self):
        data = {"schema": "aifos.rule_appeal.v1", "verdict": "overturned",
                "reason": "人数以分组写法保留,3+4=7 与合同一致",
                "evidence": "3名登记角色林川、赵百户、阿砚，加4名巡检弓兵",
                "suggested_rule_fix": "人数校验应接受分组求和写法"}
        self.assertIsNone(
            validate_rule_appeal(data, {"subject": SUBJECT}))

    def test_overturn_without_evidence_rejected(self):
        data = {"schema": "aifos.rule_appeal.v1", "verdict": "overturned",
                "reason": "我觉得没问题", "evidence": ""}
        self.assertIn(
            "逐字引用", validate_rule_appeal(data, {"subject": SUBJECT}))

    def test_fabricated_evidence_rejected(self):
        """空口翻案的主要形态:编一句原文里没有的话。"""
        data = {"schema": "aifos.rule_appeal.v1", "verdict": "overturned",
                "reason": "人数写在这里",
                "evidence": "画面共有七名人物出场"}
        self.assertIn(
            "原文", validate_rule_appeal(data, {"subject": SUBJECT}))

    def test_upheld_needs_no_evidence(self):
        data = {"schema": "aifos.rule_appeal.v1", "verdict": "upheld",
                "reason": "优化稿确实把 3 人写成 2 人", "evidence": ""}
        self.assertIsNone(
            validate_rule_appeal(data, {"subject": SUBJECT}))

    def test_bad_verdict_rejected(self):
        data = {"schema": "aifos.rule_appeal.v1", "verdict": "maybe",
                "reason": "x"}
        self.assertIn("verdict", validate_rule_appeal(data, {}))


class AppealLedgerTest(unittest.TestCase):
    def test_record_summarize_and_fix_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 台账路径由产物目录的 artifacts 锚点推导(与通道/质检台账同址)
            out_dir = Path(tmp) / "artifacts" / "p013" / "e001" / "images"
            out_dir.mkdir(parents=True)
            logs = Path(tmp) / "logs"
            for _ in range(FIX_RULE_THRESHOLD):
                record_appeal(
                    out_dir, episode_id=26, item_id="shot:2",
                    rule_id="dispatch_contract.validation",
                    verdict="overturned",
                    rule_reason="include/exclude 交集",
                    arbiter_reason="显式作用域已裁决,非冲突",
                    evidence="wardrobe",
                    suggested_rule_fix="显式声明时不应并入默认排除")
            record_appeal(
                out_dir, episode_id=26, item_id="shot:9",
                rule_id="prompt_review.immutable_facts",
                verdict="upheld", rule_reason="人数被改",
                arbiter_reason="优化稿确实把3人写成2人")
            summary = summarize_appeals(logs)
            self.assertEqual(summary["total"], FIX_RULE_THRESHOLD + 1)
            self.assertEqual(summary["overturned"], FIX_RULE_THRESHOLD)
            self.assertEqual(summary["upheld"], 1)
            flagged = rules_needing_fix(summary)
            self.assertEqual(len(flagged), 1)
            self.assertEqual(
                flagged[0]["rule_id"], "dispatch_contract.validation")
            table = format_appeal_table(summary)
            self.assertIn("该改规则", table)
            # 台账是可解析 JSONL,供后续统计与固化决策
            rows = [json.loads(line) for line in
                    (logs / "rule-appeals.jsonl").read_text(
                        encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), FIX_RULE_THRESHOLD + 1)

    def test_empty_ledger_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = summarize_appeals(Path(tmp))
            self.assertEqual(summary["total"], 0)
            self.assertIn("没有规则上诉记录", format_appeal_table(summary))


if __name__ == "__main__":
    unittest.main()


class DualReviewMergeTest(unittest.TestCase):
    """双路会诊:两路都看全图、各自深查一半;判定取共识、问题取并集、
    互斥意见单列交仲裁(绝不把矛盾指令一起塞给修图模型)。"""

    def _director(self):
        from aifos.director import Director
        return Director.__new__(Director)

    def test_lenses_cover_full_checklist_between_them(self):
        from aifos.adapters.claude_script import REVIEW_LENSES, build_qc_prompt
        self.assertEqual(set(REVIEW_LENSES), {"identity", "scene"})
        joint = " ".join(item["focus"] for item in REVIEW_LENSES.values())
        for dimension in ("人数", "骨相", "性别", "物种", "道具",
                          "文字", "物理", "服装", "景别", "光线"):
            self.assertIn(dimension, joint, dimension)
        prompt = build_qc_prompt({
            "image_uri": "/tmp/x.png", "characters": ["林川"],
            "identity_references": [], "camera": "中景",
            "review_lens": "identity"})
        self.assertIn("本次重点核查", prompt)
        # 分工不等于只看一半:非重点维度仍要通扫
        self.assertIn("其余维度仍要通扫", prompt)

    def test_consensus_verdict_and_union_of_issues(self):
        director = self._director()
        merged = director._merge_dual_verdicts(
            {"pass": True, "issues": [], "detected_count": 7},
            {"pass": False, "issues": ["核心道具短刀不在场", "多余字幕条"]})
        self.assertFalse(merged["pass"])          # 任一路不过即不过
        self.assertEqual(len(merged["issues"]), 2)
        self.assertFalse(merged["dual_review"]["agreed"])
        self.assertEqual(merged["detected_count"], 7)

    def test_same_issue_two_phrasings_deduped_and_ranked(self):
        director = self._director()
        merged = director._merge_dual_verdicts(
            {"pass": False, "issues": ["画面可见真人只有6人，少了1名弓兵"]},
            {"pass": False, "issues": ["画面可见真人只有6人,少了1名弓兵",
                                       "服装色系不符"]})
        self.assertEqual(len(merged["issues"]), 2)
        self.assertIn("弓兵", merged["issues"][0])   # 决定性问题排最前

    def test_contradictory_instructions_flagged(self):
        director = self._director()
        merged = director._merge_dual_verdicts(
            {"pass": False, "issues": ["林川右手缺少缺口单刃短刀，应当出现"]},
            {"pass": False, "issues": ["林川右手多余的缺口单刃短刀，不应出现"]})
        self.assertTrue(merged["dual_review"]["conflicts"])

    def test_unrelated_add_and_drop_not_flagged(self):
        """一个说缺道具、一个说多个人,不是矛盾,不该误报。"""
        director = self._director()
        merged = director._merge_dual_verdicts(
            {"pass": False, "issues": ["缺少核心道具青灰文书包"]},
            {"pass": False, "issues": ["画面多出一名背景路人，不应出现"]})
        self.assertFalse(merged["dual_review"]["conflicts"])
