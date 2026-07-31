"""镜头语言具象化:术语词典与镜头合同渲染注入。"""

from aifos.camera_language import camera_geometry_clause
from aifos.prompt_contract import compile_shot_prompt


def test_geometry_clause_translates_known_terms():
    clause = camera_geometry_clause(
        {"景别": "全景", "角度": "俯拍", "机位": "背面"})
    assert "按可见特征执行并核验" in clause
    assert "头顶到脚底完整入画" in clause          # 景别边界
    assert "头顶与双肩上表面" in clause            # 俯仰透视
    assert "不出现眉、眼、鼻、嘴" in clause        # 背面身份判据


def test_geometry_clause_skips_placeholders_and_unknown():
    # 「按分镜」「保持轴线」等默认占位不产出条款
    assert camera_geometry_clause(
        {"景别": "按分镜", "角度": "保持轴线", "机位": ""}) == ""
    assert camera_geometry_clause(None) == ""
    assert camera_geometry_clause({"角度": "荷兰角"}) == ""


def test_compiled_shot_prompt_carries_geometry_for_image_and_video():
    shot = {
        "shot_no": 1, "scene_no": 1, "kind": "dialogue",
        "description": "林川抬头看向房梁", "camera": "特写·仰拍",
        "duration": 2.5, "characters": ["林川"], "dialogue": None,
        "prompt": "p",
    }
    _contract, image_prompt = compile_shot_prompt(
        shot, location="驿馆内室", mode="image")
    assert "肩线以下出画" in image_prompt        # 特写边界
    assert "下颌底面与鼻底" in image_prompt      # 仰拍透视
    _contract, video_prompt = compile_shot_prompt(
        shot, location="驿馆内室", mode="video")
    assert "下颌底面与鼻底" in video_prompt      # 视频同一标准


def test_qc_feedback_camera_rule_references_visible_features():
    from aifos.qc_feedback import optimize_qc_feedback
    revision = optimize_qc_feedback(
        ["视角接近后上方俯视，不符合合同要求的仰拍"])
    assert "camera" in revision["categories"]
    assert "可见特征执行并核验" in revision["text"]


# ---- 场景多视角:机位映射与母版一致性合同 ----
def test_scene_view_mapping_from_camera():
    from aifos.camera_language import scene_view_for_camera
    assert scene_view_for_camera({"机位": "过肩"}) == "reverse"
    assert scene_view_for_camera({"机位": "背面"}) == "reverse"
    assert scene_view_for_camera("中景·背面跟拍") == "reverse"
    assert scene_view_for_camera({"机位": "侧面"}) == "side"
    assert scene_view_for_camera("全景·俯拍·推") == "main"
    assert scene_view_for_camera(None) == "main"      # 永不阻断


def test_scene_view_prompt_and_contract():
    from aifos.director import Director
    director = Director.__new__(Director)
    scene = {"location": "雨夜公寓单元房", "time_of_day": "深夜暴雨",
             "production_design": {"environment": "老式单元房,昏黄吸顶灯"}}
    prompt = director._scene_view_prompt(
        "雨夜公寓单元房", "写实悬疑", scene,
        "雨夜公寓单元房·反打视角", "反打视角",
        "从主视角正对面的机位回看同一空间")
    # 派发合同逐字校验对象名:提示词必须写出 art_name
    assert "【本图对象】雨夜公寓单元房·反打视角" in prompt
    assert "逐项一致" in prompt and "只允许摄影机位改变" in prompt
    context = director._scene_view_review_context(
        "雨夜公寓单元房", "写实悬疑", scene, "反打视角")
    assert "不构成需要裁决的冲突" in context["view_consistency_precedence"]
    assert "master_state_precedence" in context     # 空镜条款仍然在场


