"""阻断即修:提示词审核熔断镜头的编剧就地修复(capability=script+shot_repair)。

真实案例:《雨夜凶杀》镜头12要求 85mm特写+肩线以下出画,同时要求
黑衣人/林川/阿砚 3 人全部可见并分布在东北缺口、中央柱、西北木板
三个空间区域——几何互斥,审核正确熔断;但 resume 撞同一份坏数据
只会原地再熔断。修复通道:编剧只改景别/取景表述,不许动剧情事实。
"""
import unittest

from aifos.adapters.claude_script import (build_prompt,
                                          validate_shot_repair)
from aifos.rule_governance import prompt_adjudication_clause


SHOT = {
    "shot_no": 12, "scene_no": 3, "duration": 3.0,
    "camera": "85mm特写·仰拍·侧面",
    "description": "三人分布三区域的收束定格",
    "characters": ["黑衣人", "林川", "阿砚"],
    "prompt": "p",
}


class ShotRepairPromptTest(unittest.TestCase):
    def test_build_prompt_carries_shot_and_reason(self):
        prompt = build_prompt("script", {
            "shot_repair": True, "shot": SHOT,
            "location": "废茶棚", "style": "写实",
            "blocking_reason": "特写与3人三区域同框互斥"})
        self.assertIn("85mm特写", prompt)
        self.assertIn("特写与3人三区域同框互斥", prompt)
        self.assertIn("aifos.shot_repair.v1", prompt)
        # 修复边界必须写明:只许改 camera/取景,不许动剧情事实
        self.assertIn("不得增删人物", prompt)
        self.assertIn("\"shot_no\": 12", prompt)


class ShotRepairValidateTest(unittest.TestCase):
    def test_valid_repair_passes(self):
        data = {"schema": "aifos.shot_repair.v1", "shot_no": 12,
                "camera": "全景·仰拍·侧面",
                "repair_summary": "特写改全景以容纳三人三区域"}
        self.assertIsNone(validate_shot_repair(data, {"shot": SHOT}))

    def test_wrong_shot_no_rejected(self):
        data = {"schema": "aifos.shot_repair.v1", "shot_no": 13,
                "camera": "全景", "repair_summary": "x"}
        self.assertIn("shot_no", validate_shot_repair(data, {"shot": SHOT}))

    def test_camera_structure_must_match_dict_original(self):
        shot = {**SHOT, "camera": {"scale": "特写", "angle": "仰拍"}}
        data = {"schema": "aifos.shot_repair.v1", "shot_no": 12,
                "camera": "全景", "repair_summary": "x"}
        self.assertIn("同结构", validate_shot_repair(data, {"shot": shot}))
        data["camera"] = {"scale": "全景", "angle": "仰拍"}
        self.assertIsNone(validate_shot_repair(data, {"shot": shot}))

    def test_missing_summary_rejected(self):
        data = {"schema": "aifos.shot_repair.v1", "shot_no": 12,
                "camera": "全景"}
        self.assertIn("repair_summary",
                      validate_shot_repair(data, {"shot": SHOT}))


class TraditionalSimplifiedClauseTest(unittest.TestCase):
    def test_adjudication_covers_script_variants(self):
        """縣/县 曾以「用户级同级互斥」熔断整段;裁决条款必须明写
        繁简异体是同一事实,执行字形以 must_keep_verbatim 为准。"""
        policy = prompt_adjudication_clause()["policy"]
        self.assertIn("繁体/简体/异体字形", policy)
        self.assertIn("縣/县", policy)
        self.assertIn("must_keep_verbatim", policy)
        self.assertIn("不算新增或修改文字", policy)


class ExtractJsonMendTest(unittest.TestCase):
    """错误制导 JSON 修补:40KB 合法分镜不再因笔误全盘报废。"""

    def test_stray_brace_before_object_member(self):
        from aifos.adapters.claude_script import extract_json
        # 凡人修仙传实测笔误:状态表成员前多出悬空 {
        raw = ('{"shots":[{"shot_no":1,"start_state":{'
               '"韩立":{"pose":"立"},{"李长老":{"pose":"站"}}}],'
               '"prop_registry":[{"prop_id":"p1"},{"prop_id":"p2"}]}')
        data = extract_json(raw)
        self.assertEqual(len(data["shots"]), 1)
        self.assertEqual(
            data["shots"][0]["start_state"]["李长老"]["pose"], "站")
        # 数组里合法的 },{ 绝不能被误伤
        self.assertEqual(
            [p["prop_id"] for p in data["prop_registry"]], ["p1", "p2"])

    def test_wrapped_member_with_balanced_braces(self):
        from aifos.adapters.claude_script import extract_json
        raw = '{"s":{"甲":{"p":1},{"乙":{"p":2}},"t":[1]}}'
        self.assertEqual(
            extract_json(raw),
            {"s": {"甲": {"p": 1}, "乙": {"p": 2}}, "t": [1]})

    def test_trailing_comma_and_noise(self):
        from aifos.adapters.claude_script import extract_json
        raw = '思考过程… {"a":[1,2,],"b":{"k":1,}} 收尾说明'
        self.assertEqual(extract_json(raw), {"a": [1, 2], "b": {"k": 1}})

    def test_fragment_fallback_still_works(self):
        from aifos.adapters.claude_script import extract_json
        self.assertEqual(
            extract_json('杂讯 {"small":1} 杂讯'), {"small": 1})

    def test_unfixable_returns_largest_fragment(self):
        from aifos.adapters.claude_script import extract_json
        raw = '{"broken": [[[ {"ok":{"x":1}}'
        self.assertEqual(extract_json(raw), {"ok": {"x": 1}})


if __name__ == "__main__":
    unittest.main()
