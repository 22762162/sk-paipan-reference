"""光影语言层:补齐"怎么打光"这一维,并按题材给出不同视听基调。

2026-07-28 盘点:镜头语言只有景别/角度/机位共 14 条,全库关于光的
表述只有一句"主光方向保持一致"的连续性锚点——提示词从没告诉模型
怎么打光,模型只能给平光,成片自然没有氛围(用户拿参考图问"生成的图
怎么没这种光影效果")。用户同时要求:不同风格的漫剧要有不同镜头效果。
"""
import unittest

from aifos.camera_language import (ANGLE_GEOMETRY, COMPOSITION_GEOMETRY,
                                   MOVEMENT_GEOMETRY, POSITION_GEOMETRY,
                                   SCALE_GEOMETRY, camera_geometry_clause)
from aifos.lighting_language import (GENRE_LOOKS, LIGHTING_STYLES,
                                     lighting_clause, lighting_lines,
                                     match_genre, select_lighting)
from aifos.prompt_contract import compile_shot_prompt


SHOT = {"shot_no": 3, "scene_no": 1, "kind": "dialogue", "duration": 3.0,
        "characters": ["柳蘅"], "dialogue": None, "prompt": "p"}


class LightingSelectionTest(unittest.TestCase):
    def test_practical_light_wins_when_lamps_are_in_frame(self):
        """画面里点着灯笼就必须由灯笼供光——硬事实压过题材默认。"""
        style, extras = select_lighting(
            location="沈府书房", time_of_day="夜", camera="特写",
            scene_action="案上香炉青烟,窗边灯笼与烛火摇曳",
            genre="甜宠言情")
        self.assertEqual(style, "practical_lit")
        self.assertIn("volumetric", extras)   # 有香烟 → 体积光
        self.assertIn("warm_cool", extras)
        self.assertIn("shallow_dof", extras)  # 特写 → 浅景深

    def test_wide_shots_use_deep_focus(self):
        _style, extras = select_lighting(
            location="山门广场", time_of_day="清晨", camera="远景")
        self.assertIn("deep_focus", extras)
        self.assertNotIn("shallow_dof", extras)

    def test_clause_names_verifiable_features(self):
        """条款必须是可核验的画面特征,不是"氛围感"这种空话。"""
        clause = lighting_clause(
            location="废茶棚", time_of_day="雨夜", camera="近景",
            scene_action="油灯将熄", genre="古装悬疑")
        for feature in ("发丝", "暗部", "冷暖对比", "光柱"):
            self.assertIn(feature, clause, feature)
        self.assertIn("服从本场景母版", clause)   # 不与连续性锚点打架


class MotivatedLightingTest(unittest.TestCase):
    """动机化打光合同(知识大脑 motivated-lighting-contract)的落地。"""

    def test_practical_light_does_not_declare_a_second_key_light(self):
        """回归:灯笼当主光时,不能再拼一句「主光位于人物后方偏侧」。

        原 bug:practical_lit 选中后无条件 append rim,而 rim_backlight
        整段开头就在声明主光位置,一条提示词里两个主光互相打架。
        """
        clause = lighting_clause(
            location="沈府书房", time_of_day="深夜", camera="近景",
            scene_action="就着案上灯笼翻看密信", genre="古装悬疑")
        self.assertIn("实用光源主导", clause)
        self.assertNotIn("主光位于人物后方偏侧", clause)
        self.assertIn("轮廓分离(辅助光,不是主光)", clause)
        # 全条款里"主光位于"只能出现一次(即被选中的那个灯型自己)
        self.assertLessEqual(clause.count("主光位于"), 1, clause)

    def test_rim_as_key_style_keeps_full_clause(self):
        """rim 本身是主灯型时,仍要保留完整的主光位置描述。"""
        clause = lighting_clause(
            location="山门", time_of_day="清晨", camera="全景",
            genre="仙侠修真")
        self.assertIn("主光位于人物后方偏侧", clause)

    def test_causality_clauses_close_up(self):
        """近景要写全受光区域、暗部、补光、眼神光与背景衰减。"""
        clause = lighting_clause(
            location="沈府书房", time_of_day="深夜", camera="近景",
            scene_action="就着案上灯笼翻看密信")
        for feature in ("受光区域", "补光", "眼神光", "暗一至两档", "亮斑"):
            self.assertIn(feature, clause, feature)

    def test_no_catchlight_in_wide_shots(self):
        """远景看不见眼睛,强行要眼神光只是给模型加噪声。"""
        clause = lighting_clause(
            location="山门广场", time_of_day="夜", camera="远景",
            scene_action="灯笼列队")
        self.assertNotIn("眼神光", clause)
        self.assertIn("受光区域", clause)
        self.assertNotIn("近侧脸颊", clause)   # 远景写环境层次,不写五官

    def test_negatives_cover_the_observed_failure_modes(self):
        """A/B 实测拍到的两种病灶必须进负面清单。"""
        lines = lighting_lines(
            "电影级3D半写实", location="档案室", time_of_day="夜",
            camera="近景", scene_action="灯笼旁翻卷宗")
        negatives = lines[-1]
        self.assertIn("没点亮却出现它的照明效果", negatives)
        self.assertIn("无来源亮斑", negatives)
        self.assertIn("两个方向互相矛盾的主光", negatives)


