"""知识大脑 → 生产流水线的接线层。

2026-07-31 查证:知识大脑建好后 resolve 只有 Web UI 手动搜索框和单元测试
在调,生产流水线一次都没调——条目激活了也进不了片子。本层补上这段线,
并把"谁该收知识"这条边界钉死:写作类能力(LLM 接收)才收,图片/视频能力
(生成模型接收)不收,那类知识必须由人改写进生产代码模块。
"""
import unittest

from aifos import knowledge_apply
from aifos.adapters.claude_script import build_prompt


def _entry(key, stages, *, title="", principles=("原则一",),
           workflow=("步骤一",), gates=("质检一",)):
    return {
        "knowledge_key": key,
        "title": title or key,
        "version": 1,
        "content": {
            "principles": list(principles),
            "workflow": list(workflow),
            "quality_gates": list(gates),
        },
        "applicability": {"stages": list(stages)},
    }


class FakeBrain:
    """按 stage 过滤的最小 resolve,复刻真 brain 的筛选口径。"""

    def __init__(self, items, explode=False, stale=()):
        self.items = items
        self.explode = explode
        self.stale = list(stale)
        self.calls = []

    def resolve(self, *, stage="", query="", limit=4, **_kw):
        self.calls.append({"stage": stage, "query": query, "limit": limit})
        if self.explode:
            raise RuntimeError("库炸了")
        matches = [
            item for item in self.items
            if stage in (item["applicability"]["stages"])
            or "cross_stage" in item["applicability"]["stages"]
        ]
        return {"matches": matches[:limit], "skipped_stale": self.stale}


class StageRoutingTest(unittest.TestCase):
    def test_only_writing_capabilities_get_knowledge(self):
        """图片/视频/配音能力不注入——生成模型读不懂工作流指导。"""
        for capability in ("image", "frames", "cover", "video", "voice"):
            self.assertEqual(
                knowledge_apply.stage_for(capability, {}), "", capability)

    def test_script_flags_pick_the_real_stage(self):
        cases = {
            "story_analysis": "script",
            "character_design": "cast",
            "shot_repair": "storyboard",
            "prop_design": "text_assets",
            "prompt_refine": "cast",
        }
        for flag, stage in cases.items():
            self.assertEqual(
                knowledge_apply.stage_for("script", {flag: True}), stage, flag)
        # 不带开关的裸编剧调用仍归 script 阶段
        self.assertEqual(knowledge_apply.stage_for("script", {}), "script")
        self.assertEqual(knowledge_apply.stage_for("storyboard", {}),
                         "storyboard")
        self.assertEqual(knowledge_apply.stage_for("image_qc", {}), "review")

    def test_query_uses_shot_facts_not_whole_script(self):
        """检索词只取"这一镜在干什么",整份剧本 JSON 不能进来。"""
        query = knowledge_apply.query_for({
            "style": "古装悬疑",
            "shot": {"camera": "近景", "description": "灯笼旁翻卷宗"},
            "script": {"scenes": ["x" * 5000]},
        })
        self.assertIn("古装悬疑", query)
        self.assertIn("灯笼旁翻卷宗", query)
        self.assertNotIn("xxxx", query)
        self.assertLessEqual(len(query), 600)


class RankingTest(unittest.TestCase):
    def test_stage_specific_beats_cross_stage(self):
        """brain 给两者同分,但通用条目不该把对口条目挤出名额。"""
        generic = _entry("generic", ["cross_stage"])
        specific = _entry("specific", ["storyboard"])
        ranked = knowledge_apply.rank([generic, specific], "storyboard")
        self.assertEqual([item["knowledge_key"] for item in ranked],
                         ["specific", "generic"])

    def test_ranking_is_stable_within_a_group(self):
        items = [_entry("a", ["storyboard"]), _entry("b", ["storyboard"])]
        self.assertEqual(
            [i["knowledge_key"] for i in knowledge_apply.rank(items, "storyboard")],
            ["a", "b"])


