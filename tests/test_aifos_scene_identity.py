from aifos.scene_identity import (
    annotate_scene_families,
    canonical_scene_location,
    scene_family_groups,
)
from aifos.workflow import build_continuity_bible


def _bedroom_script():
    return {"scenes": [
        {"scene_no": 1, "location": "虞家别墅·虞寻欢卧室"},
        {"scene_no": 2, "location": "虞家别墅·虞寻欢卧室床侧"},
        {"scene_no": 3, "location": "虞家别墅·虞寻欢卧室至盥洗室门内"},
    ]}


def test_narrow_bedroom_zones_share_one_physical_scene():
    script = _bedroom_script()
    assert canonical_scene_location(
        script, "虞家别墅·虞寻欢卧室床侧") == "虞家别墅·虞寻欢卧室"
    assert scene_family_groups(script) == {
        "虞家别墅·虞寻欢卧室": [
            "虞家别墅·虞寻欢卧室",
            "虞家别墅·虞寻欢卧室床侧",
            "虞家别墅·虞寻欢卧室至盥洗室门内",
        ]
    }


def test_similar_prefix_does_not_merge_unrelated_places():
    script = {"scenes": [
        {"location": "明代皇宫"},
        {"location": "明代皇宫东市"},
    ]}
    assert canonical_scene_location(script, "明代皇宫东市") == "明代皇宫东市"


def test_explicit_physical_scene_id_beats_legacy_inference():
    script = {"scenes": [{
        "location": "酒店房间走廊",
        "physical_scene_id": "酒店公共走廊A",
    }]}
    assert canonical_scene_location(script, "酒店房间走廊") == "酒店公共走廊A"


def test_annotation_is_idempotent_and_preserves_visible_zone():
    script = _bedroom_script()
    assert annotate_scene_families(script) is True
    assert script["scenes"][1]["location"] == "虞家别墅·虞寻欢卧室床侧"
    assert script["scenes"][1]["scene_zone"] == "虞家别墅·虞寻欢卧室床侧"
    assert script["scenes"][1]["base_location"] == "虞家别墅·虞寻欢卧室"
    assert annotate_scene_families(script) is False


def test_continuity_bible_has_one_set_with_multiple_staging_zones():
    script = _bedroom_script()
    continuity = build_continuity_bible(
        {"title": "游戏入侵"}, script,
        {"rules": {}, "standard_fingerprint": "std",
         "burn_subtitles": False})
    assert len(continuity["scenes"]) == 1
    scene = continuity["scenes"][0]
    assert scene["physical_scene_id"] == "虞家别墅·虞寻欢卧室"
    assert scene["zones"] == [
        "虞家别墅·虞寻欢卧室",
        "虞家别墅·虞寻欢卧室床侧",
        "虞家别墅·虞寻欢卧室至盥洗室门内",
    ]