class GenreLookTest(unittest.TestCase):
    def test_each_genre_matched_and_distinct(self):
        expected = {
            "凡人修仙传·修真门派": "xianxia",
            "雨夜凶杀·古装悬疑": "suspense",
            "霸道总裁的甜宠日常": "romance",
            "深宫权谋": "palace",
            "江湖刀客": "wuxia",
            "都市职场剧": "urban",
            "阴宅灵异": "horror",
        }
        for text, key in expected.items():
            self.assertEqual(match_genre(text), key, text)
        # 每个题材盘都指向真实存在的布光风格
        for look in GENRE_LOOKS.values():
            self.assertIn(look["style"], LIGHTING_STYLES)

    def test_genres_produce_different_base_looks(self):
        """不同题材必须长出不同的样子,不能全是同一副面孔。"""
        looks = {
            genre: select_lighting(
                location="室外空地", time_of_day="白天",
                camera="近景", genre=genre)[0]
            for genre in ("仙侠修真", "古装悬疑", "甜宠言情", "宫斗权谋")
        }
        self.assertGreaterEqual(len(set(looks.values())), 3, looks)

    def test_genre_grammar_reaches_the_clause(self):
        clause = lighting_clause(
            location="山门", time_of_day="清晨", camera="全景",
            genre="仙侠修真")
        self.assertIn("仙侠", clause)
        self.assertIn("题材视听基调", clause)


class CameraVocabularyTest(unittest.TestCase):
    def test_all_five_dimensions_present(self):
        """镜头语言从 3 维(14条)扩到 5 维:补运镜与构图。"""
        self.assertGreaterEqual(len(SCALE_GEOMETRY), 10)
        self.assertGreaterEqual(len(ANGLE_GEOMETRY), 8)
        self.assertGreaterEqual(len(POSITION_GEOMETRY), 7)
        self.assertGreaterEqual(len(MOVEMENT_GEOMETRY), 9)
        self.assertGreaterEqual(len(COMPOSITION_GEOMETRY), 8)

    def test_new_terms_translate_to_geometry(self):
        clause = camera_geometry_clause({
            "景别": "中近景", "角度": "斜角", "机位": "四分之三面",
            "运镜": "环绕", "构图": "框中框"})
        for term in ("中近景", "斜角", "四分之三面", "环绕", "框中框"):
            self.assertIn(term, clause, term)
        # 斜角要说清是画面倾斜而不是人歪了
        self.assertIn("人物本身不得歪斜", clause)


class ContractInjectionTest(unittest.TestCase):
    def test_shot_contract_carries_lighting_line(self):
        contract, compact = compile_shot_prompt(
            {**SHOT, "camera": "近景·平视·正面",
             "description": "夜里书房,案上香炉青烟,窗边灯笼"},
            location="沈府书房", style="电影级3D半写实·古装悬疑",
            mode="image")
        self.assertTrue(contract["lighting"])
        self.assertIn("【光影】", compact)

    def test_non_realistic_styles_get_no_photography_terms(self):
        """Q版/二次元不塞摄影术语(与真实感层同口径)。"""
        contract, compact = compile_shot_prompt(
            {**SHOT, "camera": "近景", "description": "夜里书房,灯笼"},
            location="沈府书房", style="Q版二次元", mode="image")
        self.assertEqual(contract["lighting"], "")
        self.assertNotIn("【光影】", compact)
        self.assertEqual(lighting_lines("Q版二次元", location="书房"), [])


if __name__ == "__main__":
    unittest.main()


