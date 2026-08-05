"""四候选选片模式：冻结合同、真并行和非阻断内容质检。"""

import copy
import struct
import threading
import zlib
from dataclasses import asdict
from pathlib import Path

# 隔离基线 bb71b58 的 director 已引用公开坐标转换名，而对应提交仍只
# 暴露私有旧名；正式共同树已有公开实现。测试引导不修改生产源文件。
from aifos import spatial_blocking

if not hasattr(spatial_blocking, "canvas_from_world"):
    spatial_blocking.canvas_from_world = spatial_blocking._canvas_from_world

from aifos.director import Director
from aifos.production.base import ProviderResult
from aifos.selection_mode import build_candidate_set_version


def _png(width=9, height=16):
    def chunk(kind, data):
        crc = zlib.crc32(kind)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", crc))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\x00" + b"\x14\x28\x3c" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class _Config:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, *keys, default=None):
        value = self.data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


class _Log:
    def __init__(self):
        self.warnings = []

    def info(self, *_args):
        pass

    def warn(self, *_args):
        self.warnings.append(_args)


class _ParallelRouter:
    def __init__(self, transient_failures=None, persistent_failures=None,
                 first_wave_size=4, qc_failures=None):
        self.review_calls = 0
        self.image_calls = 0
        self.qc_calls = 0
        self.comparison_calls = 0
        self.comparison_payloads = []
        self.active = 0
        self.max_active = 0
        self.payloads = []
        self.attempts = {}
        self.transient_failures = set(transient_failures or [])
        self.persistent_failures = set(persistent_failures or [])
        self.qc_failures = set(qc_failures or [])
        self.lock = threading.Lock()
        self.first_wave_size = first_wave_size
        self.barrier = threading.Barrier(first_wave_size)

    def review_image_prompt(self, _capability, payload, _out_dir,
                            cancel=None):
        assert cancel is None or not cancel()
        with self.lock:
            self.review_calls += 1
        payload["prompt_aifos_original"] = payload.get("prompt", "")
        payload["prompt"] = "冻结后的精准镜头提示词"
        payload["prompt_compact"] = "冻结后的精准镜头提示词"
        payload["prompt_review"] = {
            "schema": "aifos.prompt-review/v1", "status": "approved"}
        return ProviderResult(provider="review", cost=0.25)

    def call(self, capability, payload, out_dir, cancel=None):
        assert cancel is None or not cancel()
        if capability == "image_qc":
            with self.lock:
                if payload.get("candidate_comparison"):
                    self.comparison_calls += 1
                    self.comparison_payloads.append(copy.deepcopy(payload))
                else:
                    self.qc_calls += 1
            if payload.get("candidate_comparison"):
                request = payload["candidate_comparison"]
                rows = []
                for position, candidate in enumerate(
                        request["candidates"]):
                    score = 90 - position
                    rows.append({
                        "candidate_id": candidate["candidate_id"],
                        "candidate_index": candidate["candidate_index"],
                        "dimension_scores": {
                            "visible_facts": score,
                            "identity": score,
                            "spatial": score,
                            "prop_physics": score,
                            "text": score,
                            "continuity": score,
                            "composition": score,
                        },
                        "evidence": [], "fatal_issues": [],
                        "soft_issues": [], "total_score": score,
                    })
                return ProviderResult(
                    provider="fake-qc", model="fake-qc-v1", cost=0.1,
                    data={
                        "schema": "aifos.candidate-comparison-result/v1",
                        "candidate_set_token": request[
                            "candidate_set_token"],
                        "target_input_hash": request["target_input_hash"],
                        "reference_selection_hash": request[
                            "reference_selection_hash"],
                        "ranking_input_hash": request[
                            "ranking_input_hash"],
                        "candidates": rows,
                        "winner_candidate_id": rows[0]["candidate_id"],
                        "winner_reason": "第一张相对最完整",
                        "confidence": 0.8,
                    })
            try:
                candidate_index = int(
                    Path(out_dir).name.rsplit("_", 1)[-1])
            except (TypeError, ValueError):
                candidate_index = 0
            passed = candidate_index not in self.qc_failures
            return ProviderResult(
                provider="fake-qc", model="fake-qc-v1", cost=0.1,
                data={
                    "pass": passed, "visual_pass": passed,
                    "issues": [] if passed else ["候选内容明显不合格"],
                    "identity_checked": True, "identity_match": True,
                    "gender_checked": True, "gender_match": True,
                    "wardrobe_checked": True, "wardrobe_match": True,
                    "count_checked": True, "count_match": True,
                    "physical_logic_checked": True,
                    "physical_logic_match": passed,
                    "spatial_logic_checked": True,
                    "spatial_logic_match": True,
                    "input_contract_passed": True,
                })
        assert capability == "image"
        with self.lock:
            self.image_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.payloads.append(copy.deepcopy(payload))
            index = int(payload["_candidate_index"])
            attempt = self.attempts.get(index, 0) + 1
            self.attempts[index] = attempt
        try:
            if attempt == 1 and index <= self.first_wave_size:
                self.barrier.wait(timeout=3)
            if (index in self.persistent_failures
                    or (index in self.transient_failures and attempt == 1)):
                raise RuntimeError(f"candidate {index} transport failed")
            path = Path(out_dir) / "candidate.png"
            path.write_bytes(_png())
            return ProviderResult(
                provider="fake-image", model="fake-v1", cost=1.0,
                data={"prompt_optimized": payload["prompt_compact"]},
                uri=str(path))
        finally:
            with self.lock:
                self.active -= 1


