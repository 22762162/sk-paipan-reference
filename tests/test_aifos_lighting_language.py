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
