"""体检(doctor)与本机 CLI 自动接线(config detect)测试。"""

import http.client
import json
import stat
import threading

from aifos.app import App
from aifos.cli import main
from aifos.config import Config
from aifos.doctor import apply_detected, detect_clis, run_doctor
from aifos.web.server import serve


def _fake_cli(tmp_path, name):
    path = tmp_path / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _only_path_candidates(monkeypatch):
    """隔离宿主机常见安装目录，只验证当前测试注入的 PATH。"""
    import aifos.doctor as doctor

    monkeypatch.setattr(
        doctor, "CLI_CANDIDATES",
        {name: [] for name in doctor.CLI_CANDIDATES},
    )


def test_detect_and_apply(tmp_path, monkeypatch):
    dreamina = _fake_cli(tmp_path, "dreamina")
    codex = _fake_cli(tmp_path, "codex")
    _only_path_candidates(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    found = detect_clis()
    assert found["dreamina"] == str(dreamina.resolve())
    assert found["codex"] == str(codex.resolve())
    assert "claude" not in found

    app = App(tmp_path / "ws")
    config_path = app.workspace.config_path
    app.close()
    applied = apply_detected(config_path, found)
    assert ("jimeng", str(dreamina.resolve())) in [
        (p, path) for p, path in applied]
    conf = Config.load(config_path)
    assert conf.get("providers", "jimeng", "enabled") is True
    assert conf.get("providers", "jimeng", "command") == \
        [str(dreamina.resolve())]
    assert conf.get("providers", "codex", "enabled") is True
    assert conf.get("providers", "codex", "command")[-1] == \
        str(codex.resolve())


def test_doctor_all_mock(tmp_path):
    app = App(tmp_path / "ws")
    try:
        report = run_doctor(app)
        assert report["real_count"] == 0
        assert report["total"] == 10
        video = next(c for c in report["capabilities"]
                     if c["capability"] == "video")
        assert video["provider"] == "mock" and not video["real"]
        assert video["hint"]              # 未接入要给接入提示
    finally:
        app.close()


def test_doctor_with_real_cli(tmp_path, monkeypatch):
    dreamina = _fake_cli(tmp_path, "dreamina")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    app = App(tmp_path / "ws", config_overrides={
        "providers": {
            "jimeng": {"enabled": True, "command": [str(dreamina)]},
            # claude 启用但底层 claude 命令不存在 → 体检要点名
            "claude": {"enabled": True,
                       "command": ["python3", "-m",
                                   "aifos.adapters.claude_script",
                                   "--claude", "/no/such/claude"]},
        }})
    try:
        report = run_doctor(app)
        video = next(c for c in report["capabilities"]
                     if c["capability"] == "video")
        assert video["provider"] == "jimeng" and video["real"]
        script = next(c for c in report["capabilities"]
                      if c["capability"] == "script")
        assert script["provider"] == "mock"      # claude 命令缺失 → 回退
        claude = next(p for p in report["providers"]
                      if p["name"] == "claude")
        assert not claude["ok"] and "/no/such/claude" in claude["detail"]
        # 视频真实 + 配音随视频自动完成 → 2 个环节已接
        voice = next(c for c in report["capabilities"]
                     if c["capability"] == "voice")
        assert voice["real"] and "随视频" in voice["provider_label"]
        assert report["real_count"] == 2
    finally:
        app.close()


def test_cli_doctor_and_detect(tmp_path, monkeypatch, capsys):
    _fake_cli(tmp_path, "dreamina")
    _only_path_candidates(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "init"]) == 0
    capsys.readouterr()
    # detect 不加 --apply:只报告
    assert main(["--workspace", ws, "config", "detect"]) == 0
    out = capsys.readouterr().out
    assert "dreamina" in out and "--apply" in out
    # detect --apply:写入配置
    assert main(["--workspace", ws, "config", "detect", "--apply"]) == 0
    assert "jimeng" in capsys.readouterr().out
    # doctor:视频已接真实产线 → 退出码 0,输出含真实/模拟标注
    assert main(["--workspace", ws, "doctor"]) == 0
    out = capsys.readouterr().out
    assert "视频" in out and "即梦" in out
    # 配音随视频自动完成 → 与视频一起算作已接真实产线
    assert "随视频自动配音" in out
    assert "内置模拟" in out and "真实产线 2/10" in out


def test_cli_doctor_all_mock_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    ws = str(tmp_path / "ws")
    assert main(["--workspace", ws, "doctor"]) == 1
    assert "0/10" in capsys.readouterr().out


def test_web_doctor_and_detect(tmp_path, monkeypatch):
    dreamina = _fake_cli(tmp_path, "dreamina")
    _only_path_candidates(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    ws = tmp_path / "ws"
    App(ws).close()
    httpd = serve(ws, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=30)
        conn.request("GET", "/api/doctor")
        resp = conn.getresponse()
        report = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert report["total"] == 10 and report["real_count"] == 0

        conn.request("POST", "/api/settings/detect")
        resp = conn.getresponse()
        view = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 200
        assert view["detected"]["dreamina"] == str(dreamina.resolve())
        assert any(a["provider"] == "jimeng" for a in view["applied"])
        row = next(p for p in view["providers"] if p["name"] == "jimeng")
        assert row["enabled"] is True

        conn.request("GET", "/api/doctor")
        resp = conn.getresponse()
        report = json.loads(resp.read().decode("utf-8"))
        video = next(c for c in report["capabilities"]
                     if c["capability"] == "video")
        assert video["real"] and video["provider"] == "jimeng"
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
