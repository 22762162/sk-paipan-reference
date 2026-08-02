"""图片成本分层：普通批量不误用高档，终稿/文字图保留高质量通道。"""

import json

from pathlib import Path

from aifos.adapters.codex_image import build_instruction
from aifos.app import App
from aifos.ops_center import OpsCenter
from aifos.production.base import ProviderResult


def _shot(readable_text=None):
    return {
        "shot_no": 1, "scene_no": 1, "unit_id": "U01",
        "prompt": "两人在办公室交谈", "description": "两人交谈",
        "characters": ["林昭"], "character_count": 1,
        "camera": "中景缓推", "dialogue": None,
        "start_state": {}, "end_state": {}, "five_dimensions": {},
        "readable_text": readable_text or {"required": False},
    }


def test_shot_payload_separates_batch_from_complex_text(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("成本路由")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        identity = tmp_path / "identity.png"
        identity.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        app.assets.register(
            project["id"], "character_identity", "林昭",
            uri=str(identity), meta={"locked": True, "character": "林昭"})
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "script": {"scenes": [{"scene_no": 1, "location": "办公室"}]},
            "storyboard": {"shots": []},
            "blocking": {"shot_index": {"1": {
                "actors": [{"actor_id": "P01", "name": "林昭"}],
                "constraint": "P01 从左侧走向桌边",
            }}},
            "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
        }
        batch = app.director._shot_payload(ctx, _shot())
        assert batch["image_task_class"] == "batch"
        assert batch["image_quality"] == "medium"
        assert batch["require_reference_images"] is True
        assert batch["character_refs"][0] == str(identity)
        assert batch["identity_references"][0]["actor_id"] == "P01"
        assert batch["character_reference_map"] == [{
            "actor_id": "P01", "character": "林昭", "uri": str(identity),
        }]

        text = app.director._shot_payload(ctx, _shot({
            "required": True, "carrier": "手机屏幕",
            "whitelist": ["直播开始"],
        }))
        assert text["image_task_class"] == "complex_text"
        assert text["image_quality"] == "high"
    finally:
        app.close()


def test_shot_payload_flattens_phase_text_for_each_static_frame(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("相位文字派发")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "script": {"scenes": [{
                "scene_no": 1, "location": "2078年现代别墅卧室",
            }]},
            "storyboard": {"shots": []}, "blocking": {},
            "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
        }
        shot2 = {
            "shot_no": 2, "scene_no": 1, "unit_id": "U02",
            "prompt": "车内长镜头", "description": (
                "起点查看手机锁屏23:10，终点手机收入口袋后看向车外"),
            "characters": [], "camera": "中景固定", "dialogue": None,
            "start_state": {}, "end_state": {}, "five_dimensions": {},
            "frame_targets": {
                "first_frame": {
                    "phase": "start", "state": "右手持亮屏手机",
                    "fallback": False,
                },
                "last_frame": {
                    "phase": "end", "state": "手机已经收入口袋",
                    "fallback": False,
                },
            },
            "frame_props": [
                {
                    "prop_id": "prop_phone_02", "phase": "start",
                    "physical_state": "亮屏", "holder": "虞寻歌右手",
                    "location": "胸前", "visibility": "visible",
                    "representation": "physical",
                },
                {
                    "prop_id": "prop_phone_02", "phase": "end",
                    "physical_state": "完全收纳", "holder": "虞寻歌",
                    "location": "风衣口袋内", "visibility": "hidden",
                    "representation": "physical",
                },
            ],
            "readable_text": {"phases": {
                "start": {
                    "required": True, "carrier": "手机锁屏",
                    "whitelist": ["23:10"], "layout": "时间居中",
                    "style": "白色系统数字", "perspective": "朝向使用者",
                },
                "end": {
                    "required": False, "carrier": "手机锁屏",
                    "whitelist": [], "layout": "", "style": "",
                    "perspective": "",
                },
            }},
        }

        end_payload = app.director._shot_payload(
            ctx, shot2, frame_kind="last_frame")
        end_instruction, _, _ = build_instruction(
            "image", end_payload, tmp_path / "end")
        assert end_payload["readable_text"] == {
            "required": False, "carrier": "手机锁屏", "whitelist": [],
            "layout": "", "style": "", "perspective": "",
        }
        assert end_payload["prompt_contract"]["readable_text_current"] == (
            end_payload["readable_text"])
        assert "白名单为空" not in end_instruction
        assert "23:10" not in end_instruction
        assert "手持屏幕关系" not in end_instruction

        start_payload = app.director._shot_payload(
            ctx, shot2, frame_kind="first_frame")
        start_instruction, _, _ = build_instruction(
            "image", start_payload, tmp_path / "start")
        assert start_payload["readable_text"]["whitelist"] == ["23:10"]
        assert "23:10" in start_instruction
        assert "手持屏幕关系" in start_instruction
        assert "白名单为空" not in start_instruction

        shot4 = {
            **shot2,
            "shot_no": 4,
            "unit_id": "U04",
            "frame_targets": {"keyframe": {
                "phase": "freeze", "state": "手机显示天赋卡",
                "fallback": False,
            }},
            "readable_text": {"phases": {
                "start": {
                    "required": True, "carrier": "手机锁屏",
                    "whitelist": ["02:21:59"], "layout": "时间居中",
                    "style": "白色系统数字", "perspective": "朝向使用者",
                },
                "freeze": {
                    "required": True, "carrier": "手机屏幕",
                    "whitelist": ["SS", "盗神"], "layout": "卡面上下排版",
                    "style": "紫红游戏卡面", "perspective": "朝向使用者",
                },
                "end": {
                    "required": False, "carrier": "手机屏幕",
                    "whitelist": [], "layout": "", "style": "",
                    "perspective": "背向机位",
                },
            }},
            "frame_props": [{
                "prop_id": "prop_phone_04", "phase": "freeze",
                "physical_state": "亮屏显示天赋卡", "holder": "虞寻歌双手",
                "location": "胸前", "visibility": "visible",
                "representation": "physical",
            }],
        }
        freeze_payload = app.director._shot_payload(ctx, shot4)
        freeze_instruction, _, _ = build_instruction(
            "image", freeze_payload, tmp_path / "freeze")
        assert freeze_payload["readable_text"]["whitelist"] == ["SS", "盗神"]
        assert "SS、盗神" in freeze_instruction
        assert "02:21:59" not in freeze_instruction
        assert "23:10" not in freeze_instruction
        assert "电脑屏幕必须打开" not in freeze_instruction
    finally:
        app.close()


