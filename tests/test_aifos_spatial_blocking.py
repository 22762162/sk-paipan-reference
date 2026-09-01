import math
from pathlib import Path

from aifos.spatial_blocking import (CAMERA_ACTOR_CLEARANCE_SAFETY_M,
                                    MIN_ACTOR_SEPARATION,
                                    MIN_CAMERA_ACTOR_CLEARANCE_M,
                                    build_character_number_map,
                                    build_spatial_plan,
                                    mark_spatial_reference_requirements,
                                    render_scene_svg,
                                    requires_spatial_reference, shot_blocking,
                                    validate_spatial_plan,
                                    write_spatial_reference_pngs,
                                    write_spatial_svgs)
from aifos.prompt_contract import build_physical_contract


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

    assert plan["schema"] == "aifos.spatial-blocking/v3"
    assert plan["summary"] == {
        "scenes": 1, "required_scenes": 1, "shots": 2, "actors": 3}
    assert plan["validation"]["passed"]
    scene = plan["scenes"][0]
    assert scene["required"] and "多人场景" in scene["reasons"][0]
    assert scene["canvas"]["orientation"] == "交互3D"
    assert scene["world"] == {
        "coordinate_system": "right-handed-y-up",
        "unit": "meter",
        "floor_width_m": 10.0,
        "floor_depth_m": 7.0,
        "floor_y_m": 0.0,
        "default_actor_height_m": 1.68,
        "default_camera_height_m": 1.55,
    }
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
    assert first["camera"]["start_3d"]["y"] == \
        first["camera"]["director_camera"]["height_m"]
    assert first["camera"]["target_3d"]["y"] == 1.25
    assert first["camera"]["horizontal_fov_degrees"] > 0
    assert first["camera"]["vertical_fov_degrees"] > \
        first["camera"]["horizontal_fov_degrees"]
    assert first["camera"]["orientation_start"]["roll_degrees"] == 0
    assert first["camera"]["frustum"]["aspect_ratio"] == "9:16"
    assert [point["phase"] for point in first["camera"]["route"]] == [
        "start", "end"]
    assert "起点→终点" in first["camera"]["direction_label"]
    assert len(first["actors"]) == first["character_count"] == 3
    assert all(actor["display_label"].startswith(actor["actor_id"])
               for actor in first["actors"])
    assert all(actor["height_m"] == 1.68
               and set(actor["start_3d"]) == {"x", "y", "z"}
               and set(actor["end_3d"]) == {"x", "y", "z"}
               and actor["start_3d"]["y"] == actor["end_3d"]["y"] == 0
               for actor in first["actors"])
    assert all([point["phase"] for point in actor["route_3d"]] == [
        "start", "end"] for actor in first["actors"])
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
    assert 'data-projection="isometric-3d"' in svg
    assert 'data-world-axis="y-up"' in svg
    assert 'data-camera-frustum="true"' in svg
    assert 'data-actor-model="stick-figure"' in svg
    assert 'data-pose="standing"' in svg
    assert "姿态火柴人" in svg
    assert svg.count('data-layout="isolated-panel"') == 2
    assert 'data-camera-phase="start"' in svg
    assert 'data-camera-phase="fixed"' in svg


def test_continuity_prefers_world_coordinates_over_canvas_rounding():
    """A one-pixel reprojection difference must not become a fake teleport."""
    storyboard = {"shots": [
        _shot(1, ["甲"], action="甲保持原位"),
        _shot(2, ["甲"], action="甲继续保持原位"),
    ]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "酒店客房"}]},
        storyboard,
        {"characters": [{"name": "甲"}], "scenes": []})
    first = shot_blocking(plan, 1)["actors"][0]
    second = shot_blocking(plan, 2)["actors"][0]
    second["start"] = {
        "x": first["end"]["x"] - 1,
        "y": first["end"]["y"] - 1,
    }
    second["start_3d"] = dict(first["end_3d"])

    report = validate_spatial_plan(plan, storyboard)

    assert report["passed"], report["issues"]


def test_continuity_still_rejects_a_real_world_space_jump():
    storyboard = {"shots": [
        _shot(1, ["甲"], action="甲保持原位"),
        _shot(2, ["甲"], action="甲继保持原位"),
    ]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "酒店客房"}]},
        storyboard,
        {"characters": [{"name": "甲"}], "scenes": []})
    second = shot_blocking(plan, 2)["actors"][0]
    second["start_3d"] = dict(second["start_3d"], x=(
        float(second["start_3d"]["x"]) + .5))

    report = validate_spatial_plan(plan, storyboard)

    assert not report["passed"]
    assert any("起点未继承上一镜终点" in issue
               for issue in report["issues"])


