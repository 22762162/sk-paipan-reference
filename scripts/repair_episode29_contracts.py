#!/usr/bin/env python3
"""Repair the frozen Episode 29 storyboard contract without touching production.

The command is deliberately narrow and dry-run by default.  It accepts only
episode 29 whose latest storyboard is either the audited v13 baseline or the
v14 produced by this script.  ``--apply`` writes v14 through
``ProjectCenter.save_document``; ``--quarantine-assets`` additionally retires
the invalid frame assets without deleting their files.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aifos.app import App  # noqa: E402
from aifos.prompt_contract import (  # noqa: E402
    compile_shot_prompt,
    synchronize_shot_execution_contract,
)


EPISODE_ID = 29
SOURCE_VERSION = 13
TARGET_VERSION = 14
REPAIR_ID = "episode29-authoritative-phase-contracts-v1"
REASON = (
    "Episode 29 首尾/冻结状态与物理空间合同失配；旧帧不得继续作为生成参考"
)
LOCATIONS = {
    1: "酒店房间内·走廊",
    2: "轿车内·高速公路",
    3: "虞家别墅·虞寻欢卧室",
    4: "虞家别墅·虞寻欢卧室",
}
MODERN_EXECUTION_STYLE = (
    "2078年现代中国都市的超写实真人电影短剧；Sony FX3数字电影质感；"
    "真实皮肤、玻璃、拉丝金属、现代织物与现代室内材质；低饱和烟墨黑、"
    "冷灰蓝、象牙白与少量暖金；受控黑柔与浅景深；所有建筑、家具、灯具、"
    "车辆、服装和道具严格服从现代剧情，不改变剧本时代与地点"
)


class RepairError(RuntimeError):
    """The database is outside the one audited repair envelope."""


def _json_hash(value) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text_card(*, required, whitelist=(), carrier="手机屏幕", **extra):
    card = {
        "required": bool(required),
        "carrier": carrier,
        "whitelist": list(whitelist),
    }
    card.update(extra)
    return card


def _set_target(
        shot, key, phase, state, *, characters=None,
        functional_figures=None):
    targets = shot.setdefault("frame_targets", {})
    target = {"phase": phase, "state": state, "fallback": False}
    if characters is not None:
        invalid = [name for name in characters
                   if name not in (shot.get("characters") or [])]
        if invalid:
            raise RepairError(
                f"镜头{shot.get('shot_no')} {key}含未登记人物: {invalid}")
        target["characters"] = list(characters)
    if functional_figures is not None:
        target["functional_figures"] = copy.deepcopy(functional_figures)
    if characters is not None or functional_figures is not None:
        target["visible_figure_count"] = (
            len(characters or [])
            + sum(int(item.get("count") or 0)
                  for item in (functional_figures or [])))
    targets[key] = target
    if key == "keyframe":
        shot["frame_target"] = copy.deepcopy(targets[key])


def _state(shot, phase, name):
    states = shot.setdefault(f"{phase}_state", {})
    state = states.setdefault(name, {})
    if not isinstance(state, dict):
        raise RepairError(
            f"镜头{shot.get('shot_no')} {phase}_state.{name} 不是对象")
    return state


def _repair_shot1(shot):
    shot["phase_locations"] = {
        "start": "酒店房间内",
        "freeze": "酒店房间门外·现代走廊",
        "end": "酒店房间门外·现代走廊",
    }
    shot["phase_visibility"] = {
        "start": {"visible": ["虞寻歌"], "offscreen": ["柳争流"]},
        "freeze": {"visible": ["虞寻歌", "柳争流"]},
        "end": {"visible": ["虞寻歌", "柳争流"]},
    }
    _state(shot, "start", "虞寻歌").update({
        "position": "酒店房间内床中央偏右",
        "pose": "仰躺于酒店床垫，由床垫与枕头完整支撑",
        "direction": "面部朝上、闭眼",
        "prop": "右侧枕边放置同一部手机，双手未持物",
    })
    _state(shot, "start", "柳争流").update({
        "position": "关闭的酒店房门外走廊，首帧不可见",
        "pose": "门外站立等待，不与床位处于同一可见空间",
        "direction": "面向酒店房门",
    })
    _state(shot, "end", "虞寻歌").update({
        "position": "现代酒店走廊远端",
        "pose": "背向镜头走向电梯，双脚由走廊地面支撑",
        "direction": "背向柳争流、面向电梯",
        "prop": "双手空置",
    })
    _state(shot, "end", "柳争流").update({
        "position": "现代酒店房门外",
        "pose": "原地站立，一手持红酒瓶，另一手并持两个空酒杯",
        "direction": "注视虞寻歌背影",
    })
    # The saved v13 sentence hard-coded the start-only bedside phone into the
    # whole-take physical rule.  Static end-frame compilation quite correctly
    # kept physical rules, so that stale start fact leaked back into the
    # corridor provider prompt.  Keep this field phase-neutral; each target
    # state and frame_props carry the actual visible supports and props.
    shot["physical_logic"] = (
        "当前画面人物身体由床垫、地面或鞋底形成真实连续支撑；当前可见道具"
        "必须由手部或稳定表面自然支撑；房门、走廊和电梯的前后空间关系连续，"
        "不得漂浮、穿模、复制或跨越封闭边界")
    _set_target(
        shot, "first_frame", "start",
        "现代酒店房间内，虞寻歌独自仰躺在床垫与枕头上；房门关闭，门外人员不入画。右侧枕边手机亮屏显示2078年2月21日、23:00。",
        characters=["虞寻歌"],
    )
    shot["frame_targets"]["first_frame"]["location"] = "酒店房间内"
    _set_target(
        shot, "keyframe", "end",
        "现代酒店走廊离场终点：虞寻歌背向镜头走向远处电梯，柳争流留在房门外持红酒瓶和两个空酒杯；画面不出现床或手机。",
        characters=["虞寻歌", "柳争流"],
    )
    shot["frame_targets"]["keyframe"]["location"] = (
        "酒店房间外·现代走廊")
    _set_target(
        shot, "last_frame", "end",
        "现代酒店走廊离场终点：虞寻歌接近电梯，柳争流留在房门外审视她的背影。",
        characters=["虞寻歌", "柳争流"],
    )
    shot["frame_targets"]["last_frame"]["location"] = (
        "酒店房间外·现代走廊")
    shot["readable_text"] = {
        "phases": {
            "start": _text_card(
                required=True,
                whitelist=("2078年2月21日", "23:00"),
                carrier="手机锁屏",
                layout="日期在屏幕上半部，时间居中放大",
                style="现代无衬线冷白字",
            ),
            "freeze": _text_card(required=False),
            "end": _text_card(required=False),
        }
    }


def _repair_shot2(shot):
    driver_start = [{
        "name": "小吴", "count": 1,
        "state": "同一名小吴坐在左前驾驶位，双手自然握住方向盘",
        "function": "本镜司机；仅此一具真人身体",
    }]
    driver_end = [{
        "name": "小吴", "count": 1,
        "state": "同一名小吴站在驾驶侧车外，双手为空",
        "function": "同一名司机的终点状态；不是新增人物",
    }]
    topology = {
        "drive_side": "左舵",
        "front_row": {
            "driver": "左前驾驶位；完整独立座椅、完整靠背与头枕",
            "passenger": "右前副驾驶位；完整独立座椅、完整靠背与头枕",
            "center_console": "位于两张前排座椅之间",
            "steering_wheel": "方向盘仅位于左前驾驶位正前方",
        },
        "passenger_seatbelt": (
            "三点式安全带上锚位于右侧B柱，肩带从虞寻歌右肩斜跨胸口至"
            "左髋/中控侧锁扣，腰带横过髋部；不穿体、不悬空、不反向"
        ),
    }
    shot["vehicle_topology"] = topology
    shot["phase_locations"] = {
        "start": "行驶中的现代左舵轿车完整前排车厢",
        "freeze": "便利店外停稳的同一辆现代左舵轿车",
        "end": "便利店外停稳的同一辆现代左舵轿车",
    }
    _state(shot, "start", "虞寻歌").update({
        "position": "右前副驾驶独立座椅",
        "pose": "背部贴合副驾驶靠背，头枕位于头后，系好三点式安全带",
        "direction": "低头看右手手机屏幕",
    })
    _state(shot, "end", "虞寻歌").update({
        "position": "停稳轿车右前副驾驶独立座椅",
        "pose": "背部贴合靠背，头枕完整，三点式安全带保持系好，双手空置",
        "direction": "隔着驾驶侧车窗看向车外小吴",
        "prop": "双手空置",
    })
    _set_target(
        shot, "first_frame", "start",
        "完整现代左舵轿车前排：小吴坐左前驾驶位，方向盘仅在他正前方；虞寻歌坐右前副驾驶，左右座椅的完整靠背、头枕及中间中控均可辨。虞寻歌三点式安全带从右侧B柱跨右肩胸口扣向左髋中控侧，手机显示23:10。",
        characters=["虞寻歌"], functional_figures=driver_start,
    )
    _set_target(
        shot, "keyframe", "end",
        "便利店外停稳的同一辆现代左舵轿车，完整前排座椅、靠背、头枕、中控和左侧方向盘保持正确。虞寻歌系三点式安全带坐右前副驾驶且双手空置；驾驶位空置，小吴站在驾驶侧车外且双手为空。",
        characters=["虞寻歌"], functional_figures=driver_end,
    )
    _set_target(
        shot, "last_frame", "end",
        "停稳轿车内，虞寻歌仍系三点式安全带坐右前副驾驶并看向车外小吴；两人双手均为空，画面无可读手机文字。",
        characters=["虞寻歌"], functional_figures=driver_end,
    )
    shot["readable_text"] = {
        "phases": {
            "start": _text_card(
                required=True, whitelist=("23:10",), carrier="手机锁屏",
                layout="时间居中放大", style="现代无衬线冷白字",
            ),
            "freeze": _text_card(required=False),
            "end": _text_card(required=False),
        }
    }
    physical = str(shot.get("physical_logic") or "").rstrip("。；")
    shot["physical_logic"] = "；".join(filter(None, (
        physical,
        "现代左舵轿车的左前驾驶位与右前副驾驶位必须各自具备完整座椅、靠背和头枕，中控严格位于两座之间，方向盘只在左前驾驶位正前方",
        topology["passenger_seatbelt"],
    ))) + "。"


def _repair_shot3(shot):
    shot["phase_locations"] = {
        "start": "虞家别墅·虞寻欢卧室门外走廊",
        "freeze": "虞家别墅·虞寻欢卧室床边",
        "end": "虞家别墅·虞寻欢卧室床边",
    }
    shot["phase_visibility"] = {
        "start": {"visible": ["虞寻歌"], "offscreen": ["虞寻欢"]},
        "freeze": {"visible": ["虞寻歌", "虞寻欢"]},
        "end": {"visible": ["虞寻歌", "虞寻欢"]},
    }
    _state(shot, "start", "虞寻歌").update({
        "position": "虞寻欢卧室门外走廊，距门一步",
        "pose": "面向关闭的卧室门站立，左手持水杯、右手持白酒杯",
        "direction": "面向卧室门",
        "prop": "左手水杯、右手白酒杯，两个杯子彼此分开且受手掌支撑",
    })
    _state(shot, "start", "虞寻欢").update({
        "position": "卧室门内，首帧不可见",
        "pose": "尚未开门，不与走廊人物处于同一可见空间",
        "direction": "朝向卧室门",
        "prop": "无持物",
    })
    _state(shot, "end", "虞寻歌").update({
        "position": "卧室床左侧床边",
        "pose": "站在床边俯视虞寻欢，双手空置",
        "direction": "俯视床上虞寻欢",
        "prop": "双手空置",
    })
    _state(shot, "end", "虞寻欢").update({
        "position": "卧室床中央",
        "pose": "仰躺床垫，头枕枕头，四肢由床垫完整支撑",
        "direction": "面部朝上、闭眼",
        "prop": "无持物",
    })
    _set_target(
        shot, "first_frame", "start",
        "虞家别墅现代卧室门外走廊，虞寻歌独自站在关闭的房门外，左手持一杯水、右手持一杯白酒；关闭房门后方的房内空间与人物均不入画。",
        characters=["虞寻歌"],
    )
    # The authoritative whole-take location contains the off-screen brother's
    # name ("虞寻欢卧室").  The first-frame scene label must be neutral so his
    # name is not reintroduced into the static prompt after phase character
    # filtering has correctly removed his identity reference.
    shot["frame_targets"]["first_frame"]["location"] = (
        "虞家别墅·卧室门外走廊")
    _set_target(
        shot, "keyframe", "end",
        "现代卧室床边终点：虞寻欢闭眼仰躺在床垫与枕头上，四肢有完整支撑；虞寻歌站在床左侧，双手空置并俯视他。",
        characters=["虞寻歌", "虞寻欢"],
    )
    shot["frame_targets"]["keyframe"]["location"] = (
        "虞家别墅·虞寻欢卧室")
    _set_target(
        shot, "last_frame", "end",
        "现代卧室床边终点：虞寻欢持续昏迷并由床垫完整支撑，虞寻歌双手空置站在床边确认结果。",
        characters=["虞寻歌", "虞寻欢"],
    )
    shot["frame_targets"]["last_frame"]["location"] = (
        "虞家别墅·虞寻欢卧室")
    shot["readable_text"] = {
        "phases": {
            phase: _text_card(required=False)
            for phase in ("start", "freeze", "end")
        }
    }


def _repair_shot4(shot):
    freeze_state = (
        "同一间2078年现代卧室，虞寻歌坐在左侧沙发并双手持同一部深蓝"
        "手机，屏幕清晰显示SS级与盗神；她仍注视屏幕。右后方虞寻欢闭眼"
        "仰躺床垫且完全无主动动作。"
    )
    shot["freeze_state"] = freeze_state
    shot["phase_locations"] = {
        phase: "虞家别墅·虞寻欢卧室"
        for phase in ("start", "freeze", "end")
    }
    _state(shot, "start", "虞寻歌").update({
        "position": "卧室左侧沙发中央",
        "pose": "坐在沙发、双脚着地，双手持同一部深蓝手机",
        "direction": "低头看手机屏幕",
        "prop": "手机仅显示02:21:59，游戏入口尚未开启",
    })
    _state(shot, "end", "虞寻歌").update({
        "position": "卧室左侧沙发中央",
        "pose": "仍坐在沙发并双手持同一部手机",
        "direction": "视线离开手机，转看右后方床上的虞寻欢",
        "prop": "手机屏幕斜离摄影机，内容不可读",
    })
    _set_target(
        shot, "first_frame", "start",
        "现代卧室起点：虞寻歌坐在左侧沙发，双手持同一部手机，屏幕只显示02:21:59；右后方虞寻欢闭眼仰躺床垫。",
        characters=["虞寻歌", "虞寻欢"],
    )
    _set_target(
        shot, "keyframe", "freeze", freeze_state,
        characters=["虞寻歌", "虞寻欢"])
    _set_target(
        shot, "last_frame", "end",
        "现代卧室终点：虞寻歌仍双手持手机，但屏幕斜离摄影机且内容不可读；她的视线已经转向右后方床上持续昏迷的虞寻欢。",
        characters=["虞寻歌", "虞寻欢"],
    )
    shot["readable_text"] = {
        "phases": {
            "start": _text_card(
                required=True, whitelist=("02:21:59",),
                carrier="手机屏幕", layout="时间居中",
                style="现代无衬线冷白字",
            ),
            "freeze": _text_card(
                required=True, whitelist=("SS级", "盗神"),
                carrier="手机屏幕",
                layout="SS级位于天赋等级区，盗神位于天赋名称区",
                style="清晰现代游戏界面字形",
            ),
            "end": _text_card(
                required=False, carrier="背向摄影机的手机屏幕"),
        }
    }
    for item in shot.get("frame_props") or []:
        if not isinstance(item, dict) or item.get("prop_id") != "prop_game_phone_01":
            continue
        phase = item.get("phase")
        if phase == "start":
            item["physical_state"] = "同一部深蓝手机；屏幕仅显示02:21:59，游戏未开启"
        elif phase == "freeze":
            item["physical_state"] = "同一部深蓝手机；屏幕仅显示SS级与盗神"
            item["visibility"] = "visible"
        elif phase == "end":
            item["physical_state"] = "同一部深蓝手机；屏幕斜离摄影机，内容不可读"
            item["visibility"] = "visible"
            item["text_visibility"] = "unreadable"


REPAIRERS = {
    1: _repair_shot1,
    2: _repair_shot2,
    3: _repair_shot3,
    4: _repair_shot4,
}


def _shot_map(storyboard):
    shots = storyboard.get("shots") if isinstance(storyboard, dict) else None
    if not isinstance(shots, list):
        raise RepairError("storyboard.shots 缺失")
    result = {}
    for shot in shots:
        if not isinstance(shot, dict):
            raise RepairError("storyboard 包含非对象镜头")
        try:
            number = int(shot.get("shot_no"))
        except (TypeError, ValueError):
            raise RepairError("storyboard 包含无效 shot_no") from None
        if number in result:
            raise RepairError(f"storyboard 存在重复镜头 {number}")
        result[number] = shot
    if set(result) != set(LOCATIONS):
        raise RepairError(
            f"Episode 29 v13 镜头集合必须为1-4，实际为{sorted(result)}")
    return result


def before_assertions(storyboard):
    shots = _shot_map(storyboard)
    return {
        "shot_count_is_4": len(shots) == 4,
        "shot1_scene_was_poisoned": (
            (shots[1].get("prompt_contract") or {}).get("scene")
            == "明代宫殿内景"),
        "shot4_keyframe_was_end": (
            ((shots[4].get("frame_targets") or {}).get("keyframe") or {})
            .get("phase") == "end"),
        "shot4_text_was_cross_phase": set(
            (shots[4].get("readable_text") or {}).get("whitelist") or []
        ).issuperset({"02:21:59", "02:22:00"}),
    }


def after_assertions(storyboard):
    shots = _shot_map(storyboard)
    shot1_first = copy.deepcopy(shots[1])
    shot1_first["frame_kind"] = "first_frame"
    shot1_first_contract, shot1_first_prompt = compile_shot_prompt(
        shot1_first, location=shot1_first["location"], mode="image",
        references=[
            {"index": 1, "label": "虞寻歌身份图", "role": "identity",
             "character": "虞寻歌"},
            {"index": 2, "label": "柳争流身份图", "role": "identity",
             "character": "柳争流"},
        ])
    shot1_last = copy.deepcopy(shots[1])
    shot1_last["frame_kind"] = "last_frame"
    shot1_last_contract, shot1_last_prompt = compile_shot_prompt(
        shot1_last, location=shot1_last["location"], mode="image")
    shot3_first = copy.deepcopy(shots[3])
    shot3_target = shot3_first["frame_targets"]["first_frame"]
    shot3_first["frame_kind"] = "first_frame"
    shot3_contract, shot3_prompt = compile_shot_prompt(
        shot3_first, location=shot3_first["location"], mode="image",
        references=[
            {"index": 1, "label": "虞寻歌身份图", "role": "identity",
             "character": "虞寻歌"},
            {"index": 2, "label": "虞寻欢身份图", "role": "identity",
             "character": "虞寻欢"},
        ])
    shot3_subject = shot3_contract.get("subject") or {}
    shot3_actors = shot3_subject.get("actors") or []
    shot3_refs = shot3_contract.get("references") or []

    def actor_name(value):
        """Extract the registered name from phase-rich actor render lines."""
        text = str(value or "")
        return text.split("=", 1)[-1].split("（", 1)[0].strip()
    checks = {
        "all_locations_authoritative": all(
            shot.get("location") == LOCATIONS[number]
            and (shot.get("prompt_contract") or {}).get("scene")
            == LOCATIONS[number]
            for number, shot in shots.items()),
        "shot1_bed_corridor_separated": (
            "酒店房间内" in shots[1]["phase_locations"]["start"]
            and "走廊" in shots[1]["phase_locations"]["end"]
            and "床" in shots[1]["frame_targets"]["first_frame"]["state"]
            and "走廊" in shots[1]["frame_targets"]["last_frame"]["state"]
            and shots[1]["frame_targets"]["first_frame"]["characters"]
            == ["虞寻歌"]
            and shots[1]["frame_targets"]["keyframe"]["characters"]
            == ["虞寻歌", "柳争流"]
            and shots[1]["frame_targets"]["first_frame"]["location"]
            == "酒店房间内"
            and shots[1]["frame_targets"]["keyframe"]["location"]
            == "酒店房间外·现代走廊"
            and shots[1]["frame_targets"]["last_frame"]["location"]
            == "酒店房间外·现代走廊"),
        "shot1_static_contracts_are_phase_exact": (
            shot1_first_contract.get("scene") == "酒店房间内"
            and len(shot1_first_contract.get("subject", {}).get(
                "actors") or []) == 1
            and actor_name((shot1_first_contract.get("subject", {}).get(
                "actors") or [""])[0]) == "虞寻歌"
            and len(shot1_first_contract.get("references") or []) == 1
            and (shot1_first_contract.get("references") or [{}])[0].get(
                "character") == "虞寻歌"
            and "柳争流" not in shot1_first_prompt
            and shot1_last_contract.get("scene")
            == "酒店房间外·现代走廊"
            and "枕边" not in shot1_last_prompt
            and "取手机" not in shot1_last_prompt),
        "shot2_front_row_topology_locked": all(
            token in json.dumps(
                shots[2].get("vehicle_topology"), ensure_ascii=False)
            for token in ("左前驾驶位", "右前副驾驶位", "头枕", "中控", "方向盘", "三点式安全带")),
        "shot2_functional_driver_is_one_person": (
            shots[2]["frame_targets"]["first_frame"]["characters"]
            == ["虞寻歌"]
            and shots[2]["frame_targets"]["first_frame"]
            ["functional_figures"][0]["name"] == "小吴"
            and shots[2]["frame_targets"]["first_frame"]
            ["functional_figures"][0]["count"] == 1
            and "左前驾驶位" in shots[2]["frame_targets"]["first_frame"]
            ["functional_figures"][0]["state"]
            and "驾驶侧车外" in shots[2]["frame_targets"]["last_frame"]
            ["functional_figures"][0]["state"]
            and shots[2]["frame_targets"]["last_frame"]
            ["visible_figure_count"] == 2),
        "shot3_door_to_bed_separated": (
            "门外" in shots[3]["frame_targets"]["first_frame"]["state"]
            and "左手持一杯水" in shots[3]["frame_targets"]["first_frame"]["state"]
            and "床边" in shots[3]["frame_targets"]["last_frame"]["state"]
            and shots[3]["frame_targets"]["first_frame"]["characters"]
            == ["虞寻歌"]
            and shots[3]["frame_targets"]["first_frame"]
            ["visible_figure_count"] == 1
            and shots[3]["frame_targets"]["first_frame"]["location"]
            == "虞家别墅·卧室门外走廊"
            and shots[3]["frame_targets"]["keyframe"]["characters"]
            == ["虞寻歌", "虞寻欢"]
            and shots[3]["frame_targets"]["last_frame"]["characters"]
            == ["虞寻歌", "虞寻欢"]
            and shots[3]["frame_targets"]["keyframe"]["location"]
            == "虞家别墅·虞寻欢卧室"
            and shots[3]["frame_targets"]["last_frame"]["location"]
            == "虞家别墅·虞寻欢卧室"),
        "shot3_first_static_contract_is_single": (
            shot3_contract.get("scene") == "虞家别墅·卧室门外走廊"
            and shot3_subject.get("count") == 1
            and shot3_subject.get("visible_count") == 1
            and len(shot3_actors) == 1
            and actor_name(shot3_actors[0]) == "虞寻歌"
            and len(shot3_refs) == 1
            and shot3_refs[0].get("character") == "虞寻歌"
            and "虞寻欢" not in " ".join(str(item) for item in shot3_actors)
            and "虞寻欢" not in shot3_prompt
            and not any(token in shot3_prompt for token in (
                "碰杯", "饮尽", "失衡"))),
        "shot4_keyframe_is_freeze": (
            shots[4]["frame_targets"]["keyframe"]["phase"] == "freeze"
            and shots[4]["frame_target"]["phase"] == "freeze"),
        "shot4_text_is_phase_exact": (
            shots[4]["readable_text"]["phases"]["start"]["whitelist"]
            == ["02:21:59"]
            and shots[4]["readable_text"]["phases"]["freeze"]["whitelist"]
            == ["SS级", "盗神"]
            and not shots[4]["readable_text"]["phases"]["end"]["required"]),
        "shot4_end_screen_unreadable": (
            "不可读" in shots[4]["end_state"]["虞寻歌"]["prop"]
            and "虞寻欢" in shots[4]["end_state"]["虞寻歌"]["direction"]),
    }
    checks["passed"] = all(checks.values())
    return checks


def build_repaired_storyboard(storyboard):
    repaired = copy.deepcopy(storyboard)
    shots = _shot_map(repaired)
    for number, shot in shots.items():
        shot["location"] = LOCATIONS[number]
        shot["scene_location"] = LOCATIONS[number]
        REPAIRERS[number](shot)
        # Explicit location is set before compilation so no prose keyword,
        # including a negated "不得出现宫殿", can retarget the scene.
        synchronize_shot_execution_contract(
            shot, location=LOCATIONS[number], style=MODERN_EXECUTION_STYLE)
    repaired["repair_metadata"] = {
        "repair_id": REPAIR_ID,
        "episode_id": EPISODE_ID,
        "source_storyboard_version": SOURCE_VERSION,
        "target_storyboard_version": TARGET_VERSION,
        "authoritative_locations": LOCATIONS,
        "asset_quarantine_optional": True,
    }
    payload = copy.deepcopy(repaired)
    payload["repair_metadata"].pop("document_sha256", None)
    repaired["repair_metadata"]["document_sha256"] = _json_hash(payload)
    checks = after_assertions(repaired)
    if not checks["passed"]:
        raise RepairError(f"修复后断言失败: {checks}")
    return repaired


def _connect_read_only(workspace):
    db_path = Path(workspace).resolve() / "aifos.db"
    if not db_path.is_file():
        raise RepairError(f"数据库不存在: {db_path}")
    # ``immutable=1`` keeps a genuine dry-run from creating ``-shm``/``-wal``
    # sidecars.  It also works on macOS volumes where SQLite's read-only URI
    # otherwise still attempts to open a write-capable shared-memory file.
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_context(conn):
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id=?", (EPISODE_ID,)).fetchone()
    if episode is None:
        raise RepairError("只允许修复存在的 episode_id=29")
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (episode["project_id"],)
    ).fetchone()
    row = conn.execute(
        "SELECT * FROM documents WHERE episode_id=? AND kind='storyboard' "
        "ORDER BY version DESC LIMIT 1", (EPISODE_ID,),
    ).fetchone()
    if row is None:
        raise RepairError("Episode 29 没有 storyboard")
    storyboard = json.loads(row["content"])
    return dict(episode), dict(project), int(row["version"]), storyboard


def _asset_name(episode_number, shot_no):
    return f"e{int(episode_number):03d}_shot{int(shot_no):03d}"


def _row_meta(row):
    try:
        return json.loads((row or {}).get("meta") or "{}")
    except (TypeError, ValueError):
        return {}


def _latest_asset(conn, project_id, kind, name):
    row = conn.execute(
        "SELECT * FROM assets WHERE project_id=? AND kind=? AND name=? "
        "ORDER BY version DESC LIMIT 1", (project_id, kind, name),
    ).fetchone()
    return dict(row) if row is not None else None


def asset_plan(conn, workspace, episode, project):
    workspace = Path(workspace).resolve()
    artifacts = (workspace / "artifacts").resolve()
    quarantine_root = (
        artifacts / "quarantine" / "episode_029_contract_repair")
    items = []
    for shot_no in LOCATIONS:
        name = _asset_name(episode["number"], shot_no)
        for kind in ("first_frame", "last_frame"):
            items.append((kind, name, True))
    for shot_no in (2, 4):
        items.append(("image", _asset_name(episode["number"], shot_no), shot_no == 4))

    planned = []
    for kind, name, move_file in items:
        row = _latest_asset(conn, project["id"], kind, name)
        deleted = bool(row and _row_meta(row).get("deleted"))
        source = Path(row["uri"]).resolve() if row and row.get("uri") else None
        destination = None
        if move_file and source is not None:
            destination = quarantine_root / kind / name / source.name
        planned.append({
            "kind": kind,
            "name": name,
            "asset_id": int(row["id"]) if row else None,
            "active": bool(row and not deleted),
            "source": str(source) if source else "",
            "source_exists": bool(source and source.is_file()),
            "move": bool(move_file and row and not deleted and source and source.is_file()),
            "destination": str(destination) if destination else "",
            "destination_exists": bool(destination and destination.is_file()),
            "soft_delete": bool(row and not deleted),
        })

    # Episode 29 predates candidate-set registration.  Its historical
    # director fallback may still discover and re-register this deterministic
    # path even when the active ``image`` asset points at a candidate folder.
    # Quarantine the orphan separately; the registered shot4 image already
    # has its own tombstone above, so this file-only action must not create a
    # second tombstone.
    shot4_name = _asset_name(episode["number"], 4)
    canonical = (
        artifacts
        / f"p{int(project['id']):03d}"
        / f"e{int(episode['number']):03d}"
        / "images" / "shot_004.keyframe.png"
    ).resolve()
    registered_sources = {
        str(Path(item["source"]).resolve())
        for item in planned if item.get("source")
    }
    if str(canonical) not in registered_sources:
        destination = (
            quarantine_root / "image" / shot4_name
            / "canonical_fallback" / canonical.name)
        planned.append({
            "kind": "image_file_only",
            "name": f"{shot4_name}:canonical_fallback",
            "asset_id": None,
            "active": canonical.is_file(),
            "source": str(canonical),
            "source_exists": canonical.is_file(),
            "move": canonical.is_file(),
            "destination": str(destination),
            "destination_exists": destination.is_file(),
            "soft_delete": False,
            "file_only": True,
        })
    return planned


def _validate_move(item, workspace):
    if not item["move"]:
        return
    source = Path(item["source"]).resolve()
    artifacts = (Path(workspace).resolve() / "artifacts").resolve()
    try:
        source.relative_to(artifacts)
    except ValueError as exc:
        raise RepairError(f"拒绝移动 artifacts 目录外文件: {source}") from exc
    destination = Path(item["destination"])
    if destination.exists() and source.exists():
        if destination.read_bytes() != source.read_bytes():
            raise RepairError(f"隔离目标已存在且内容不同: {destination}")


def _apply_asset_plan(app, plan):
    moved = []
    for item in plan:
        _validate_move(item, app.workspace.root)
    for item in plan:
        if not item["move"]:
            continue
        source = Path(item["source"])
        destination = Path(item["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            source.unlink()
        else:
            shutil.move(str(source), str(destination))
        moved.append({"source": str(source), "destination": str(destination)})

    tombstones = []
    episode = app.projects.get_episode(EPISODE_ID)
    for item in plan:
        if not item["soft_delete"]:
            continue
        meta = {
            "physical_invalid": True,
            "reference_eligible": False,
            "reason": REASON,
            "repair_id": REPAIR_ID,
            "episode_id": EPISODE_ID,
            "episode_number": int(episode["number"]),
        }
        if item["destination"] and (
                item["move"] or item.get("destination_exists")):
            meta["quarantine_uri"] = item["destination"]
        row = app.assets.soft_delete(
            int(episode["project_id"]), item["kind"], item["name"], meta=meta)
        if row is not None:
            tombstones.append({
                "asset_id": int(row["id"]), "kind": item["kind"],
                "name": item["name"], "version": int(row["version"]),
            })
    return {"moved": moved, "tombstones": tombstones}


def _matching_v14(version, storyboard):
    marker = storyboard.get("repair_metadata") if isinstance(storyboard, dict) else {}
    return (
        version == TARGET_VERSION
        and isinstance(marker, dict)
        and marker.get("repair_id") == REPAIR_ID
        and marker.get("source_storyboard_version") == SOURCE_VERSION
    )


def run_repair(workspace, *, apply=False, quarantine_assets=False):
    workspace = Path(workspace).resolve()
    with _connect_read_only(workspace) as conn:
        episode, project, version, storyboard = _read_context(conn)
        if version == SOURCE_VERSION:
            before = before_assertions(storyboard)
            repaired = build_repaired_storyboard(storyboard)
            already_applied = False
        elif _matching_v14(version, storyboard):
            before = {"already_repaired_v14": True}
            repaired = storyboard
            already_applied = True
            checks = after_assertions(repaired)
            if not checks["passed"]:
                raise RepairError(f"现有v14修复标记存在但断言失败: {checks}")
        else:
            raise RepairError(
                f"只允许 storyboard v13 基线或本脚本生成的v14；当前为v{version}")
        plan = asset_plan(conn, workspace, episode, project)

    report = {
        "mode": "apply" if apply else "dry-run",
        "episode_id": EPISODE_ID,
        "source_version": version,
        "target_version": TARGET_VERSION,
        "already_applied": already_applied,
        "before_assertions": before,
        "after_assertions": after_assertions(repaired),
        "document_write": bool(apply and not already_applied),
        "asset_quarantine_requested": bool(quarantine_assets),
        "asset_plan": plan,
        "asset_result": {"moved": [], "tombstones": []},
    }
    if not apply:
        return report

    app = App(workspace)
    try:
        latest, latest_version = app.projects.latest_document(
            EPISODE_ID, "storyboard")
        if not already_applied:
            if latest_version != SOURCE_VERSION:
                raise RepairError(
                    f"应用前版本已变化：预期v13，实际v{latest_version}")
            written = app.projects.save_document(
                EPISODE_ID, "storyboard", repaired)
            if written != TARGET_VERSION:
                raise RepairError(f"预期写入v14，实际写入v{written}")
        elif not _matching_v14(latest_version, latest):
            raise RepairError("应用前v14标记已变化，拒绝继续")
        if quarantine_assets:
            report["asset_result"] = _apply_asset_plan(app, plan)
    finally:
        app.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=REPO_ROOT / "workspace",
        help="AIFOS workspace（默认是当前工作树 workspace）")
    parser.add_argument("--episode-id", type=int, default=EPISODE_ID)
    parser.add_argument("--source-version", type=int, default=SOURCE_VERSION)
    parser.add_argument(
        "--apply", action="store_true",
        help="实际通过 App API 保存 storyboard v14")
    parser.add_argument(
        "--quarantine-assets", action="store_true",
        help="列出/处理无效首尾帧及shot2/4关键图；实际处理仍需--apply")
    args = parser.parse_args(argv)
    if args.episode_id != EPISODE_ID:
        parser.error("该脚本只允许 --episode-id 29")
    if args.source_version != SOURCE_VERSION:
        parser.error("该脚本只允许 --source-version 13")
    try:
        report = run_repair(
            args.workspace, apply=args.apply,
            quarantine_assets=args.quarantine_assets)
    except RepairError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"status": "ok", **report},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
