"""Formal shot candidate promotion is CAS-safe and resumable."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.selection_mode import build_candidate_set_version


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "workspace")
    yield instance
    instance.close()


def _seed_episode(app, tmp_path, *, shot_count=1, complete=True):
    project, _ = app.projects.get_or_create_project(
        "四图选片测试", style="电影级半写实", aspect="9:16")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    script = {
        "characters": [{"name": "沈砚", "role": "主角"}],
        "scenes": [{"scene_no": 1, "location": "书房"}],
    }
    shots = [{
        "shot_no": index,
        "scene_no": 1,
        "characters": ["沈砚"],
        "description": f"沈砚查看第{index}份案卷",
        "start_state": {},
        "end_state": {},
    } for index in range(1, shot_count + 1)]
    app.projects.save_document(episode["id"], "script", script)
    app.projects.save_document(
        episode["id"], "storyboard", {"shots": shots})
    ctx = {
        "project": dict(project), "episode": dict(episode),
        "out_root": app.director._episode_dir(project, episode),
    }
    items = []
    groups = {}
    for shot in shots:
        version = build_candidate_set_version(
            episode_id=str(episode["id"]),
            shot_no=shot["shot_no"],
            contract_revision=3,
            candidate_revision=7,
            prompt=f"镜头{shot['shot_no']}完整提示词",
            reference_manifest=[{"role": "identity", "uri": "face.png"}],
        )
        candidate_count = 4 if complete else 3
        candidates = []
        for index in range(1, candidate_count + 1):
            uri = tmp_path / f"shot-{shot['shot_no']}-candidate-{index}.png"
            uri.write_bytes(f"shot-{shot['shot_no']}-{index}".encode())
            candidates.append({
                "candidate_index": index,
                "candidate_id": f"{version.token}#{index}",
                "candidate_set_token": version.token,
                "candidate_seed": 1000 + shot["shot_no"] * 10 + index,
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
            "candidate_set_id": f"set-{shot['shot_no']}",
            "candidate_set_token": version.token,
            "contract_revision": 3,
            "candidate_revision": 7,
            "candidate_count": candidate_count,
            "expected_count": 4,
            "selection_required": True,
            "complete": complete,
            "technical_incomplete": not complete,
            "same_prompt": True,
            "same_references": True,
            "candidates": candidates,
        }
        groups[shot["shot_no"]] = group
        items.append({
            "id": f"shot:{shot['shot_no']}",
            "category": "shot_image",
            "shot_no": shot["shot_no"],
            "status": ("awaiting_selection" if complete
                       else "technical_incomplete"),
            "image_quality": "medium",
            "candidate_group": copy.deepcopy(group),
            "candidate_set_token": version.token,
            "candidate_revision": 7,
            "selection_required": True,
            "technical_incomplete": not complete,
            "qc": {
                "passed": False,
                "issues": ["内容观察保留，不得被选片伪造成通过"],
            },
        })
    app.director._plan_write(ctx, {"items": items})
    return project, episode, ctx, groups


def _select(app, group, *, shot_no=1, candidate_index=2, source="manual"):
    return app.director.select_shot_candidate(
        "四图选片测试", 1, shot_no,
        group["candidate_set_id"], group["candidate_set_token"],
        group["candidate_revision"],
        group["candidates"][candidate_index - 1]["candidate_id"],
        candidate_index, source=source)


def test_select_promotes_only_plan_uri_preserves_qc_and_seed(app, tmp_path):
    project, _episode, ctx, groups = _seed_episode(
        app, tmp_path, shot_count=2)
    group = groups[1]

    result = _select(app, group, shot_no=1, candidate_index=2)

    assert result["status"] == "selected"
    assert result["already_selected"] is False
    assert result["remaining"] == 1
    assert result["all_selected"] is False
    assert result["need_resume"] is False
    assert result["last_pending"] is False
    assert result["selected_uri"] == group["candidates"][1]["uri"]
    plan = app.director._plan_read(ctx)
    item = next(row for row in plan["items"] if row["id"] == "shot:1")
    assert item["status"] == "done"
    assert item["output_uri"] == group["candidates"][1]["uri"]
    assert item["qc"] == {
        "passed": False,
        "issues": ["内容观察保留，不得被选片伪造成通过"],
    }
    assert item["candidate_group"]["selection"]["candidate_seed"] == 1012
    asset = app.assets.latest(
        project["id"], "image", "e001_shot001")
    meta = json.loads(asset["meta"])
    assert asset["uri"] == group["candidates"][1]["uri"]
    assert meta["candidate_set_token"] == group["candidate_set_token"]
    assert meta["candidate_revision"] == 7
    assert meta["candidate_id"] == group["candidates"][1]["candidate_id"]
    assert meta["candidate_seed"] == 1012
    assert meta["selection_source"] == "manual"
    assert "qc_passed" not in meta


def test_repeat_is_idempotent_and_last_selection_requests_resume(app, tmp_path):
    project, _episode, _ctx, groups = _seed_episode(app, tmp_path)
    first = _select(app, groups[1], candidate_index=3, source="ai")
    repeated = _select(app, groups[1], candidate_index=3, source="ai")

    assert first["last_pending"] is True
    assert first["all_selected"] is True
    assert first["need_resume"] is True
    assert repeated["already_selected"] is True
    assert repeated["asset_id"] == first["asset_id"]
    assert repeated["asset_version"] == first["asset_version"] == 1
    assert len(app.assets.history(
        project["id"], "image", "e001_shot001")) == 1
    state = app.director.shot_candidate_selection_state(
        "四图选片测试", 1)
    assert state["total"] == state["selected"] == 1
    assert state["remaining"] == 0
    assert state["all_selected"] is state["need_resume"] is True


@pytest.mark.parametrize("field", ["id", "token", "revision"])
def test_stale_group_identity_is_rejected(app, tmp_path, field):
    _project, _episode, _ctx, groups = _seed_episode(app, tmp_path)
    group = groups[1]
    args = {
        "expected_candidate_set_id": group["candidate_set_id"],
        "expected_candidate_set_token": group["candidate_set_token"],
        "expected_candidate_revision": group["candidate_revision"],
    }
    args[{
        "id": "expected_candidate_set_id",
        "token": "expected_candidate_set_token",
        "revision": "expected_candidate_revision",
    }[field]] = "old" if field != "revision" else 6
    with pytest.raises(AifosError, match="stale_candidate_set"):
        app.director.select_shot_candidate(
            "四图选片测试", 1, 1,
            args["expected_candidate_set_id"],
            args["expected_candidate_set_token"],
            args["expected_candidate_revision"],
            group["candidates"][0]["candidate_id"], 1)


def test_double_selection_has_exactly_one_winner(app, tmp_path):
    project, _episode, _ctx, groups = _seed_episode(app, tmp_path)
    group = groups[1]

    def choose(index):
        try:
            return ("ok", _select(app, group, candidate_index=index))
        except AifosError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(choose, (1, 2)))

    assert [status for status, _ in outcomes].count("ok") == 1
    error = next(value for status, value in outcomes if status == "error")
    assert "selection_conflict" in error
    history = app.assets.history(
        project["id"], "image", "e001_shot001")
    assert len(history) == 1


def test_technical_incomplete_group_cannot_be_selected(app, tmp_path):
    _project, _episode, _ctx, groups = _seed_episode(
        app, tmp_path, complete=False)
    with pytest.raises(AifosError, match="candidate_group_incomplete"):
        _select(app, groups[1], candidate_index=1)
    state = app.director.shot_candidate_selection_state(
        "四图选片测试", 1, shot_no=1)
    assert state["remaining"] == 1
    assert state["items"][0]["technical_incomplete"] is True