def test_shot_payload_scopes_cast_references_and_functional_people_by_frame(
        tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project("静帧人物作用域")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        identity_paths = {}
        for name in ("虞寻歌", "柳争流"):
            identity = tmp_path / f"{name}.png"
            identity.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
            identity_paths[name] = str(identity)
            app.assets.register(
                project["id"], "character_identity", name,
                uri=str(identity),
                meta={"locked": True, "character": name})
        ctx = {
            "project": dict(project), "episode": dict(episode),
            "script": {
                "characters": [
                    {"name": "虞寻歌", "gender": "女性",
                     "age_range": "24至28岁"},
                    {"name": "柳争流", "gender": "女性",
                     "age_range": "24至28岁"},
                ],
                "scenes": [{"scene_no": 1, "location": "现代酒店房间"}],
            },
            "storyboard": {"shots": []},
            "blocking": {"shot_index": {
                "1": {
                    "actors": [
                        {"actor_id": "P01", "name": "虞寻歌"},
                        {"actor_id": "P02", "name": "柳争流"},
                    ],
                    "dialogue_continuity": {
                        "screen_left_name": "虞寻歌",
                        "screen_right_name": "柳争流",
                    },
                },
                "2": {
                    "actors": [
                        {"actor_id": "P01", "name": "虞寻歌"},
                        {"actor_id": "F01", "name": "小吴"},
                    ],
                },
            }},
            "aspect": "9:16", "dims": {"width": 1080, "height": 1920},
        }
        shot1 = {
            "shot_no": 1, "scene_no": 1, "unit_id": "U01",
            "prompt": "虞寻歌进门后与柳争流隔门相望",
            "description": "虞寻歌进门，随后柳争流出现",
            "characters": ["虞寻歌", "柳争流"],
            "character_number_map": {
                "P01": {"name": "虞寻歌"},
                "P02": {"name": "柳争流"},
            },
            "camera": "中景固定", "dialogue": None,
            "start_state": {
                "虞寻歌": {"position": "房门内", "wardrobe": "米色风衣"},
                "柳争流": {"position": "走廊", "wardrobe": "深蓝西装"},
            },
            "end_state": {
                "虞寻歌": {"position": "房门内", "wardrobe": "米色风衣"},
                "柳争流": {"position": "走廊", "wardrobe": "深蓝西装"},
            },
            "narrative_overlays": [{
                "name": "柳争流内心Q版", "host_character": "柳争流",
                "action": "在柳争流肩边出现",
            }],
            "five_dimensions": {}, "readable_text": {"required": False},
            "frame_targets": {
                "first_frame": {
                    "phase": "start", "state": "虞寻歌独自走进房间",
                    "characters": ["虞寻歌"], "visible_figure_count": 1,
                    "fallback": False,
                },
                "last_frame": {
                    "phase": "end", "state": "虞寻歌与柳争流隔门相望",
                    "characters": ["虞寻歌", "柳争流"],
                    "visible_figure_count": 2, "fallback": False,
                },
            },
        }

        first = app.director._shot_payload(
            ctx, shot1, frame_kind="first_frame")
        assert first["characters"] == ["虞寻歌"]
        assert first["identity_characters"] == ["虞寻歌"]
        assert first["character_count"] == first["visible_figure_count"] == 1
        assert first["action"] == "虞寻歌独自走进房间"
        assert set(first["start_state"]) == {"虞寻歌"}
        assert set(first["end_state"]) == {"虞寻歌"}
        assert first["narrative_overlays"] == []
        assert [row["character"] for row in first["identity_references"]] == [
            "虞寻歌"]
        assert first["character_reference_map"] == [{
            "actor_id": "P01", "character": "虞寻歌",
            "uri": identity_paths["虞寻歌"],
        }]
        assert "柳争流" not in json.dumps(
            first["reference_manifest"], ensure_ascii=False)
        assert [actor["name"] for actor in first["spatial_blocking"]["actors"]] == [
            "虞寻歌"]
        assert "dialogue_continuity" not in first["spatial_blocking"]

        last = app.director._shot_payload(
            ctx, shot1, frame_kind="last_frame")
        assert last["characters"] == ["虞寻歌", "柳争流"]
        assert last["identity_characters"] == ["虞寻歌", "柳争流"]
        assert last["character_count"] == last["visible_figure_count"] == 2
        assert {row["character"] for row in last["reference_manifest"]
                if row.get("role") == "identity"} == {"虞寻歌", "柳争流"}

        paired = app.director._shot_payload(ctx, shot1, frame_kind="frames")
        assert paired["characters"] == ["虞寻歌", "柳争流"]
        assert paired["identity_characters"] == ["虞寻歌", "柳争流"]

        shot2 = {
            "shot_no": 2, "scene_no": 1, "unit_id": "U02",
            "prompt": "完整代驾长镜头",
            "description": "小吴驾车、停车、下车并递交物品",
            "characters": ["虞寻歌"], "camera": "中景固定",
            "dialogue": None, "start_state": {}, "end_state": {},
            "five_dimensions": {}, "readable_text": {"required": False},
            "functional_figures": [
                {"name": "小吴", "count": 1,
                 "state": "双手控制方向盘", "function": "代驾司机"},
                {"name": "小吴", "count": 1,
                 "state": "车辆停稳后解开安全带", "function": "接受委托"},
                {"name": "小吴", "count": 1,
                 "state": "站在驾驶侧车外", "function": "递交白酒"},
            ],
            "visible_figure_count": 4,
            "frame_targets": {
                "first_frame": {
                    "phase": "start", "state": "虞寻歌坐副驾驶，小吴驾车",
                    "characters": ["虞寻歌"],
                    "functional_figures": [{
                        "name": "小吴", "count": 1,
                        "state": "左前驾驶位双手握方向盘",
                        "function": "代驾司机",
                    }],
                    "visible_figure_count": 2, "fallback": False,
                },
                "last_frame": {
                    "phase": "end", "state": "虞寻歌坐副驾驶，小吴已下车",
                    "characters": ["虞寻歌"],
                    "functional_figures": [{
                        "name": "小吴", "count": 1,
                        "state": "驾驶侧车外双手为空",
                        "function": "完成代驾",
                    }],
                    "visible_figure_count": 2, "fallback": False,
                },
            },
        }
        driver_first = app.director._shot_payload(
            ctx, shot2, frame_kind="first_frame")
        driver_last = app.director._shot_payload(
            ctx, shot2, frame_kind="last_frame")
        assert driver_first["functional_figures"] == [{
            "name": "小吴", "count": 1,
            "state": "左前驾驶位双手握方向盘", "function": "代驾司机",
        }]
        assert driver_last["functional_figures"] == [{
            "name": "小吴", "count": 1,
            "state": "驾驶侧车外双手为空", "function": "完成代驾",
        }]
        assert driver_first["visible_figure_count"] == 2
        assert driver_last["visible_figure_count"] == 2
        assert driver_first["composition_contract"][
            "expected_visible_figure_count"] == 2
        assert "解开安全带" not in driver_first["prompt_compact"]
        assert "站在驾驶侧车外" not in driver_first["prompt_compact"]
        assert "双手控制方向盘" not in driver_last["prompt_compact"]
    finally:
        app.close()


def test_cover_is_final_high_and_carries_locked_portraits(tmp_path):
    calls = []

    class Router:
        def call(self, capability, payload, out_dir):
            calls.append((capability, payload, out_dir))
            return ProviderResult(
                provider="codex", cost=0, uri=str(tmp_path / "c.png"))

    identity = tmp_path / "林昭.png"
    identity.write_bytes(b"x")
    OpsCenter(Router()).make_cover(
        {"project_title": "测试剧", "episode_number": 1, "logline": "危机"},
        tmp_path, identity_references=[{
            "character": "林昭", "uri": str(identity), "version": 1,
        }])
    capability, payload, _ = calls[0]
    assert capability == "cover"
    assert payload["image_task_class"] == "final"
    assert payload["image_quality"] == "high"
    assert payload["require_reference_images"] is True
    assert payload["character_refs"] == [str(identity)]

    instruction, _targets, _data = build_instruction(
        "cover", payload, tmp_path)
    assert "人工锁定最终立绘" in instruction
    assert "允许与人物参考图服装不同" in instruction
    assert str(identity) in instruction


def test_shot_payload_repairs_story_logic_before_provider_input(tmp_path):
    app = App(tmp_path / "ws")
    try:
        project, _ = app.projects.get_or_create_project(
            "大明合同修正", style="电影级明代历史漫剧")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        for name in ("沈砚", "陈允"):
            identity = tmp_path / f"{name}.png"
            identity.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
            app.assets.register(
                project["id"], "character_identity", name,
                uri=str(identity),
                meta={"locked": True, "character": name})
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "script": {
                "story_world": {
                    "name": "洪武大明",
                    "era_and_location": "明初清河驿馆",
                    "hard_rules": "服化道严格符合洪武年间",
                    "sanctioned_anachronisms": [],
                },
                    "characters": [
                        {"name": "陈允", "gender": "男性",
                         "age_range": "40至50岁"},
                        {"name": "沈砚", "gender": "男性",
                         "age_range": "20至25岁"},
                    ],
                "scenes": [{
                    "scene_no": 1,
                    "location": "赴任途中·驿馆内室",
                }],
            },
            "storyboard": {"shots": []},
            "blocking": {},
            "aspect": "9:16",
            "dims": {"width": 1080, "height": 1920},
        }
        shot = {
            "shot_no": 1,
            "scene_no": 1,
            "unit_id": "U01",
            "prompt": "沈砚布旅装跪坐榻前，榻边搭青官袍",
            "description": (
                "沈砚布旅装跪坐榻前给陈允喂水，"
                "榻边搭青官袍，几上油灯投暖光"),
            "characters": ["陈允", "沈砚"],
            "character_count": 2,
            "camera": "中景固定",
            "dialogue": None,
            "start_state": {
                "陈允": {"pose": "卧床", "wardrobe": "旧中衣"},
                "沈砚": {"pose": "跪坐", "wardrobe": "宽松青官袍"},
            },
            "end_state": {
                "陈允": {"pose": "卧床", "wardrobe": "旧中衣"},
                "沈砚": {"pose": "跪坐", "wardrobe": "宽松青官袍"},
            },
            "appearance_state_required": True,
                "five_dimensions": {},
                "readable_text": {"required": False},
                "frame_targets": {"keyframe": {
                    "phase": "freeze",
                    "state": "沈砚穿布旅装跪坐榻前给卧床的陈允喂水",
                    "fallback": False,
                }},
            }

        payload = app.director._shot_payload(ctx, shot)

        assert payload["start_state"]["沈砚"]["wardrobe"] == "布旅装"
        assert payload["end_state"]["沈砚"]["wardrobe"] == "布旅装"
        assert len(payload["semantic_corrections"]) == 2
        assert payload["prompt_contract_validation"]["passed"] is True
        assert "开放式浅盏油灯" in payload["prompt_compact"]
        assert "玻璃灯罩" in payload["prompt_compact"]
        assert "煤油灯筒" in payload["prompt_compact"]
    finally:
        app.close()
