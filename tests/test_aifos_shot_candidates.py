"""分镜画面按需多版:默认每镜1张，人工不满意时再生成 1-4 张备选。"""

import json

import pytest

from aifos.app import App
from aifos.auto_select import CANDIDATE_GROUP_SIZE
from aifos.errors import AifosError


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _to_preflight(app, title="万妖图录", number=1):
    app.director.produce(title, number, pause_for_confirm=True)
    summary = app.director.produce(title, number, pause_for_confirm=True)
    assert summary["status"] == "awaiting_confirm"
    project = app.projects.get_project(title)
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=?",
        (project["id"], number))
    return project, episode


def test_default_is_one_image_per_shot(app):
    """默认不给每镜都画4张:出图量等于镜头数,备选一张都不预生成。"""
    project, episode = _to_preflight(app)
    storyboard, _ = app.projects.latest_document(
        episode["id"], "storyboard")
    images = [row for row in app.assets.list(project["id"], "image")
              if row["uri"]]
    assert len(images) == len(storyboard["shots"])
    assert app.assets.list(project["id"], "shot_candidate") == []


def test_generate_two_candidates_on_demand_with_recommendation(app):
    project, episode = _to_preflight(app)
    result = app.director.generate_shot_candidates(
        "万妖图录", 1, 1, count=2, feedback="主角表情太平淡，眼神要更狠")
    assert result["created"] == 2
    assert len(result["candidates"]) == 2
    # 两张差异轴不同，且都带上了人工的修改意见
    assert len({item["variant_label"] for item in result["candidates"]}) == 2
    assert all(item["feedback"].startswith("主角表情太平淡")
               for item in result["candidates"])
    # CODEX 给推荐但不自动换图:本镜关键帧仍是原来那张
    assert result["recommendation"]["available"] is True
    assert result["recommended_index"] in (1, 2)
    status = app.director.shot_candidate_status(project["id"], 1, 1)
    assert status["recommended_index"] == result["recommended_index"]
    assert sum(1 for item in status["candidates"]
               if item["recommended"]) == 1
    assert not any(item["selected"] for item in status["candidates"])
    image = app.assets.latest(project["id"], "image", "e001_shot001")
    assert "from_shot_candidate" not in json.loads(image["meta"])


def test_candidate_prompt_keeps_the_shot_contract_locked(app):
    project, _episode = _to_preflight(app)
    app.director.generate_shot_candidates("万妖图录", 1, 1, count=4)
    row = app.assets.latest(
        project["id"], "shot_candidate", "e001_shot001:02")
    prompt = json.loads(row["meta"])["prompt"]
    assert "【本张备选差异轴】" in prompt
    assert "差异轴不得改动其中任何一项" in prompt


def test_selecting_a_candidate_replaces_the_keyframe_and_downstream(app):
    project, episode = _to_preflight(app)
    before = app.assets.latest(project["id"], "image", "e001_shot001")
    frames_before = app.assets.latest(
        project["id"], "first_frame", "e001_shot001")["version"]
    app.director.generate_shot_candidates("万妖图录", 1, 1, count=2)
    result = app.director.select_shot_candidate("万妖图录", 1, 1, 2)

    after = app.assets.latest(project["id"], "image", "e001_shot001")
    assert after["version"] == before["version"] + 1
    assert after["uri"] == result["uri"]
    meta = json.loads(after["meta"])
    assert meta["from_shot_candidate"] == 2
    assert meta["selection_source"] == "human"
    # 关键帧换了,同场首尾帧必须按新图重做
    assert app.assets.latest(
        project["id"], "first_frame",
        "e001_shot001")["version"] > frames_before
    status = app.director.shot_candidate_status(project["id"], 1, 1)
    assert [item["index"] for item in status["candidates"]
            if item["selected"]] == [2]


def test_count_is_bounded_to_one_through_four(app):
    _to_preflight(app)
    for bad in (0, 5, "很多"):
        with pytest.raises(AifosError, match="1-4"):
            app.director.generate_shot_candidates(
                "万妖图录", 1, 1, count=bad)


def test_unknown_shot_and_missing_candidate_are_refused(app):
    _to_preflight(app)
    with pytest.raises(AifosError, match="镜头不存在"):
        app.director.generate_shot_candidates("万妖图录", 1, 999, count=1)
    with pytest.raises(AifosError, match="没有第3张备选"):
        app.director.select_shot_candidate("万妖图录", 1, 1, 3)


