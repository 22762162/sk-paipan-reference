"""经验库归纳:把反复出现的原始观察提炼成可复用通用规则并自动采纳。

病灶(2026-07-28 实测):《雨夜凶杀》攒了 372 条真实质检教训,状态全是
pending_review——"未人工批准前不会注入后续提示词"。没人会手工批 372 条;
而且原始观察是镜头级的("某人左前胸的致命伤不可见"),直接注入所有提示词
反而是污染。解法是先归纳再采纳:只有不含镜头级专名、可跨镜执行的结论
才自动进提示词,原始观察仍保持 pending 可查可撤。
"""
import unittest

from aifos.adapters.claude_script import (build_prompt,
                                          validate_lesson_distill)


OBSERVATIONS = [
    "阿砚左前胸的致命伤完全不可见，没有任何血迹",
    "画面可见真人只有6人，少了1名弓兵",
    "林川右手的短刀被画成了长杆兵器",
]


class DistillPromptTest(unittest.TestCase):
    def test_prompt_numbers_observations_and_states_rules(self):
        prompt = build_prompt("script", {
            "lesson_distill": True,
            "observations": OBSERVATIONS,
            "max_rules": 8})
        self.assertIn("1. 阿砚左前胸", prompt)
        self.assertIn("3. 林川右手", prompt)
        self.assertIn("aifos.lesson_distill.v1", prompt)
        # 铁律必须写进提示词:通用、可核验、不带专名
        self.assertIn("跨镜头普遍适用", prompt)
        self.assertIn("不得出现具体角色名", prompt)
        self.assertIn("最多输出 8 条", prompt)


class DistillValidateTest(unittest.TestCase):
    def _payload(self):
        return {"proper_nouns": ["阿砚", "林川", "缺口单刃短刀"]}

    def test_generalizable_rules_pass(self):
        data = {"schema": "aifos.lesson_distill.v1", "rules": [
            {"rule": "剧情关键伤情与血迹必须在画面上可见成立",
             "covers": [1]},
            {"rule": "功能人物数量必须逐个点清，不得少画", "covers": [2]},
        ]}
        self.assertIsNone(validate_lesson_distill(data, self._payload()))
        self.assertEqual(len(data["rules"]), 2)

    def test_rules_carrying_shot_level_names_are_rejected(self):
        """带专名的规则对新镜头无效,还会把旧剧情污染进新画面。"""
        data = {"schema": "aifos.lesson_distill.v1", "rules": [
            {"rule": "阿砚的致命伤必须画出来"}]}
        error = validate_lesson_distill(data, self._payload())
        self.assertIn("镜头级专名", error)
        self.assertIn("阿砚", error)

    def test_overlong_and_empty_rules_rejected(self):
        payload = self._payload()
        self.assertIn("过长", validate_lesson_distill(
            {"schema": "aifos.lesson_distill.v1",
             "rules": [{"rule": "要注意" * 30}]}, payload))
        self.assertIn("过短", validate_lesson_distill(
            {"schema": "aifos.lesson_distill.v1",
             "rules": [{"rule": "注意"}]}, payload))
        self.assertIn("至少归纳出一条", validate_lesson_distill(
            {"schema": "aifos.lesson_distill.v1", "rules": []}, payload))

    def test_plain_string_rules_are_normalized(self):
        data = {"schema": "aifos.lesson_distill.v1",
                "rules": ["道具形制必须与母版同一件，不得换成别的兵器"]}
        self.assertIsNone(validate_lesson_distill(data, self._payload()))
        self.assertEqual(data["rules"][0]["covers"], [])


class LedgerAdoptionTest(unittest.TestCase):
    class _Assets:
        def __init__(self, rows):
            self.rows = list(rows)
            self.written = []

        def active_list(self, project_id, kind=None):
            return self.rows

        def register(self, project_id, kind, name, meta=None,
                     new_version=False):
            self.written.append((name, meta))

    def test_only_recurring_pending_items_are_distilled(self):
        from aifos.lessons import DOMAIN_IMAGE, pending_observations
        rows = [
            {"name": "l1", "meta": {"issue": "反复出现的问题", "count": 5,
                                    "domain": DOMAIN_IMAGE}},
            {"name": "l2", "meta": {"issue": "只出现一次的即兴抱怨",
                                    "count": 1, "domain": DOMAIN_IMAGE}},
            {"name": "l3", "meta": {"issue": "已批准的规则", "count": 9,
                                    "domain": DOMAIN_IMAGE,
                                    "approved_for_prompt": True,
                                    "status": "approved"}},
        ]
        pending = pending_observations(self._Assets(rows), 1)
        issues = [item["issue"] for item in pending]
        self.assertEqual(issues, ["反复出现的问题"])

    def test_adopted_rules_are_immediately_injectable(self):
        from aifos.lessons import (DISTILLED_SCOPE, adopt_distilled_rules,
                                   lesson_lines)
        assets = self._Assets([])
        written = adopt_distilled_rules(
            assets, 1, [{"rule": "剧情关键伤情必须在画面上可见", "covers": [1]}])
        self.assertEqual(written, 1)
        name, meta = assets.written[0]
        self.assertTrue(name.startswith("distilled:image:"))
        self.assertTrue(meta["approved_for_prompt"])
        self.assertEqual(meta["status"], "approved")
        self.assertEqual(meta["scope"], DISTILLED_SCOPE)
        # 采纳后立刻可被注入(不再需要人工批准)
        injected = lesson_lines(
            self._Assets([{"name": name, "meta": meta}]), 1)
        self.assertEqual(len(injected), 1)
        self.assertIn("剧情关键伤情", injected[0])


if __name__ == "__main__":
    unittest.main()
