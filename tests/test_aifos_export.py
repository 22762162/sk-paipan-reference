"""成品包导出测试。"""

import http.client
import io
import json
import threading
import zipfile

import pytest

from aifos.app import App
from aifos.cli import main
from aifos.errors import AifosError
from aifos.export_kit import build_export_zip
from aifos.web.server import serve


def test_export_zip_contents(tmp_path):
    app = App(tmp_path / "ws")
    try:
        app.director.produce("万妖图录", 15)
        data, filename = build_export_zip(app, "万妖图录", 15)
        assert filename == "万妖图录_第15集_成品包.zip"
        bundle = zipfile.ZipFile(io.BytesIO(data))
        names = bundle.namelist()
        assert "剧本.txt" in names
        assert "标题与话题.txt" in names
        assert "发布信息.json" in names
        assert "剪映草稿.json" in names
        assert any(n.startswith("视频/") for n in names)
        # 标准产线的配音/口型已集成在即梦视频内，不再导出独立 TTS。
        assert not any(n.startswith("配音/") for n in names)
        assert "制作合同/本集制作标准.json" in names
        assert "制作合同/连续性圣经.json" in names
        assert "制作合同/五维分镜.json" in names
        assert "制作合同/生产门禁.json" in names
        assert "质检/图文检查板.html" in names
        assert "质检/check-delivery.py" in names
        assert any(n.startswith("人物设定/") for n in names)
        assert any(n.startswith("封面") for n in names)
        script_text = bundle.read("剧本.txt").decode("utf-8")
        assert "第1场" in script_text and ":" in script_text
        standard = json.loads(bundle.read(
            "制作合同/本集制作标准.json").decode("utf-8"))
        assert standard["profile_key"] == "sk-manju-v5"
        assert standard["version_id"] == app.standards.active()["version_id"]
        assert standard["fingerprint"] == app.standards.active()["fingerprint"]
    finally:
        app.close()


def test_export_empty_episode_raises(tmp_path):
    app = App(tmp_path / "ws")
    try:
        with pytest.raises(AifosError):
            build_export_zip(app, "不存在", 1)
    finally:
        app.close()


def test_web_export_endpoint(tmp_path):
    ws = tmp_path / "ws"
    boot = App(ws)
    boot.director.produce("万妖图录", 1)
    boot.close()
    httpd = serve(ws, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=30)
        conn.request("GET", "/api/export/1")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "application/zip"
        assert body[:2] == b"PK"
        conn.request("GET", "/api/export/999")
        assert conn.getresponse().status in (400, 404)
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_export(tmp_path, capsys):
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "produce", "--title", "万妖图录",
                 "--episode", "1"]) == 0
    out = tmp_path / "bundle.zip"
    capsys.readouterr()
    assert main(["--workspace", ws, "export", "--project", "万妖图录",
                 "--episode", "1", "--out", str(out)]) == 0
    assert "成品包已导出" in capsys.readouterr().out
    assert zipfile.ZipFile(out).namelist()
