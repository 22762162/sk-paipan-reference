"""Deterministic, strict timeline JSON exchange without editor dependencies."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aifos.timeline_exchange import (
    HASH_ALGORITHM,
    TIMELINE_SCHEMA,
    TIMELINE_VERSION,
    TimelineValidationError,
    build_timeline,
    export_timeline_json,
    load_timeline_json,
    validate_timeline,
)


class TimelineExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video1 = self.root / "shot-001.mp4"
        self.video2 = self.root / "shot-002.mp4"
        self.voice2 = self.root / "shot-002.wav"
        self.video1.write_bytes(b"video-one")
        self.video2.write_bytes(b"video-two")
        self.voice2.write_bytes(b"voice-two")
        self.storyboard = {
            "version": "storyboard-v7",
            "shots": [
                {
                    "shot_no": 1,
                    "shot_id": "opening",
                    "unit_id": "U01",
                    "duration": 2.5,
                    "edit_transition": {
                        "type": "dissolve", "duration": 0.5},
                },
                {
                    "shot_no": 2,
                    "unit_id": "U02",
                    "duration": 3.0,
                    "audio_refs": [{"uri": str(self.voice2)}],
                },
            ],
        }
        self.outputs = [
            {
                "shot_no": 1, "uri": str(self.video1),
                "duration": 2.5, "provider": "dreamina",
                "model": "seedance-2.0-fast", "audio_in_video": True,
            },
            {
                "shot_no": 2, "uri": str(self.video2),
                "actual_duration": 2.75, "provider": "dreamina",
                "model": "seedance-2.5",
            },
        ]

    def tearDown(self):
        self.temp.cleanup()

    def build(self, **kwargs):
        return build_timeline(
            self.storyboard, self.outputs, name="第1集", **kwargs)

    def assert_invalid(self, callable_, text):
        with self.assertRaisesRegex(TimelineValidationError, text):
            callable_()

    def test_builds_deterministic_otio_inspired_timeline(self):
        first = self.build()
        second = self.build()
        reversed_completion = build_timeline(
            self.storyboard, list(reversed(self.outputs)), name="第1集")

        self.assertEqual(first, second)
        self.assertEqual(first, reversed_completion)
        self.assertEqual(first["schema"], TIMELINE_SCHEMA)
        self.assertEqual(first["version"], TIMELINE_VERSION)
        self.assertEqual(first["source_shot_ids"], ["opening", "shot:002"])
        self.assertEqual(first["source_shot_numbers"], [1, 2])
        self.assertEqual(first["duration"], 5.25)
        self.assertEqual(
            [(clip["start"], clip["duration"], clip["end"])
             for clip in first["clips"]],
            [(0.0, 2.5, 2.5), (2.5, 2.75, 5.25)],
        )
        self.assertEqual(first["clips"][0]["planned_duration"], 2.5)
        self.assertEqual(first["clips"][1]["planned_duration"], 3.0)
        self.assertEqual(first["clips"][0]["transition"], {
            "type": "dissolve", "duration": 0.5})
        self.assertEqual(first["clips"][1]["transition"], {
            "type": "cut", "duration": 0.0})
        self.assertEqual(first["tracks"][0]["clip_ids"], [
            "clip:opening", "clip:shot:002"])
        self.assertEqual(first["tracks"][1]["clip_ids"], [
            "clip:opening", "clip:shot:002"])
        audit = validate_timeline(first)
        self.assertEqual(audit["clips"], 2)
        self.assertEqual(audit["duration"], 5.25)

    def test_local_media_uses_content_hash_and_embedded_audio_reuses_it(self):
        timeline = self.build()
        first = timeline["clips"][0]
        expected = hashlib.sha256(b"video-one").hexdigest()

        self.assertEqual(first["video_ref"]["source_hash"], {
            "algorithm": HASH_ALGORITHM,
            "value": expected,
            "basis": "content",
        })
        self.assertEqual(
            first["audio_refs"][0]["source_hash"],
            first["video_ref"]["source_hash"],
        )
        self.assertTrue(first["audio_refs"][0]["embedded"])
        second_audio = timeline["clips"][1]["audio_refs"][0]
        self.assertEqual(
            second_audio["source_hash"]["value"],
            hashlib.sha256(b"voice-two").hexdigest(),
        )

    def test_declared_hash_wins_and_remote_reference_gets_stable_hash(self):
        declared = "a" * 64
        outputs = copy.deepcopy(self.outputs)
        outputs[0]["sha256"] = declared
        outputs[1]["uri"] = "https://cdn.example/shot-002.mp4"
        first = build_timeline(self.storyboard, outputs)
        second = build_timeline(self.storyboard, outputs)

        self.assertEqual(first["clips"][0]["video_ref"]["source_hash"], {
            "algorithm": "sha256", "value": declared, "basis": "declared"})
        remote_hash = first["clips"][1]["video_ref"]["source_hash"]
        self.assertEqual(remote_hash["basis"], "reference")
        self.assertEqual(
            remote_hash, second["clips"][1]["video_ref"]["source_hash"])

    def test_export_is_stable_utf8_json_and_load_revalidates(self):
        timeline = self.build()
        first_path = export_timeline_json(
            timeline, self.root / "one" / "timeline.json")
        second_path = export_timeline_json(
            timeline, self.root / "two" / "timeline.json")

        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
        self.assertEqual(load_timeline_json(first_path), timeline)
        raw = json.loads(first_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["timeline_hash"], timeline["timeline_hash"])

    def test_missing_or_extra_video_output_is_rejected(self):
        self.assert_invalid(
            lambda: build_timeline(self.storyboard, self.outputs[:1]),
            "丢镜:2",
        )
        extra = self.outputs + [{
            "shot_no": 3, "uri": str(self.root / "shot-003.mp4"),
            "duration": 1,
        }]
        self.assert_invalid(
            lambda: build_timeline(self.storyboard, extra),
            "storyboard 外镜头:3",
        )

    def test_duplicate_storyboard_or_output_identity_is_rejected(self):
        duplicated_storyboard = copy.deepcopy(self.storyboard)
        duplicated_storyboard["shots"][1]["shot_no"] = 1
        self.assert_invalid(
            lambda: build_timeline(duplicated_storyboard, self.outputs),
            "镜头号重复:1",
        )
        duplicated_outputs = self.outputs + [copy.deepcopy(self.outputs[0])]
        self.assert_invalid(
            lambda: build_timeline(self.storyboard, duplicated_outputs),
            "视频输出镜头号重复:1",
        )
        duplicate_ids = copy.deepcopy(self.storyboard)
        duplicate_ids["shots"][1]["shot_id"] = "opening"
        self.assert_invalid(
            lambda: build_timeline(duplicate_ids, self.outputs),
            "shot_id 重复:opening",
        )

    def test_zero_negative_nan_duration_or_negative_start_is_rejected(self):
        for bad in (0, -1, float("nan")):
            storyboard = copy.deepcopy(self.storyboard)
            storyboard["shots"][0]["duration"] = bad
            self.assert_invalid(
                lambda storyboard=storyboard: build_timeline(
                    storyboard, self.outputs),
                "计划时长",
            )
        outputs = copy.deepcopy(self.outputs)
        outputs[0]["actual_duration"] = -0.1
        self.assert_invalid(
            lambda: build_timeline(self.storyboard, outputs),
            "视频时长",
        )
        outputs = copy.deepcopy(self.outputs)
        outputs[0]["timeline_start"] = -1
        self.assert_invalid(
            lambda: build_timeline(self.storyboard, outputs),
            "timeline_start",
        )

    def test_explicit_overlap_is_rejected_but_gap_is_valid(self):
        overlapping = copy.deepcopy(self.outputs)
        overlapping[0]["timeline_start"] = 0
        overlapping[1]["timeline_start"] = 2
        self.assert_invalid(
            lambda: build_timeline(self.storyboard, overlapping),
            "时间线重叠",
        )
        gapped = copy.deepcopy(self.outputs)
        gapped[1]["timeline_start"] = 4
        timeline = build_timeline(self.storyboard, gapped)
        self.assertEqual(timeline["clips"][1]["start"], 4.0)
        self.assertEqual(timeline["duration"], 6.75)

    def test_invalid_transition_is_rejected(self):
        storyboard = copy.deepcopy(self.storyboard)
        storyboard["shots"][0]["edit_transition"] = {
            "type": "cut", "duration": 0.2}
        self.assert_invalid(
            lambda: build_timeline(storyboard, self.outputs),
            "硬切时长必须为0",
        )
        storyboard = copy.deepcopy(self.storyboard)
        storyboard["shots"][0]["edit_transition"] = {
            "type": "dissolve", "duration": 2.6}
        self.assert_invalid(
            lambda: build_timeline(storyboard, self.outputs),
            "转场时长超过镜头时长",
        )

    def test_validation_detects_overlap_missing_clip_and_track_drift(self):
        timeline = self.build()
        overlap = copy.deepcopy(timeline)
        overlap["clips"][1]["start"] = 1.0
        overlap["clips"][1]["end"] = 3.75
        self.assert_invalid(
            lambda: validate_timeline(overlap, verify_hash=False),
            "时间线重叠",
        )
        missing = copy.deepcopy(timeline)
        missing["clips"].pop()
        self.assert_invalid(
            lambda: validate_timeline(missing, verify_hash=False),
            "镜头集合不完整",
        )
        track_drift = copy.deepcopy(timeline)
        track_drift["tracks"][0]["clip_ids"].reverse()
        self.assert_invalid(
            lambda: validate_timeline(track_drift, verify_hash=False),
            "视频轨没有按顺序覆盖全部镜头",
        )

    def test_timeline_hash_detects_any_post_build_mutation(self):
        timeline = self.build()
        timeline["clips"][0]["video_ref"]["uri"] = "/tampered.mp4"
        self.assert_invalid(
            lambda: validate_timeline(timeline),
            "timeline_hash 不匹配",
        )

    def test_expected_shot_ids_are_strictly_checked(self):
        timeline = self.build()
        validate_timeline(
            timeline, expected_shot_ids=["opening", "shot:002"])
        self.assert_invalid(
            lambda: validate_timeline(
                timeline, expected_shot_ids=["opening", "missing"]),
            "预期镜头集合不一致",
        )


if __name__ == "__main__":
    unittest.main()
