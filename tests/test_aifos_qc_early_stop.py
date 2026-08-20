"""精修循环非收敛早停:同一质检问题签名连续重复时提前转入降级接收。

背景:修复循环原来只有 10 轮上限;同一问题原地踏步时(如参考图冲突
每轮重复)会烧掉全部轮次才晋升相对最优稿。早停在第 3 次出现同一
签名时提前走同一条 _promote_relative_best_nonblocking 路径——行为与
十轮上限一致,只是不再浪费中间轮次。
"""

import pytest

from aifos.app import App
from aifos.director import Director


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def test_issue_signature_normalizes_wording():
    sig_a = Director._qc_issue_signature(
        ["图3的用途与内容冲突，需要移除", "腕带颜色为黑色而非红色"])
    sig_b = Director._qc_issue_signature(
        ["图 3 的用途与内容冲突、需要移除！", "腕带颜色为黑色而非红色。"])
    assert sig_a == sig_b
    assert sig_a

    other = Director._qc_issue_signature(["构图偏移了"])
    assert other != sig_a


def test_signature_empty_issues():
    assert Director._qc_issue_signature([]) == ""
    assert Director._qc_issue_signature(None) == ""


def _shot_task(app, signatures):
    project, _ = app.projects.get_or_create_project("早停测试")
    episode, _ = app.projects.get_or_create_episode(project["id"], 1)
    return {
        "item_id": "shot:8",
        "payload": {
            "shot_no": 8,
            "_episode_id": episode["id"],
            "_qc_issue_signatures": list(signatures),
        },
    }


def test_stagnation_detected_on_third_repeat(app):
    director = app.director
    task = _shot_task(app, ["甲问题", "乙问题", "甲问题"])
    assert director._qc_stagnation_rounds(task, "甲问题") == 2
    assert director._qc_stagnation_reached(task, "甲问题") is True
    assert director._qc_stagnation_reached(task, "丙问题") is False


def test_stagnation_not_reached_below_threshold(app):
    director = app.director
    task = _shot_task(app, ["甲问题"])
    assert director._qc_stagnation_reached(task, "甲问题") is False
    empty = _shot_task(app, [])
    assert director._qc_stagnation_reached(empty, "甲问题") is False


def test_stagnation_history_recording_is_capped(app):
    director = app.director
    task = _shot_task(app, [])
    for index in range(9):
        director._record_qc_issue_signature(task, f"问题{index}")
    stored = task["payload"]["_qc_issue_signatures"]
    assert len(stored) <= 6
    assert stored[-1] == "问题8"
