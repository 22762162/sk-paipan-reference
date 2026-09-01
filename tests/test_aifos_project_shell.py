"""资产中心的作品名管理:自己起名新建、改名、删掉只剩空壳的死剧名。

删整集作品时项目壳会被刻意保留(承载跨集母资产),作品删光后这个壳就成了
下拉框里的死名字。这里覆盖:空壳可删、有剧集必拒、图片资产必须显式确认、
磁盘原图一律不动。
"""

import http.client
import json
import threading
from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.web.server import serve

STATIC = Path(__file__).parents[1] / "aifos" / "web" / "static"


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw)


# ---- 自己起名:不跑剧集也能建壳并存资产 ----

def test_custom_project_name_can_hold_studio_assets_without_any_episode(app):
    project, created = app.projects.get_or_create_project("我的资产库")
    assert created is True and project["title"] == "我的资产库"
    result = app.director.generate_studio_asset(
        "我的资产库", "character", "林晚", "冷艳短发女警,黑风衣,雨夜霓虹")
    assert Path(result["items"][0]["uri"]).exists()
    summary = app.history.project_shell_summary("我的资产库")
    assert summary["episode_count"] == 0
    assert summary["image_asset_count"] == 1
    assert summary["deletable"] is True


def test_creating_an_existing_name_reuses_it_instead_of_duplicating(app):
    app.projects.get_or_create_project("我的资产库")
    _project, created = app.projects.get_or_create_project("我的资产库")
    assert created is False
    assert len(app.projects.list_projects()) == 1


# ---- 删剧名 ----

def test_project_with_episodes_is_refused_with_actionable_message(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    with pytest.raises(AifosError) as excinfo:
        app.history.delete_project("万妖图录", delete_assets=True)
    message = str(excinfo.value)
    assert "还有 1 集作品记录" in message and "第1集" in message
    assert "历史记录" in message          # 明确告诉用户下一步去哪
    assert app.projects.get_project("万妖图录") is not None


def test_shell_with_images_requires_explicit_confirmation(app):
    app.projects.get_or_create_project("我的资产库")
    app.director.generate_studio_asset(
        "我的资产库", "character", "林晚", "冷艳短发女警,黑风衣,雨夜霓虹")
    with pytest.raises(AifosError) as excinfo:
        app.history.delete_project("我的资产库")
    assert "1 张图片资产" in str(excinfo.value)
    assert app.projects.get_project("我的资产库") is not None


def test_deleting_shell_clears_records_but_never_touches_files(app):
    app.projects.get_or_create_project("我的资产库")
    created = app.director.generate_studio_asset(
        "我的资产库", "character", "林晚", "冷艳短发女警,黑风衣,雨夜霓虹")
    image = Path(created["items"][0]["uri"])
    result = app.history.delete_project("我的资产库", delete_assets=True)
    assert result["deleted_project"] == "我的资产库"
    assert result["assets_removed"] == 1
    assert result["asset_files_preserved"] is True
    assert image.exists()                      # 磁盘原图一张都不能少
    assert app.projects.get_project("我的资产库") is None
    assert app.projects.list_projects() == []
    assert app.db.query("SELECT * FROM assets") == []


def test_empty_shell_left_by_deleting_the_last_work_becomes_deletable(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    episode = app.db.query_one("SELECT id FROM episodes LIMIT 1")
    app.history.delete_episode_work(episode["id"], delete_assets=False)
    # 删作品刻意保留项目壳 —— 这正是下拉框里的死名字来源
    assert app.projects.get_project("万妖图录") is not None
    summary = app.history.project_shell_summary("万妖图录")
    assert summary["episode_count"] == 0 and summary["deletable"] is True
    app.history.delete_project("万妖图录", delete_assets=True)
    assert app.projects.get_project("万妖图录") is None


def test_delete_unknown_project_reports_clearly(app):
    with pytest.raises(AifosError) as excinfo:
        app.history.delete_project("查无此剧")
    assert "作品不存在" in str(excinfo.value)


def test_shell_summary_of_unknown_project_is_none(app):
    assert app.history.project_shell_summary("查无此剧") is None


# ---- Web API ----

def test_project_shell_web_endpoints(tmp_path):
    workspace = tmp_path / "workspace"
    instance = App(workspace)
    instance.director.produce("万妖图录", 1, pause_for_confirm=True)
    instance.close()
    server = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, created = _request(port, "POST", "/api/project/create",
                                   {"title": "我的资产库"})
        assert status == 201 and created["created"] is True

        status, again = _request(port, "POST", "/api/project/create",
                                 {"title": "我的资产库"})
        assert status == 201 and again["created"] is False

        status, blank = _request(port, "POST", "/api/project/create",
                                 {"title": "   "})
        assert status == 400 and "作品名" in blank["error"]

        status, shell = _request(
            port, "GET",
            "/api/project/shell?project=%E6%88%91%E7%9A%84%E8%B5%84%E4%BA%A7"
            "%E5%BA%93")
        assert status == 200
        assert shell["episode_count"] == 0 and shell["deletable"] is True

        # 有剧集的作品必须被拒绝，且理由能直接指导下一步
        status, refused = _request(port, "POST", "/api/project/delete", {
            "title": "万妖图录", "delete_assets": True})
        assert status == 400
        assert "作品记录" in refused["error"]

        status, gone = _request(port, "POST", "/api/project/delete", {
            "title": "我的资产库", "delete_assets": True})
        assert status == 200 and gone["deleted_project"] == "我的资产库"

        status, missing = _request(port, "POST", "/api/project/delete",
                                   {"title": "我的资产库"})
        assert status == 400 and "作品不存在" in missing["error"]

        status, overview = _request(port, "GET", "/api/overview")
        assert status == 200
        assert "我的资产库" not in [p["title"] for p in overview["projects"]]
    finally:
        server.shutdown()
        server.server_close()


def test_project_rename_still_keeps_assets(tmp_path):
    workspace = tmp_path / "workspace"
    instance = App(workspace)
    instance.projects.get_or_create_project("旧名字")
    created = instance.director.generate_studio_asset(
        "旧名字", "character", "林晚", "冷艳短发女警,黑风衣,雨夜霓虹")
    asset_id = created["items"][0]["asset_id"]
    instance.close()
    server = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, renamed = _request(port, "POST", "/api/project/rename", {
            "title": "旧名字", "new_title": "新名字"})
        assert status == 200 and renamed["title"] == "新名字"
        status, catalog = _request(
            port, "GET",
            "/api/asset-images?project=%E6%96%B0%E5%90%8D%E5%AD%97")
        assert status == 200
        assert asset_id in [item["asset_id"] for item in catalog["items"]]
    finally:
        server.shutdown()
        server.server_close()


# ---- 前端契约 ----

def test_project_shell_ui_contract():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert 'id="project-new"' in js
    assert 'id="project-rename"' in js
    assert 'id="project-delete"' in js
    assert "/api/project/create" in js
    assert "/api/project/delete" in js
    assert "/api/project/shell?project=" in js
    assert "bindProjectShellControls(title)" in js
    # 空作品列表时也要能自己起名建库，不能只叫用户去生产总览
    assert 'bindProjectShellControls("")' in js
    assert "project-shell-blocked" in js      # 有剧集时给出可执行的拒绝说明
    assert ".project-shell-btn" in css
    assert ".project-shell-blocked" in css