class AiDirectorTest(unittest.TestCase):
    """AI 导演:专业词汇约束下的逐镜导演建议(人审后同通道执行)。"""

    def _payload(self, count=1):
        return {"visible_count": count}

    def test_prompt_carries_facts_vocab_and_principles(self):
        from aifos.adapters.claude_script import build_prompt
        prompt = build_prompt("script", {
            "ai_director": True,
            "genre": "古装悬疑·悬疑/罪案",
            "genre_grammar": "低调硬光,框中框制造窥视感",
            "shot_facts": {"shot_no": 5, "description": "赵典吏查看官凭",
                           "必须可见人数": 1},
            "context_shots": [{"位置": "上一镜", "camera": "中景"}],
            "spatial": "赵典吏在画面左三分之一,距摄影机2.1米",
            "qc_issues": ["景别偏宽"],
            "vocabulary": {"景别": ["特写", "中景"]}})
        for token in ("AI 导演", "赵典吏查看官凭", "低调硬光", "景别偏宽",
                      "容量", "轴线", "不改剧情", "aifos.ai_director.v1"):
            self.assertIn(token, prompt, token)
        # 阐述未填写时槽位有明确占位,不是空洞
        self.assertIn("(未填写)", prompt)

    def test_prompt_anchors_on_director_statement(self):
        from aifos.adapters.claude_script import build_prompt
        prompt = build_prompt("script", {
            "ai_director": True,
            "director_statement": "基调:压抑隐忍；情绪高点:第2场官凭到手",
            "shot_facts": {}, "vocabulary": {}})
        self.assertIn("查偏基准", prompt)
        self.assertIn("压抑隐忍", prompt)
        self.assertIn("回到阐述轨道", prompt)

    def test_statement_text_assembled_in_field_order(self):
        from types import SimpleNamespace
        from aifos.director import Director
        doc = {"tone": "压抑隐忍", "intent": "让观众替沈砚舟捏汗",
               "avoid": "", "pacing": "  "}
        stub = SimpleNamespace(
            STATEMENT_FIELDS=Director.STATEMENT_FIELDS,
            projects=SimpleNamespace(
                latest_document=lambda _eid, _kind: (doc, 3)))
        text = Director._director_statement_text(stub, 7)
        self.assertEqual(
            text, "一句话意图:让观众替沈砚舟捏汗；基调:压抑隐忍")
        stub_empty = SimpleNamespace(
            STATEMENT_FIELDS=Director.STATEMENT_FIELDS,
            projects=SimpleNamespace(
                latest_document=lambda _eid, _kind: (None, 0)))
        self.assertEqual(Director._director_statement_text(stub_empty, 7), "")

    def test_valid_suggestion_passes_and_is_cleaned(self):
        from aifos.adapters.claude_script import validate_ai_director
        data = {"schema": "aifos.ai_director.v1",
                "camera": {"景别": "特写", "构图": "框中框", "角度": ""},
                "lighting_style": "practical_lit",
                "note": "官凭纸面向光,指腹压痕清晰",
                "rationale": "单人查证信息镜,特写+框中框强化窥视与紧张"}
        self.assertIsNone(validate_ai_director(data, self._payload(1)))
        self.assertNotIn("角度", data["camera"])   # 空值清掉

    def test_out_of_vocabulary_terms_rejected(self):
        from aifos.adapters.claude_script import validate_ai_director
        data = {"schema": "aifos.ai_director.v1",
                "camera": {"景别": "超级大特写"},
                "rationale": "想更近一点看清楚"}
        self.assertIn("词典", validate_ai_director(data, self._payload(1)))

    def test_capacity_physics_cannot_be_violated(self):
        """AI导演也不许给3人镜头开特写——与预检同一条物理。"""
        from aifos.adapters.claude_script import validate_ai_director
        data = {"schema": "aifos.ai_director.v1",
                "camera": {"景别": "特写"},
                "rationale": "特写更有冲击力"}
        error = validate_ai_director(data, self._payload(3))
        self.assertIn("装不下", error)

    def test_rationale_is_mandatory(self):
        from aifos.adapters.claude_script import validate_ai_director
        data = {"schema": "aifos.ai_director.v1",
                "camera": {"景别": "中景"}, "rationale": ""}
        self.assertIn("rationale", validate_ai_director(
            data, self._payload(1)))

    def test_all_empty_suggestion_rejected(self):
        from aifos.adapters.claude_script import validate_ai_director
        data = {"schema": "aifos.ai_director.v1", "camera": {},
                "lighting_style": "", "note": "",
                "rationale": "现状已经是最优处理,无需改动"}
        self.assertIn("至少给一项", validate_ai_director(
            data, self._payload(1)))
