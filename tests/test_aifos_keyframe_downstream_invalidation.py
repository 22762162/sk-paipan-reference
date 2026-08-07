"""Changing a formal keyframe retires only its dependent continuity suffix."""

from pathlib import Path

import pytest

from aifos.app import App
from aifos.continuity_graph import build_keyframe_continuity_plan
from aifos.production.base import ProviderResult
from aifos.selection_mode import build_candidate_set_version


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "workspace")
    yield instance
    instance.close()


def _seed_chain(app, *, title):
    project, _ = app.projects.get_or_create_project(title)
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    script = {
        "characters": [{"name": "虞寻歌", "role": "主角"}],
        "scenes": [
            {"scene_no": 1, "location": "虞家别墅·虞寻欢卧室"},
            # Editorial scene 2 is still the same physical set and therefore
            # remains in the same continuity chain.
            {"scene_no": 2, "location": "虞家别墅·虞寻欢卧室"},
            {"scene_no": 3, "location": "酒店走廊"},
        ],
    }
    shots = [
        {
            "shot_no": shot_no,
            "scene_no": {1: 1, 2: 1, 3: 2, 4: 3}[shot_no],
            "characters": ["虞寻歌"],
            "description": f"镜头{shot_no}",
            "start_state": {},
            "end_state": {},
        }
        for shot_no in range(1, 5)
    ]
    storyboard = {"shots": shots}
    app.projects.save_document(episode["id"], "script", script)
    app.projects.save_document(episode["id"], "storyboard", storyboard)
    ctx = {
        "project": dict(project),
        "episode": dict(episode),
        "script": script,
        "storyboard": storyboard,
        "out_root": app.director._episode_dir(project, episode),
    }
    plan = build_keyframe_continuity_plan(
        shots, {row["scene_no"]: row["location"] for row in script["scenes"]})
    assert [group["shot_nos"] for group in plan["groups"]] == [
        [1, 2, 3], [4]]
    return project, episode, ctx, shots


def _register_chain_assets(app, project_id, tmp_path):
    """Create active frames/videos and return their original rows/files."""
    rows = {}
    files = []
    for shot_no in range(1, 5):
        name = f"e001_shot{shot_no:03d}"
        for kind in ("first_frame", "last_frame", "video"):
            path = tmp_path / f"old-{kind}-{shot_no}.bin"
            path.write_bytes(f"{kind}-{shot_no}".encode())
            files.append(path)
            rows[(shot_no, kind)] = app.assets.register(
                project_id, kind, name, uri=str(path),
                meta={"original": True, "shot_no": shot_no})
    return rows, files


def _assert_only_suffix_invalidated(app, project_id, original_rows):
    for shot_no in (2, 3):
        name = f"e001_shot{shot_no:03d}"
        for kind in ("first_frame", "last_frame", "video"):
            assert app.assets.latest(project_id, kind, name) is None
            history = app.assets.history(project_id, kind, name)
            assert len(history) == 2
            assert history[0]["id"] == original_rows[(shot_no, kind)]["id"]
            assert app.assets.is_deleted(history[-1])

    # The group prefix and an independent location/continuity group remain
    # immediately reusable.
    for shot_no in (1, 4):
        name = f"e001_shot{shot_no:03d}"
        for kind in ("first_frame", "last_frame", "video"):
            active = app.assets.latest(project_id, kind, name)
            assert active is not None
            assert active["id"] == original_rows[(shot_no, kind)]["id"]
            assert len(app.assets.history(project_id, kind, name)) == 1


def test_completed_keyframe_replacement_invalidates_only_group_suffix(
        app, tmp_path):
    project, _episode, ctx, shots = _seed_chain(
        app, title="自动关键帧替换失效测试")
    old_image = tmp_path / "old-shot2.png"
    old_image.write_bytes(b"old-keyframe")
    old_image_row = app.assets.register(
        project["id"], "image", "e001_shot002", uri=str(old_image),
        meta={"file_sha256": app.director._file_sha256(old_image)})
    original_rows, old_files = _register_chain_assets(
        app, project["id"], tmp_path)

    new_image = tmp_path / "new-shot2.png"
    new_image.write_bytes(b"new-keyframe")
    result = ProviderResult(
        provider="stub", model="stub", cost=0.0,
        uri=str(new_image), data={})
    result.qc = {"passed": True}
    quality = {
        "level": "medium", "recommended": "medium",
        "source": "test", "rule": "", "reasons": [],
    }

    app.director._register_completed_shot_result(
        ctx, shots[1], quality, result,
        payload={"shot_no": 2, "prompt": "替换后的镜头2"})

    current = app.assets.latest(
        project["id"], "image", "e001_shot002")
    assert current is not None
    assert current["uri"] == str(new_image)
    image_history = app.assets.history(
        project["id"], "image", "e001_shot002")
    assert len(image_history) == 2
    assert image_history[0]["id"] == old_image_row["id"]
    assert all(path.is_file() for path in [old_image, *old_files])
    _assert_only_suffix_invalidated(app, project["id"], original_rows)


