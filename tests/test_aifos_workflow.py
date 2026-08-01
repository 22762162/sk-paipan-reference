"""SK 漫剧 V5 工业流契约与真实交付复核。"""

import json
import subprocess
import sys
from pathlib import Path

from aifos.app import App
from aifos.web.server import _episode_payload


def test_five_dimension_preflight_and_delivery(tmp_path):
    app = App(tmp_path / "ws")
    try:
        paused = app.director.produce(
            "逆光成团", 1, premise="女团第一次直播前的后台危机",
            pause_for_confirm=True)
        assert paused["status"] == "awaiting_script"
        paused = app.director.produce(
            "逆光成团", 1, pause_for_confirm=True)  # 剧本确认
        assert paused["status"] == "awaiting_cast"
        project = app.projects.get_project("逆光成团")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                "逆光成团", 1, character["name"], 1)
        paused = app.director.produce(
            "逆光成团", 1, pause_for_confirm=True)
        assert paused["status"] == "awaiting_confirm"

        continuity, _ = app.projects.latest_document(
            episode["id"], "continuity")
        storyboard, _ = app.projects.latest_document(
            episode["id"], "storyboard")
        preflight, _ = app.projects.latest_document(
            episode["id"], "preflight")

        profile = storyboard["profile"]
        assert profile == continuity["production_profile"]
        assert profile["video_model"] == "seedance2.0fast_vip"
        assert profile["model_upgrade_policy"]["candidate_capability_key"] == (
            "seedance2_5")
        assert profile["model_upgrade_policy"]["reported_limits"] == {
            "max_material_assets": 40,
            "max_total_references": 50,
            "max_duration_seconds": 30,
        }
        assert profile["resolution"] == "720p"
        assert profile["voice"] == "jimeng_builtin"
        assert profile["lip_sync"] is True
        assert profile["burn_subtitles"] is False
        assert preflight["passed"]
        assert [gate["id"] for gate in preflight["gates"]] == [
            "script_bible", "character_assets", "continuity", "spatial",
            "spatial_seedance", "five_dimensions", "duration",
            "dialogue", "performance", "camera", "people", "text",
            "frames", "audio", "profile"]

        shots = storyboard["shots"]
        assert not ({"reaction", "beat"} & {shot["kind"] for shot in shots})
        assert all(8 <= shot["duration"] <= 15 for shot in shots)
        assert all(
            [beat["phase"] for beat in shot["temporal_beats"]]
            == ["setup", "main", "settle"]
            for shot in shots)
        assert all(shot["long_take_contract"]["enabled"] for shot in shots)
        assert all(shot["duration"] * 2 == int(shot["duration"] * 2)
                   for shot in shots)
        assert all(shot["character_count"] == len(shot["characters"])
                   for shot in shots)
        assert all(shot["start_state"] and shot["end_state"]
                   and shot["five_dimensions"] and shot["seedance_prompt"]
                   for shot in shots)

        out = Path(paused["artifacts_dir"])
        first_keyframe = out / "images" / "shot_001.keyframe.svg"
        # Mock 预览也遵守正式产线：镜头画面无名牌、说明文字和对白字幕。
        assert "<text" not in first_keyframe.read_text(encoding="utf-8")

        done = app.director.produce("逆光成团", 1)
        assert done["status"] == "done"
        first_video = json.loads(
            (out / "videos" / "shot_001.video.json").read_text(
                encoding="utf-8"))
        assert first_video["voice"] == "jimeng_builtin"
        assert first_video["lip_sync"] is True
        assert first_video["forbid_subtitles"] is True

        draft = json.loads(
            (out / "edit" / "draft_content.json").read_text(
                encoding="utf-8"))
        assert draft["tracks"]["audio"] == []
        assert draft["tracks"]["subtitle"] == []

        qc = json.loads((out / "qc_report.json").read_text(encoding="utf-8"))
        assert qc["passed"] and qc["technical_passed"]
        assert qc["content_passed"]
        assert qc["delivery_check"]["passed"]
        assert qc["delivery_check"]["executed"]
        verifier = out / "check-delivery.py"
        rerun = subprocess.run(
            [sys.executable, str(verifier)], capture_output=True, text=True)
        assert rerun.returncode == 0

        payload = _episode_payload(app, episode["id"])
        assert payload["continuity"]
        assert payload["blocking"]["validation"]["passed"]
        assert payload["blocking"]["scenes"][0]["svg_url"].startswith(
            "/artifacts/")
        assert payload["preflight"]["passed"]
        assert payload["content_review"]["passed"]
        assert payload["artifacts"]["review_board"]
    finally:
        app.close()