def _director(defaults=None):
    director = Director.__new__(Director)
    merged_defaults = {"parallel_images": 4}
    merged_defaults.update(defaults or {})
    director.config = _Config({"defaults": merged_defaults})
    director.log = _Log()
    return director


def test_selection_mode_config_switches_are_independent_and_compatible():
    director = _director({
        "selection_mode": True,
        "image_content_qc": True,
        "video_content_qc": True,
        "shot_candidate_count": 2,
    })
    assert director._selection_mode_enabled() is True
    assert director._image_qc_enabled() is True
    assert director._video_content_qc_enabled() is True
    assert director._shot_candidate_count() == 4
    assert director.log.warnings

    # 新键存在时拥有优先权；只有缺失时才回退旧 image_qc。
    director.config = _Config({"defaults": {
        "selection_mode": False,
        "image_content_qc": False, "image_qc": True}})
    assert director._image_qc_enabled() is False
    director.config = _Config({"defaults": {
        "selection_mode": False, "image_qc": False}})
    assert director._image_qc_enabled() is False
    director.config = _Config({"defaults": {"selection_mode": False}})
    assert director._image_qc_enabled() is True
    assert director._video_content_qc_enabled() is True


def test_four_candidates_share_one_reviewed_contract_and_overlap(tmp_path):
    director = _director({
        "selection_mode": True,
        "shot_candidate_count": 4,
    })
    director.router = _ParallelRouter()
    progress_updates = []
    payload = {
        "_episode_id": "episode-1",
        "_contract_revision": 1,
        "_candidate_revision": 1,
        "item_id": "shot:7",
        "shot_no": 7,
        "prompt": "AIFOS 原始镜头提示词",
        "reference_manifest": [{
            "index": 1,
            "uri": "/tmp/reference-face.png",
            "role": "identity",
        }],
        "_candidate_progress_callback": lambda value: (
            progress_updates.append(copy.deepcopy(value))),
    }

    result = director._generate_selection_candidates_parallel(
        "image", payload, tmp_path, None, {"item_id": "shot:7"}, 4)

    assert director.router.review_calls == 1
    assert director.router.image_calls == 4
    assert director.router.qc_calls == 4
    assert director.router.comparison_calls == 1
    assert director.router.max_active >= 2
    group = result.data["candidate_group"]
    candidates = group["candidates"]
    assert group["candidate_count"] == group["expected_count"] == 4
    assert group["parallelism"] == 4
    assert group["selection_required"] is True
    assert group["candidate_set_token"].startswith("cset-v1:")
    assert group["version"]["token"] == group["candidate_set_token"]
    assert group["candidate_revision"] == 1
    assert group["same_prompt"] is True
    assert group["same_references"] is True
    assert len({row["prompt_hash"] for row in candidates}) == 1
    assert len({row["reference_hash"] for row in candidates}) == 1
    assert len({row["input_hash"] for row in candidates}) == 1
    assert {row["candidate_seed"] for row in candidates} == {None}
    assert len({row["candidate_variation_key"] for row in candidates}) == 4
    assert all(row["seed_consumed"] is False for row in candidates)
    assert all(row["reproducible"] is False for row in candidates)
    assert all(
        row["candidate_seed_semantics"]
        == "request_variation_marker_not_reproducible"
        for row in candidates)
    assert {row["candidate_set_token"] for row in candidates} == {
        group["candidate_set_token"]}
    assert len({row["candidate_id"] for row in candidates}) == 4
    assert [row["candidate_index"] for row in candidates] == [1, 2, 3, 4]
    assert len({str(Path(row["uri"]).parent) for row in candidates}) == 4
    assert all(Path(row["uri"]).exists() for row in candidates)
    assert {p["prompt_compact"] for p in director.router.payloads} == {
        "冻结后的精准镜头提示词"}

    # 底层候选组提供 AI 推荐；上层导演据此自动晋升当前版本。
    assert result.uri == ""
    assert "selected_pull" not in group
    assert "selected_uri" not in group
    assert "canonical_uri" not in group
    assert group["recommended_candidate_id"].endswith("#1")
    assert group["recommended_candidate_index"] == 1
    visible_updates = [
        update for update in progress_updates
        if update.get("candidates")]
    assert visible_updates
    assert any(len(update["candidates"]) < 4
               for update in visible_updates)
    assert len(visible_updates[-1]["candidates"]) == 4
    assert visible_updates[-1]["status"] == "qc_complete"
    assert visible_updates[-1]["live_progress"] is True
    assert visible_updates[-1]["candidate_set_token"] == \
        group["candidate_set_token"]
    assert visible_updates[-1]["frozen_prompt_hash"] == \
        group["frozen_prompt_hash"]
    assert visible_updates[-1]["frozen_reference_hash"] == \
        group["frozen_reference_hash"]
    assert visible_updates[-1]["frozen_input_hash"] == \
        group["frozen_input_hash"]
    assert not (tmp_path / "shot_007.keyframe.png").exists()
    assert Director._candidate_selection_pending(result) is True
    assert group["selection_required"] is True
    assert round(result.cost, 2) == 4.75


