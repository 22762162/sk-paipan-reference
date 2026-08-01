"""出错经验库:质检失败原因自动归档 → 注入后续出图/视频提示词。"""

import struct
import zlib

import pytest

from aifos.app import App
from aifos.lessons import (lesson_lines, lesson_worthy, lessons_block,
                           project_lessons, record_lessons)
from aifos.production.base import ProviderResult


def _valid_png(width=9, height=16):
    def chunk(kind, data):
        crc = zlib.crc32(kind)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", crc))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\x00" + b"\x14\x28\x3c" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def test_record_and_aggregate(app):
    project, _ = app.projects.get_or_create_project("经验库")
    record_lessons(app.assets, project["id"],
                   ["镜头3出现笔记本电脑,与古代场景年代不符"],
                   category="shot_image")
    record_lessons(app.assets, project["id"],
                   ["镜头7出现笔记本电脑,与古代场景年代不符",
                    "盒子里的笔记本被拉得很长,比例失常"],
                   category="frames")
    lessons = project_lessons(app.assets, project["id"])
    assert len(lessons) == 2
    top = lessons[0]
    assert "笔记本电脑" in top["issue"]
    assert top["count"] == 2                      # 同类问题聚合(镜头号归一)
    assert top["categories"]["shot_image"] == 1
    assert top["categories"]["frames"] == 1
    assert top["categories"]["era"] == 2
    assert top["categories"]["prop"] == 2
    lines = lesson_lines(app.assets, project["id"])
    assert lines == []  # 质检观察未人工批准前不得污染后续提示词
    assert top["scope"] == "qc_observation"
    assert top["status"] == "pending_review"
    assert top["approved_for_prompt"] is False
    assert lessons_block(app.assets, project["id"]) == ""


def test_environment_noise_is_not_a_lesson():
    assert not lesson_worthy("质检产线不可用，图片未放行:超时")
    assert not lesson_worthy("短")
    assert lesson_worthy("古代场景出现现代手机")


def test_unapproved_qc_observations_do_not_enter_other_shot_prompts(app):
    """本镜失败观察不能自动升级为跨镜头永久规则。"""
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    project = app.projects.get_project("万妖图录")
    record_lessons(app.assets, project["id"],
                   ["古代场景出现笔记本电脑,时代错乱"], category="shot_image")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "script": script}
    shot = {"shot_no": 1, "scene_no": 1,
            "characters": [], "description": "空镜", "duration": 3}
    prompt = app.director._rich_shot_prompt(ctx, shot, "山门")
    assert "严禁再犯" not in prompt
    assert "古代场景出现笔记本电脑" not in prompt
    assert "【ERA LOCK】" in prompt          # 时代硬约束逐镜写死
    video_prompt = app.director._seedance_video_prompt(ctx, shot, [])
    assert "【时代与物理】" in video_prompt
    assert "严禁再犯" not in video_prompt
    assert "古代场景出现笔记本电脑" not in video_prompt


