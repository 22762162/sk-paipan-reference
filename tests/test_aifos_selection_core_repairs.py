"""Director invariants agreed in the Codex/Claude selection-mode review."""

import copy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from aifos.director import Director, _PLAN_IO_LOCK
from aifos.production.base import ProviderResult
from aifos.selection_mode import build_candidate_set_version


class _Config:
    def __init__(self, defaults):
        self.data = {"defaults": defaults}

    def get(self, *keys, default=None):
        value = self.data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


def _director(defaults):
    director = Director.__new__(Director)
    director.config = _Config(defaults)
    director.log = SimpleNamespace(info=lambda *_: None, warn=lambda *_: None)
    return director


def _complete_group(tmp_path):
    version = build_candidate_set_version(
        episode_id="episode-1", shot_no=3, contract_revision=1,
        candidate_revision=2, prompt="frozen", reference_manifest=[])
    candidates = []
    for index, score in enumerate((11, 90, 55, 40), 1):
        uri = tmp_path / f"candidate-{index}.png"
        uri.write_bytes(str(index).encode())
        candidates.append({
            "candidate_index": index,
            "candidate_id": f"{version.token}#{index}",
            "candidate_set_token": version.token,
            "uri": str(uri),
            "passed": True,
            "score": score,
            "candidate_seed": None,
            "candidate_variation_key": f"variation-{index}",
            "seed_consumed": False,
        })
    return {
        "schema": "aifos.shot-candidate-group/v1",
        "version": asdict(version),
        "candidate_set_id": "set-3",
        "candidate_set_token": version.token,
        "contract_revision": 1,
        "candidate_revision": 2,
        "candidate_count": 4,
        "expected_count": 4,
        "complete": True,
        "technical_incomplete": False,
        "selection_required": True,
        "recommended_candidate_index": 2,
        "same_prompt": True,
        "same_references": True,
        "candidates": candidates,
    }


def test_new_candidate_group_rejects_corrupted_file_after_probe(tmp_path):
    group = _complete_group(tmp_path)
    first = group["candidates"][0]
    first["technical_probe"] = {
        "schema": "aifos.image-media-probe/v1",
        "status": "passed",
        "probe_ok": True,
        "decoded": True,
    }

    assert Director._shot_candidate_group_valid({
        "candidate_group": group}) is False


def test_shot_content_hash_ignores_derived_continuity_reference():
    director = _director({"selection_mode": True})
    shot = {"shot_no": 1, "scene_no": 1, "description": "人物入门"}
    base = {
        "prompt_compact": "人物入门；图1=身份母资产",
        "prompt_contract": {"action": "人物入门", "references": [{
            "index": 1, "role": "identity", "label": "身份母资产"}]},
        "reference_manifest": [{
            "role": "identity", "uri": "/locked/face.png"}],
    }
    with_continuity = copy.deepcopy(base)
    with_continuity["prompt_compact"] += "；图2=上一镜尾帧"
    with_continuity["prompt_contract"]["references"].append({
        "index": 2, "role": "continuity", "label": "上一镜尾帧"})
    with_continuity["reference_manifest"].append({
        "role": "continuity", "uri": "/generated/previous-shot.png"})
    changed_identity = copy.deepcopy(base)
    changed_identity["reference_manifest"][0]["uri"] = "/locked/new-face.png"

    assert director._shot_content_hash(shot, base) == \
        director._shot_content_hash(shot, with_continuity)
    assert director._shot_content_hash(shot, base) != \
        director._shot_content_hash(shot, changed_identity)


def test_repair_group_delegates_to_four_candidate_path_when_qc_off():
    director = _director({
        "selection_mode": True, "image_content_qc": True})
    seen = {}

    def generate(_capability, payload, _out_dir, _cancel, _qc_spec):
        seen.update(copy.deepcopy(payload))
        return ProviderResult(provider="fake", cost=0, data={}, uri="")

    director._generate_shot_candidate_group = generate
    director._generate_repair_candidate_group(
        "image", {"prompt": "revised"}, Path("/tmp"), None, {})

    assert seen["prompt"] == "revised"
    assert seen["qc_consecutive_failures_base"] == 1


def test_strict_mode_ai_promotes_only_qc_passing_current_candidate(tmp_path):
    director = _director({
        "selection_mode": False, "image_content_qc": True})
    group = _complete_group(tmp_path)
    result = ProviderResult(
        provider="fake", cost=4,
        data={"candidate_group": group}, uri="")

    promoted = director._ai_promote_generated_candidate_group(result)

    assert promoted.uri == group["candidates"][1]["uri"]
    selection = promoted.data["candidate_group"]["selection"]
    assert selection["source"] == "ai"
    assert selection["candidate_index"] == 2
    assert selection["candidate_set_token"] == group[
        "candidate_set_token"]
    assert selection["seed_consumed"] is False
    assert promoted.data["candidate_group"]["selection_required"] is False
    assert Director._candidate_selection_pending(promoted) is False


