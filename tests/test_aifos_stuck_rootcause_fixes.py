"""「反复卡住」根因修复的回归锁。

根因1:景别容量 < 人数合同 → 编译出同级互斥合同 → 审核必熔断。
根因4:重启遗留 generating 僵尸认领 + awaiting_human 门禁被自动重派绕过。
根因5:.png 文件装 JPEG 字节 → API 校验 400 → image_qc 阶梯断级。
"""

import json

import pytest

from aifos.app import App
from aifos.camera_language import (
    CAMERA_SCALE_CAPACITY,
    enforce_scale_capacity,
    scale_capacity,
)
from aifos.prompt_contract import compile_shot_prompt
from aifos.production.api_providers import sniff_image_media
from aifos.workflow import _camera_plan


# ---------- 根因1:景别容量可行性门禁 ----------

def test_scale_capacity_matches_geometry():
    # 容量与 SCALE_GEOMETRY 同源:特写肩线以下出画,装不下第二个人。
    assert CAMERA_SCALE_CAPACITY == {
        "大特写": 1, "特写": 1, "近景": 2, "中景": 4}
    assert scale_capacity("全景") > 100
    assert scale_capacity("") > 100  # 未知景别不设限,宁可漏修不误改


def test_enforce_scale_capacity_upgrades_only_when_needed():
    assert enforce_scale_capacity("特写", 1) == ("特写", "")
    scale, note = enforce_scale_capacity("特写", 3)
    assert scale == "中景" and "升档" in note
    scale, note = enforce_scale_capacity("近景", 7)
    assert scale == "全景" and "7人" in note
    # 人数未知/非法时绝不动景别。
    assert enforce_scale_capacity("特写", None) == ("特写", "")
    assert enforce_scale_capacity("特写", "abc") == ("特写", "")
    assert enforce_scale_capacity("特写", 0) == ("特写", "")


def test_camera_plan_never_emits_infeasible_scale():
    # 盲轮换曾把 85mm 特写配给多人镜头 → 同级互斥 → 审核必熔断。
    for index in range(1, 9):
        plan = _camera_plan("", "dialogue", index, visible_count=3)
        assert scale_capacity(plan["shot_scale"]) >= 3, plan
        assert not (plan["lens"] == "85mm"
                    and plan["shot_scale"] in ("近景", "特写")
                    and scale_capacity(plan["shot_scale"]) < 3)
    # 显式特写 + 7 人:升档并留审计说明。
    plan = _camera_plan("特写", "dialogue", 1, visible_count=7)
    assert plan["shot_scale"] == "全景"
    assert "升档" in plan["capacity_note"]
    # 单人镜头不受影响,特写仍是特写。
    plan = _camera_plan("特写", "reaction", 1, visible_count=1)
    assert plan["shot_scale"] == "特写"
    assert "capacity_note" not in plan


def test_compile_shot_prompt_fixes_saved_infeasible_contract():
    """已保存的旧分镜(特写×3人全见)在编译期就地升档,不再送去熔断。"""
    shot = {
        "shot_no": 8, "scene_no": 1, "kind": "dialogue",
        "camera": "85mm特写,平视,正面",
        "characters": ["林川", "赵百户", "阿砚"],
        "description": "对峙",
        "action": "林川举手自证",
        "five_dimensions": {"camera_design": {
            "shot_scale": "特写", "lens": "85mm", "angle": "平视",
            "camera_position": "正面", "movement": "固定",
            "composition": "中心构图",
        }},
        "start_state": {}, "end_state": {},
    }
    contract, prompt = compile_shot_prompt(
        shot, location="废茶棚", style="明代历史", references=[],
        mode="image")
    camera = contract["camera"]
    assert camera["景别"] == "中景"
    assert "容量修正" in camera and "3人" in camera["容量修正"]
    # 85mm 是近景/特写绑定焦段,升档后必须一起改,否则再造一对矛盾。
    assert camera["焦段"] == "35mm"
    # 审计键不进提示词正文。
    assert "容量修正" not in prompt
    assert "特写" not in prompt.split("【镜头】")[1].split("。")[0]