def _seed_candidate_group(app, ctx, tmp_path, *, shot_no=2):
    version = build_candidate_set_version(
        episode_id=str(ctx["episode"]["id"]), shot_no=shot_no,
        contract_revision=3, candidate_revision=7,
        prompt="镜头2固定卧室提示词",
        reference_manifest=[{"role": "scene", "uri": "bedroom.png"}],
    )
    candidates = []
    for index in range(1, 5):
        uri = tmp_path / f"candidate-{index}.png"
        uri.write_bytes(f"candidate-{index}".encode())
        candidates.append({
            "candidate_index": index,
            "candidate_id": f"{version.token}#{index}",
            "candidate_set_token": version.token,
            "candidate_seed": 2000 + index,
            "uri": str(uri),
            "prompt_hash": version.prompt_digest,
            "reference_hash": version.reference_digest,
        })
    group = {
        "schema": "aifos.shot-candidate-group/v1",
        "version": {
            "schema": version.schema,
            "episode_id": version.episode_id,
            "shot_no": version.shot_no,
            "contract_revision": version.contract_revision,
            "candidate_revision": version.candidate_revision,
            "prompt_digest": version.prompt_digest,
            "reference_digest": version.reference_digest,
            "token": version.token,
        },
        "candidate_set_id": "set-2",
        "candidate_set_token": version.token,
        "contract_revision": 3,
        "candidate_revision": 7,
        "candidate_count": 4,
        "expected_count": 4,
        "selection_required": True,
        "complete": True,
        "technical_incomplete": False,
        "same_prompt": True,
        "same_references": True,
        "candidates": candidates,
    }
    app.director._plan_write(ctx, {"items": [{
        "id": "shot:2", "category": "shot_image", "shot_no": 2,
        "status": "awaiting_selection", "image_quality": "medium",
        "candidate_group": group,
        "candidate_set_token": version.token,
        "candidate_revision": 7,
        "selection_required": True,
        "technical_incomplete": False,
    }]})
    return group


def _select(app, title, group, *, candidate_index, source):
    candidate = group["candidates"][candidate_index - 1]
    return app.director.select_shot_candidate(
        title, 1, 2,
        group["candidate_set_id"], group["candidate_set_token"],
        group["candidate_revision"], candidate["candidate_id"],
        candidate_index, source=source)


def test_manual_candidate_override_invalidates_only_group_suffix(
        app, tmp_path):
    title = "人工改选关键帧失效测试"
    project, _episode, ctx, _shots = _seed_chain(app, title=title)
    group = _seed_candidate_group(app, ctx, tmp_path)

    first = _select(
        app, title, group, candidate_index=1, source="ai")
    original_rows, old_files = _register_chain_assets(
        app, project["id"], tmp_path)

    changed = _select(
        app, title, group, candidate_index=4, source="manual")

    assert first["asset_version"] == 1
    assert changed["asset_version"] == 2
    assert changed["selected_uri"] == group["candidates"][3]["uri"]
    image_history = app.assets.history(
        project["id"], "image", "e001_shot002")
    assert [row["version"] for row in image_history] == [1, 2]
    assert image_history[0]["uri"] == group["candidates"][0]["uri"]
    assert all(path.is_file() for path in old_files)
    assert Path(image_history[0]["uri"]).is_file()
    _assert_only_suffix_invalidated(app, project["id"], original_rows)


@pytest.mark.parametrize("legacy_missing_digest", [False, True])
def test_render_plan_reconcile_versions_changed_pixels_and_retires_suffix(
        app, tmp_path, legacy_missing_digest):
    """断点对账不能按同一镜头文字哈希把新像素吞成旧资产。"""
    project, _episode, ctx, _shots = _seed_chain(
        app, title=f"断点关键帧像素替换-{legacy_missing_digest}")
    old_uri = tmp_path / "reconcile-old-shot2.png"
    old_uri.write_bytes(b"old-pixels")
    old_meta = {"shot_content_hash": "same-contract"}
    if not legacy_missing_digest:
        old_meta["file_sha256"] = app.director._file_sha256(old_uri)
    old_row = app.assets.register(
        project["id"], "image", "e001_shot002",
        uri=str(old_uri), meta=old_meta)
    original_rows, old_files = _register_chain_assets(
        app, project["id"], tmp_path)
    replacement = tmp_path / "reconcile-new-shot2.png"
    replacement.write_bytes(b"new-pixels")
    app.director._plan_write(ctx, {"items": [{
        "id": "shot:2", "category": "shot_image", "shot_no": 2,
        "status": "done", "output_uri": str(replacement),
        "content_hash": "same-contract", "image_quality": "medium",
        "qc": {"passed": True},
    }]})

    result = app.director.reconcile_completed_shot_images(ctx)

    assert result["recovered"] == 1
    current = app.assets.latest(
        project["id"], "image", "e001_shot002")
    assert current["version"] == 2
    assert current["uri"] == str(replacement)
    current_meta = app.director._asset_meta(current)
    assert current_meta["file_sha256"] == app.director._file_sha256(
        replacement)
    history = app.assets.history(
        project["id"], "image", "e001_shot002")
    assert history[0]["id"] == old_row["id"]
    assert old_uri.is_file() and replacement.is_file()
    assert all(path.is_file() for path in old_files)
    _assert_only_suffix_invalidated(app, project["id"], original_rows)
