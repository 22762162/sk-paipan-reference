import http.client
import json
import threading

from aifos.app import App
from aifos.web.server import serve


DIRECTOR_KNOWLEDGE = {
    "shot_language": {
        "shot_patterns": ["斜侧近景缓推"],
        "shot_scales": ["近景"],
        "camera_angles": ["平视"],
        "camera_positions": ["斜侧"],
        "lenses": ["85mm"],
        "camera_moves": ["推"],
        "compositions": ["三分法"],
        "transitions": ["硬切"],
        "rhythm": ["一镜一个动作"],
        "forbidden": ["无动机环绕"],
    },
    "visual_effects": {
        "lighting": ["暖色侧光"],
        "atmosphere": ["薄雾"],
        "optical": ["浅景深"],
        "color_grade": ["低饱和暖调"],
        "materials": ["布料与金属分层"],
        "particles": [],
        "post_process": ["高光晕染"],
        "forbidden": ["特效遮脸"],
    },
    "selection_rules": [{
        "when": "对白",
        "shots": ["斜侧近景缓推"],
        "effects": ["暖色侧光"],
        "purpose": "靠近人物情绪",
    }],
}


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
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


def test_firefire_web_control_plane_and_style_gate(tmp_path):
    workspace = tmp_path / "workspace"
    App(workspace).close()
    server = serve(workspace, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, overview = _request(port, "GET", "/api/firefire")
        assert status == 200 and overview["name"] == "火火漫剧研究室"
        status, session = _request(port, "POST", "/api/firefire/session", {
            "name": "待确认案例", "source_url": "https://example.com/case",
        })
        assert status == 201 and session["status"] == "awaiting_rights"
        status, _ = _request(port, "POST", "/api/firefire/analyse", {
            "session_id": session["id"],
        })
        assert status == 409
        status, ready = _request(port, "POST", "/api/firefire/session", {
            "name": "可研究案例", "source_url": "https://example.com/ready",
            "rights_confirmed": True,
        })
        assert status == 201
        status, style = _request(port, "POST", "/api/firefire/style", {
            "name": "验证草稿", "session_id": ready["id"],
            "compiled_style": "剧情适配的可执行风格提示词，禁止字幕、logo、水印",
            "director_knowledge": DIRECTOR_KNOWLEDGE,
        })
        assert status == 201 and style["status"] == "draft"
        assert style["director_ready"] is True
        status, reply = _request(port, "POST", "/api/produce", {
            "title": "风格门禁测试", "episode": 1,
            "style_pack_id": style["id"], "review": True,
        })
        assert status == 409 and "人工确认" in reply["error"]
        status, published = _request(
            port, "POST", "/api/firefire/style/publish",
            {"style_id": style["id"], "approved_by": "tester"})
        assert status == 200 and published["status"] == "approved"
        status, overview = _request(port, "GET", "/api/overview")
        assert status == 200
        assert any(item["id"] == style["id"]
                   for item in overview["firefire"]["styles"])
        assert overview["firefire"]["counts"]["knowledge_active"] == 1
        status, resolved = _request(
            port, "POST", "/api/firefire/knowledge/resolve", {
                "stage": "video",
                "task_type": "depth_control",
                "query": "用深度视频复刻动作和运镜",
            })
        assert status == 200
        assert resolved["matches"][0][
            "knowledge_key"] == "depth-structure-control"
        status, rejected = _request(
            port, "POST", "/api/firefire/knowledge", {
                "knowledge_key": "water",
                "title": "万能高级感",
                "summary": "高级一点",
                "provenance": {"source_url": "https://example.com/water"},
            })
        assert status == 400
        assert "价值门禁" in rejected["error"]
    finally:
        server.shutdown()
        server.server_close()