class AttachTest(unittest.TestCase):
    def test_attach_puts_directives_on_payload(self):
        brain = FakeBrain([_entry("k1", ["storyboard"], title="两层视频提示词")])
        payload = {"script": {}}
        self.assertTrue(knowledge_apply.attach(brain, "storyboard", payload))
        block = payload["knowledge_directives"]
        self.assertIn("知识大脑", block)
        self.assertIn("两层视频提示词", block)
        self.assertIn("原则", block)
        self.assertIn("质检", block)

    def test_image_capability_never_attaches(self):
        brain = FakeBrain([_entry("k1", ["images"])])
        payload = {"prompt": "一张图"}
        self.assertFalse(knowledge_apply.attach(brain, "image", payload))
        self.assertNotIn("knowledge_directives", payload)
        self.assertEqual(brain.calls, [])   # 连查都不该查

    def test_brain_failure_never_breaks_production(self):
        """知识是增益,不能因为它把生产跑挂。"""
        brain = FakeBrain([], explode=True)
        payload = {"script": {}}
        self.assertFalse(knowledge_apply.attach(brain, "storyboard", payload))
        self.assertNotIn("knowledge_directives", payload)

    def test_stale_entries_are_shouted_about(self):
        """制作标准一升版,全部知识会一次性对不上并被静默跳过。

        这是接线最容易白做的地方:成片悄悄变差,没人知道该去 refresh。
        """
        brain = FakeBrain([_entry("k1", ["storyboard"])],
                          stale=["motivated-lighting-contract"])
        warnings = []
        knowledge_apply.attach(brain, "storyboard", {"script": {}},
                               warn=warnings.append)
        self.assertEqual(len(warnings), 1)
        self.assertIn("motivated-lighting-contract", warnings[0])
        self.assertIn("refresh", warnings[0])

    def test_no_brain_is_a_no_op(self):
        payload = {"script": {}}
        self.assertFalse(knowledge_apply.attach(None, "storyboard", payload))
        self.assertNotIn("knowledge_directives", payload)

    def test_total_budget_is_capped(self):
        """知识是配料不是主菜:再多条目也不能撑爆提示词。"""
        fat = ["很长的原则" * 200]
        brain = FakeBrain([
            _entry(f"k{i}", ["storyboard"], principles=fat) for i in range(6)
        ])
        payload = {"script": {}}
        knowledge_apply.attach(brain, "storyboard", payload)
        self.assertLessEqual(
            len(payload["knowledge_directives"]),
            knowledge_apply.MAX_TOTAL_CHARS + 400)


class PromptRenderingTest(unittest.TestCase):
    def test_directives_reach_the_writer_prompt(self):
        payload = {"script": {"title": "x"},
                   "knowledge_directives": "【知识大脑·已验证方法】某条"}
        prompt = build_prompt("storyboard", payload)
        self.assertTrue(prompt.endswith("【知识大脑·已验证方法】某条"))

    def test_prompt_unchanged_without_directives(self):
        base = build_prompt("storyboard", {"script": {"title": "x"}})
        self.assertNotIn("知识大脑", base)


# ---- 端到端:真 App + 真知识大脑,验证接线确实通了 ----
# 上面的用例都用假 brain 验逻辑;这一段用 App(tmp_path) 起真库,内置种子
# 会被 ensure_seed 自动激活,从而验证 app.py→router→payload 这条线。

import pytest                                              # noqa: E402

from aifos.app import App                                  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws", config_overrides={})
    yield instance
    instance.close()


def test_router_attaches_knowledge_to_writing_capability(app):
    """分镜能力过一次 router,payload 上就该多出知识指令。"""
    payload = {"script": {"title": "测试", "scenes": []}}
    app.router.call("storyboard", payload, app.workspace.artifacts_dir)
    block = payload.get("knowledge_directives", "")
    assert "知识大脑" in block
    # 内置种子 depth-structure-control 覆盖 storyboard 阶段
    assert "深度结构控制" in block


def test_router_leaves_image_payload_untouched(app):
    """图片能力的提示词预算不许被知识指令挤占。"""
    payload = {"shot_no": 1, "prompt": "一张图"}
    app.router.call("image", payload, app.workspace.artifacts_dir)
    assert "knowledge_directives" not in payload


def test_router_without_knowledge_behaves_as_before(app):
    """没接知识大脑时行为与接线前完全一致。"""
    app.router.knowledge = None
    payload = {"script": {"title": "测试", "scenes": []}}
    app.router.call("storyboard", payload, app.workspace.artifacts_dir)
    assert "knowledge_directives" not in payload


if __name__ == "__main__":
    unittest.main()