def test_enrich_tolerates_loose_ai_storyboard(tmp_path):
    """真实编剧模型的宽松分镜产出不再崩溃:
    dialogue 字符串/characters 单名/camera 对象/duration 带单位/整镜字符串。"""
    from aifos.workflow import (build_continuity_bible, enrich_storyboard,
                                production_profile)

    script = {
        "project_title": "容错测试", "episode_number": 1,
        "episode_title": "T", "logline": "L",
        "characters": [{"name": "周鹿", "role": "主角"}],
        "scenes": [{"scene_no": 1, "location": "夜市",
                    "characters": ["周鹿"], "action": "追查线索",
                    "lines": [{"character": "周鹿",
                               "dialogue": "跟上"}]}],
    }
    app = App(tmp_path / "ws")
    try:
        profile = production_profile(app.config, app.standards.active())
    finally:
        app.close()
    continuity = build_continuity_bible(
        {"title": "容错测试", "style": ""}, script, profile)
    loose = {"shots": [
        "开场空镜一句话描述",                            # 整镜是字符串
        {"scene_no": "1", "dialogue": "直接一句台词",     # 台词是字符串
         "characters": "周鹿",                          # 单名而非列表
         "camera": {"scale": "近景"},                    # 镜头是对象
         "duration": "2.5"},                            # 数字字符串
        {"scene_no": 1, "dialogue": {"character": "周鹿",
                                     "dialogue": ""}},   # 空台词
        12345,                                           # 非法条目
    ]}
    storyboard = enrich_storyboard(script, loose, continuity, profile)
    shots = storyboard["shots"]
    assert shots, "宽松产出应被归一化而不是崩溃"
    talk = next(s for s in shots
                if (s.get("dialogue") or {}).get("dialogue") == "直接一句台词")
    assert talk["dialogue"]["character"] == "周鹿"
    assert talk["characters"] == ["周鹿"]
    assert all(isinstance(s.get("camera_plan", s.get("camera")), (dict, str))
               for s in shots)
    assert all(s["seedance_prompt"] for s in shots)


def test_environment_shot_remains_strictly_empty(tmp_path):
    """空镜不再从场次人物表擅自补入第一名角色。"""
    from aifos.workflow import (build_continuity_bible, enrich_storyboard,
                                production_profile)

    script = {
        "project_title": "空镜测试",
        "episode_number": 1,
        "episode_title": "T",
        "logline": "L",
        "characters": [{"name": "程沐", "role": "主角", "gender": "女"}],
        "scenes": [{
            "scene_no": 1,
            "location": "清晨办公室",
            "characters": ["程沐"],
            "action": "窗帘被风吹动",
            "lines": [],
        }],
    }
    app = App(tmp_path / "ws")
    try:
        profile = production_profile(app.config, app.standards.active())
    finally:
        app.close()
    continuity = build_continuity_bible(
        {"title": "空镜测试", "style": ""}, script, profile)
    storyboard = enrich_storyboard(script, {"shots": [{
        "scene_no": 1,
        "kind": "environment",
        "characters": [],
        "description": "窗帘被风吹动，桌面文件轻响",
    }]}, continuity, profile)
    shot = storyboard["shots"][0]
    assert shot["characters"] == []
    assert shot["character_count"] == 0
    assert "严格共0人" in shot["seedance_prompt"]
    assert "无人空镜" in shot["seedance_prompt"]


