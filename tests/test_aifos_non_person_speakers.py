"""旁白/音效/字幕等叙事装置永远不得被当成人物实体。

这个问题修好过又复发,原因是过去只在提示词里约束模型(软约束),确定性
词表既不含旁白音效、又只在一个次要回退处调用。本测试按「词表 × 入口」
逐一锁死:任何一个入口漏掉过滤都会在这里红掉,不会再悄悄退化。
"""

import pytest

from aifos.adapters.claude_script import (strip_non_person_speakers,
                                          validate_script)
from aifos.director import is_unresolved_character
from aifos.script_import import (is_likely_performance_label,
                                 is_non_person_label, parse_text_script)
from aifos.story_analysis import is_likely_non_person_name

# 真实剧本里出现过的各种写法:中文、英文缩写、带括号、带全角冒号。
NON_PERSON_LABELS = [
    "旁白", "画外音", "画外白", "独白", "内心独白", "心声", "心理活动",
    "解说", "解说词", "叙述者", "讲述人", "旁白者", "解说员",
    "音效", "声效", "音效提示", "配乐", "背景音乐", "环境音", "音轨",
    "字幕", "花字", "提示音", "广播", "播报", "片头", "片尾",
    "BGM", "bgm", "SFX", "OS", "O.S.", "VO", "V.O.", "SE",
    "(旁白)", "【音效】", "（画外音）", "系统提示", "静默",
]

# 这些必须仍被当成真人,防止词表过宽误伤。
REAL_NAMES = [
    "朱慈烺", "李继周", "程沐", "小鹿", "石头", "林晚晴",
    "白起", "音音", "宋声声", "Alice", "K先生", "阿广",
]


@pytest.mark.parametrize("label", NON_PERSON_LABELS)
def test_vocabulary_recognizes_every_non_person_label(label):
    assert is_non_person_label(label) is True


@pytest.mark.parametrize("name", REAL_NAMES)
def test_vocabulary_does_not_swallow_real_people(name):
    assert is_non_person_label(name) is False


