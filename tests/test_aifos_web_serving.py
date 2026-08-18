"""Web 静态/产物服务:HTTP Range、内容协商压缩与零装配产物路由。"""

import gzip
import http.client
import json
import threading

import pytest

from aifos.app import App
from aifos.web import server as server_module
from aifos.web.server import serve

VIDEO = bytes(range(256)) * 64          # 16 KB 伪视频内容
TEXT = "数据分析报告📊\n" * 400         # >1KB 的文本,触发 gzip 阈值


@pytest.fixture()
def server(tmp_path):
    ws = tmp_path / "ws"
    App(ws).close()  # 初始化工作区
    httpd = serve(ws, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": httpd.server_address[1], "workspace": ws}
    httpd.shutdown()
    httpd.server_close()


def _raw(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    result = (resp.status, dict(resp.getheaders()), body)
    conn.close()
    return result


def _put_artifact(server, rel, data):
    target = server["workspace"] / "artifacts" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        target.write_bytes(data)


# ---- 产物路由不再装配 App ----

def test_artifact_served_without_app_construction(server, monkeypatch):
    """静态产物路由不依赖数据库与业务中心装配。"""
    _put_artifact(server, "ep1/shot1.png", VIDEO)

    def forbidden(*args, **kwargs):
        raise AssertionError("产物服务不应构造 App")

    monkeypatch.setattr(server_module, "App", forbidden)
    status, headers, body = _raw(server["port"], "/artifacts/ep1/shot1.png")
    assert status == 200
    assert body == VIDEO
    assert headers.get("Content-Type") == "image/png"


def test_artifact_path_traversal_still_blocked(server):
    status, _, _ = _raw(server["port"], "/artifacts/../aifos.db")
    assert status == 404


# ---- HTTP Range ----

def test_range_basic_returns_206_with_exact_slice(server):
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    status, headers, body = _raw(
        server["port"], "/artifacts/ep1/clip.mp4",
        headers={"Range": "bytes=100-199"})
    assert status == 206
    assert headers.get("Content-Range") == f"bytes 100-199/{len(VIDEO)}"
    assert headers.get("Accept-Ranges") == "bytes"
    assert headers.get("Content-Type") == "video/mp4"
    assert body == VIDEO[100:200]


def test_range_open_end_and_suffix(server):
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    status, headers, body = _raw(
        server["port"], "/artifacts/ep1/clip.mp4",
        headers={"Range": "bytes=16000-"})
    assert status == 206
    assert body == VIDEO[16000:]
    assert headers.get("Content-Range") == f"bytes 16000-{len(VIDEO)-1}/{len(VIDEO)}"

    status, headers, body = _raw(
        server["port"], "/artifacts/ep1/clip.mp4",
        headers={"Range": "bytes=-50"})
    assert status == 206
    assert body == VIDEO[-50:]


def test_range_end_is_clamped_to_file_size(server):
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    status, headers, body = _raw(
        server["port"], "/artifacts/ep1/clip.mp4",
        headers={"Range": f"bytes=0-{len(VIDEO) + 9999}"})
    assert status == 206
    assert len(body) == len(VIDEO)


def test_range_unsatisfiable_returns_416(server):
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    status, headers, _ = _raw(
        server["port"], "/artifacts/ep1/clip.mp4",
        headers={"Range": f"bytes={len(VIDEO)}-"})
    assert status == 416
    assert headers.get("Content-Range") == f"bytes */{len(VIDEO)}"


def test_range_malformed_or_multi_is_ignored(server):
    """RFC 9110 允许忽略无法满足的语法:回 200 全量而非报错。"""
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    for header in ("bytes=abc-def", "bytes=0-10,20-30", "items=0-5"):
        status, _, body = _raw(
            server["port"], "/artifacts/ep1/clip.mp4",
            headers={"Range": header})
        assert status == 200, header
        assert body == VIDEO


def test_full_response_advertises_range_support(server):
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    status, headers, body = _raw(server["port"], "/artifacts/ep1/clip.mp4")
    assert status == 200
    assert headers.get("Accept-Ranges") == "bytes"
    assert body == VIDEO


# ---- 内容协商压缩 ----

def test_gzip_negotiated_for_large_text(server):
    _put_artifact(server, "exports/report.jsonl", TEXT)
    status, headers, body = _raw(
        server["port"], "/artifacts/exports/report.jsonl",
        headers={"Accept-Encoding": "gzip, br"})
    assert status == 200
    assert headers.get("Content-Encoding") == "gzip"
    assert headers.get("Vary") == "Accept-Encoding"
    assert gzip.decompress(body).decode("utf-8") == TEXT
    assert len(body) < len(TEXT.encode("utf-8")) // 4


def test_gzip_not_applied_without_accept_or_for_binary(server):
    _put_artifact(server, "exports/report.jsonl", TEXT)
    _put_artifact(server, "ep1/shot.png", VIDEO)
    status, headers, body = _raw(server["port"],
                                 "/artifacts/exports/report.jsonl")
    assert status == 200
    assert "Content-Encoding" not in headers
    assert body.decode("utf-8") == TEXT

    status, headers, body = _raw(
        server["port"], "/artifacts/ep1/shot.png",
        headers={"Accept-Encoding": "gzip"})
    assert status == 200
    assert "Content-Encoding" not in headers
    assert body == VIDEO


def test_gzip_not_applied_to_small_files(server):
    _put_artifact(server, "exports/tiny.jsonl", "短")
    status, headers, body = _raw(
        server["port"], "/artifacts/exports/tiny.jsonl",
        headers={"Accept-Encoding": "gzip"})
    assert status == 200
    assert "Content-Encoding" not in headers


def test_json_api_gzip_roundtrip(server):
    """大 JSON 响应(如看板)按协商压缩且可还原。"""
    status, headers, body = _raw(
        server["port"], "/api/overview",
        headers={"Accept-Encoding": "gzip"})
    assert status == 200
    if "Content-Encoding" in headers:
        assert headers["Content-Encoding"] == "gzip"
        payload = json.loads(gzip.decompress(body))
    else:  # 空工作区看板可能小于压缩阈值
        payload = json.loads(body)
    assert "episodes" in payload and "stats" in payload


def test_range_takes_precedence_over_gzip(server):
    """Range 请求不压缩:字节区间必须对应原始文件。"""
    _put_artifact(server, "exports/report.jsonl", TEXT)
    status, headers, body = _raw(
        server["port"], "/artifacts/exports/report.jsonl",
        headers={"Range": "bytes=0-99", "Accept-Encoding": "gzip"})
    assert status == 206
    assert "Content-Encoding" not in headers
    assert body == TEXT.encode("utf-8")[:100]


# ---- 缓存策略不回归 ----

def test_cache_headers_preserved(server):
    _put_artifact(server, "ep1/clip.mp4", VIDEO)
    _, headers, _ = _raw(server["port"], "/artifacts/ep1/clip.mp4?v=3")
    assert headers.get("Cache-Control") == "public, max-age=86400"

    _, headers, _ = _raw(server["port"], "/static/app.js")
    assert headers.get("Cache-Control") == "no-cache"

    _, headers, _ = _raw(
        server["port"], "/artifacts/ep1/clip.mp4?v=3",
        headers={"Range": "bytes=0-9"})
    assert headers.get("Cache-Control") == "public, max-age=86400"