def test_comparison_uploads_only_eligible_candidates_and_rebases_refs(
        tmp_path):
    director = _director({"selection_mode": True, "parallel_images": 4})
    director.router = _ParallelRouter(qc_failures={1})
    identity = tmp_path / "identity.png"
    identity.write_bytes(_png())
    payload = {
        "_episode_id": "episode-1",
        "_contract_revision": 1,
        "_candidate_revision": 1,
        "item_id": "shot:eligible",
        "shot_no": 12,
        "prompt": "只比较内容质检合格候选",
        "reference_manifest": [{
            "index": 1,
            "uri": str(identity),
            "label": "主角身份锚",
            "role": "identity",
            "binding": "只锁身份",
        }],
    }

    result = director._generate_selection_candidates_parallel(
        "image", payload, tmp_path, None,
        {"item_id": "shot:eligible"}, 4)

    group = result.data["candidate_group"]
    by_index = {
        row["candidate_index"]: row for row in group["candidates"]}
    assert by_index[1]["passed"] is False
    assert director.router.comparison_calls == 1
    comparison = director.router.comparison_payloads[0]
    requested_ids = [
        row["candidate_id"]
        for row in comparison["candidate_comparison"]["candidates"]]
    assert requested_ids == [
        by_index[index]["candidate_id"] for index in (2, 3, 4)]
    uploaded_candidate_uris = [comparison["image_uri"], *[
        row["uri"] for row in comparison["reference_manifest"]
        if row.get("role") == "candidate_comparison"]]
    assert uploaded_candidate_uris == [
        by_index[index]["uri"] for index in (2, 3, 4)]
    assert by_index[1]["uri"] not in uploaded_candidate_uris
    assert [
        row["index"] for row in comparison["reference_manifest"]
    ] == [2, 3, 4]
    reference_binding = comparison[
        "candidate_comparison"]["reference_bindings"][0]
    assert reference_binding["image_index"] == 4
    assert reference_binding["uri"] == str(identity)


