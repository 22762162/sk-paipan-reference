"""素材上传/下载测试:图片与视频人工修改后导入。"""

import base64
import http.client
import json
import threading
from pathlib import Path

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.errors import AifosError
from aifos.web.server import serve

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4 = b"\x00\x00\x00 ftypisom" + b"\x00" * 32


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _to_preflight(app, title="万妖图录", number=1, finish=False):
    summary = app.director.produce(
        title, number, auto_select_assets=True)
    assert summary["status"] == "done"
    return summary


def test_import_character_art(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    app.director.produce("万妖图录", 1, pause_for_confirm=True)  # 剧本确认
    project = app.projects.get_project("万妖图录")
    episode = app.db.query_one(
        "SELECT * FROM episodes WHERE project_id=? AND number=1",
        (project["id"],))
    script, _ = app.projects.latest_document(episode["id"], "script")
    name = script["characters"][0]["name"]
    result = app.director.import_image(
        "万妖图录", 1, {"kind": "character_art", "name": name}, PNG, ".png")
    row = app.assets.latest(project["id"], "character_art", name)
    assert row["version"] == 1
    assert json.loads(row["meta"])["uploaded"] is True
    identity = app.assets.latest(project["id"], "character_identity", name)
    assert json.loads(identity["meta"])["locked"] is True
    assert Path(result["uri"]).read_bytes() == PNG


def test_import_shot_image_invalidates_video(app):
    _to_preflight(app, "万妖图录", 2, finish=True)
    project = app.projects.get_project("万妖图录")
    assert app.assets.latest(project["id"], "video", "e002_shot001")
    frames_v1 = app.assets.latest(
        project["id"], "first_frame", "e002_shot001")["version"]
    app.director.import_image(
        "万妖图录", 2, {"kind": "shot", "shot_no": 1}, PNG, ".png")
    assert app.assets.latest(project["id"], "video", "e002_shot001") is None
    assert app.assets.latest(
        project["id"], "first_frame",
        "e002_shot001")["version"] == frames_v1 + 1
    image = app.assets.latest(project["id"], "image", "e002_shot001")
    assert image["uri"].endswith(".upload.png")


def test_import_video(app):
    _to_preflight(app, "万妖图录", 3, finish=True)
    project = app.projects.get_project("万妖图录")
    result = app.director.import_video("万妖图录", 3, 1, MP4)
    row = app.assets.latest(project["id"], "video", "e003_shot001")
    assert row["uri"] == result["uri"]
    assert Path(result["uri"]).read_bytes() == MP4


def test_import_rejects_bad_content(app):
    app.director.produce("万妖图录", 1, pause_for_confirm=True)
    app.director.produce("万妖图录", 1, pause_for_confirm=True)  # 剧本确认
    with pytest.raises(AifosError):
        app.director.import_image(
            "万妖图录", 1, {"kind": "shot", "shot_no": 1},
            b"not an image", ".png")
    with pytest.raises(AifosError):
        app.director.import_video("万妖图录", 1, 1, b"not video")
    with pytest.raises(AifosError):
        app.director.import_image(
            "万妖图录", 1, {"kind": "shot", "shot_no": 1}, PNG, ".exe")


def test_web_upload_endpoint(tmp_path):
    ws = tmp_path / "ws"
    boot = App(ws)
    _to_preflight(boot)
    boot.close()
    httpd = serve(ws, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=30)
        payload = json.dumps({
            "episode_id": 1,
            "target": {"kind": "shot", "shot_no": 1},
            "filename": "edited.png",
            "data_base64": base64.b64encode(PNG).decode(),
        })
        conn.request("POST", "/api/upload", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        reply = json.loads(resp.read())
        assert resp.status == 200, reply
        assert reply["uri"].endswith(".upload.png")
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_import(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "1"]) == 0
    boot = App(ws)
    try:
        _to_preflight(boot)
    finally:
        boot.close()
    art = tmp_path / "edited.png"
    art.write_bytes(PNG)
    capsys.readouterr()
    assert main(["--workspace", ws, "import", "--project", "万妖图录",
                 "--episode", "1", "--shot", "2",
                 "--file", str(art)]) == 0
    assert "已导入" in capsys.readouterr().out
    video = tmp_path / "cut.mp4"
    video.write_bytes(MP4)
    assert main(["--workspace", ws, "import", "--project", "万妖图录",
                 "--episode", "1", "--shot", "2",
                 "--file", str(video)]) == 0
