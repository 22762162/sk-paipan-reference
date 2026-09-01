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


class DurationRepairTest(unittest.TestCase):
    """时长默认锁死;只有熔断原因就是时长违规时才授权本次修复动
    duration,且必须落在可提交档内并同步改写表演内容。"""

    @staticmethod
    def _data(**extra):
        return {"schema": "aifos.shot_repair.v1", "shot_no": 12,
                "camera": "全景·仰拍·侧面",
                "repair_summary": "拉满表演到5秒", **extra}

    def test_unauthorized_duration_output_rejected(self):
        error = validate_shot_repair(
            self._data(duration=5), {"shot": SHOT})
        self.assertIn("未授权", error)

    def test_authorized_repair_needs_performance_rewrite(self):
        payload = {"shot": SHOT, "allow_duration_change": True}
        error = validate_shot_repair(self._data(duration=5), payload)
        self.assertIn("description", error)
        self.assertIsNone(validate_shot_repair(
            self._data(duration=5, description="补上黑衣人抬眼的反应拍"),
            payload))

    def test_authorized_repair_must_land_in_legal_band(self):
        payload = {"shot": SHOT, "allow_duration_change": True}
        self.assertIn("4秒", validate_shot_repair(
            self._data(duration=3, description="x"), payload))
        self.assertIn("拆分", validate_shot_repair(
            self._data(duration=22, description="x"), payload))
        self.assertIn("秒数", validate_shot_repair(
            self._data(duration="很久", description="x"), payload))

    def test_prompt_switches_duration_rule_by_authorization(self):
        base = {"shot_repair": True, "shot": SHOT, "location": "废茶棚",
                "style": "写实", "blocking_reason": "本镜声明3秒,低于硬下限"}
        locked = build_prompt("script", base)
        self.assertIn("不得改动时长", locked)
        allowed = build_prompt(
            "script", {**base, "allow_duration_change": True})
        self.assertIn("被授权且必须把 duration 修到合法档内", allowed)
        self.assertIn("禁止只改数字不改表演内容", allowed)


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


class ExplicitAgeRangeTest(unittest.TestCase):
    """身份门禁:裸数字年龄区间(25-30)不能因缺「岁」字被打回人工。

    实测:凡人修仙传 script v5 陈师兄 age_range="25-30",门禁却报
    「年龄段尚未明确」熔断整条 cast 产线——校验器过度字面化。"""

    def test_bare_numeric_ranges_accepted(self):
        from aifos.identity_facts import explicit_age_range
        for value in ("25-30", "17", "25至30", "25~30", "25-30岁", "青年"):
            self.assertTrue(explicit_age_range(value), value)

    def test_ambiguous_values_still_rejected(self):
        from aifos.identity_facts import explicit_age_range
        for value in ("", "未知", "模糊", "3000", "随便"):
            self.assertFalse(explicit_age_range(value), value)

    def test_chen_shixiong_card_resolves(self):
        from aifos.identity_facts import unresolved_identity_fields
        self.assertEqual(
            unresolved_identity_fields(
                {"gender": "男", "age_range": "25-30"}), [])


class StagingRepairTest(unittest.TestCase):
    """走位默认锁死;previz 判定走位类问题时授权改写调度,不许动剧情。"""

    def test_prompt_switches_staging_rule_by_authorization(self):
        base = {"shot_repair": True, "shot": SHOT, "location": "废茶棚",
                "style": "写实",
                "blocking_reason": "林川的移动路径在44%处穿过长桌"}
        locked = build_prompt("script", base)
        self.assertIn("不得改变任何人物朝向、站位与行走路线", locked)
        allowed = build_prompt(
            "script", {**base, "allow_staging_change": True})
        self.assertIn("被授权且必须在 description 里改写", allowed)
        self.assertIn("绕开写明的障碍物", allowed)
        self.assertIn("不改剧情语义", allowed)
        # 两种授权互不串线:走位授权不解锁时长
        self.assertIn("不得改动时长", allowed)