def test_four_candidate_slots_respect_provider_parallel_limit(tmp_path):
    director = _director({
        "selection_mode": True,
        "shot_candidate_count": 4,
        "parallel_images": 3,
    })
    director.router = _ParallelRouter(first_wave_size=3)
    payload = {
        "_episode_id": "episode-1",
        "_contract_revision": 1,
        "_candidate_revision": 1,
        "item_id": "shot:9",
        "shot_no": 9,
        "prompt": "三路额度下仍须填满四个候选槽",
        "reference_manifest": [],
    }

    result = director._generate_selection_candidates_parallel(
        "image", payload, tmp_path, None, {"item_id": "shot:9"}, 4)

    assert director.router.image_calls == 4
    assert director.router.max_active == 3
    assert result.data["candidate_group"]["candidate_count"] == 4
    assert result.data["candidate_group"]["parallelism"] == 3
    assert result.data["candidate_group"]["complete"] is True


def test_interrupted_candidate_progress_reuses_finished_slots(tmp_path):
    director = _director({"selection_mode": True, "parallel_images": 4})
    director.router = _ParallelRouter(first_wave_size=2)
    payload = {
        "_episode_id": "episode-1",
        "_contract_revision": 1,
        "_candidate_revision": 1,
        "item_id": "shot:10",
        "shot_no": 10,
        "prompt": "同一冻结镜头合同",
        "reference_manifest": [],
    }
    first = director._generate_selection_candidates_parallel(
        "image", payload, tmp_path, None, {"item_id": "shot:10"}, 4)
    interrupted = copy.deepcopy(first.data["candidate_group"])
    interrupted["schema"] = "aifos.shot-candidate-progress/v1"
    interrupted["live_progress"] = True
    interrupted["complete"] = False
    interrupted["candidates"] = interrupted["candidates"][:2]

    director.router = _ParallelRouter(first_wave_size=2)
    resumed_payload = copy.deepcopy(payload)
    resumed_payload["_candidate_set_id"] = interrupted[
        "candidate_set_id"]
    resumed_payload["_resume_candidate_group"] = interrupted
    resumed = director._generate_selection_candidates_parallel(
        "image", resumed_payload, tmp_path, None,
        {"item_id": "shot:10"}, 4)

    assert director.router.image_calls == 2
    group = resumed.data["candidate_group"]
    assert group["candidate_count"] == 4
    assert [row["candidate_index"] for row in group["candidates"]] == [
        1, 2, 3, 4]
    assert group["candidates"][0]["uri"] == \
        interrupted["candidates"][0]["uri"]
    assert group["candidates"][1]["uri"] == \
        interrupted["candidates"][1]["uri"]