def test_vehicle_blocking_uses_scene_model_room_without_changing_default():
    """Episode 29 镜2回归：车厢不能继续套用 10x7m 通用房间。"""
    location = "轿车内/高速公路"
    shot = _shot(2, ["虞寻歌", "司机"], "固定", "两人坐在车内交谈")
    shot["scene_no"] = 2
    shot["start_state"]["虞寻歌"].update({
        "position": "画面右1/3", "pose": "坐在副驾驶座椅上"})
    shot["end_state"]["虞寻歌"] = dict(
        shot["start_state"]["虞寻歌"])
    shot["start_state"]["司机"].update({
        "position": "画面左1/3", "pose": "坐在驾驶座椅上"})
    shot["end_state"]["司机"] = dict(shot["start_state"]["司机"])
    script = {"scenes": [{"scene_no": 2, "location": location}]}
    storyboard = {"shots": [shot]}
    continuity = {
        "characters": [{"name": "虞寻歌", "role": "主角"},
                       {"name": "司机", "role": "配角"}],
        "scenes": [{"name": "未使用"}, {"name": location}],
    }
    scene_model = {
        "location": location,
        "room": {"floor_width_m": 1.85, "floor_depth_m": 4.4,
                 "wall_height_m": 1.55},
    }

    default_plan = build_spatial_plan(script, storyboard, continuity)
    room_plan = build_spatial_plan(
        script, storyboard, continuity,
        scene_models={location: scene_model})

    assert default_plan["scenes"][0]["world"]["floor_width_m"] == 10.0
    assert default_plan["scenes"][0]["world"]["floor_depth_m"] == 7.0
    assert room_plan["source_fingerprint"] != \
        default_plan["source_fingerprint"]
    world = room_plan["scenes"][0]["world"]
    assert world["floor_width_m"] == 1.85
    assert world["floor_depth_m"] == 4.4
    block = shot_blocking(room_plan, 2)
    room_points = [
        point
        for actor in block["actors"]
        for point in (actor["start_3d"], actor["end_3d"])
    ] + [block["camera"]["start_3d"], block["camera"]["end_3d"]]
    assert all(abs(point["x"]) <= 1.85 / 2 for point in room_points)
    assert all(abs(point["z"]) <= 4.4 / 2 for point in room_points)
    assert room_plan["validation"]["passed"], \
        room_plan["validation"]["issues"]


def test_reentering_actors_with_same_inherited_point_are_spread_apart():
    """分别在不同前镜出现的人物，可能各自把同一左侧坐标写入
    previous_end；之后首次同框时必须按当前站位合同重新避碰。
    """
    one = _shot(1, ["甲"], action="甲单独站在画面左侧")
    two = _shot(2, ["乙"], action="乙单独站在画面左侧")
    three = _shot(3, ["甲", "乙"], action="甲乙重新同框对峙")
    three["start_state"]["甲"]["position"] = "画面左1/3"
    three["start_state"]["乙"]["position"] = "画面右1/3"
    three["end_state"] = three["start_state"]
    storyboard = {"shots": [one, two, three]}

    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "书房"}]},
        storyboard,
        {"characters": [{"name": "甲"}, {"name": "乙"}],
         "scenes": [{"name": "书房"}]})

    block = shot_blocking(plan, 3)
    starts = [actor["start"] for actor in block["actors"]]
    prior_end = shot_blocking(plan, 2)["actors"][0]["end"]
    assert math.dist(
        (starts[0]["x"], starts[0]["y"]),
        (starts[1]["x"], starts[1]["y"])) >= MIN_ACTOR_SEPARATION
    assert next(
        actor["start"] for actor in block["actors"]
        if actor["name"] == "乙") == prior_end
    assert plan["validation"]["passed"], plan["validation"]["issues"]


