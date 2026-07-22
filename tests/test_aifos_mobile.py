"""手机版 PWA 包装与局域网启动参数的静态契约测试。"""

from pathlib import Path

from aifos.cli import _build_parser
from aifos.web.server import access_payload


STATIC = Path(__file__).parents[1] / "aifos" / "web" / "static"


def test_serve_lan_flag_is_explicit():
    args = _build_parser().parse_args(["serve", "--lan", "--port", "9000"])
    assert args.command == "serve"
    assert args.lan is True
    assert args.host == "127.0.0.1"
    assert args.port == 9000


def test_access_payload_only_advertises_lan_when_bound_all(monkeypatch):
    monkeypatch.setattr(
        "aifos.web.server._private_lan_addresses",
        lambda: ["192.168.8.20"],
    )
    local = access_payload("127.0.0.1", 8619)
    assert local["lan_enabled"] is False
    assert local["lan_urls"] == []
    lan = access_payload("0.0.0.0", 8619)
    assert lan["lan_enabled"] is True
    assert lan["lan_urls"] == ["http://192.168.8.20:8619/"]


def test_mobile_ui_contract_and_pwa_registration():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in html
    assert 'rel="manifest"' in html
    assert "apple-mobile-web-app-capable" in html
    assert "mobile-access-global" in html
    assert "env(safe-area-inset-bottom)" in css
    assert "grid-template-columns: repeat(5" in css
    assert "min-height: 44px" in css
    assert ".shot-production-table thead" in css
    assert ".storyboard-table-row > [data-label]::before" in css
    assert ".storyboard-frame-pair" in css
    assert "scroll-snap-type: x mandatory" in css
    assert 'serviceWorker.register("/sw.js",' in js
    assert 'updateViaCache: "none"' in js
    assert "/api/access" in js
    assert "beforeinstallprompt" in js