def test_compile_keeps_feasible_closeup_untouched():
    shot = {
        "shot_no": 3, "scene_no": 1, "kind": "reaction",
        "camera": "特写", "characters": ["林川"],
        "description": "惊愕", "action": "瞳孔骤缩",
        "five_dimensions": {"camera_design": {
            "shot_scale": "特写", "lens": "85mm"}},
        "start_state": {}, "end_state": {},
    }
    contract, _prompt = compile_shot_prompt(
        shot, location="废茶棚", style="明代历史", references=[],
        mode="image")
    assert contract["camera"]["景别"] == "特写"
    assert contract["camera"]["焦段"] == "85mm"
    assert "容量修正" not in contract["camera"]


# ---------- 根因5:media_type 按真实字节 ----------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def test_sniff_image_media_trusts_bytes_over_suffix():
    assert sniff_image_media(PNG_BYTES) == "image/png"
    # 生产实证:.png 文件装 JPEG 字节 → 按后缀声明必 400。
    assert sniff_image_media(JPEG_BYTES, "image/png") == "image/jpeg"
    assert sniff_image_media(WEBP_BYTES, "image/png") == "image/webp"
    assert sniff_image_media(b"GIF89a" + b"\x00" * 10) == "image/gif"
    # 认不出的字节回落到后缀声明,不瞎猜。
    assert sniff_image_media(b"\x00" * 16, "image/webp") == "image/webp"
    assert sniff_image_media(b"", "image/png") == "image/png"


def test_qc_content_declares_actual_media_type(tmp_path):
    from aifos.production.api_providers import ClaudeApiProvider
    lying = tmp_path / "shot.keyframe.png"  # 后缀撒谎:装的是 JPEG
    lying.write_bytes(JPEG_BYTES)
    provider = ClaudeApiProvider.__new__(ClaudeApiProvider)
    content = provider._qc_content("检查这张图", {
        "image_uri": str(lying), "reference_manifest": []})
    image_blocks = [
        block for block in content if block.get("type") == "image"]
    assert image_blocks
    assert image_blocks[0]["source"]["media_type"] == "image/jpeg"


# ---------- 根因4:僵尸清扫 + 人工门禁 ----------

@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _preproduce(app, title="卡住修复", number=1):
    app.director.produce(title, number, pause_for_confirm=True)
    summary = app.director.produce(title, number, pause_for_confirm=True)
    if summary["status"] == "awaiting_cast":
        project = app.projects.get_project(title)
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=?",
            (project["id"], number))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                title, number, character["name"], 1)
        app.director.produce(title, number, pause_for_confirm=True)
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    return project, episode


def _plan_path(app, project):
    return (app.workspace.artifacts_dir
            / f"p{project['id']:03d}" / "e001" / "render_plan.json")


def test_reconcile_resets_stale_generating_claims(app):
    """重启前被认领的 generating 条目必须在下一轮生产入口重置。"""
    project, episode = _preproduce(app)
    plan_path = _plan_path(app, project)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stale_ids = []
    for item in plan["items"]:
        if item["category"] == "shot_image" and len(stale_ids) < 2:
            item["status"] = "generating"
            stale_ids.append(item["id"])
        elif item["category"] == "frames" and len(stale_ids) < 3:
            item["status"] = "retrying"
            stale_ids.append(item["id"])
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "storyboard": storyboard}
    result = app.director.reconcile_completed_shot_images(ctx)

    assert result["stale_reset"] == len(stale_ids)
    refreshed = json.loads(plan_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in refreshed["items"]}
    for item_id in stale_ids:
        assert by_id[item_id]["status"] == "pending"
        reset = by_id[item_id]["stale_reset"]
        assert reset["previous_status"] in ("generating", "retrying")
        assert "中断遗留" in reset["reason"]


def test_reconcile_reports_awaiting_human_shots_for_gate(app):
    """awaiting_human 镜头清单必须上报,供生产入口跳过(人工门禁)。"""
    project, episode = _preproduce(app)
    plan_path = _plan_path(app, project)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    gated = []
    for item in plan["items"]:
        if item["category"] == "shot_image" and len(gated) < 2:
            item["status"] = "awaiting_human"
            item["qc"] = {"passed": False, "issues": ["测试失败原因"]}
            gated.append(int(item["id"].split(":")[1]))
    plan_path.write_text(json.dumps(plan, ensure_ascii=False),
                         encoding="utf-8")

    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    ctx = {"project": dict(project), "episode": dict(episode),
           "out_root": app.workspace.artifacts_dir
           / f"p{project['id']:03d}" / "e001",
           "storyboard": storyboard}
    result = app.director.reconcile_completed_shot_images(ctx)
    assert result["awaiting_human_shots"] == sorted(gated)
