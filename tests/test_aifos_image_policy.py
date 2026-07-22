"""图片成本分层：普通批量不误用高档，终稿/文字图保留高质量通道。"""

from pathlib import Path

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.ops_center import OpsCenter
from aifos.production.base import ProviderResult


def _shot(readable_text=None):
    return {
        "shot_no": 1, "scene_no": 1, "unit_id": "U01",
        "prompt": "两人在办公室交谈", "description": "两人交谈",
        "characters": ["林昭"], "character_count": 1,
        "camera": "中景缓推", "dialogue": None,
        "start_state": {}, "end_state": {}, "five_dimensions": {},
        "readable_text": readable_text or {"required": False},
    }


def test_shot_payload_separates_batch_from_complex_text(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("成本路由")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        identity = tmp_path / "identity.png"
        identity.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        app.assets.register(
            project["id"], "character_identity", "林昭",
            uri=str(identity), meta={"locked": True, "character": "林昭"})
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "script": {"scenes": [{"scene_no": 1, "location": "办公室"}]},
            "storyboard": {"shots": []},
            "blocking": {"shot_index": {"1": {
                "actors": [{"actor_id": "P01", "name": "林昭"}],
                "constraint": "P01 从左侧走向桌边",
            }}},
            "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
        }
        batch = app.director._shot_payload(ctx, _shot())
        assert batch["image_task_class"] == "batch"
        assert batch["image_quality"] == "medium"
        assert batch["require_reference_images"] is True
        assert batch["character_refs"][0] == str(identity)
        assert batch["identity_references"][0]["actor_id"] == "P01"
        assert batch["character_reference_map"] == [{
            "actor_id": "P01", "character": "林昭", "uri": str(identity),
        }]

        text = app.director._shot_payload(ctx, _shot({
            "required": True, "carrier": "手机屏幕",
            "whitelist": ["直播开始"],
        }))
        assert text["image_task_class"] == "complex_text"
        assert text["image_quality"] == "high"
    finally:
        app.close()


def test_cover_is_final_high_and_carries_locked_portraits(tmp_path):
    calls = []

    class Router:
        def call(self, capability, payload, out_dir):
            calls.append((capability, payload, out_dir))
            return ProviderResult(
                provider="codex", cost=0, uri=str(tmp_path / "c.png"))

    identity = tmp_path / "林昭.png"
    identity.write_bytes(b"x")
    OpsCenter(Router()).make_cover(
        {"project_title": "测试剧", "episode_number": 1, "logline": "危机"},
        tmp_path, identity_references=[{
            "character": "林昭", "uri": str(identity), "version": 1,
        }])
    capability, payload, _ = calls[0]
    assert capability == "cover"
    assert payload["image_task_class"] == "final"
    assert payload["image_quality"] == "high"
    assert payload["require_reference_images"] is True
    assert payload["character_refs"] == [str(identity)]

    instruction, _targets, _data = build_instruction(
        "cover", payload, tmp_path)
    assert "人工锁定最终立绘" in instruction
    assert "允许与人物参考图服装不同" in instruction
    assert str(identity) in instruction