def test_strict_shot_group_finishes_through_ai_promotion(tmp_path):
    director = _director({
        "selection_mode": False, "image_content_qc": True})
    group = _complete_group(tmp_path)
    recommended_uri = group["candidates"][1]["uri"]
    generated = ProviderResult(
        provider="fake", cost=4,
        data={"candidate_group": group}, uri="")
    generated.qc = {"passed": True, "issues": [], "score": 90}
    director._generate_image_gacha = lambda *_args, **_kwargs: generated

    result = director._generate_shot_candidate_group(
        "image", {"shot_no": 3}, tmp_path, None, {"item_id": "shot:3"})

    assert result.uri == recommended_uri
    assert result.data["selection"]["source"] == "ai"
    assert result.data["selection"]["candidate_index"] == 2


def test_selection_mode_never_auto_promotes_candidate(tmp_path):
    director = _director({
        "selection_mode": True, "image_content_qc": True})
    result = ProviderResult(
        provider="fake", cost=4,
        data={"candidate_group": _complete_group(tmp_path)}, uri="")

    unchanged = director._ai_promote_generated_candidate_group(result)

    assert unchanged.uri == ""
    assert "selection" not in unchanged.data["candidate_group"]
    assert Director._candidate_selection_pending(unchanged) is True


def test_plan_seed_holds_one_lock_for_read_modify_write():
    director = _director({"selection_mode": True})
    state = {"items": []}

    def read(_ctx):
        assert _PLAN_IO_LOCK._is_owned()
        return copy.deepcopy(state)

    def write(_ctx, plan):
        assert _PLAN_IO_LOCK._is_owned()
        state.clear()
        state.update(copy.deepcopy(plan))

    director._plan_read = read
    director._plan_write = write
    director._plan_seed({}, "shot_image", [{
        "id": "shot:1", "category": "shot_image", "prompt": "x"}])
    assert state["items"][0]["id"] == "shot:1"


def test_reconcile_reuses_legacy_single_asset_without_candidate_field(
        tmp_path):
    director = _director({
        "selection_mode": True, "image_content_qc": False})
    uri = tmp_path / "legacy.png"
    uri.write_bytes(b"legacy")
    plan = {"items": [{
        "id": "shot:1", "category": "shot_image", "shot_no": 1,
        "status": "done", "output_uri": str(uri),
    }]}
    writes = []
    registered = []
    director._plan_read = lambda _ctx: copy.deepcopy(plan)
    director._plan_write = lambda _ctx, value: writes.append(
        copy.deepcopy(value))
    director.assets = SimpleNamespace(latest=lambda *_args: None)
    director._shot_name = lambda _ctx, shot_no: f"shot-{shot_no}"
    director._shot_content_hash = lambda _shot: "hash"
    director._shot_image_meta = lambda *_args: {}
    director._register_shot_asset = lambda *args, **kwargs: registered.append(
        (args, kwargs))
    ctx = {
        "project": {"id": 1}, "out_root": tmp_path,
        "storyboard": {"shots": [{"shot_no": 1}]},
    }

    result = director.reconcile_completed_shot_images(ctx)

    assert result["recovered"] == 1
    assert len(registered) == 1
    assert writes == []


def test_reconcile_requires_complete_group_once_candidate_field_exists(
        tmp_path):
    director = _director({
        "selection_mode": True, "image_content_qc": False})
    uri = tmp_path / "new.png"
    uri.write_bytes(b"new")
    plan = {"items": [{
        "id": "shot:1", "category": "shot_image", "shot_no": 1,
        "status": "done", "output_uri": str(uri), "candidate_group": {},
    }]}
    writes = []
    registered = []
    director._plan_read = lambda _ctx: copy.deepcopy(plan)
    director._plan_write = lambda _ctx, value: writes.append(
        copy.deepcopy(value))
    director.assets = SimpleNamespace(latest=lambda *_args: None)
    director._shot_name = lambda _ctx, shot_no: f"shot-{shot_no}"
    director._register_shot_asset = lambda *args, **kwargs: registered.append(
        (args, kwargs))
    ctx = {
        "project": {"id": 1}, "out_root": tmp_path,
        "storyboard": {"shots": [{"shot_no": 1}]},
    }

    result = director.reconcile_completed_shot_images(ctx)

    assert result["recovered"] == 0
    assert registered == []
    assert writes[-1]["items"][0]["status"] == "pending"


def test_regenerate_all_audit_keeps_feedback_and_prompt_diff():
    director = _director({"selection_mode": True})
    director._stable_hash = lambda value: f"hash:{value}"

    audit = director._candidate_regeneration_audit(
        "旧提示词：人物站在门外", "新提示词：人物已经进入门内",
        user_feedback="空间位置错了，门外应改为门内")

    assert audit["source"] == "user_regenerate_all_candidates"
    assert audit["user_feedback"] == "空间位置错了，门外应改为门内"
    assert audit["prompt_changed"] is True
    assert "-旧提示词：人物站在门外" in audit["prompt_diff"]
    assert "+新提示词：人物已经进入门内" in audit["prompt_diff"]