def test_shot_contract_declares_camera_precedence():
    """镜头合同必须带 camera_precedence 显式裁决:多套机位并存时
    以融合后的 camera 字段为唯一执行值(关键帧首熔断的点名要求)。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 7, "scene_no": 1, "kind": "action",
            "camera": "近景·俯拍·严格侧面", "description": "林川侧身窥视",
            "duration": 2.5, "characters": ["林川"], "dialogue": None,
            "prompt": "p"}
    contract, prompt = compile_shot_prompt(
        shot, location="屋檐下", mode="image")
    assert "唯一执行镜位" in contract["camera_precedence"]
    assert "不构成需要裁决的冲突" in contract["camera_precedence"]
    assert "唯一执行镜位(camera_precedence)" in prompt


def test_top_down_position_geometry_avoids_facial_claims():
    """顶拍+正面曾物理互斥熔断:「只见头顶」vs「双眼可见」。

    顶拍语境的机位词描述躯干朝向,不得断言面部可见性;
    平视/俯拍语境保持原眼部判据。
    """
    top = camera_geometry_clause(
        {"景别": "近景", "角度": "顶拍", "机位": "正面"})
    assert "双眼可见" not in top
    assert "只见头顶" in top and "躯干腹面朝上" in top
    level = camera_geometry_clause({"角度": "平视", "机位": "正面"})
    assert "双眼可见" in level
    high = camera_geometry_clause({"角度": "俯拍", "机位": "正面"})
    assert "双眼可见" in high      # 俯拍非垂直,面部仍可见,两者兼容


def test_camera_precedence_covers_crop_visibility():
    """景别边界裁出画的伤口/道具,「必须可见」自动不适用——
    「特写肩线以下出画」vs「肩下伤口必须可见」曾同级互斥熔断。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 9, "scene_no": 2, "kind": "action",
            "camera": "特写", "description": "林川特写", "duration": 2,
            "characters": ["林川"], "dialogue": None, "prompt": "p"}
    contract, _prompt = compile_shot_prompt(
        shot, location="公寓", mode="image")
    clause = contract["camera_precedence"]
    assert "裁出画" in clause and "自动不适用" in clause
    assert "不构成可见性冲突" in clause


def test_aspect_tokens_stripped_from_camera_and_precedence_declared():
    """分镜把 16:9 写进 camera 曾与项目画幅 9:16 同级互斥熔断。

    画幅唯一执行值是 aspect 字段:编译时从镜头字段剥离比例字样,
    并声明 aspect_precedence。
    """
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 1, "scene_no": 1, "kind": "environment",
            "camera": "16:9广角固定机位·远景", "description": "雨夜街巷",
            "duration": 3, "characters": [], "dialogue": None, "prompt": "p"}
    contract, _prompt = compile_shot_prompt(
        shot, location="街巷", mode="image")
    for value in contract["camera"].values():
        assert "16:9" not in str(value)
    assert "唯一执行值" in contract["aspect_precedence"]


