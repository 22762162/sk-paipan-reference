import math
from pathlib import Path

from aifos.spatial_blocking import (MIN_ACTOR_SEPARATION,
                                    MIN_CAMERA_SEPARATION,
                                    build_character_number_map,
                                    build_spatial_plan, render_scene_svg,
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
    continuity = {"characters": [
        {"name": name, "role": "主角" if index == 0 else "配角"}
        for index, name in enumerate(cast)],
                  "scenes": [{"name": "旧仓库"}]}
    storyboard = {"shots": [
        _shot(1, cast, "跟", "三人从门口走向桌边"),
        _shot(2, cast, "固定", "三人停下对峙"),
    ]}
    plan = build_spatial_plan(script, storyboard, continuity)

    assert plan["schema"] == "aifos.spatial-blocking/v2"
    assert plan["summary"] == {
        "scenes": 1, "required_scenes": 1, "shots": 2, "actors": 3}
    assert plan["validation"]["passed"]
    scene = plan["scenes"][0]
    assert scene["required"] and "多人场景" in scene["reasons"][0]
    assert [actor["actor_id"] for actor in scene["actors"]] == [
        "P01", "P02", "P03"]
    assert plan["character_number_map"]["P01"]["display_label"] == \
        "P01 主角·林昭"
    assert plan["character_ids_by_name"]["沈砚"] == "P03"
    first = shot_blocking(plan, 1)
    second = shot_blocking(plan, 2)
    assert first["camera"]["lens_mm"] == 35
    assert first["camera"]["movement"] == "跟"
    assert first["camera"]["moving"]
    assert [point["phase"] for point in first["camera"]["route"]] == [
        "start", "end"]
    assert "起点→终点" in first["camera"]["direction_label"]
    assert len(first["actors"]) == first["character_count"] == 3
    assert all(actor["display_label"].startswith(actor["actor_id"])
               for actor in first["actors"])
    assert all([point["phase"] for point in actor["route"]] == [
        "start", "end"] for actor in first["actors"])
    assert second["actors"][0]["start"] == first["actors"][0]["end"]
    assert second["camera"]["moving"] is False
    assert second["camera"]["start"] == second["camera"]["end"]
    assert second["camera"]["direction_label"] == "静止机位：起点=终点"
    assert "严格 3 人" in first["constraint"]
    assert "最终画面不得出现人物编号" in first["constraint"]
    assert validate_spatial_plan(plan, storyboard)["passed"]

    paths = write_spatial_svgs(plan, tmp_path / "blocking")
    assert len(paths) == 1 and Path(paths[0]).is_file()
    svg = Path(paths[0]).read_text(encoding="utf-8")
    assert "35mm" in svg and "P01 主角·林昭" in svg
    assert 'data-layout="per-shot-panels"' in svg
    assert svg.count('data-layout="isolated-panel"') == 2
    assert 'data-camera-phase="start"' in svg
    assert 'data-camera-phase="fixed"' in svg


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


def test_crowded_center_positions_are_spread_and_routes_remain_parseable(
        tmp_path):
    cast = ["林昭", "小狐", "沈砚", "阿云", "秦月", "顾北", "夏荷", "团长"]
    same_state = {
        name: {"position": "画面中心", "direction": "面向主体", "pose": "向前走"}
        for name in cast
    }
    shot = _shot(1, cast, "移", "众人向前走")
    shot["start_state"] = same_state
    shot["end_state"] = same_state
    continuity = {"characters": [
        {"name": name, "role": "主角" if index == 0 else "角色"}
        for index, name in enumerate(cast)], "scenes": []}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "排练厅"}]},
        {"shots": [shot]}, continuity)
    block = shot_blocking(plan, 1)

    assert plan["validation"]["passed"], plan["validation"]["issues"]
    for key in ("start", "end"):
        points = [actor[key] for actor in block["actors"]]
        assert all(
            math.dist((left["x"], left["y"]), (right["x"], right["y"]))
            >= MIN_ACTOR_SEPARATION
            for index, left in enumerate(points)
            for right in points[index + 1:]
        )
    all_actor_markers = [
        (actor["actor_id"], phase, actor[phase])
        for actor in block["actors"] for phase in ("start", "end")
    ]
    assert all(
        math.dist((left[2]["x"], left[2]["y"]),
                  (right[2]["x"], right[2]["y"])) >= MIN_ACTOR_SEPARATION
        for index, left in enumerate(all_actor_markers)
        for right in all_actor_markers[index + 1:]
    )
    assert all(
        math.dist((camera_point["x"], camera_point["y"]),
                  (marker[2]["x"], marker[2]["y"])) >= MIN_CAMERA_SEPARATION
        for camera_point in (block["camera"]["start"], block["camera"]["end"])
        for marker in all_actor_markers
    )
    assert all(actor["moving"] and len(actor["route"]) == 2
               and actor["route_direction"] != "静止"
               for actor in block["actors"])
    svg = render_scene_svg(plan["scenes"][0])
    assert 'data-overlap-policy="separate-label-lanes"' in svg
    assert all(character["display_label"] in svg
               for character in plan["character_number_map"].values())
    crowded_path = write_spatial_svgs(plan, tmp_path / "crowded")[0]
    assert Path(crowded_path).read_text(encoding="utf-8") == svg


def test_storyboard_exports_same_number_map_for_prompt_references():
    from aifos.workflow import enrich_storyboard

    script = {
        "episode_title": "编号测试",
        "characters": [{"name": "林昭", "role": "主角"},
                       {"name": "沈砚", "role": "对手"}],
        "scenes": [{"scene_no": 1, "location": "走廊",
                    "characters": ["林昭", "沈砚"], "action": "两人对峙"}],
    }
    continuity = {
        "characters": [{"name": "林昭", "role": "主角",
                        "default_position": "画面左1/3"},
                       {"name": "沈砚", "role": "对手",
                        "default_position": "画面右2/3"}],
        "scenes": [{"name": "走廊"}],
    }
    profile = {
        "max_segment_seconds": 15, "time_precision_seconds": .5,
        "standard_fingerprint": "test", "rules": {
            "performance": {"reaction_after_key_dialogue": False,
                            "beat_at_emotional_peak": False},
        },
    }
    storyboard = enrich_storyboard(script, {"shots": [{
        "scene_no": 1, "characters": ["林昭", "沈砚"],
        "description": "林昭向前走，沈砚原地不动", "camera": "中景推",
    }]}, continuity, profile)

    assert storyboard["character_number_map"] == build_character_number_map(
        continuity, storyboard)
    shot = storyboard["shots"][0]
    assert shot["character_number_ids"] == ["P01", "P02"]
    assert shot["character_number_map"]["P01"]["display_label"] == \
        "P01 主角·林昭"
    assert "人物编号映射（仅用于提示词引用，不生成画面文字）" in \
        shot["seedance_prompt"]
    assert "最终画面不得出现P01等人物编号" in shot["seedance_prompt"]
    plan = build_spatial_plan(script, storyboard, continuity)
    moving_by_name = {
        actor["name"]: actor["moving"]
        for actor in shot_blocking(plan, 1)["actors"]
    }
    assert moving_by_name == {"林昭": True, "沈砚": False}
