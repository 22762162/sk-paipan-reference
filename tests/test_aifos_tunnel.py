"""外网访问:纯标准库 QR 生成 + cloudflared 隧道地址发布 + /qr.svg。

QR 编码器用 ISO/IEC 18004 参考产出的“黄金矩阵”做回归(黄金数据由成熟
实现 segno 预先生成并固化在此,运行时不依赖任何第三方库)。
"""

from pathlib import Path

import pytest

from aifos import qrcode, tunnel
from aifos.web.server import access_payload

# 由 segno(mode=byte, micro=False, boost_error=False)生成的黄金矩阵:
# 只要本仓库 QR 编码器与之逐位一致,即与成熟实现产出的可扫码 QR 完全相同。
GOLDEN = {
    ("https://bouquet-cdna-pub-decide.trycloudflare.com/", "M"): [
        "111111100010111001000110001111111",
        "100000101001000010101010001000001",
        "101110101000101100001110001011101",
        "101110101101101110111110101011101",
        "101110100001001110001101001011101",
        "100000100101001100110010101000001",
        "111111101010101010101010101111111",
        "000000001010100110111011100000000",
        "100000101101011001010010011001110",
        "011110001000111101011101000011110",
        "010011110100011111001010010010110",
        "100010010110010101100000110111100",
        "010001100110010101011001001000001",
        "111110011110110000100001001100111",
        "001100111100010110000110011010010",
        "000101000110000010001010110101101",
        "101001110100100101000000010111001",
        "101101010010001110111001001001111",
        "000010100010101101111011001101101",
        "001000011001001010011010000011101",
        "000011101100111100110010110111011",
        "111001011000000000001111000110000",
        "100011110100111001101110100101110",
        "101010001001011110111010001111101",
        "110000111000100010101000111111011",
        "000000001000010111111101100010101",
        "111111100000110101000011101010100",
        "100000100001010001100001100011111",
        "101110100110011110000010111111010",
        "101110100100000001110001010110111",
        "101110100110100101010010010110011",
        "100000100010001000001010010111100",
        "111111101001111101010011101100010",
    ],
    ("http://192.168.1.7:8619/", "Q"): [
        "11111110011111110101101111111",
        "10000010011001110101001000001",
        "10111010110101010100001011101",
        "10111010001110111000001011101",
        "10111010101010000100101011101",
        "10000010100010100101101000001",
        "11111110101010101010101111111",
        "00000000001100110001000000000",
        "01001010110010001001110110100",
        "01001001001100101011011010111",
        "10100011110101100101000001101",
        "10101001100100000111100100011",
        "10111110110011001110000000001",
        "10111101101011010000101010111",
        "00110010101010101100110110011",
        "10001101100100100110001111010",
        "01110111001001010010100100011",
        "10010001101100101001101011001",
        "00101111101100101010000110101",
        "00001000000100100100000000011",
        "11010111010111011111111110001",
        "00000000101100010100100010101",
        "11111110010111011011101010011",
        "10000010000101000000100011001",
        "10111010110111011001111110000",
        "10111010010010000110100101010",
        "10111010000001010100000100011",
        "10000010101100011100000100011",
        "11111110001101000111111010010",
    ],
    ("https://aifos.app/e/12", "L"): [
        "1111111010011010101111111",
        "1000001001100000001000001",
        "1011101000101111101011101",
        "1011101000001100001011101",
        "1011101001010010101011101",
        "1000001000101010101000001",
        "1111111010101010101111111",
        "0000000010110100000000000",
        "1101101001110001001000001",
        "0110110001001111000111110",
        "1111111010110101110111001",
        "1010010001111010101001111",
        "0011111111011011101100001",
        "1110000111000101100010010",
        "1111101001011101111011111",
        "1000110011110001000101101",
        "1001011111100110111110110",
        "0000000010011010100010110",
        "1111111001010010101010001",
        "1000001001101110100010000",
        "1011101011001101111110011",
        "1011101010100011011000011",
        "1011101001001011010011111",
        "1000001010100001000110111",
        "1111111010001110100001001",
    ],
}


def _rows(matrix):
    return ["".join(str(v) for v in row) for row in matrix]


@pytest.mark.parametrize("key", list(GOLDEN))
def test_qr_matches_golden_reference(key):
    """QR 编码结果与成熟实现逐位一致(证明可被任意扫码器识别)。"""
    text, ecl = key
    assert _rows(qrcode.encode(text, ecl)) == GOLDEN[key]


def test_qr_auto_selects_version_by_length():
    short = qrcode.encode("http://a.b/", "M")
    long = qrcode.encode("https://" + "x" * 90 + ".trycloudflare.com/", "M")
    assert len(long) > len(short)          # 更长的内容自动升到更大版本
    assert (len(short) - 17) % 4 == 0      # 合法 QR 尺寸 = 17 + 4*版本