def test_freeze_condition_derived_from_end_when_missing():
    """start≠end 且未显式声明定格状态 → 按"动作完成态"承接 end,
    带 derived_from 溯源,不再阻断(阿砚 freeze 冲突真实事故)。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 15, "scene_no": 3, "kind": "action",
            "camera": "中景", "description": "阿砚收拾文书包",
            "duration": 2.0, "characters": ["阿砚"], "dialogue": None,
            "prompt": "p",
            "frame_targets": {"keyframe": {"phase": "freeze",
                                            "state": "阿砚半蹲收拾"}},
            "start_state": {"阿砚": {"pose": "站立整理桌面"}},
            "end_state": {"阿砚": {"pose": "半蹲合拢包袱"}}}
    contract, _prompt = compile_shot_prompt(
        shot, location="茶棚", mode="image")
    freeze = contract["character_conditions"]["阿砚"]["freeze"]
    # 定格状态存在且不携带任何阻断性 issue;合同级 issues 亦无 freeze 冲突
    assert isinstance(freeze, dict)
    assert not freeze.get("issues")
    assert not any("freeze" in str(item)
                   for item in contract.get("issues", []))


def test_dead_state_renders_forceful_visual_clause():
    """life=dead 键值对约束力近零(阿砚被画成睁眼活人的真实事故);
    死亡/昏迷/静止须翻译成强制性视觉判据,常规镜头零污染。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 11, "scene_no": 2, "kind": "action",
            "camera": "近景·俯拍", "description": "林川俯身查看阿砚",
            "duration": 2.5, "characters": ["林川", "阿砚"],
            "dialogue": None, "prompt": "p",
            "start_state": {"阿砚": {"pose": "仰卧",
                                      "condition": {"life_state": "dead"}}},
            "end_state": {"阿砚": {"pose": "仰卧",
                                    "condition": {"life_state": "dead"}}}}
    _contract, prompt = compile_shot_prompt(
        shot, location="茶棚", mode="image")
    assert "【硬状态·强制执行】" in prompt
    assert "阿砚已死亡" in prompt and "绝不允许睁眼" in prompt
    plain = {"shot_no": 1, "scene_no": 1, "kind": "dialogue",
             "camera": "中景", "description": "对话", "duration": 2,
             "characters": ["林川"], "dialogue": None, "prompt": "p"}
    _c2, p2 = compile_shot_prompt(plain, location="茶棚", mode="image")
    assert "【硬状态·强制执行】" not in p2


def test_core_picture_line_leads_the_prompt():
    """首句权重:核心动作+关键道具状态置顶(道具执行缺失13次的对策);
    内部 prop_id 不得入提示词,经注册表解析中文名。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 9, "scene_no": 2, "kind": "action",
            "camera": "中景", "description": "黑衣人持刀逼近",
            "duration": 2.5, "characters": ["黑衣人"], "dialogue": None,
            "prompt": "p",
            "prop_registry": [{"prop_id": "YYXS-E01-PROP-DAGGER-001",
                                "name": "缺口单刃短刀", "kind": "core",
                                "instance_count": 1}],
            "frame_targets": {"keyframe": {"phase": "end",
                "state": "黑衣人右手反握短刀,刀尖朝下,刃面带血"}},
            "frame_props": [
                {"prop_id": "YYXS-E01-PROP-DAGGER-001", "phase": "end",
                 "visibility": "visible", "holder": "黑衣人",
                 "physical_state": "刀尖朝下,刃面带血"}]}
    _contract, prompt = compile_shot_prompt(
        shot, location="茶棚", mode="image")
    line2 = prompt.split("\n")[1]
    assert line2.startswith("【核心画面】")
    assert "缺口单刃短刀" in line2 and "由黑衣人持有" in line2
    assert "YYXS-E01" not in line2


def test_camera_precedence_covers_orientation_visibility():
    """背面机位 vs 相向/张口/胸前细节曾同级互斥:机位决定可见面,
    被背向的正面细节自动免验,朝向按机位语境重解。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 13, "scene_no": 3, "kind": "dialogue",
            "camera": "中景·背面", "description": "两人背身对话",
            "duration": 2.5, "characters": ["林川", "阿砚"],
            "dialogue": None, "prompt": "p"}
    contract, _prompt = compile_shot_prompt(
        shot, location="茶棚", mode="image")
    clause = contract["camera_precedence"]
    assert "被机位背向的面部表情" in clause
    assert "以机位为准" in clause
    assert "不构成需要裁决的冲突" in clause


