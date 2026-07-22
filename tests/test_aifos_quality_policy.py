"""按下游错误影响范围自动判级，并保留人工覆盖。"""

import pytest

from aifos.quality_policy import (
    formal_reference_allowed,
    normalize_quality_policy,
    recommend_asset_quality,
    recommend_shot_image_quality,
    resolve_image_quality,
    resolve_video_quality,
    set_policy_choices,
)


def _shot(**updates):
    shot = {
        "shot_no": 1,
        "characters": ["林昭"],
        "character_count": 1,
        "camera": "中景固定",
        "description": "林昭走进办公室",
        "readable_text": {"required": False},
        "five_dimensions": {"camera_design": {"shot_scale": "中景"}},
        "performance": {},
    }
    shot.update(updates)
    return shot


def test_formal_shot_defaults_medium_and_critical_risks_upgrade_high():
    assert recommend_shot_image_quality(_shot())["level"] == "medium"
    assert recommend_shot_image_quality(_shot(
        readable_text={"required": True}))["level"] == "high"
    assert recommend_shot_image_quality(_shot(
        characters=["A", "B", "C"], character_count=3))["level"] == "high"
    assert recommend_shot_image_quality(_shot(camera="面部大特写"))["level"] == "high"
    assert recommend_shot_image_quality(_shot(
        description="她崩溃哭泣"))["level"] == "high"
    assert recommend_shot_image_quality(
        _shot(), continuity_anchor=True)["level"] == "high"


def test_mother_assets_high_drafts_low_and_manual_override_wins():
    policy = normalize_quality_policy()
    assert recommend_asset_quality("character_candidate")["level"] == "high"
    draft = recommend_asset_quality("shot_candidate")
    assert draft["level"] == "low"
    decision = resolve_image_quality(
        draft, policy, "shot-candidate:1", explicit_override="medium")
    assert decision["level"] == "medium"
    assert decision["recommended"] == "low"
    assert decision["source"] == "manual"


@pytest.mark.parametrize(("choice", "level", "resolution"), [
    ("auto", "medium", "720p"),
    ("low", "low", "480p"),
    ("medium", "medium", "720p"),
    ("high", "high", "1080p"),
])
def test_seedance_three_quality_levels(choice, level, resolution):
    policy = set_policy_choices(None, video_default=choice)
    decision = resolve_video_quality(policy, shot_no=3)
    assert decision["level"] == level
    assert decision["resolution"] == resolution


def test_low_quality_is_never_a_formal_video_reference():
    assert formal_reference_allowed("low") is False
    assert formal_reference_allowed("medium") is True
    assert formal_reference_allowed("high") is True