def test_candidate_progress_persists_exact_round_resume_state(tmp_path):
    director = _director({"selection_mode": True, "parallel_images": 4})
    director.router = _ParallelRouter()
    updates = []
    resume_payload = {
        "_episode_id": "episode-1",
        "_candidate_generation_round": 3,
        "_candidate_revision": 3,
        "_contract_revision": 3,
        "shot_no": 11,
        "prompt": "第3轮替换后的精准静态合同",
        "prompt_compact": "第3轮替换后的精准静态合同",
        "reference_manifest": [{
            "index": 1, "uri": "/tmp/round-3-face.png",
            "role": "identity",
        }],
    }
    history = [
        {"generation_round": 1, "candidate_count": 4},
        {"generation_round": 2, "candidate_count": 4},
    ]
    best = {
        "schema": "aifos.shot-best-provisional/v1",
        "uri": "/tmp/round-2-best.png",
        "ranking_score": 88.0,
        "data": {}, "qc": {}, "fallbacks": [],
    }
    resume_state = {
        "schema": director.CANDIDATE_RESUME_SCHEMA,
        "generation_round": 3,
        "max_candidate_rounds": 10,
        "round_history": history,
        "best_provisional": best,
        "resume_payload": resume_payload,
    }
    payload = copy.deepcopy(resume_payload)
    payload["_candidate_resume_state"] = copy.deepcopy(resume_state)
    payload["_candidate_progress_callback"] = lambda value: (
        updates.append(copy.deepcopy(value)))

    director._generate_selection_candidates_parallel(
        "image", payload, tmp_path, None, {"item_id": "shot:11"}, 4)

    assert updates
    # The old implementation first overwrote the plan with a schema-less
    # 0/4 row.  Every new progress row is now immediately resumable.
    assert updates[0]["schema"] == "aifos.shot-candidate-progress/v1"
    assert all(
        row["resume_state"]["generation_round"] == 3
        and row["resume_state"]["round_history"] == history
        and row["resume_state"]["best_provisional"] == best
        for row in updates)
    frozen_resume = updates[-1]["resume_state"]["resume_payload"]
    assert frozen_resume["prompt"] == "冻结后的精准镜头提示词"
    assert frozen_resume["prompt_compact"] == "冻结后的精准镜头提示词"
    assert frozen_resume["_prompt_review_frozen_input_hash"]
    assert updates[-1]["status"] == "qc_complete"

    # Simulate a service restart after all four files landed but before the
    # outer repair loop returned.  The frozen reviewed contract must make all
    # four slots reusable with zero new image calls and zero re-review calls.
    live_group = updates[-1]
    restored, resumed = director._candidate_payload_from_resume_progress(
        {"_episode_id": "episode-1", "shot_no": 11,
         "prompt": "进程重启后重新编译出的原始合同"},
        live_group, max_rounds=10)
    assert resumed is True
    restored["_candidate_set_id"] = live_group["candidate_set_id"]
    restored["_candidate_revision"] = live_group["candidate_revision"]
    restored["_contract_revision"] = live_group["contract_revision"]
    restored["_resume_candidate_group"] = copy.deepcopy(live_group)
    director.router = _ParallelRouter()

    resumed_result = director._generate_selection_candidates_parallel(
        "image", restored, tmp_path, None, {"item_id": "shot:11"}, 4)

    assert director.router.review_calls == 0
    assert director.router.image_calls == 0
    assert resumed_result.data["candidate_group"]["candidate_count"] == 4


def test_resume_progress_restores_repaired_contract_history_and_best():
    director = _director()
    history = [
        {"generation_round": 1, "candidate_count": 4},
        {"generation_round": 2, "candidate_count": 4},
        {"generation_round": 3, "candidate_count": 4},
    ]
    best = {
        "schema": "aifos.shot-best-provisional/v1",
        "uri": "/tmp/round-2-best.png",
        "provider": "test", "model": "test-v1",
        "fallbacks": [], "qc": {"passed": False},
        "data": {}, "ranking_score": 91.0,
    }
    repaired = {
        "_episode_id": "episode-1", "shot_no": 12,
        "prompt": "第4轮只保留唯一可拍终态",
        "prompt_compact": "第4轮只保留唯一可拍终态",
        "reference_manifest": [{
            "index": 1, "uri": "/tmp/correct-face.png",
            "role": "identity",
        }],
        "_candidate_generation_round": 4,
        "_candidate_revision": 4,
        "_contract_revision": 4,
    }
    live_group = {
        "resume_state": {
            "schema": director.CANDIDATE_RESUME_SCHEMA,
            "generation_round": 4,
            "max_candidate_rounds": 10,
            "round_history": history,
            "best_provisional": best,
            "resume_payload": repaired,
        },
    }

    restored, resumed = director._candidate_payload_from_resume_progress(
        {"_episode_id": "episode-1", "shot_no": 12,
         "prompt": "原始第1轮合同"},
        live_group, max_rounds=10)

    assert resumed is True
    assert restored["prompt"] == "第4轮只保留唯一可拍终态"
    assert restored["reference_manifest"] == repaired["reference_manifest"]
    assert restored["_candidate_generation_round"] == 4
    assert restored["_candidate_round_history"] == history
    assert restored["_candidate_best_provisional"] == best


