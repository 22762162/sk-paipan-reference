import pytest

from aifos.spatial_blocking import (
    _actor_is_moving,
    _collision_free_actor_route,
    _position_x,
    _pose_profile,
    build_spatial_plan,
    declared_scale,
    shot_blocking,
)


def test_anatomical_left_right_is_not_mistaken_for_room_side():
    assert _position_x("右腕旁", 500) == 500
    assert _position_x("床左侧腕部旁", 500) == 300


def test_actor_motion_is_local_and_respects_static_contract():
    shot = {
        "description": "虞寻歌进入房间，虞寻欢始终仰躺保持不动",
        "prompt": "虞寻歌走到床左侧；虞寻欢身体静止",
    }
    assert not _actor_is_moving(
        "虞寻欢", ["虞寻歌", "虞寻欢"], shot,
        {"position": "床中央", "condition": {"mobility": "immobile"}},
        {"position": "床中央", "condition": {"mobility": "immobile"}},
    )
    assert _actor_is_moving(
        "虞寻歌", ["虞寻歌", "虞寻欢"], shot,
        {"position": "房门内"}, {"position": "床左侧"},
    )


def test_same_wrist_anchor_and_negated_motion_stay_fixed():
    assert not _actor_is_moving(
        "虞寻歌", ["虞寻歌"],
        {"description": "虞寻歌不再绕床或平移", "prompt": ""},
        {"position": "右腕旁"}, {"position": "床左侧腕部旁"},
    )


def test_positive_retreat_into_room_is_detected():
    assert _actor_is_moving(
        "虞寻歌", ["虞寻歌"],
        {"description": "虞寻歌退入盥洗室", "prompt": ""},
        {"position": "床左侧"}, {"position": "盥洗室门内"},
    )


def test_limb_leaving_contact_does_not_move_whole_actor_marker():
    assert not _actor_is_moving(
        "虞寻歌", ["虞寻歌"],
        {"description": "虞寻歌俯身凝视环痕", "prompt": ""},
        {"position": "床左侧腕旁", "pose": "两指隔绳闭合"},
        {"position": "床左侧腕旁", "pose": "两指离开皮绳"},
    )


def test_actor_route_detours_around_real_furniture_box():
    scene_model = {
        "room": {"floor_width_m": 6.0, "floor_depth_m": 5.0},
        "objects": [{
            "name": "电脑桌",
            "category": "furniture",
            "position_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
            "width_m": 1.6,
            "depth_m": .8,
            "height_m": .75,
            "rotation_y_deg": 0,
        }],
    }
    route = _collision_free_actor_route(
        {"x": -2.0, "y": 0.0, "z": 0.0},
        {"x": 2.0, "y": 0.0, "z": 0.0},
        scene_model,
        scene_model["room"],
    )

    assert len(route) >= 3
    assert route[0]["x"] == -2.0
    assert route[-1]["x"] == 2.0
    assert any(abs(point["z"]) >= .6 for point in route[1:-1])


def test_floor_rug_does_not_force_actor_detour():
    scene_model = {
        "room": {"floor_width_m": 6.0, "floor_depth_m": 5.0},
        "objects": [{
            "name": "厚地毯",
            "category": "decor",
            "position_3d": {"x": 0.0, "y": 0.0, "z": 0.0},
            "width_m": 2.0,
            "depth_m": 2.0,
            "height_m": .5,
        }],
    }
    route = _collision_free_actor_route(
        {"x": -2.0, "y": 0.0, "z": 0.0},
        {"x": 2.0, "y": 0.0, "z": 0.0},
        scene_model,
        scene_model["room"],
    )
    assert len(route) == 2


@pytest.mark.parametrize(
    "local_pose",
    (
        "站在床左侧",
        "站着等待",
        "站稳脚步",
        "从床边站起",
        "身体直立",
        "直起身体",
        "立于盥洗室门内",
    ),
)
def test_standing_synonyms_are_resolved_from_actor_local_state(local_pose):
    profile = _pose_profile(
        {"position": local_pose},
        action="弟弟仍然仰躺在床上",
        phase="end",
    )

    assert profile["pose"] == "standing"
    assert profile["height_m"] == 1.68
    assert profile["support"] == "双脚/地面"


def test_whole_shot_action_cannot_supply_or_contaminate_actor_pose():
    profile = _pose_profile(
        {"position": "床左侧", "action": "右掌张开"},
        action="虞寻欢仰躺在床上",
    )

    assert profile["pose"] == "standing"