def test_repeated_rounds_roll_through_the_four_slots(app):
    """连着两轮各要2张:占满四个槽位,不互相覆盖。"""
    project, _episode = _to_preflight(app)
    app.director.generate_shot_candidates("万妖图录", 1, 1, count=2)
    app.director.generate_shot_candidates("万妖图录", 1, 1, count=2)
    status = app.director.shot_candidate_status(project["id"], 1, 1)
    assert [item["index"] for item in status["candidates"]] == [
        1, 2, 3, 4][:CANDIDATE_GROUP_SIZE]


def test_selection_is_refused_while_the_episode_is_running(app):
    project, episode = _to_preflight(app)
    app.director.generate_shot_candidates("万妖图录", 1, 1, count=1)
    app.projects.set_episode_status(episode["id"], "images")
    with pytest.raises(AifosError, match="正在生产中"):
        app.director.select_shot_candidate("万妖图录", 1, 1, 1)
    app.projects.set_episode_status(episode["id"], "awaiting_confirm")
    assert app.director.select_shot_candidate(
        "万妖图录", 1, 1, 1)["candidate_index"] == 1
    assert project["id"]


def test_cli_alt_generates_then_picks(tmp_path, capsys):
    from aifos.cli import main

    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "produce", "--title", "万妖图录",
          "--episode", "1", "--review"])
    main(["--workspace", ws, "confirm", "--project", "万妖图录",
          "--episode", "1"])
    capsys.readouterr()
    assert main(["--workspace", ws, "alt", "--project", "万妖图录",
                 "--episode", "1", "--shot", "1", "--count", "2",
                 "--feedback", "眼神要更狠"]) == 0
    out = capsys.readouterr().out
    assert "已按需生成 2 张备选" in out
    assert "CODEX 推荐" in out
    assert main(["--workspace", ws, "alt", "--project", "万妖图录",
                 "--episode", "1", "--shot", "1", "--pick", "2"]) == 0
    out = capsys.readouterr().out
    assert "已定版备选 2" in out
    app = App(ws)
    try:
        project = app.projects.get_project("万妖图录")
        meta = json.loads(app.assets.latest(
            project["id"], "image", "e001_shot001")["meta"])
        assert meta["from_shot_candidate"] == 2
    finally:
        app.close()


def test_web_endpoints_generate_and_select(tmp_path):
    import threading
    from aifos.web.server import serve
    from tests.test_aifos_web import _json_request, _wait_job

    ws = tmp_path / "ws"
    instance = App(ws)
    try:
        _to_preflight(instance)
        episode = instance.db.query_one("SELECT id FROM episodes LIMIT 1")
        episode_id = episode["id"]
    finally:
        instance.close()
    httpd = serve(ws, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        status, bad = _json_request(port, "POST", "/api/shot/candidates", {
            "episode_id": episode_id, "shot_no": 1, "count": 9})
        assert status == 400 and "1-4" in bad["error"]
        status, reply = _json_request(port, "POST", "/api/shot/candidates", {
            "episode_id": episode_id, "shot_no": 1, "count": 2,
            "feedback": "光再暗一点"})
        assert status == 202 and reply["job_id"]
        job = _wait_job(port, reply["job_id"])
        assert job["status"] == "done", job
        status, detail = _json_request(
            port, "GET", f"/api/episode/{episode_id}")
        shot_candidates = detail["shot_candidates"]["1"]
        assert len(shot_candidates["candidates"]) == 2
        assert shot_candidates["recommended_index"] in (1, 2)
        assert all(item["url"].startswith("/artifacts/")
                   for item in shot_candidates["candidates"])
        status, picked = _json_request(port, "POST", "/api/shot/select", {
            "episode_id": episode_id, "shot_no": 1, "candidate_index": 1})
        assert status == 200
        assert picked["candidate_index"] == 1
        assert [item["index"] for item in picked["candidates"]
                if item["selected"]] == [1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_single_candidate_skips_the_selector(app):
    """只要1张时没有可比较对象:不调用评选,直接交人工看。"""
    project, _episode = _to_preflight(app)
    calls = []
    original = app.router.call

    def spy(capability, payload, out_dir, cancel=None):
        calls.append(capability)
        return original(capability, payload, out_dir, cancel=cancel)

    app.router.call = spy
    try:
        result = app.director.generate_shot_candidates(
            "万妖图录", 1, 1, count=1)
    finally:
        app.router.call = original
    assert "image_select" not in calls
    assert result["recommendation"]["available"] is False
    assert len(app.director.shot_candidate_status(
        project["id"], 1, 1)["candidates"]) == 1
