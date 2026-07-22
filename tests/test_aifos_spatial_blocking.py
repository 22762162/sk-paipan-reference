from pathlib import Path

from aifos.spatial_blocking import (build_spatial_plan, render_scene_svg,
                                    shot_blocking, validate_spatial_plan,
                                    write_spatial_svgs)


def _shot(no, characters, movement="固定", action="站在原位"):
    states = {
        name: {
            "position": ["画面左1/3", "画面中心", "画面右1/3"][index % 3],
            "direction": "面向对手，视线不越轴",
            "pose": action,
        }
        for index, name in enumerate(characters)
    }
    return {
        "shot_no": no, "scene_no": 1, "unit_id": f"U{no:02d}",
        "characters": characters, "character_count": len(characters),
        "description": action, "prompt": action, "camera": f"中景{movement}",
        "start_state": states, "end_state": states,
        "five_dimensions": {"camera_design": {
            "lens": "35mm", "movement": movement,
            "camera_position": "正面", "axis_offset_degrees": -30,
        }},
    }


def test_group_scene_builds_routes_camera_and_continuity(tmp_path):
    cast = ["林昭", "小狐", "沈砚"]
    script = {"scenes": [{"scene_no": 1, "location": "旧仓库"}]}
    continuity = {"characters": [{"name": name} for name in cast],
                  "scenes": [{"name": "旧仓库"}]}
    storyboard = {"shots": [
        _shot(1, cast, "跟", "三人从门口走向桌边"),
        _shot(2, cast, "固定", "三人停下对峙"),
    ]}
    plan = build_spatial_plan(script, storyboard, continuity)

    assert plan["schema"] == "aifos.spatial-blocking/v1"
    assert plan["summary"] == {
        "scenes": 1, "required_scenes": 1, "shots": 2, "actors": 3}
    assert plan["validation"]["passed"]
    scene = plan["scenes"][0]
    assert scene["required"] and "多人场景" in scene["reasons"][0]
    assert [actor["actor_id"] for actor in scene["actors"]] == [
        "P01", "P02", "P03"]
    first = shot_blocking(plan, 1)
    second = shot_blocking(plan, 2)
    assert first["camera"]["lens_mm"] == 35
    assert first["camera"]["movement"] == "跟"
    assert len(first["actors"]) == first["character_count"] == 3
    assert second["actors"][0]["start"] == first["actors"][0]["end"]
    assert "严格 3 人" in first["constraint"]
    assert validate_spatial_plan(plan, storyboard)["passed"]

    paths = write_spatial_svgs(plan, tmp_path / "blocking")
    assert len(paths) == 1 and Path(paths[0]).is_file()
    svg = Path(paths[0]).read_text(encoding="utf-8")
    assert "180°轴线" in svg and "35mm" in svg and "P01" in svg


def test_svg_escapes_scene_and_actor_labels():
    storyboard = {"shots": [_shot(1, ["A&B"], action="向前走")]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "门口 < 室内"}]},
        storyboard, {"characters": [{"name": "A&B"}], "scenes": []})
    svg = render_scene_svg(plan["scenes"][0])
    assert "A&amp;B" in svg and "门口 &lt; 室内" in svg
    assert "< 室内" not in svg


def test_validation_rejects_character_drift():
    storyboard = {"shots": [_shot(1, ["甲", "乙"])]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "房间"}]}, storyboard,
        {"characters": [{"name": "甲"}, {"name": "乙"}], "scenes": []})
    plan["shot_index"]["1"]["actors"].pop()
    report = validate_spatial_plan(plan, storyboard)
    assert not report["passed"]
    assert "人物名单/数量" in report["issues"][0]
