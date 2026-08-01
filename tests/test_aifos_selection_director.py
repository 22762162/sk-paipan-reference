"""四候选选片模式：冻结合同、真并行和非阻断内容质检。"""

import copy
import threading
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
                 first_wave_size=4):
        self.review_calls = 0
        self.image_calls = 0
        self.active = 0
        self.max_active = 0
        self.payloads = []
        self.attempts = {}
        self.transient_failures = set(transient_failures or [])
        self.persistent_failures = set(persistent_failures or [])
        self.lock = threading.Lock()
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
        assert capability == "image"
        assert cancel is None or not cancel()
        with self.lock:
            self.image_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.payloads.append(copy.deepcopy(payload))
            index = int(payload["_candidate_index"])
            attempt = self.attempts.get(index, 0) + 1
            self.attempts[index] = attempt
        try:
            if attempt == 1:
                self.barrier.wait(timeout=3)
            if (index in self.persistent_failures
                    or (index in self.transient_failures and attempt == 1)):
                raise RuntimeError(f"candidate {index} transport failed")
            path = Path(out_dir) / "candidate.png"
            path.write_bytes(
                f"candidate-{index}".encode())
            return ProviderResult(
                provider="fake-image", model="fake-v1", cost=1.0,
                data={"prompt_optimized": payload["prompt_compact"]},
                uri=str(path))
        finally:
            with self.lock:
                self.active -= 1


def _director(defaults=None):
    director = Director.__new__(Director)
    director.config = _Config({"defaults": defaults or {}})
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
    assert director._image_qc_enabled() is False
    assert director._video_content_qc_enabled() is False
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
    }

    result = director._generate_selection_candidates_parallel(
        "image", payload, tmp_path, None, {"item_id": "shot:7"}, 4)

    assert director.router.review_calls == 1
    assert director.router.image_calls == 4
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

    # 候选完成不等于选片完成，不能偷偷生成 canonical 或 selected 字段。
    assert result.uri == ""
    assert "selected_pull" not in group
    assert "selected_uri" not in group
    assert "canonical_uri" not in group
    assert group["recommended_candidate_id"] == ""
    assert not (tmp_path / "shot_007.keyframe.png").exists()
    assert Director._candidate_selection_pending(result) is True
    assert result.cost == 4.25


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


def test_unfilled_candidate_slot_keeps_successes_for_resume(tmp_path):
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
    assert group["complete"] is False
    assert group["technical_incomplete"] is True
    assert group["candidate_count"] == 3
    assert [row["candidate_index"] for row in group["candidate_errors"]] == [3]
    assert len(success_uris) == 3
    assert result.data["candidate_uris"] == success_uris
    assert all(Path(uri).exists() for uri in success_uris)
    assert Director._candidate_group_technical_incomplete(result) is True
    assert Director._candidate_selection_pending(result) is True
    assert result.uri == ""

    # 续产沿用同一不可变版本，只补失败的第3槽，不重烧成功的三张。
    director.router = _ParallelRouter(first_wave_size=1)
    resumed = director._generate_selection_candidates_parallel(
        "image", {
            "_episode_id": "episode-1",
            "_contract_revision": group["contract_revision"],
            "_candidate_revision": group["candidate_revision"],
            "_candidate_set_id": group["candidate_set_id"],
            "_resume_candidate_group": copy.deepcopy(group),
            "item_id": "shot:9", "shot_no": 9, "prompt": "冻结镜头",
            "reference_manifest": [],
        }, tmp_path, None, {"item_id": "shot:9"}, 4)
    resumed_group = resumed.data["candidate_group"]
    assert director.router.image_calls == 1
    assert resumed_group["candidate_count"] == 4
    assert resumed_group["candidate_set_token"] == group[
        "candidate_set_token"]
    assert {
        row["candidate_index"]: row["uri"]
        for row in resumed_group["candidates"] if row["candidate_index"] != 3
    } == {
        row["candidate_index"]: row["uri"]
        for row in group["candidates"]
    }


def test_disabled_image_content_qc_never_blocks_or_auto_repairs():
    director = _director({"selection_mode": True})
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