@pytest.mark.parametrize(
    ("local_pose", "expected_pose", "expected_envelope"),
    (
        ("坐在书案后", "sitting", 1.22),
        ("伏案查看卷宗", "leaning_seated", 1.05),
        ("跪在堂下", "kneeling", 1.12),
        ("蹲在墙边", "crouching", .98),
        ("仰躺在床上", "lying", .55),
    ),
)
def test_pose_envelope_never_replaces_adult_stature(
        local_pose, expected_pose, expected_envelope):
    profile = _pose_profile({"position": local_pose})

    assert profile["pose"] == expected_pose
    assert profile["height_m"] == expected_envelope
    assert profile["stature_m"] == 1.68


def test_two_actor_standing_and_lying_poses_remain_isolated_in_plan():
    shot = {
        "shot_no": 15,
        "scene_no": 1,
        "characters": ["虞寻歌", "虞寻欢"],
        "character_count": 2,
        "description": "虞寻歌站在床左侧，虞寻欢仰躺在床上",
        "prompt": "弟弟虞寻欢保持仰躺，姐姐虞寻歌直起身体",
        "camera": "双人中景固定",
        "start_state": {
            "虞寻歌": {
                "position": "床左侧",
                "action": "站起后右掌张开",
                "support": "双脚由地面支撑",
            },
            "虞寻欢": {
                "position": "床上",
                "pose": "仰躺",
                "support": "床榻",
            },
        },
        "end_state": {
            "虞寻歌": {
                "position": "站在盥洗室门内",
                "action": "站稳",
                "support": "双脚由门内地面支撑",
            },
            "虞寻欢": {
                "position": "床上",
                "pose": "仰躺",
                "support": "床榻",
            },
        },
        "five_dimensions": {"camera_design": {
            "lens": "35mm",
            "movement": "固定",
            "camera_position": "正面",
            "axis_offset_degrees": -30,
        }},
    }
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "酒店卧室"}]},
        {"shots": [shot]},
        {
            "characters": [
                {"name": "虞寻歌", "role": "主角"},
                {"name": "虞寻欢", "role": "弟弟"},
            ],
            "scenes": [{"name": "酒店卧室"}],
        },
    )

    actors = {
        actor["name"]: actor for actor in shot_blocking(plan, 15)["actors"]
    }
    assert actors["虞寻歌"]["pose_start"] == "standing"
    assert actors["虞寻歌"]["pose_end"] == "standing"
    assert actors["虞寻歌"]["start_height_m"] == 1.68
    assert actors["虞寻歌"]["end_height_m"] == 1.68
    assert actors["虞寻歌"]["stature_m"] == 1.68
    assert actors["虞寻欢"]["pose_start"] == "lying"
    assert actors["虞寻欢"]["pose_end"] == "lying"
    assert actors["虞寻欢"]["start_height_m"] == .55
    assert actors["虞寻欢"]["end_height_m"] == .55
    assert actors["虞寻欢"]["stature_m"] == 1.68


@pytest.mark.parametrize(
    ("shot_size", "expected_distance"),
    (("中近景", 1.9), ("中全景", 3.8)),
)
def test_composite_scale_uses_specific_token_and_distance(
        shot_size, expected_distance):
    shot = {
        "shot_no": 1,
        "scene_no": 1,
        "characters": ["甲"],
        "character_count": 1,
        "description": "甲站在房间中央",
        "prompt": "甲站在房间中央",
        "camera": f"{shot_size}固定",
        "start_state": {"甲": {"position": "站在房间中央"}},
        "end_state": {"甲": {"position": "站在房间中央"}},
        "five_dimensions": {"camera_design": {
            "shot_size": shot_size,
            "lens": "35mm",
            "movement": "固定",
            "camera_position": "正面",
            "axis_offset_degrees": -30,
        }},
    }

    assert declared_scale(shot) == shot_size
    plan = build_spatial_plan(
        {"scenes": [{"scene_no": 1, "location": "室内"}]},
        {"shots": [shot]},
        {
            "characters": [{"name": "甲", "role": "主角"}],
            "scenes": [{"name": "室内"}],
        },
    )
    camera = shot_blocking(plan, 1)["camera"]
    assert camera["scale_for_distance"] == shot_size
    assert camera["scale_distance_m"] == expected_distance
    assert camera["director_camera"]["declared"]["shot_size"] == shot_size
    assert camera["director_camera"]["desired_distance_m"] == expected_distance


def test_spatial_scale_prefers_authoritative_shot_scale_over_old_aliases():
    shot = {
        "camera": "中近景85mm",
        "five_dimensions": {"camera_design": {
            "shot_scale": "中全景",
            "shot_size": "特写",
            "scale": "近景",
        }},
    }
    assert declared_scale(shot) == "中全景"