def test_only_explicit_manual_or_ai_selection_unlocks_candidate_group():
    version = build_candidate_set_version(
        episode_id="episode-1", shot_no=7, contract_revision=1,
        candidate_revision=1, prompt="镜头", reference_manifest=[])
    group = {
        "schema": "aifos.shot-candidate-group/v1",
        "selection_required": True,
        "version": asdict(version),
        "candidates": [{
            "candidate_index": 1,
            "candidate_set_token": version.token,
            "uri": "/tmp/chosen.png",
        }],
    }
    result = ProviderResult(
        provider="test", cost=0, data={"candidate_group": group})
    assert Director._candidate_selection_pending(result) is True

    group["selection"] = {
        "token": version.token,
        "candidate_index": 1,
        "selected_uri": "/tmp/chosen.png",
        "source": "system",
    }
    assert Director._candidate_selection_pending(result) is True
    group["selection"]["source"] = "manual"
    assert Director._candidate_selection_pending(result) is False
    group["selection"]["source"] = "ai"
    assert Director._candidate_selection_pending(result) is False
    stale = build_candidate_set_version(
        episode_id="episode-1", shot_no=7, contract_revision=1,
        candidate_revision=2, prompt="镜头", reference_manifest=[])
    group["selection"]["token"] = stale.token
    assert Director._candidate_selection_pending(result) is True


def test_one_candidate_technical_failure_is_retried_and_fills_slot(tmp_path):
    director = _director({"selection_mode": True})
    director.router = _ParallelRouter(transient_failures={2})
    result = director._generate_selection_candidates_parallel(
        "image", {
            "_episode_id": "episode-1",
            "_contract_revision": 1,
            "_candidate_revision": 1,
            "item_id": "shot:8", "shot_no": 8, "prompt": "冻结镜头",
            "reference_manifest": [],
        }, tmp_path, None, {"item_id": "shot:8"}, 4)

    group = result.data["candidate_group"]
    assert director.router.image_calls == 5
    assert group["complete"] is True
    assert group["technical_incomplete"] is False
    assert group["candidate_errors"] == []
    assert group["candidate_count"] == 4
    retried = next(
        row for row in group["candidates"] if row["candidate_index"] == 2)
    assert retried["technical_attempts"] == 2
    assert Director._candidate_group_technical_incomplete(result) is False


def test_unfilled_candidate_slot_promotes_best_usable_without_mobile_gate(tmp_path):
    director = _director({"selection_mode": True})
    director.router = _ParallelRouter(persistent_failures={3})
    result = director._generate_selection_candidates_parallel(
        "image", {
            "_episode_id": "episode-1",
            "_contract_revision": 1,
            "_candidate_revision": 1,
            "item_id": "shot:9", "shot_no": 9, "prompt": "冻结镜头",
            "reference_manifest": [],
        }, tmp_path, None, {"item_id": "shot:9"}, 4)

    group = result.data["candidate_group"]
    success_uris = [row["uri"] for row in group["candidates"]]
    assert director.router.image_calls == 5
    assert group["complete"] is True
    assert group["slot_complete"] is False
    assert group["missing_slot_count"] == 1
    assert group["technical_incomplete"] is False
    assert group["candidate_count"] == 3
    assert [row["candidate_index"] for row in group["candidate_errors"]] == [3]
    assert len(success_uris) == 3
    assert result.data["candidate_uris"] == success_uris
    assert all(Path(uri).exists() for uri in success_uris)
    assert Director._candidate_group_technical_incomplete(result) is False
    assert Director._candidate_selection_pending(result) is True
    assert group["selection_required"] is True
    assert result.uri == ""
    promoted = director._ai_promote_generated_candidate_group(result)
    promoted_group = promoted.data["candidate_group"]
    assert promoted_group["selection_required"] is False
    assert promoted_group["selection"]["source"] == "ai"
    assert promoted.uri in success_uris
    assert Director._candidate_selection_pending(promoted) is False