def test_qc_failure_auto_records_lesson(app, tmp_path):
    """质检失败(即使重画后通过)自动进经验库——闭环的核心。"""
    # 新产线默认是选片模式；本测试专门验证显式开启内容QC时的旧闭环。
    app.config.data.setdefault("defaults", {})["selection_mode"] = False
    app.config.data["defaults"]["image_content_qc"] = True
    image = tmp_path / "shot.png"
    image.write_bytes(_valid_png())
    calls = {"qc": 0}

    class StubRouter:
        def review_image_prompt(self, capability, payload, out_dir,
                                cancel=None):
            return None

        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                return ProviderResult(provider="codex", cost=1.0,
                                      uri=str(image))
            calls["qc"] += 1
            first = calls["qc"] == 1
            if payload.get("required_provider") == "codex":
                return ProviderResult(
                    provider="codex", cost=0.1,
                    data={
                        "pass": False,
                        "issues": ["古代大殿出现现代笔记本电脑"],
                        "image_error": {
                            "summary": "古代大殿出现现代笔记本电脑",
                            "categories": ["era"],
                            "evidence": ["画面可见现代笔记本电脑"],
                        },
                        "prompt_diagnosis": {
                            "status": "needs_patch",
                            "issues": ["提示词未明确禁止现代设备"],
                            "irrelevant_or_conflicting_sections": [],
                        },
                        "reference_diagnosis": {
                            "status": "correct", "issues": [],
                            "missing_roles": [],
                        },
                        "targeted_prompt_patch": {
                            "instructions": ["只删除现代笔记本电脑"],
                            "preserve": ["人物", "构图", "古代大殿"],
                            "max_scope": "current_shot_only",
                        },
                        "reference_adjustments": [],
                        "codex_escalation": {
                            "aifos_action": "targeted_redraw",
                            "aifos_instructions": [
                                "只删除现代笔记本电脑"],
                        },
                    })
            diagnostics = ({
                "image_error": {
                    "summary": "古代大殿出现现代笔记本电脑",
                    "categories": ["era"],
                    "evidence": ["画面可见现代笔记本电脑"],
                },
                "prompt_diagnosis": {
                    "status": "needs_patch",
                    "issues": ["提示词未明确禁止现代设备"],
                    "irrelevant_or_conflicting_sections": [],
                },
                "reference_diagnosis": {
                    "status": "correct",
                    "issues": [],
                    "missing_roles": [],
                },
                "targeted_prompt_patch": {
                    "instructions": ["只删除现代笔记本电脑"],
                    "preserve": ["人物", "构图", "古代大殿"],
                },
                "codex_escalation": {
                    "aifos_action": "targeted_redraw",
                    "reason": "删除时代错误的现代设备",
                    "aifos_instructions": ["只删除现代笔记本电脑"],
                },
                "reference_adjustments": [],
            } if first else {})
            return ProviderResult(
                provider="codex", cost=0.1,
                data={"pass": not first,
                      "visual_pass": not first,
                      "input_contract_pass": not first,
                      "identity_checked": True, "identity_match": True,
                      "gender_checked": True, "gender_match": True,
                      "wardrobe_checked": True,
                      "wardrobe_match": True,
                      "count_checked": True, "count_match": True,
                      "overlay_count_checked": True,
                      "overlay_count_match": True,
                      "physical_logic_checked": True,
                      "physical_logic_match": True,
                      "spatial_logic_checked": True,
                      "spatial_logic_match": True,
                      "issues": (["镜头1古代大殿里出现了笔记本电脑"]
                                 if first else []),
                      **diagnostics})

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {"prompt": "x", "shot_no": 1,
                  "_episode_id": "unit-test"}, tmp_path, None,
        {"characters": [], "count": 0, "designs": "", "location": "",
         "action": "", "forbid": []})
    assert result.qc["passed"] is True
    assert result.qc["lesson_issues"], "重画通过后首轮问题必须保留为教训"
    # 经 _plan_mark 归档(生产主路径的必经点)
    project, _ = app.projects.get_or_create_project("闭环")
    ctx = {"project": dict(project),
           "episode": {"id": 1, "number": 1},
           "out_root": tmp_path}
    app.director._plan_seed(ctx, "shot_image", [
        {"id": "shot:1", "category": "shot_image", "label": "镜1",
         "prompt": "x"}])
    app.director._plan_mark(ctx, "shot:1", "done",
                            extra={"qc": dict(result.qc)})
    lessons = project_lessons(app.assets, project["id"])
    assert lessons and "笔记本电脑" in lessons[0]["issue"]


def test_era_and_deformation_in_global_forbid(app):
    forbid = "、".join(app.director._FORBID)
    assert "时代错乱" in forbid
    assert "拉长" in forbid or "变形" in forbid


def test_time_travel_props_are_sanctioned_not_blocked(app):
    """穿越剧:剧本声明的跨时代物品必须画出,不被 ERA LOCK/质检卡掉。"""
    app.director.produce("穿越回明朝", 1, pause_for_confirm=True)
    project = app.projects.get_project("穿越回明朝")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    script.setdefault("story_world", {})["sanctioned_anachronisms"] = [
        "主角随身的智能手机"]
    app.projects.save_document(episode["id"], "script", script)
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "script": script}
    shot = {"shot_no": 1, "scene_no": 1, "characters": [],
            "description": "主角掏出手机查资料", "duration": 3}
    prompt = app.director._rich_shot_prompt(ctx, shot, "明代大殿")
    assert "剧情白名单优先于通用时代禁令" in prompt
    assert "智能手机" in prompt
    assert "以本剧剧本为唯一标准" in prompt
    video_prompt = app.director._seedance_video_prompt(ctx, shot, [])
    assert "智能手机" in video_prompt and "绝不判错" in video_prompt
    # 质检端同样拿到白名单
    assert app.director._era_exceptions(ctx) == ["主角随身的智能手机"]
    spec = app.director._qc_spec(
        project["id"], [], era_exceptions=app.director._era_exceptions(ctx))
    assert spec["era_exceptions"] == ["主角随身的智能手机"]
    from aifos.adapters.claude_script import build_prompt
    qc_prompt = build_prompt("image_qc", {
        "image_uri": "/tmp/x.png", "characters": [], "count": 0,
        "era_exceptions": spec["era_exceptions"]})
    assert "禁止当成时代错乱判失败" in qc_prompt
    assert "智能手机" in qc_prompt


def test_normalize_bible_defaults_sanctioned_list():
    from aifos.adapters.claude_script import normalize_script_bible
    script = {"scenes": [], "characters": []}
    normalize_script_bible(script)
    assert script["story_world"]["sanctioned_anachronisms"] == []
    script2 = {"scenes": [], "characters": [],
               "story_world": {"sanctioned_anachronisms":
                               [" 主角的手机 ", ""]}}
    normalize_script_bible(script2)
    assert script2["story_world"]["sanctioned_anachronisms"] == ["主角的手机"]