def test_scale_visibility_tiering_in_contract_and_qc():
    """尺度可辨性:远景分辨不出刀刃缺口——微细节按景别分级免验,
    道具在场性/大形态/持有人任何景别必验(用户拍板的裁决)。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 6, "scene_no": 2, "kind": "environment",
            "camera": "远景", "description": "雨夜长街全景",
            "duration": 3, "characters": ["黑衣人"], "dialogue": None,
            "prompt": "p"}
    contract, _prompt = compile_shot_prompt(
        shot, location="长街", mode="image")
    clause = contract["camera_precedence"]
    assert "低于本景别物理可辨尺度的微细节" in clause
    assert "刃口缺口" in clause
    assert "在场性、大形态、颜色系与持有人" in clause   # 不在免验之列

    from aifos.adapters.claude_script import build_qc_prompt
    base = {"image_uri": "/tmp/x.png", "characters": ["黑衣人"],
            "identity_references": []}
    far = build_qc_prompt({**base, "camera": "远景·俯拍"})
    assert "不得作为不合格理由" in far
    close = build_qc_prompt({**base, "camera": "特写"})
    assert "必须核验一致" in close


def test_explicit_reference_scope_overrides_role_defaults():
    """显式 include+exclude=作者已裁决:排除域不再并角色默认。

    实测(镜头02/16 批量熔断):背面立绘/穿着类道具的显式继承里必须有
    wardrobe、prop_position,而角色默认排除也含它们——旧逻辑排除域
    永远并默认,显式覆盖不可能生效,include/exclude 必然交集。"""
    from aifos.prompt_contract import compile_shot_prompt
    shot = {"shot_no": 2, "scene_no": 1, "kind": "environment",
            "camera": "全景", "description": "d", "duration": 3,
            "characters": [], "dialogue": None, "prompt": "p"}
    refs = [{
        "index": 1, "label": "旧靛青举人青袍", "role": "prop",
        "binding": "穿着类道具", "uri": "/tmp/x.png",
        "inherits": ["prop_structure", "prop_material",
                     "wardrobe", "prop_position"],
        "excludes": ["background", "composition", "extra_props"],
    }, {
        "index": 2, "label": "背面母资产", "role": "identity_detail",
        "binding": "背面轮廓", "uri": "/tmp/y.png",
        "inherits": ["背面轮廓", "hair_silhouette", "wardrobe",
                     "prop_position"],
        "excludes": ["face_identity_override", "pose", "background"],
    }, {
        # 只给 include 的条目仍受角色默认排除保护
        "index": 3, "label": "普通参考", "role": "prop",
        "binding": "结构", "uri": "/tmp/z.png",
        "inherits": ["prop_structure"],
    }]
    contract, _ = compile_shot_prompt(
        shot, location="废茶棚", mode="image", references=refs)
    for ref in contract["references"]:
        scope = ref.get("inherit_scope") or {}
        overlap = set(scope.get("include") or []) & set(
            scope.get("exclude") or [])
        assert not overlap, (ref.get("index"), sorted(overlap))
    third = next(r for r in contract["references"] if r["index"] == 3)
    assert "wardrobe" in (third["inherit_scope"]["exclude"] or [])


def test_vague_population_disarmed_by_explicit_counts():
    """上诉庭固化(12次误杀中9次):文本已有明确数字人数时,
    「一群/众人」只是修辞,不再判「模糊人数」。"""
    from aifos.prompt_contract import compile_shot_prompt
    base = {"shot_no": 1, "scene_no": 1, "kind": "environment",
            "duration": 3, "characters": ["林川"], "dialogue": None,
            "prompt": "p", "camera": "全景",
            "frame_target": {"phase": "end", "state": "队列静立",
                             "fallback": False, "explicit": True,
                             "fallback_declared": True}}
    # 有明确数字 → 不判
    c1, _ = compile_shot_prompt(
        {**base, "description": "画面严格共5人,一群巡检弓兵列于身后"},
        location="驿道", style="写实", mode="image")
    assert not [i for i in (c1.get("population") or {}).get("issues", [])
                if "模糊人数" in i]
    # 无任何明确数量 → 照判
    c2, _ = compile_shot_prompt(
        {**base, "description": "一群百姓围观"},
        location="驿道", style="写实", mode="image")
    assert [i for i in (c2.get("population") or {}).get("issues", [])
            if "模糊人数" in i]


def test_history_narration_is_not_a_process_violation():
    """上诉庭固化:「已经从昏迷中醒来」是交代历史、终态唯一,
    不是要求同帧画两个阶段。"""
    from aifos.prompt_contract import validate_shot_prompt_contract, \
        compile_shot_prompt
    base = {"shot_no": 1, "scene_no": 1, "kind": "dialogue",
            "duration": 3, "characters": ["林川"], "dialogue": None,
            "prompt": "p", "camera": "近景"}
    ok, _ = compile_shot_prompt(
        {**base, "frame_target": {
            "phase": "end", "state": "林川已经从昏迷中醒来,坐靠柱脚",
            "fallback": False, "explicit": True,
            "fallback_declared": True}},
        location="废茶棚", style="写实", mode="image")
    report = validate_shot_prompt_contract(ok)
    assert not [i for i in report.get("issues", [])
                if "多个时间状态" in i], report["issues"]
    # 真过程(显式轨迹箭头)仍要拦
    bad, _ = compile_shot_prompt(
        {**base, "frame_target": {
            "phase": "end", "state": "林川 起身→行至门边→推门",
            "fallback": False, "explicit": True,
            "fallback_declared": True}},
        location="废茶棚", style="写实", mode="image")
    report2 = validate_shot_prompt_contract(bad)
    assert [i for i in report2.get("issues", [])
            if "多个时间状态" in i]


# ---- 表现性运镜:词条齐备、长词优先、艺术运镜不被求解覆盖 ----

def test_expressive_movements_registered_with_verifiable_clauses():
    from aifos.camera_language import (EXPRESSIVE_MOVEMENTS,
                                       MOVEMENT_GEOMETRY)
    assert EXPRESSIVE_MOVEMENTS == {
        "甩镜", "螺旋环绕", "穿越", "俯冲", "升格", "希区柯克变焦"}
    for term in EXPRESSIVE_MOVEMENTS:
        assert term in MOVEMENT_GEOMETRY
        # 每条都必须是"画面里应该看到什么"的可核验描述
        assert len(MOVEMENT_GEOMETRY[term]) >= 20
    assert "实验级" in MOVEMENT_GEOMETRY["希区柯克变焦"]


def test_movement_priority_expressive_wins_over_derived_static():
    from aifos.camera_language import (MOVEMENT_GEOMETRY,
                                       movement_geometry_for)
    # 病根复现:分镜写了艺术运镜,3D 求解误报"固定"——词典合同必须获胜
    assert movement_geometry_for(
        "甩镜", "固定") == MOVEMENT_GEOMETRY["甩镜"]
    assert movement_geometry_for(
        "情绪极点升格慢放", "固定") == MOVEMENT_GEOMETRY["升格"]
    # 经典运镜维持"三维调度为准"的既有优先级
    assert movement_geometry_for(
        "缓推", "拉") == MOVEMENT_GEOMETRY["拉"]
    assert movement_geometry_for("缓推", "") == MOVEMENT_GEOMETRY["推"]
    assert movement_geometry_for("按分镜", "") == ""


def test_movement_longest_token_wins_over_substring():
    from aifos.camera_language import (MOVEMENT_GEOMETRY,
                                       movement_geometry_for)
    # 「螺旋环绕」不得被子串「环绕」截胡
    assert movement_geometry_for(
        "螺旋环绕", "固定") == MOVEMENT_GEOMETRY["螺旋环绕"]
    assert movement_geometry_for(
        "螺旋环绕上升", "环绕") == MOVEMENT_GEOMETRY["螺旋环绕"]


def test_expressive_movement_flows_into_video_contract_clause():
    from aifos.camera_language import camera_geometry_clause
    clause = camera_geometry_clause({"景别": "近景", "运镜": "俯冲"})
    assert "俯冲" in clause and "视平线急速下移" in clause