def test_explicitly_disabled_image_content_qc_never_blocks_or_auto_repairs():
    director = _director({
        "selection_mode": True, "image_content_qc": False})
    failed = ProviderResult(provider="test", cost=0)
    failed.qc = {
        "passed": False,
        "hard_failure": True,
        "issues": ["画面内容不符合设定"],
    }
    assert director._critical_qc_error(failed) == ""
    report, repaired = director._auto_repair_qc_item(
        {}, {}, {}, {"id": "shot:1"}, failed.qc)
    assert repaired == 0
    assert report["advisory_only"] is True
    assert report["blocking"] is False
    assert report["awaiting_human"] is False

    director.config = _Config({"defaults": {
        "selection_mode": False, "image_content_qc": True}})
    assert "画面内容不符合设定" in director._critical_qc_error(failed)


def test_disabled_image_qc_draws_one_four_candidate_round_without_qc_calls(
        tmp_path):
    director = _director({
        "selection_mode": True,
        "image_content_qc": False,
        "shot_candidate_count": 4,
        "shot_max_candidate_rounds": 10,
    })
    director.router = _ParallelRouter()

    result = director._generate_shot_candidate_group(
        "image", {
            "_episode_id": "episode-qc-off",
            "_contract_revision": 1,
            "_candidate_revision": 1,
            "shot_no": 2,
            "prompt": "人物站在门内，保持真实空间关系",
            "reference_manifest": [],
            "aspect": "9:16",
        }, tmp_path, None, {"count": 1, "location": "门内"})

    assert director.router.image_calls == 4
    assert director.router.qc_calls == 0
    assert director.router.comparison_calls == 0
    assert result.uri
    group = result.data["candidate_group"]
    assert group["generation_round"] == 1
    assert group["round_status"] == "qc_unavailable"
    assert len(group["candidate_round_history"]) == 1
    assert all(row["qc_disabled"] for row in group["candidates"])


def test_disabled_video_content_qc_keeps_only_technical_failures():
    director = _director({"selection_mode": True})
    ctx = {"storyboard": {"shots": [{"shot_no": 1}, {"shot_no": 2}]}}
    observations_only = director._build_technical_video_qc_report(ctx, {
        "passed": False,
        "issues": [{
            "check": "content", "shot_no": 1, "severity": "error",
            "rerunnable": True, "message": "动作表现不够精彩",
        }],
    })
    assert observations_only["passed"] is True
    assert observations_only["awaiting_human"] is False
    assert observations_only["failed_shots"] == []
    assert len(observations_only["content_observations"]) == 1

    technical = director._build_technical_video_qc_report(ctx, {
        "passed": False,
        "issues": [{
            "check": "video", "shot_no": 2, "severity": "error",
            "rerunnable": True, "message": "视频文件缺失",
        }],
    })
    assert technical["passed"] is False
    assert technical["failed_shots"] == [2]
    assert technical["awaiting_human"] is False
    failed_shot = next(
        item for item in technical["shots"] if item["shot_no"] == 2)
    assert failed_shot["status"] == "failed"
    assert failed_shot["decision"]["action"] == "direct_video_retry"


def test_plan_seed_preserves_candidate_group_for_same_shot_contract():
    director = _director({"selection_mode": True})
    token = "cset-v1:current"
    previous = {
        "id": "shot:5", "category": "shot_image",
        "content_hash": "same", "status": "awaiting_selection",
        "candidate_group": {"candidate_set_token": token},
        "candidate_uris": ["one", "two", "three", "four"],
        "candidate_count": 4,
        "candidate_set_token": token,
        "candidate_revision": 3,
        "selection_required": True,
        "selection": {"candidate_set_token": token, "source": "manual"},
    }
    stored = {"items": [copy.deepcopy(previous)]}
    director._plan_read = lambda _ctx: copy.deepcopy(stored)

    def write(_ctx, plan):
        stored.clear()
        stored.update(copy.deepcopy(plan))

    director._plan_write = write
    director._plan_seed({}, "shot_image", [{
        "id": "shot:5", "category": "shot_image",
        "content_hash": "same", "prompt": "current",
    }])

    item = stored["items"][0]
    assert item["status"] == "awaiting_selection"
    for key in (
            "candidate_group", "candidate_uris", "candidate_count",
            "candidate_set_token", "candidate_revision",
            "selection_required", "selection"):
        assert item[key] == previous[key]
