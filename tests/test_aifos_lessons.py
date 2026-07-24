"""出错经验库:质检失败原因自动归档 → 注入后续出图/视频提示词。"""

import pytest

from aifos.app import App
from aifos.lessons import (lesson_lines, lesson_worthy, lessons_block,
                           project_lessons, record_lessons)
from aifos.production.base import ProviderResult


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
    assert top["categories"] == {"shot_image": 1, "frames": 1}
    lines = lesson_lines(app.assets, project["id"])
    assert any("已出错2次" in line for line in lines)
    assert "严禁重犯" in lessons_block(app.assets, project["id"])


def test_environment_noise_is_not_a_lesson():
    assert not lesson_worthy("质检产线不可用，图片未放行:超时")
    assert not lesson_worthy("短")
    assert lesson_worthy("古代场景出现现代手机")


def test_lessons_injected_into_shot_and_video_prompts(app):
    """记过的错必须出现在后续出图提示词与视频提示词里。"""
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
    assert "严禁再犯" in prompt and "笔记本电脑" in prompt
    assert "【ERA LOCK】" in prompt          # 时代硬约束逐镜写死
    video_prompt = app.director._seedance_video_prompt(ctx, shot, [])
    assert "【时代与物理】" in video_prompt
    assert "严禁再犯" in video_prompt and "笔记本电脑" in video_prompt


def test_qc_failure_auto_records_lesson(app, tmp_path):
    """质检失败(即使重画后通过)自动进经验库——闭环的核心。"""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    calls = {"qc": 0}

    class StubRouter:
        def call(self, capability, payload, out_dir, cancel=None):
            if capability == "image":
                return ProviderResult(provider="codex", cost=1.0,
                                      uri=str(image))
            calls["qc"] += 1
            first = calls["qc"] == 1
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
                "reference_adjustments": [],
            } if first else {})
            return ProviderResult(
                provider="codex", cost=0.1,
                data={"pass": not first,
                      "identity_checked": True, "identity_match": True,
                      "gender_checked": True, "gender_match": True,
                      "count_checked": True, "count_match": True,
                      "issues": (["镜头1古代大殿里出现了笔记本电脑"]
                                 if first else []),
                      **diagnostics})

    app.director.router = StubRouter()
    result = app.director._generate_image_with_qc(
        "image", {"prompt": "x", "shot_no": 1}, tmp_path, None,
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
