"""Formal shot candidate promotion is CAS-safe and resumable."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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


def test_live_progress_candidate_can_be_manually_preselected(app, tmp_path):
    _project, _episode, ctx, groups = _seed_episode(
        app, tmp_path, complete=False)
    group = groups[1]
    plan = app.director._plan_read(ctx)
    item = plan["items"][0]
    item.pop("candidate_group", None)
    item["status"] = "generating"
    item["candidate_progress"] = {
        **copy.deepcopy(group),
        "schema": "aifos.shot-candidate-progress/v1",
        "live_progress": True,
        "completed_count": len(group["candidates"]),
        "settled_count": len(group["candidates"]),
    }
    app.director._plan_write(ctx, plan)

    result = _select(app, group, candidate_index=1)

    assert result["status"] == "selection_pending"
    assert result["pending"] is True
    assert result["selected_uri"] == group["candidates"][0]["uri"]
    stored = app.director._plan_read(ctx)["items"][0]
    assert stored["manual_candidate_selection"]["candidate_index"] == 1
    assert stored["candidate_progress"]["selection"]["pending"] is True
    assert app.assets.latest(
        _project["id"], "image", "e001_shot001") is None


def test_manual_selection_overrides_prior_ai_selection(app, tmp_path):
    project, _episode, ctx, groups = _seed_episode(app, tmp_path)
    group = groups[1]
    ai = _select(app, group, candidate_index=2, source="ai")

    manual = _select(app, group, candidate_index=4, source="manual")

    assert ai["source"] == "ai"
    assert manual["source"] == "manual"
    assert manual["candidate_index"] == 4
    assert manual["asset_version"] == 2
    item = app.director._plan_read(ctx)["items"][0]
    assert item["candidate_group"]["selection"]["source"] == "manual"
    assert item["output_uri"] == group["candidates"][3]["uri"]
    history = app.assets.history(project["id"], "image", "e001_shot001")
    assert len(history) == 2


def test_inflight_manual_choice_is_promoted_even_when_qc_failed(
        app, tmp_path):
    _project, _episode, _ctx, groups = _seed_episode(app, tmp_path)
    group = copy.deepcopy(groups[1])
    chosen = group["candidates"][2]
    chosen.update({"passed": False, "score": 0.25,
                   "issues": ["构图仍有风险"], "provider": "image-api",
                   "model": "seedream"})
    group["manual_selection_request"] = {
        "candidate_set_id": group["candidate_set_id"],
        "candidate_set_token": group["candidate_set_token"],
        "candidate_revision": group["candidate_revision"],
        "candidate_id": chosen["candidate_id"],
        "candidate_index": chosen["candidate_index"],
        "selected_at": 123.0,
    }
    result = SimpleNamespace(
        data={"candidate_group": group}, uri="", provider="",
        model="", qc={"passed": False}, cost=0.0, fallbacks=[])

    promoted = app.director._manual_promote_generated_candidate_group(result)

    selection = promoted.data["candidate_group"]["selection"]
    assert promoted.uri == chosen["uri"]
    assert selection["source"] == "manual"
    assert selection["candidate_passed"] is False
    assert selection["best_effort_risk"] is True
    assert promoted.qc["nonblocking_risk"]["issues"] == ["构图仍有风险"]


def test_candidate_progress_reporter_persists_images_and_returns_manual_pick(
        app, tmp_path):
    _project, _episode, ctx, groups = _seed_episode(app, tmp_path)
    group = groups[1]
    task = {
        "capability": "image", "item_id": "shot:1", "payload": {},
    }
    app.director._attach_candidate_progress_reporter(ctx, task)
    callback = task["payload"]["_candidate_progress_callback"]
    progress = {
        "candidate_set_id": group["candidate_set_id"],
        "candidate_set_token": group["candidate_set_token"],
        "candidate_revision": group["candidate_revision"],
        "generation_round": 2,
        "max_candidate_rounds": 10,
        "completed_count": 1,
        "settled_count": 1,
        "expected_count": 4,
        "status": "generating",
        "candidates": [copy.deepcopy(group["candidates"][0])],
    }

    assert callback(progress) is None
    plan = app.director._plan_read(ctx)
    item = plan["items"][0]
    assert item["candidate_progress"]["candidates"][0]["uri"] == \
        group["candidates"][0]["uri"]
    pending = {
        "candidate_set_id": group["candidate_set_id"],
        "candidate_set_token": group["candidate_set_token"],
        "candidate_revision": group["candidate_revision"],
        "candidate_id": group["candidates"][0]["candidate_id"],
        "candidate_index": 1,
        "selected_uri": group["candidates"][0]["uri"],
        "source": "manual",
        "pending": True,
    }
    item["manual_candidate_selection"] = pending
    app.director._plan_write(ctx, plan)

    returned = callback(progress)

    assert returned == pending
    stored = app.director._plan_read(ctx)["items"][0]
    assert stored["candidate_progress"]["selection"] == pending


def test_new_repair_round_atomically_replaces_old_group_and_prompt(
        app, tmp_path):
    _project, _episode, ctx, groups = _seed_episode(app, tmp_path)
    old_group = groups[1]
    task = {
        "capability": "image", "item_id": "shot:1", "payload": {},
    }
    app.director._attach_candidate_progress_reporter(ctx, task)
    callback = task["payload"]["_candidate_progress_callback"]
    new_token = "cset-v1:round-6-targeted"
    candidates = copy.deepcopy(old_group["candidates"][:2])
    for index, candidate in enumerate(candidates, 1):
        candidate.update({
            "candidate_id": f"{new_token}#{index}",
            "candidate_set_token": new_token,
        })
    prompt = "第6轮精准合同：只保留唯一可拍终态"
    progress = {
        "schema": "aifos.shot-candidate-progress/v1",
        "candidate_set_id": "set-round-6",
        "candidate_set_token": new_token,
        "candidate_revision": 12,
        "contract_revision": 9,
        "generation_round": 6,
        "max_candidate_rounds": 10,
        "completed_count": 2,
        "settled_count": 2,
        "expected_count": 4,
        "candidate_count": 2,
        "status": "generating",
        "prompt": prompt,
        "prompt_optimized": prompt,
        "prompt_used": prompt,
        "candidates": candidates,
        "resume_state": {
            "schema": app.director.CANDIDATE_RESUME_SCHEMA,
            "generation_round": 6,
            "max_candidate_rounds": 10,
            "round_history": [
                {"generation_round": value, "candidate_count": 4}
                for value in range(1, 6)
            ],
            "best_provisional": {},
            "resume_payload": {
                "_episode_id": _episode["id"],
                "shot_no": 1,
                "prompt": prompt,
                "prompt_compact": prompt,
                "_candidate_generation_round": 6,
            },
        },
    }

    callback(progress)

    item = app.director._plan_read(ctx)["items"][0]
    assert item["status"] == "generating"
    assert item["generation_round"] == 6
    assert item["candidate_set_token"] == new_token
    assert item["candidate_count"] == 2
    assert item["candidate_group"]["candidate_set_token"] == new_token
    assert item["candidate_group"]["candidate_count"] == 2
    assert item["candidate_group"]["candidates"] == candidates
    assert item["prompt"] == item["prompt_optimized"] == prompt
    assert "qc" not in item
    archived = item["candidate_group_history"][-1]
    assert archived["candidate_set_token"] == \
        old_group["candidate_set_token"]
    assert archived["render_qc"]["passed"] is False
    assert len(item["candidate_round_history"]) == 5


def test_paused_plan_reconcile_resumes_newest_round_not_old_group(
        app, tmp_path):
    _project, episode, ctx, groups = _seed_episode(app, tmp_path)
    old_group = groups[1]
    prompt = "第7轮恢复合同：手机只在左手，腕绳只在右腕"
    progress = {
        "schema": "aifos.shot-candidate-progress/v1",
        "candidate_set_id": "set-round-7",
        "candidate_set_token": "cset-v1:round-7",
        "candidate_revision": 13,
        "contract_revision": 10,
        "generation_round": 7,
        "max_candidate_rounds": 10,
        "completed_count": 1,
        "settled_count": 1,
        "expected_count": 4,
        "candidate_count": 1,
        "status": "generating",
        "candidates": [copy.deepcopy(old_group["candidates"][0])],
        "resume_state": {
            "schema": app.director.CANDIDATE_RESUME_SCHEMA,
            "generation_round": 7,
            "max_candidate_rounds": 10,
            "round_history": [
                {"generation_round": value, "candidate_count": 4}
                for value in range(1, 7)
            ],
            "best_provisional": {},
            "resume_payload": {
                "_episode_id": episode["id"],
                "shot_no": 1,
                "prompt": prompt,
                "prompt_compact": prompt,
                "_candidate_generation_round": 7,
            },
        },
    }
    plan = app.director._plan_read(ctx)
    item = plan["items"][0]
    item["status"] = "technical_incomplete"
    item["generation_round"] = 7
    item["candidate_progress"] = progress
    app.director._plan_write(ctx, plan)

    assert app.director._reconcile_candidate_progress_plan(ctx) == 1
    stored = app.director._plan_read(ctx)["items"][0]
    assert stored["candidate_group"]["candidate_set_token"] == \
        progress["candidate_set_token"]
    assert stored["candidate_set_token"] == progress["candidate_set_token"]
    assert stored["prompt"] == stored["prompt_optimized"] == prompt
    restored, resumed = \
        app.director._candidate_payload_from_resume_progress(
            {"_episode_id": episode["id"], "shot_no": 1,
             "prompt": "旧第2轮失败合同"},
            stored["candidate_progress"], max_rounds=10)
    assert resumed is True
    assert restored["prompt"] == prompt
    assert restored["_candidate_generation_round"] == 7


def test_regenerate_requires_confirmation_and_rejects_old_cas(app, tmp_path):
    _project, _episode, _ctx, groups = _seed_episode(app, tmp_path)
    group = groups[1]
    with pytest.raises(AifosError, match="再次确认"):
        app.director.regenerate_shot_candidates(
            "四图选片测试", 1, 1,
            group["candidate_set_id"], group["candidate_set_token"],
            group["candidate_revision"], "新的完整精准镜头提示词",
            confirm=False)
    with pytest.raises(AifosError, match="stale_candidate_set"):
        app.director.regenerate_shot_candidates(
            "四图选片测试", 1, 1,
            "old-id", group["candidate_set_token"],
            group["candidate_revision"], "新的完整精准镜头提示词",
            confirm=True)


def test_regenerate_archives_group_keeps_files_and_invalidates_formal_chain(
        app, tmp_path):
    project, _episode, ctx, groups = _seed_episode(app, tmp_path)
    old_group = groups[1]
    selected = _select(app, old_group, candidate_index=4)
    original_candidate_files = [
        row["uri"] for row in old_group["candidates"]]
    asset_name = "e001_shot001"
    for kind in ("first_frame", "last_frame", "video"):
        path = tmp_path / f"old-{kind}.bin"
        path.write_bytes(kind.encode())
        app.assets.register(
            project["id"], kind, asset_name, uri=str(path),
            meta={"old_chain": True})
    for kind, name in (
            ("edit", "e001_final"),
            ("review_board", "e001"),
            ("clip", "e001_scene01")):
        path = tmp_path / f"old-{kind}.bin"
        path.write_bytes(kind.encode())
        app.assets.register(project["id"], kind, name, uri=str(path))

    result = app.director.regenerate_shot_candidates(
        "四图选片测试", 1, 1,
        old_group["candidate_set_id"], old_group["candidate_set_token"],
        old_group["candidate_revision"],
        "镜头1新完整提示词：沈砚在书房查看案卷，固定机位与空间关系",
        confirm=True)

    assert result["status"] == "regenerating_candidates"
    assert result["candidate_set_id"] != old_group["candidate_set_id"]
    assert result["candidate_set_token"] == ""
    assert result["candidate_revision"] == 8
    assert result["contract_revision"] == 4
    assert result["history_count"] == 1
    assert result["need_resume"] is True
    assert all(Path(uri).is_file() for uri in original_candidate_files)
    plan = app.director._plan_read(ctx)
    item = next(row for row in plan["items"] if row["id"] == "shot:1")
    assert item["status"] == "pending"
    assert item["candidate_generation_status"] == "regenerating_candidates"
    assert item["candidate_group"]["candidate_set_id"] \
        == result["candidate_set_id"]
    assert item["candidate_group"]["candidate_count"] == 0
    assert item["candidate_group"]["candidate_revision"] == 8
    assert item["candidate_group"]["contract_revision"] == 4
    assert item["custom_prompt"] is True
    assert item["prompt"].startswith("镜头1新完整提示词")
    assert "selection" not in item
    assert "output_uri" not in item
    assert "qc" not in item
    archived = item["candidate_group_history"][0]
    assert archived["candidate_set_token"] == old_group["candidate_set_token"]
    assert archived["selection"]["candidate_seed"] == 1014
    assert archived["render_qc"]["passed"] is False
    for kind in ("image", "first_frame", "last_frame", "video"):
        assert app.assets.latest(project["id"], kind, asset_name) is None
    assert app.assets.latest(project["id"], "edit", "e001_final") is None
    assert app.assets.latest(project["id"], "review_board", "e001") is None
    assert app.assets.latest(project["id"], "clip", "e001_scene01") is None
    assert len(app.assets.history(
        project["id"], "image", asset_name)) == 2
    assert selected["asset_version"] == 1

    # A duplicate browser action carrying the retired identity cannot enqueue
    # another revision or archive the same group twice.
    with pytest.raises(AifosError, match="stale_candidate_set"):
        app.director.regenerate_shot_candidates(
            "四图选片测试", 1, 1,
            old_group["candidate_set_id"], old_group["candidate_set_token"],
            old_group["candidate_revision"], "重复提交不应生效", confirm=True)


def test_new_group_after_regeneration_promotes_new_asset_version(app, tmp_path):
    project, episode, ctx, groups = _seed_episode(app, tmp_path)
    old_group = groups[1]
    _select(app, old_group, candidate_index=1)
    regenerated = app.director.regenerate_shot_candidates(
        "四图选片测试", 1, 1,
        old_group["candidate_set_id"], old_group["candidate_set_token"],
        old_group["candidate_revision"], "重生后完整提示词", confirm=True)

    version = build_candidate_set_version(
        episode_id=str(episode["id"]), shot_no=1,
        contract_revision=regenerated["contract_revision"],
        candidate_revision=regenerated["candidate_revision"],
        prompt="重生后完整提示词", reference_manifest=[])
    candidates = []
    for index in range(1, 5):
        uri = tmp_path / f"new-candidate-{index}.png"
        uri.write_bytes(f"new-{index}".encode())
        candidates.append({
            "candidate_index": index,
            "candidate_id": f"{version.token}#{index}",
            "candidate_set_token": version.token,
            "candidate_seed": 2000 + index,
            "uri": str(uri),
            "prompt_hash": version.prompt_digest,
            "reference_hash": version.reference_digest,
        })
    plan = app.director._plan_read(ctx)
    item = next(row for row in plan["items"] if row["id"] == "shot:1")
    item["status"] = "awaiting_selection"
    item["candidate_group"].update({
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
        "candidate_set_token": version.token,
        "candidate_count": 4,
        "complete": True,
        "technical_incomplete": False,
        "same_prompt": True,
        "same_references": True,
        "candidates": candidates,
    })
    app.director._plan_write(ctx, plan)

    result = app.director.select_shot_candidate(
        "四图选片测试", 1, 1,
        regenerated["candidate_set_id"], version.token,
        regenerated["candidate_revision"], candidates[1]["candidate_id"],
        2, source="manual")

    assert result["asset_version"] == 3  # v1 old, v2 tombstone, v3 selected
    assert result["selected_uri"] == candidates[1]["uri"]
    assert len(app.assets.history(
        project["id"], "image", "e001_shot001")) == 3
    final_plan = app.director._plan_read(ctx)
    final_item = next(
        row for row in final_plan["items"] if row["id"] == "shot:1")
    assert final_item["candidate_group_history"][0][
        "candidate_set_token"] == old_group["candidate_set_token"]
    assert final_item["selection"]["candidate_seed"] == 2002