def test_vocabulary_module_stays_a_zero_dependency_leaf():
    """词表必须留在零依赖叶子模块里。

    它一旦被挪回 script_import 这种反向依赖编剧桥的模块,新校验环节想
    复用就会撞循环导入,于是绕过或另写一份——这正是历史复发路径之一。
    """
    import ast
    import pathlib

    source = pathlib.Path("aifos/speaker_labels.py").read_text("utf-8")
    tree = ast.parse(source)
    imports = [node for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert imports == [], "speaker_labels 必须保持零依赖,便于任何层直接复用"


@pytest.mark.parametrize("label", NON_PERSON_LABELS)
def test_all_five_gates_reject_non_person_labels(label):
    """五道闸门必须同时拦住;少一道就会有一条路径漏进人物表。"""
    # 闸门1:剧本导入的说话人解析
    assert is_non_person_label(label) is True
    # 闸门2:AI 逐句说话人归并后的人物表重建
    assert is_likely_non_person_name(label) is True
    # 闸门3:生产闸门(决定是否生成立绘/候选图)
    assert is_unresolved_character({"name": label, "role": "配角"}) is True
    # 闸门4:AI 剧本的结构化清洗
    script = {"characters": [{"name": label}, {"name": "程沐"}],
              "scenes": [{"scene_no": 1, "characters": [label, "程沐"],
                          "lines": [{"character": label, "dialogue": "夜色渐深"}]}]}
    strip_non_person_speakers(script)
    assert [c["name"] for c in script["characters"]] == ["程沐"]
    assert script["scenes"][0]["characters"] == ["程沐"]
    # 闸门5:导演级物理/空间/可拍摄性门禁不得把旁白判成未声明角色
    from aifos.story_logic import is_non_person_label as gate5
    assert gate5(label) is True


def test_imported_novel_does_not_create_narration_character():
    """「旁白:…」写成标准角色格式也不能变成人物,台词转为场景叙述保留。"""
    text = "\n".join([
        "第一场 东宫书房",
        "旁白：夜色渐深，宫墙外传来更鼓。",
        "音效：雷声由远及近。",
        "朱慈烺：你今日为何来迟？",
        "李继周：回殿下，路上遇雨。",
    ])
    script = parse_text_script(text, "测试剧", 1)
    names = [c["name"] for c in script["characters"]]
    assert "旁白" not in names and "音效" not in names
    assert set(names) == {"朱慈烺", "李继周"}
    for scene in script["scenes"]:
        assert "旁白" not in scene["characters"]
        assert "音效" not in scene["characters"]
    # 旁白文本不能丢,应作为场景叙述保留下来
    assert "夜色渐深" in " ".join(s["action"] for s in script["scenes"])


def test_ai_script_narration_line_does_not_fail_validation():
    """模型把旁白正确写成声音来源时不得判校验失败。

    这是历史复发的关键诱因:校验一旦因“场次出现未声明角色”打回,模型
    下一轮就会把旁白补进人物表来迎合校验,问题就绕回来了。
    """
    script = {
        "characters": [
            {"name": "程沐", "role": "主角"},
        ],
        "scenes": [{
            "scene_no": 1, "location": "老宅",
            "characters": ["程沐"],
            "lines": [
                {"character": "旁白", "dialogue": "那一年的雨下得格外久。"},
                {"character": "程沐", "dialogue": "我回来了。"},
            ],
        }],
    }
    assert validate_script(script, {}) is None
    assert [c["name"] for c in script["characters"]] == ["程沐"]
    assert script["scenes"][0]["characters"] == ["程沐"]
    narration = script["scenes"][0]["lines"][0]
    assert narration["non_person_voice"] is True
    # 台词逐字保留,后续可做画外配音
    assert narration["dialogue"] == "那一年的雨下得格外久。"


def test_non_person_is_excluded_not_sent_back_for_human_resolution():
    """旁白无需人工归并:直接剔除,不能进“待确认人物”阻断名单。"""
    from aifos.story_analysis import unresolved_character_labels

    script = {"characters": [{"name": "旁白", "role": "配角"},
                             {"name": "沉声", "role": "配角"}]}
    labels = unresolved_character_labels(script)
    # 表演提示词要人工归并回真实人物;旁白不是归并问题,静默剔除即可
    assert "沉声" in labels
    assert "旁白" not in labels


def test_performance_cue_and_non_person_stay_separate_concepts():
    """两类问题的处置不同,判定函数不能互相污染。"""
    assert is_likely_performance_label("沉声") is True
    assert is_non_person_label("沉声") is False
    assert is_non_person_label("旁白") is True
    assert is_likely_performance_label("旁白") is False


def test_script_parse_error_is_recorded_into_the_lessons_library(tmp_path):
    """自愈闭环:系统自动纠正过旁白误识别,就要记进经验库(剧本域)。

    这类错误在图片质检里永远看不到——它发生在剧本阶段,等到出图时已经
    变成一张多余立绘。所以必须在剧本阶段留痕。
    """
    from aifos.app import App
    from aifos.lessons import DOMAIN_SCRIPT, project_lessons

    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("经验库剧本域")
        ctx = {"project": dict(project)}
        recorded = app.director._record_script_lessons(ctx, {
            "non_person_labels_removed": ["旁白", "音效"],
        })
        assert recorded == 2

        lessons = project_lessons(app.assets, project["id"])
        assert len(lessons) == 2
        assert all(item["domain"] == DOMAIN_SCRIPT for item in lessons)
        assert any("旁白" in item["issue"] for item in lessons)
        # 沿用既有闭环:默认待审核,未经人工批准不得注入提示词
        assert all(item["status"] == "pending_review" for item in lessons)
        assert all(not item["approved_for_prompt"] for item in lessons)
    finally:
        app.close()


def test_script_lessons_never_leak_into_image_prompts(tmp_path):
    """分域隔离:剧本教训不进出图提示词,出图教训不进编剧提示词。"""
    from aifos.app import App
    from aifos.lessons import (DOMAIN_IMAGE, DOMAIN_SCRIPT, lessons_block,
                               project_lessons, record_lessons,
                               script_lessons_block)

    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("分域隔离")
        pid = project["id"]
        record_lessons(app.assets, pid, ["把旁白当成了人物写进人物表"],
                       category="non_person_speaker", domain=DOMAIN_SCRIPT)
        record_lessons(app.assets, pid, ["古代场景出现了现代笔记本电脑"],
                       category="shot_image", domain=DOMAIN_IMAGE)

        # 人工批准两条,才能验证注入时是否串味
        import json
        for row in app.assets.active_list(pid, kind="lesson"):
            raw = row["meta"]
            meta = json.loads(raw or "{}") if isinstance(raw, str) else (
                dict(raw or {}))
            meta["status"] = "approved"
            meta["approved_for_prompt"] = True
            app.assets.register(pid, "lesson", row["name"], meta=meta,
                                new_version=True)

        image_block = lessons_block(app.assets, pid)
        script_block = script_lessons_block(app.assets, pid)
        assert "笔记本电脑" in image_block and "旁白" not in image_block
        assert "旁白" in script_block and "笔记本电脑" not in script_block
        # 看板不分域,两条都要看得到
        assert len(project_lessons(app.assets, pid)) == 2
        assert len(project_lessons(app.assets, pid,
                                   domain=DOMAIN_SCRIPT)) == 1
    finally:
        app.close()


def test_legacy_lessons_without_domain_stay_in_the_image_domain(tmp_path):
    """历史记录没有 domain 字段,必须仍按出图域处理,不能凭空跑进剧本域。"""
    from aifos.app import App
    from aifos.lessons import DOMAIN_IMAGE, DOMAIN_SCRIPT, project_lessons

    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("历史兼容")
        pid = project["id"]
        app.assets.register(pid, "lesson", "legacyfingerprint", meta={
            "issue": "古代场景出现了现代笔记本电脑",
            "count": 3, "categories": {"shot_image": 3},
        })
        assert len(project_lessons(app.assets, pid,
                                   domain=DOMAIN_IMAGE)) == 1
        assert project_lessons(app.assets, pid, domain=DOMAIN_SCRIPT) == []
    finally:
        app.close()