def test_qr_finder_patterns_present():
    """三个定位角:7x7 外框 + 3x3 实心,是扫码器对齐的关键。"""
    m = qrcode.encode("https://aifos.example.com/", "M")
    n = len(m)
    for top, left in ((0, 0), (0, n - 7), (n - 7, 0)):
        assert all(m[top][left + i] == 1 for i in range(7))       # 上边
        assert all(m[top + 6][left + i] == 1 for i in range(7))   # 下边
        assert m[top + 3][left + 3] == 1                          # 中心
        assert m[top + 1][left + 1] == 0                          # 内白环


def test_qr_rejects_unknown_ecl():
    with pytest.raises(ValueError):
        qrcode.encode("x", "Z")


def test_qr_raises_when_too_long():
    with pytest.raises(ValueError):
        qrcode.encode("x" * 400, "H")


def test_render_terminal_is_faithful_to_matrix():
    """终端渲染(纯字符模式)如实反映矩阵,扫出来才不会错。"""
    m = qrcode.encode("http://192.168.1.7:8619/", "M")
    n = len(m)
    quiet = 4
    lines = qrcode.render_terminal(m, quiet=quiet, ansi=False).split("\n")
    assert len(lines) == n + quiet * 2
    for r in range(n):
        line = lines[r + quiet]
        for c in range(n):
            cell = line[(c + quiet) * 2:(c + quiet) * 2 + 2]
            assert (1 if cell == "██" else 0) == m[r][c]


def test_render_svg_has_one_rect_per_dark_module():
    m = qrcode.encode("https://aifos.app/", "M")
    dark = sum(sum(row) for row in m)
    svg = qrcode.render_svg(m)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<rect") == dark + 1        # +1 为白色背景


# ---- 隧道地址发布 ----------------------------------------------------
def test_parse_quick_url_from_cloudflared_log():
    line = ("2024-01-01 INF |  https://bouquet-cdna-pub-decide."
            "trycloudflare.com  |")
    assert (tunnel.parse_quick_url(line)
            == "https://bouquet-cdna-pub-decide.trycloudflare.com")
    assert tunnel.parse_quick_url("no url here") is None


def test_public_url_roundtrip(tmp_path):
    assert tunnel.read_public_url(tmp_path) is None
    data = tunnel.write_public_url(
        tmp_path, "https://foo.trycloudflare.com", mode="quick")
    assert data["url"] == "https://foo.trycloudflare.com/"
    assert tunnel.read_public_url(tmp_path)["url"] \
        == "https://foo.trycloudflare.com/"
    tunnel.clear_public_url(tmp_path)
    assert tunnel.read_public_url(tmp_path) is None


def test_build_command_quick_vs_named():
    quick = tunnel.build_command("cloudflared", 8619)
    assert "--url" in quick and "http://localhost:8619" in quick
    named = tunnel.build_command("cloudflared", 8619, name="aifos")
    assert named[-2:] == ["run", "aifos"]


def test_run_tunnel_publishes_quick_url(tmp_path):
    """快速隧道:从伪 cloudflared 输出里解析地址并发布。"""
    class FakeProc:
        def __init__(self):
            self.stdout = iter_lines()
            self._returncode = 0

        def wait(self):
            return 0

    def iter_lines():
        class _S:
            def __init__(self):
                self._lines = [
                    "starting tunnel\n",
                    "your quick tunnel: https://abc-def.trycloudflare.com\n",
                    "",
                ]
                self._i = 0

            def readline(self):
                line = self._lines[self._i]
                self._i += 1
                return line

            def close(self):
                pass
        return _S()

    events = []

    def fake_popen(cmd, **kw):
        return FakeProc()

    code = tunnel.run_tunnel(
        tmp_path, 8619, on_event=lambda k, p: events.append((k, p)),
        popen=fake_popen)
    assert code == 0
    assert ("url", "https://abc-def.trycloudflare.com") in events
    assert tunnel.read_public_url(tmp_path)["url"] \
        == "https://abc-def.trycloudflare.com/"


def test_run_tunnel_missing_cloudflared(tmp_path):
    def boom(cmd, **kw):
        raise FileNotFoundError()

    errors = []
    code = tunnel.run_tunnel(
        tmp_path, 8619, on_event=lambda k, p: errors.append((k, p)),
        popen=boom)
    assert code == 127
    assert any(k == "error" for k, _ in errors)


# ---- /api/access 暴露外网地址 ---------------------------------------
def test_access_payload_surfaces_public_url(tmp_path):
    ws = tmp_path / "ws"
    (ws).mkdir()
    payload = access_payload("127.0.0.1", 8619, workspace=str(ws))
    assert payload["public_url"] is None
    tunnel.write_public_url(
        Path(ws).resolve(), "https://foo.trycloudflare.com", mode="quick")
    payload = access_payload("127.0.0.1", 8619, workspace=str(ws))
    assert payload["public_url"] == "https://foo.trycloudflare.com/"
    assert payload["public"]["mode"] == "quick"