def test_seedance_spatial_png_required_for_group_and_changed_camera(
        tmp_path, monkeypatch):
    # 本容器/CI 无真实 SVG 转换器:打桩转换环节,专测"该带图的镜头
    # 都被标记、生成并绑定 PNG"这条产线逻辑(真实转换 macOS sips 覆盖)
    from aifos import spatial_blocking as sb
    PNG = (b"\x89PNG\r\n\x1a\n" + b"0" * 100)

    def fake_render(svg_path, png_path):
        Path(png_path).write_bytes(PNG)
        return ""

    monkeypatch.setattr(sb, "spatial_png_supported", lambda: True)
    monkeypatch.setattr(sb, "_render_svg_png", fake_render)
    shots = [
        _shot(1, ["甲"], "固定", "甲站在原位"),
        _shot(2, ["甲"], "固定", "甲站在原位"),
        _shot(3, ["甲", "乙"], "固定", "两人对峙"),
    ]
    # 强制第2镜换机位，但镜内保持静止。
    shots[1]["five_dimensions"]["camera_design"]["camera_position"] = "侧面"
    shots[1]["five_dimensions"]["camera_design"]["axis_offset_degrees"] = 45
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "办公室"}]},
        {"shots": shots},
        {"characters": [{"name": "甲"}, {"name": "乙"}], "scenes": []})
    mark_spatial_reference_requirements(plan)

    assert not requires_spatial_reference(shot_blocking(plan, 1))
    assert requires_spatial_reference(shot_blocking(plan, 2))
    assert "相邻镜头机位变化" in (
        shot_blocking(plan, 2)["spatial_reference_reason"])
    assert requires_spatial_reference(shot_blocking(plan, 3))
    assert "2人同框" in shot_blocking(plan, 3)["spatial_reference_reason"]

    paths = write_spatial_reference_pngs(plan, tmp_path / "seedance")
    assert set(paths) == {2, 3}
    single_shot_svg = (
        tmp_path / "seedance" / "shot_002_space.svg").read_text(
            encoding="utf-8")
    assert 'viewBox="0 0 620 ' in single_shot_svg
    assert "P01 角色·甲" in single_shot_svg
    assert "P02 角色·乙" not in single_shot_svg
    for shot_no, uri in paths.items():
        path = Path(uri)
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert shot_blocking(plan, shot_no)["spatial_reference_uri"] == uri


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


def test_two_person_dialogue_locks_axis_screen_sides_and_eyelines():
    first = _shot(1, ["沈眉", "顾长渊"], "固定", "两人隔案对话试探")
    first.update({
        "kind": "dialogue",
        "dialogue": {"character": "沈眉", "dialogue": "你究竟是谁？"},
    })
    first["five_dimensions"]["camera_design"].update({
        "shot_scale": "全景", "camera_position": "正面",
        "axis_offset_degrees": -30,
    })
    second = _shot(2, ["沈眉", "顾长渊"], "固定", "顾长渊回答沈眉")
    second.update({
        "kind": "dialogue",
        "dialogue": {"character": "顾长渊", "dialogue": "你很快会知道。"},
    })
    second["five_dimensions"]["camera_design"].update({
        "shot_scale": "近景", "camera_position": "过肩",
        "axis_offset_degrees": 30,
    })
    storyboard = {"shots": [first, second]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "书阁"}]},
        storyboard,
        {"characters": [
            {"name": "沈眉", "role": "主角"},
            {"name": "顾长渊", "role": "重要配角"},
        ], "scenes": [{"name": "书阁"}]})

    assert plan["validation"]["passed"], plan["validation"]["issues"]
    assert any("双人对话" in reason
               for reason in plan["scenes"][0]["reasons"])
    one = shot_blocking(plan, 1)
    two = shot_blocking(plan, 2)
    d1 = one["dialogue_continuity"]
    d2 = two["dialogue_continuity"]
    assert d1["schema"] == "aifos.dialogue-continuity/v1"
    assert d1["screen_left_actor_id"] == d2["screen_left_actor_id"]
    assert d1["screen_right_actor_id"] == d2["screen_right_actor_id"]
    assert d1["axis_id"] == d2["axis_id"]
    assert d1["camera_side_sign"] == d2["camera_side_sign"]
    assert d1["coverage"] == "双人建立镜头"
    assert d2["coverage"] == "同侧过肩正反打"
    for block in (one, two):
        left_id = block["dialogue_continuity"]["screen_left_actor_id"]
        right_id = block["dialogue_continuity"]["screen_right_actor_id"]
        actors = {
            actor["actor_id"]: actor for actor in block["actors"]}
        assert actors[left_id]["gaze_target_actor_id"] == right_id
        assert actors[right_id]["gaze_target_actor_id"] == left_id
        assert actors[left_id]["eyeline_screen_direction"] == "向右"
        assert actors[right_id]["eyeline_screen_direction"] == "向左"
        assert "双人对话轴线锁" in block["constraint"]

    physical = build_physical_contract({
        **second, "spatial_blocking": two})
    assert any("双人对话180°轴线合同" in rule
               and "禁止交换左右" in rule
               for rule in physical["rules"])


def test_dialogue_validation_rejects_camera_on_or_across_axis():
    shot = _shot(1, ["甲", "乙"], "固定", "甲乙对话")
    shot.update({
        "kind": "dialogue",
        "dialogue": {"character": "甲", "dialogue": "你来了。"},
    })
    storyboard = {"shots": [shot]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "房间"}]},
        storyboard,
        {"characters": [{"name": "甲"}, {"name": "乙"}], "scenes": []})
    block = shot_blocking(plan, 1)
    on_axis = dict(block["dialogue_continuity"]["axis"]["a"])
    block["camera"]["start"] = on_axis
    block["camera"]["end"] = on_axis

    report = validate_spatial_plan(plan, storyboard)

    assert not report["passed"]
    assert any("跨越双人表演轴" in issue for issue in report["issues"])


