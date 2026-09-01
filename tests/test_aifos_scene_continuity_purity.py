"""Regression gates for pure prompt compilation and one-set video inputs."""

import copy

import pytest

from aifos.app import App
from aifos.director import Director


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _image(path, marker=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG + marker)
    return str(path)


def _shot(shot_no, scene_no):
    return {
        "shot_no": shot_no,
        "scene_no": scene_no,
        "unit_id": f"U{shot_no:02d}",
        "duration": 5.0,
        "characters": [],
        "visible_figure_count": 0,
        "prompt": "卧室内固定家具保持不变。",
        "description": f"镜头{shot_no}只改变机位，不改变卧室陈设。",
        "camera": (
            "9:16竖幅，50mm平视中景；摄影机保持固定机位，"
            "不推、不拉、不摇、不移、不升降、不环绕、不变焦。"
        ),
        "shot_contract": {
            "景别": "全景", "角度": "俯拍", "焦段": "24mm",
            "机位": "正面", "运镜": "环绕", "构图": "居中",
        },
        "five_dimensions": {"camera_design": {}},
        "start_state": {},
        "end_state": {},
        "readable_text": {"required": False},
        # Force the repair compiler down the historically mutating path.
        "prompt_block_repair": {"repair_summary": "收敛为固定机位"},
    }


def _scene_ctx(app, tmp_path):
    project, _ = app.projects.get_or_create_project("场景连续性纯度")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    physical_scene = "虞家别墅·虞寻欢卧室"
    script = {
        "characters": [],
        "scenes": [
            {
                "scene_no": 1,
                "location": "虞家别墅·虞寻欢卧室床侧",
                "physical_scene_id": physical_scene,
            },
            {
                "scene_no": 2,
                "location": "虞家别墅·虞寻欢卧室窗侧",
                "physical_scene_id": physical_scene,
            },
        ],
    }
    shots = [_shot(1, 1), _shot(2, 2)]
    canonical_uri = _image(tmp_path / "canonical-bedroom.png", b"canonical")
    canonical = app.assets.register(
        project["id"], "scene_art",
        app.director._scene_view_asset_name(
            physical_scene, app.director.SCENE_PANORAMA_KEY),
        uri=canonical_uri,
        meta={
            "image_quality": "high",
            "base_location": physical_scene,
            "physical_scene_id": physical_scene,
            "file_sha256": Director._file_sha256(canonical_uri),
        },
    )
    legacy_uri = _image(tmp_path / "legacy-similar-bedroom.png", b"legacy")
    legacy = app.assets.register(
        project["id"], "scene_art", physical_scene,
        uri=legacy_uri,
        meta={
            "image_quality": "high",
            "base_location": physical_scene,
            "physical_scene_id": physical_scene,
            "file_sha256": Director._file_sha256(legacy_uri),
            "superseded_by_asset_id": int(canonical["id"]),
        },
    )
    # Simulate an old saved manual reference document.  A new canonical
    # panorama now exists, so the historical similar-room asset must not be
    # allowed back into any current Seedance manifest.
    app.projects.save_document(episode["id"], "video_references", {
        "schema": "aifos.video-references/v1",
        "shots": {
            str(shot["shot_no"]): [{
                "asset_id": int(legacy["id"]),
                "kind": legacy["kind"],
                "name": legacy["name"],
                "version": int(legacy["version"]),
            }]
            for shot in shots
        },
    })
    frames = {}
    images = []
    for shot in shots:
        shot_no = int(shot["shot_no"])
        first = _image(tmp_path / f"shot-{shot_no}-first.png", b"first")
        last = _image(tmp_path / f"shot-{shot_no}-last.png", b"last")
        keyframe = _image(
            tmp_path / f"shot-{shot_no}-keyframe.png", b"keyframe")
        frames[shot_no] = {
            "shot_no": shot_no,
            "first": first,
            "last": last,
            "image_quality": "high",
        }
        images.append({"shot_no": shot_no, "uri": keyframe})
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "out_root": tmp_path,
        "script": script,
        "storyboard": {"shots": shots},
        "blocking": {},
        "images": images,
        "frames": list(frames.values()),
        "videos": [],
        "aspect": "9:16",
        "dims": {"width": 720, "height": 1280},
        "quality_policy": {},
        "production_profile": {
            "voice": "jimeng_builtin",
            "lip_sync": True,
            "burn_subtitles": False,
            "video_model": "seedance2.0fast_vip",
            "standard_fingerprint": "std-scene-lock",
            "rules": {"production": {"model_upgrade_policy": {}}},
        },
    }
    return ctx, frames, canonical, legacy


def test_repeated_image_and_video_prompt_compilation_is_pure_and_stable(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        ctx, frames, _canonical, _legacy = _scene_ctx(app, tmp_path / "art")
        shot = ctx["storyboard"]["shots"][0]
        original_shot = copy.deepcopy(shot)
        original_board = copy.deepcopy(ctx["storyboard"])

        image_payloads = [
            app.director._shot_payload(ctx, shot) for _ in range(3)]
        image_inputs = [
            app.director._image_generation_input(payload)
            for payload in image_payloads]
        video_tasks = [
            app.director._prepare_video_call(ctx, shot, frames)
            for _ in range(3)]

        assert shot == original_shot
        assert ctx["storyboard"] == original_board
        assert [payload["prompt_compact"] for payload in image_payloads] == [
            image_payloads[0]["prompt_compact"]] * 3
        assert [item["input_hash"] for item in image_inputs] == [
            image_inputs[0]["input_hash"]] * 3
        assert [
            task["payload"]["input_signature"] for task in video_tasks
        ] == [video_tasks[0]["payload"]["input_signature"]] * 3
        assert [
            task["payload"]["prompt_compact"] for task in video_tasks
        ] == [video_tasks[0]["payload"]["prompt_compact"]] * 3
    finally:
        app.close()


@pytest.mark.parametrize("reference_source", ["manual", "frozen"])
def test_same_physical_scene_uses_one_canonical_asset_and_drops_legacy(
        tmp_path, reference_source):
    app = App(tmp_path / "ws")
    try:
        ctx, frames, canonical, legacy = _scene_ctx(app, tmp_path / "art")
        if reference_source == "frozen":
            ctx["frozen_video_reference_asset_ids"] = {
                str(shot["shot_no"]): [
                    int(canonical["id"]), int(legacy["id"])]
                for shot in ctx["storyboard"]["shots"]
            }
        tasks = [
            app.director._prepare_video_call(ctx, shot, frames)
            for shot in ctx["storyboard"]["shots"]
        ]
        payloads = [task["payload"] for task in tasks]

        assert {payload["physical_scene_id"] for payload in payloads} == {
            "虞家别墅·虞寻欢卧室"}
        assert {payload["canonical_scene_asset_id"] for payload in payloads} \
            == {int(canonical["id"])}
        assert {
            payload["canonical_scene_reference_uri"] for payload in payloads
        } == {canonical["uri"]}
        assert {
            payload["input_snapshot"]["canonical_scene_file_sha256"]
            for payload in payloads
        } == {Director._file_sha256(canonical["uri"])}

        for payload in payloads:
            scene_entries = [
                item for item in payload["reference_manifest"]
                if item["kind"] == "scene_art"]
            assert [item["asset_id"] for item in scene_entries] == [
                int(canonical["id"])]
            assert [item["uri"] for item in scene_entries] == [
                canonical["uri"]]
            assert legacy["uri"] not in payload["reference_images"]
            assert all(
                int(item["asset_id"]) != int(legacy["id"])
                for item in payload["reference_manifest"])
    finally:
        app.close()