def test_saved_storyboard_repairs_official_uniform_continuity_without_rewrite():
    from aifos.workflow import repair_storyboard_appearance_continuity

    continuity = {
        "characters": [{
            "name": "沈砚", "default_position": "画面中",
            "default_wardrobe": "旧灰长衫", "signature_prop": "袖中鱼符",
        }],
    }
    storyboard = {"shots": [
        {
            "shot_no": 7, "scene_no": 2, "characters": ["沈砚"],
            "description": "沈砚青官袍乌纱下轿立定",
            "prompt": "沈砚青官袍乌纱略不合身",
            "start_state": {"沈砚": {"pose": "立定"}},
            "end_state": {"沈砚": {"pose": "立定"}},
        },
        {
            "shot_no": 8, "scene_no": 2, "characters": ["沈砚"],
            "description": "沈砚侧背微颔首",
            "prompt": "沈砚青官袍侧背前景",
            "start_state": {"沈砚": {"pose": "立定"}},
            "end_state": {"沈砚": {"pose": "立定微颔首"}},
        },
        {
            "shot_no": 9, "scene_no": 2, "characters": ["沈砚"],
            "description": "沈砚略顿",
            "prompt": "沈砚略顿，冷天光压抑",
            "start_state": {"沈砚": {"pose": "立定"}},
            "end_state": {"沈砚": {"pose": "手将探袖"}},
        },
        {
            "shot_no": 10, "scene_no": 2, "characters": ["沈砚"],
            "description": "沈砚身穿旧月白直裰继续答话",
            "prompt": "沈砚旧月白直裰正面",
            "start_state": {"沈砚": {"pose": "立定"}},
            "end_state": {"沈砚": {"pose": "立定"}},
        },
    ]}

    repaired = repair_storyboard_appearance_continuity(
        storyboard, continuity)
    shots = repaired["shots"]
    expected = shots[0]["end_state"]["沈砚"]["wardrobe"]
    assert "青官袍" in expected
    assert shots[1]["start_state"]["沈砚"]["wardrobe"] == expected
    assert shots[2]["end_state"]["沈砚"]["wardrobe"] == expected
    assert shots[3]["end_state"]["沈砚"]["wardrobe"] == expected
    assert shots[3]["appearance_continuity_issues"]
    assert "服装" + expected in shots[2]["seedance_prompt_compact"]


def test_current_shot_wardrobe_fact_repairs_leaked_global_costume():
    from aifos.workflow import reconcile_shot_semantics

    repaired = reconcile_shot_semantics({
        "shot_no": 1,
        "characters": ["沈砚", "陈允"],
        "description": (
            "沈砚布旅装跪坐床前给陈允喂水，"
            "榻边搭着崭新青官袍"),
        "start_state": {
            "沈砚": {"pose": "跪坐", "wardrobe": "宽松青官袍"},
            "陈允": {"pose": "卧床", "wardrobe": "中衣"},
        },
        "end_state": {
            "沈砚": {"pose": "跪坐", "wardrobe": "宽松青官袍"},
            "陈允": {"pose": "卧床", "wardrobe": "中衣"},
        },
    })

    assert repaired["start_state"]["沈砚"]["wardrobe"] == "布旅装"
    assert repaired["end_state"]["沈砚"]["wardrobe"] == "布旅装"
    assert len(repaired["semantic_corrections"]) == 2
    assert {
        item["field"] for item in repaired["semantic_corrections"]
    } == {"start_state.wardrobe", "end_state.wardrobe"}
    assert all(
        item["from"] == "宽松青官袍"
        and item["to"] == "布旅装"
        for item in repaired["semantic_corrections"])


def test_scene_beat_never_makes_dead_character_breathe():
    from aifos.workflow import _append_performance_beats

    script = {
        "characters": [
            {"name": "陈允"},
            {"name": "沈砚"},
        ],
        "scenes": [{
            "scene_no": 1,
            "location": "清河驿馆",
            "characters": ["陈允", "沈砚"],
        }],
    }
    shots = _append_performance_beats([{
        "scene_no": 1,
        "kind": "physical",
        "characters": ["陈允", "沈砚"],
        "description": "沈砚看着陈允咽气，身体僵住",
        "end_state": {
            "陈允": {"pose": "尸身静卧", "injury": "已咽气"},
            "沈砚": {"pose": "跪坐僵住"},
        },
    }], script, {
        "performance": {
            "beat_at_emotional_peak": True,
            "beat_seconds": [2, 4],
        },
    })

    beat = shots[-1]
    assert beat["kind"] == "beat"
    assert beat["characters"] == ["沈砚"]
    assert "沈砚" in beat["description"]
    assert "陈允" not in beat["description"]


