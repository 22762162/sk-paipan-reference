"""场景/边界输入签名：同 URI 被改写也必须使下游缓存失效。"""

import binascii
import struct
import zlib

import pytest

from aifos.app import App
from aifos.director import Director
from aifos.production.base import ProviderResult


def _write_png(path, rgb):
    """Write a valid one-pixel RGB PNG without adding a Pillow dependency."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(payload)
    return str(path)


def _frame_payload(tmp_path):
    keyframe = _write_png(tmp_path / "keyframe.png", (255, 0, 0))
    predecessor = _write_png(tmp_path / "previous-tail.png", (0, 255, 0))
    scene = _write_png(tmp_path / "canonical-scene.png", (0, 0, 255))
    return {
        "shot_no": 2,
        "prompt_compact": "同一卧室内，人物从上一镜位置继续向窗边移动。",
        "keyframe_reference_uri": keyframe,
        "chain_first_uri": predecessor,
        "scene_ref": scene,
        "canonical_scene_asset_id": 41,
        "canonical_scene_asset_version": 3,
        "canonical_scene_reference_uri": scene,
        "physical_scene_id": "虞家别墅_虞寻欢卧室",
    }


def test_frame_signature_changes_when_keyframe_bytes_change_at_same_uri(
        tmp_path):
    payload = _frame_payload(tmp_path)
    before = Director._frame_input_snapshot(payload)["input_signature"]

    _write_png(tmp_path / "keyframe.png", (255, 255, 0))

    after = Director._frame_input_snapshot(payload)["input_signature"]
    assert after != before


def test_frame_signature_changes_when_previous_tail_bytes_change_at_same_uri(
        tmp_path):
    payload = _frame_payload(tmp_path)
    before = Director._frame_input_snapshot(payload)["input_signature"]

    _write_png(tmp_path / "previous-tail.png", (0, 255, 255))

    after = Director._frame_input_snapshot(payload)["input_signature"]
    assert after != before


def test_frame_signature_changes_with_canonical_scene_version_or_bytes(
        tmp_path):
    payload = _frame_payload(tmp_path)
    initial = Director._frame_input_snapshot(payload)["input_signature"]

    version_changed = {
        **payload,
        "canonical_scene_asset_version": 4,
    }
    version_signature = Director._frame_input_snapshot(
        version_changed)["input_signature"]
    assert version_signature != initial

    _write_png(tmp_path / "canonical-scene.png", (255, 0, 255))
    bytes_signature = Director._frame_input_snapshot(
        version_changed)["input_signature"]
    assert bytes_signature != version_signature


def test_frame_dispatch_refreezes_signature_after_prompt_review(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("首尾帧最终输入冻结")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "out_root": tmp_path,
        }
        app.director._plan_write(ctx, {"items": [{
            "id": "frames:2",
            "category": "frames",
            "label": "镜头 02 首尾帧",
            "status": "pending",
        }]})
        payload = _frame_payload(tmp_path)
        early_snapshot = Director._frame_input_snapshot(payload)
        payload["input_snapshot"] = early_snapshot
        payload["input_signature"] = early_snapshot["input_signature"]

        # Simulate the real ordering: the stage took its reuse snapshot, then
        # Codex review replaced the final provider-facing prompt.
        reviewed_prompt = "同一冻结卧室内，人物只向窗边移动一步。"
        payload["prompt_compact"] = reviewed_prompt
        task = {
            "item_id": "frames:2",
            "capability": "frames",
            "payload": payload,
            "sub_dir": "frames",
            "tag": 2,
        }

        app.director._prepare_dispatch_contracts(ctx, [task])

        final_snapshot = task["payload"]["input_snapshot"]
        assert final_snapshot["prompt"] == reviewed_prompt
        assert final_snapshot["input_signature"] != (
            early_snapshot["input_signature"])
        assert task["payload"]["input_signature"] == (
            final_snapshot["input_signature"])
        assert task["_dispatch_contract"]["payload"][
            "input_signature"] == final_snapshot["input_signature"]
    finally:
        app.close()


@pytest.mark.parametrize(
    ("field", "filename", "replacement"),
    [
        ("first", "first.png", (255, 255, 255)),
        ("last", "last.png", (32, 64, 96)),
        ("keyframe", "keyframe.png", (96, 64, 32)),
        ("canonical_scene_reference_uri", "scene.png", (128, 0, 128)),
    ],
)
def test_video_signature_changes_when_bound_pixels_change_at_same_path(
        tmp_path, field, filename, replacement):
    paths = {
        "first": _write_png(tmp_path / "first.png", (255, 0, 0)),
        "last": _write_png(tmp_path / "last.png", (0, 255, 0)),
        "keyframe": _write_png(tmp_path / "keyframe.png", (0, 0, 255)),
        "canonical_scene_reference_uri": _write_png(
            tmp_path / "scene.png", (255, 255, 0)),
    }
    payload = {
        "shot_no": 1,
        "prompt_compact": "卧室内单一连续动作。",
        **paths,
        "reference_images": [paths["canonical_scene_reference_uri"]],
        "reference_manifest": [{
            "index": 3,
            "asset_id": 41,
            "kind": "scene_art",
            "name": "卧室::view:panorama",
            "version": 3,
            "uri": paths["canonical_scene_reference_uri"],
            "binding": "统一物理母场景",
        }],
        "physical_scene_id": "虞家别墅_虞寻欢卧室",
        "canonical_scene_asset_id": 41,
        "canonical_scene_asset_version": 3,
        "duration": 5,
        "video_model_tier": "seedance2_0",
        "video_resolution": "720p",
    }
    before = Director._video_input_snapshot(payload)["input_signature"]

    _write_png(tmp_path / filename, replacement)

    after = Director._video_input_snapshot(payload)["input_signature"]
    assert after != before, f"{field} 同路径像素改写后必须使视频输入失效"


def test_video_signature_changes_when_motion_reference_changes_at_same_uri(
        tmp_path):
    motion_reference = tmp_path / "motion-reference.mp4"
    motion_reference.write_bytes(b"motion-version-0001")
    payload = {
        "shot_no": 1,
        "prompt_compact": "只读取参考视频的运动轨迹。",
        "reference_videos": [str(motion_reference)],
        "duration": 5,
        "video_model_tier": "seedance2_0",
        "video_resolution": "720p",
    }
    before = Director._video_input_snapshot(payload)["input_signature"]

    # Keep the exact URI (and even byte length) while replacing its content.
    motion_reference.write_bytes(b"motion-version-0002")

    after = Director._video_input_snapshot(payload)["input_signature"]
    assert after != before


def test_provider_prompt_used_is_audit_only_not_video_reuse_cache_key(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("视频请求与执行签名")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        request_prompt = "冻结请求：人物沿卧室窗边只向前走一步。"
        provider_prompt = (
            request_prompt
            + "\n--duration 5 --resolution 720p --voice builtin")
        payload = {
            "shot_no": 1,
            "prompt": request_prompt,
            "prompt_compact": request_prompt,
            "duration": 5,
            "video_model_tier": "seedance2_0",
            "video_resolution": "720p",
        }
        request_snapshot = Director._video_input_snapshot(payload)
        request_signature = request_snapshot["input_signature"]
        payload["input_snapshot"] = request_snapshot
        payload["input_signature"] = request_signature
        task = {
            "shot": {"shot_no": 1, "duration": 5.0},
            "payload": payload,
            "quality": {
                "level": "medium", "resolution": "720p",
                "source": "default",
            },
            "reference_assets": [],
            "reference_manifest": [],
        }
        generated = tmp_path / "generated.mp4"
        generated.write_bytes(b"generated video")
        result = ProviderResult(
            provider="mock", model="seedance2.0fast_vip", cost=0.0,
            uri=str(generated), data={
                "prompt_used": provider_prompt,
                "voice": "jimeng_builtin",
                "lip_sync": True,
            })

        app.director._finish_video_call(
            {"project": dict(project), "episode": dict(episode)},
            task, result)

        row = app.assets.latest(
            project["id"], "video", "e001_shot001")
        meta = app.director._asset_meta(row)
        # The provider may append transport flags.  Those bytes belong to the
        # execution audit, not to the deterministic request cache identity.
        assert meta["request_signature"] == request_signature
        assert meta["execution_snapshot"]["prompt_sent"] == provider_prompt
        assert meta["execution_snapshot"]["prompt_sent_hash"] != (
            request_snapshot["prompt_sent_hash"])
        # Preparing the identical frozen request again must hit the cache even
        # though the provider reported a different actual prompt_used string.
        assert app.director._video_asset_matches_current_input(
            {"project": dict(project)}, row, task) is True
    finally:
        app.close()


@pytest.mark.parametrize(
    ("stored_signature", "should_reuse"),
    [
        ("sig-current", True),
        ("", False),
        ("sig-stale", False),
    ],
    ids=["exact-match", "missing-signature", "mismatched-signature"],
)
def test_video_stage_reuses_only_exact_current_prepared_signature(
        tmp_path, monkeypatch, stored_signature, should_reuse):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("视频签名复用")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        output = tmp_path / "existing.mp4"
        output.write_bytes(b"existing video")
        meta = (
            {
                "input_signature": stored_signature,
                "file_sha256": Director._file_sha256(output),
            }
            if stored_signature else {})
        app.assets.register(
            project["id"], "video", "e001_shot001",
            uri=str(output), meta=meta)
        shot = {"shot_no": 1, "duration": 5.0}
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "storyboard": {"shots": [shot]},
            "frames": [{"shot_no": 1, "first": "first.png",
                        "last": "last.png"}],
            "videos": [],
            "out_root": tmp_path,
        }
        prepared = []
        generated = []

        def prepare(_ctx, current_shot, _frames):
            task = {
                "shot": current_shot,
                "payload": {"shot_no": 1,
                            "input_signature": "sig-current"},
                "quality": {
                    "level": "medium", "resolution": "720p",
                    "source": "default",
                },
                "reference_assets": [],
                "reference_manifest": [],
            }
            prepared.append(task)
            return task

        def run(_ctx, tasks):
            generated.extend(tasks)
            return {
                1: {"shot_no": 1, "uri": str(tmp_path / "new.mp4")}
            } if tasks else {}

        monkeypatch.setattr(app.director, "_prepare_video_call", prepare)
        monkeypatch.setattr(app.director, "_run_videos_parallel", run)

        result = app.director._stage_videos(ctx)

        # Even an existing file must first be compared with the signature of
        # this run's fully prepared Seedance request.
        assert len(prepared) == 1
        assert result["reused"] == int(should_reuse)
        assert result["generated"] == int(not should_reuse)
        assert len(generated) == int(not should_reuse)
        if should_reuse:
            assert ctx["videos"][0]["uri"] == str(output)
        else:
            assert ctx["videos"][0]["uri"] == str(tmp_path / "new.mp4")
    finally:
        app.close()