def test_validation_rejects_malformed_3d_actor_and_camera_contract():
    storyboard = {"shots": [_shot(1, ["甲"], "升", "甲向前走")]}
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "房间"}]}, storyboard,
        {"characters": [{"name": "甲"}], "scenes": []})
    block = plan["shot_index"]["1"]
    block["actors"][0]["start_3d"].pop("z")
    block["camera"]["vertical_fov_degrees"] = 0

    report = validate_spatial_plan(plan, storyboard)

    assert not report["passed"]
    assert any("缺少合法三维站位/路线" in issue
               for issue in report["issues"])
    assert any("缺少合法三维机位/视锥" in issue
               for issue in report["issues"])


def test_final_director_camera_clearance_survives_canvas_quantization():
    """回归旗舰 e2e 镜4：顶拍特写机位曾量化成 0.2997m。"""
    shot = _shot(4, ["墨童"], "固定", "墨童说话")
    shot.update({
        "kind": "dialogue",
        "camera": "特写",
        "dialogue": {
            "character": "墨童",
            "dialogue": "白澈，小心！它就藏在附近。",
        },
    })
    shot["start_state"]["墨童"]["position"] = "画面中"
    shot["end_state"]["墨童"]["position"] = "画面中"
    shot["five_dimensions"]["camera_design"].update({
        "shot_scale": "特写",
        "angle": "顶拍",
        "lens": "85mm",
        "camera_position": "过肩",
        "movement": "固定",
        "axis_offset_degrees": -30,
    })
    script = {"scenes": [{"scene_no": 1, "location": "妖市地穴"}]}
    storyboard = {"shots": [shot]}
    continuity = {
        "characters": [{"name": "墨童", "role": "同伴"}],
        "scenes": [{"name": "妖市地穴"}],
    }

    results = [
        build_spatial_plan(script, storyboard, continuity)
        for _ in range(20)
    ]

    assert all(plan["validation"]["passed"] for plan in results)
    resolved = [shot_blocking(plan, 4) for plan in results]
    assert len({
        (block["camera"]["start"]["x"],
         block["camera"]["start"]["y"])
        for block in resolved
    }) == 1
    for block in resolved:
        camera = block["camera"]
        director = camera["director_camera"]
        assert director["clearance_adjusted"] is True
        assert director["clearance_before_m"] < \
            MIN_CAMERA_ACTOR_CLEARANCE_M
        assert director["clearance_m"] >= \
            CAMERA_ACTOR_CLEARANCE_SAFETY_M
        assert camera["start"] == camera["end"]
        assert camera["moving"] is False
        assert camera["lens_mm"] == 85
        assert director["desired_distance_m"] == 1.0
        assert all(
            math.hypot(
                camera[camera_phase]["x"] - actor[actor_phase]["x"],
                camera[camera_phase]["z"] - actor[actor_phase]["z"],
            ) >= MIN_CAMERA_ACTOR_CLEARANCE_M
            for camera_phase in ("start_3d", "end_3d")
            for actor_phase in ("start_3d", "end_3d")
            for actor in block["actors"]
        )


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
        math.dist(
            (block["camera"][camera_phase]["x"],
             block["camera"][camera_phase]["z"]),
            (actor[actor_phase]["x"], actor[actor_phase]["z"]),
        ) >= MIN_CAMERA_ACTOR_CLEARANCE_M
        for camera_phase, actor_phase in (
            ("start_3d", "start_3d"), ("end_3d", "end_3d"))
        for actor in block["actors"]
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


def test_lying_actor_uses_support_pose_and_low_camera_target():
    shot = _shot(1, ["朱慈烺"], "固定", "朱慈烺仰卧于床榻")
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "寝殿"}]},
        {"shots": [shot]},
        {"characters": [{"name": "朱慈烺", "role": "主角"}],
         "scenes": [{"name": "寝殿"}]})
    block = shot_blocking(plan, 1)
    actor = block["actors"][0]

    assert actor["pose_start"] == actor["pose_end"] == "lying"
    assert actor["support_end"] == "床榻"
    assert actor["height_m"] == .55
    assert block["camera"]["target_3d"]["y"] == .42
    assert plan["validation"]["passed"], plan["validation"]["issues"]
    svg = render_scene_svg(plan["scenes"][0])
    assert 'data-actor-model="stick-figure"' in svg
    assert 'data-pose="lying"' in svg
    assert svg.count('data-stick-head="true"') == 1