def test_long_take_policy_folds_setup_and_reaction_into_dialogue_shots():
    from aifos.workflow import _append_performance_beats

    script = {"scenes": [{
        "scene_no": 1,
        "characters": ["甲", "乙"],
        "location": "县衙签押房",
    }]}
    raw = [
        {"scene_no": 1, "kind": "environment", "duration": 4,
         "description": "晨光压在案卷和官凭上", "characters": []},
        {"scene_no": 1, "kind": "dialogue", "duration": 6,
         "description": "甲把官凭推到案中", "characters": ["甲", "乙"],
         "dialogue": {"character": "甲", "dialogue": "请验官凭。"}},
        {"scene_no": 1, "kind": "dialogue", "duration": 6,
         "description": "乙抬眼发问", "characters": ["甲", "乙"],
         "dialogue": {"character": "乙", "dialogue": "你惯用哪只手？"}},
    ]
    rules = {
        "production": {
            "preferred_segment_seconds": [8, 15],
            "long_take_policy": {
                "enabled": True,
                "preferred_seconds": [8, 15],
            },
        },
        "dialogue": {"split_at_natural_pause": True,
                     "max_chars_per_shot": 25},
        "performance": {
            "reaction_after_key_dialogue": False,
            "beat_at_emotional_peak": False,
            "physical_action_separate_shot": False,
        },
    }

    shots = _append_performance_beats(raw, script, rules)

    assert len(shots) == 2
    assert all(8 <= shot["duration"] <= 15 for shot in shots)
    assert "晨光压在案卷和官凭上" in shots[0]["description"]
    assert shots[0]["embedded_performance"]["listener_reaction"][
        "character"] == "乙"
    assert shots[1]["embedded_performance"]["listener_reaction"][
        "character"] == "甲"
    assert "emotional_settle" in shots[1]["embedded_performance"]
    assert not any(shot.get("kind") in ("reaction", "beat") for shot in shots)


def test_saved_dead_actor_beat_is_retargeted_without_changing_shot_number():
    from aifos.workflow import repair_storyboard_appearance_continuity

    continuity = {
        "characters": [
            {"name": "陈允", "default_wardrobe": "旧中衣"},
            {"name": "沈砚", "default_wardrobe": "布旅装"},
        ],
    }
    script = {
        "scenes": [{
            "scene_no": 1,
            "characters": ["陈允", "沈砚"],
        }],
    }
    storyboard = {
        "appearance_state_version": 1,
        "character_number_map": {
            "P01": {"actor_id": "P01", "name": "陈允"},
            "P02": {"actor_id": "P02", "name": "沈砚"},
        },
        "shots": [
            {
                "shot_no": 5,
                "scene_no": 1,
                "kind": "physical",
                "characters": ["陈允", "沈砚"],
                "description": "陈允咽气，沈砚僵住",
                "start_state": {
                    "陈允": {"pose": "仰卧", "wardrobe": "旧中衣"},
                    "沈砚": {"pose": "跪坐", "wardrobe": "布旅装"},
                },
                "end_state": {
                    "陈允": {
                        "pose": "尸身态", "injury": "已咽气",
                        "wardrobe": "旧中衣",
                    },
                    "沈砚": {"pose": "跪坐僵住", "wardrobe": "布旅装"},
                },
            },
            {
                "shot_no": 6,
                "scene_no": 1,
                "kind": "beat",
                "characters": ["陈允"],
                "description": "陈允用呼吸、眼神和细微肢体完成情绪余波",
                "performance": {
                    "micro_expression": "陈允呼吸和眼神发生变化",
                },
                "start_state": {
                    "陈允": {
                        "pose": "尸身态", "injury": "已咽气",
                        "wardrobe": "旧中衣",
                    },
                },
                "end_state": {
                    "陈允": {
                        "pose": "完成呼吸变化", "wardrobe": "旧中衣",
                    },
                },
            },
        ],
    }

    repaired = repair_storyboard_appearance_continuity(
        storyboard, continuity, script)
    beat = repaired["shots"][1]

    assert beat["shot_no"] == 6
    assert beat["characters"] == ["沈砚"]
    assert beat["character_count"] == 1
    assert list(beat["character_number_map"]) == ["P02"]
    assert "沈砚" in beat["description"]
    assert "陈允用呼吸" not in beat["description"]
    assert any(
        "已死亡人物" in item["reason"]
        for item in beat["semantic_corrections"])
