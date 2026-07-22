"""iCloud 图片镜像：完成点、增量回填、安全边界与 Web 可见性。"""

import http.client
import json
import threading
import time
from pathlib import Path

import aifos.icloud_sync as icloud_module
from aifos.app import App
from aifos.web.server import serve


def _config(cloud_docs, enabled=True):
    return {
        "icloud_sync": {
            "enabled": enabled,
            "root": str(cloud_docs / "AIFOS"),
        }
    }


def _use_cloud_docs(monkeypatch, cloud_docs):
    monkeypatch.setattr(
        icloud_module, "_default_cloud_docs_root", lambda: cloud_docs)


def _make_asset(app, project_title="手机查看项目", episode=2,
                kind="image", name=None, payload=b"image-v1"):
    project, _ = app.projects.get_or_create_project(project_title)
    source = (app.workspace.artifacts_dir / f"p{project['id']:03d}"
              / f"e{episode:03d}" / "images"
              / f"shot_{episode:03d}.keyframe.png")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    row = app.assets.register(
        project["id"], kind, name or f"e{episode:03d}_shot001",
        uri=str(source), meta={"episode_number": episode})
    return dict(row), source


def test_registered_image_is_mirrored_after_app_close(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    row, source = _make_asset(app)
    app.close()

    copies = list((cloud_docs / "AIFOS").glob(
        "P001_手机查看项目__W*/第002集/10_关键帧/*.png"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == source.read_bytes()
    assert (f"关键帧__e002_shot001__A{row['id']:06d}__v001.png"
            == copies[0].name)
    manifest = json.loads((cloud_docs / "AIFOS"
                           / ".aifos-image-sync.json").read_text())
    assert any(key.endswith(f":{row['id']}:v1")
               for key in manifest["entries"])


def test_backfill_is_incremental_and_excludes_thumbnail_cache(
        tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    ws = tmp_path / "ws"
    disabled = App(ws, config_overrides=_config(cloud_docs, enabled=False))
    _make_asset(disabled, kind="character_candidate", name="林晚:01")
    project = disabled.projects.get_project("手机查看项目")
    thumb = disabled.workspace.artifacts_dir / ".thumbs" / "fake.png"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"thumb")
    disabled.assets.register(
        project["id"], "image", "e002_thumb", uri=str(thumb))
    disabled.close()

    app = App(ws, config_overrides=_config(cloud_docs))
    first = app.icloud_sync.backfill()
    second = app.icloud_sync.backfill()
    app.close()
    assert first["synced"] == 1 and first["ignored"] == 1
    assert first["failed"] == 0
    assert second["synced"] == 0 and second["skipped"] == 1
    assert len(list((cloud_docs / "AIFOS").rglob("*.png"))) == 1
    assert list((cloud_docs / "AIFOS").glob(
        "P001_手机查看项目__W*/00_项目素材/01_人物候选/*.png"))


def test_sync_failure_never_rolls_back_asset_registration(
        tmp_path, monkeypatch):
    cloud_docs = tmp_path / "missing-cloud-docs"
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    row, _ = _make_asset(app)
    result = app.icloud_sync.sync_asset(row)
    assert result["status"] == "failed"
    assert app.assets.get(row["id"])["uri"] == row["uri"]
    assert app.icloud_sync.status()["state"] == "unavailable"
    app.close()


def test_external_and_non_image_sources_are_not_copied(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    project, _ = app.projects.get_or_create_project("安全边界")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    row = app.assets.register(
        project["id"], "image", "e001_outside", uri=str(outside))
    assert app.icloud_sync.sync_asset(dict(row))["status"] == "ignored"
    text = app.workspace.artifacts_dir / "p001" / "e001" / "note.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("not image")
    row = app.assets.register(
        project["id"], "image", "e001_text", uri=str(text),
        new_version=True)
    assert app.icloud_sync.sync_asset(dict(row))["status"] == "ignored"
    app.close()
    assert not list((cloud_docs / "AIFOS").rglob("outside.png"))


def test_settings_and_backfill_endpoints(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps(_config(cloud_docs, enabled=False)), encoding="utf-8")
    app = App(ws)
    _make_asset(app)
    app.close()

    httpd = serve(ws, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection(
        "127.0.0.1", httpd.server_address[1], timeout=30)

    def request(method, path, body=None):
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))

    try:
        status, view = request("GET", "/api/settings")
        assert status == 200 and view["icloud_sync"]["state"] == "disabled"
        status, view = request(
            "POST", "/api/settings", {"icloud_sync": {"enabled": True}})
        assert status == 200 and view["icloud_sync"]["enabled"] is True
        status, report = request("POST", "/api/icloud-sync/backfill")
        assert status == 200 and report["synced"] == 1
        status, overview = request("GET", "/api/overview")
        assert status == 200 and overview["icloud_sync"]["synced"] == 1
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()


def test_settings_ui_exposes_sync_state_and_visible_failures():
    root = Path(__file__).resolve().parents[1]
    script = (root / "aifos" / "web" / "static" / "app.js").read_text(
        encoding="utf-8")
    style = (root / "aifos" / "web" / "static" / "style.css").read_text(
        encoding="utf-8")
    assert "iCloud 图片同步" in script
    assert "补同步现有图片" in script
    assert "sync.last_error" in script
    assert ".icloud-sync-card" in style


def test_config_cannot_move_safety_boundary_outside_real_clouddocs(
        tmp_path, monkeypatch):
    real_cloud = tmp_path / "real-CloudDocs"
    real_cloud.mkdir()
    _use_cloud_docs(monkeypatch, real_cloud)
    unsafe = tmp_path / "outside" / "AIFOS"
    config = {"icloud_sync": {
        "enabled": True, "root": str(unsafe),
        # 旧测试/误配置即使自报根目录，也不能改变真实安全边界。
        "cloud_docs_root": str(tmp_path),
    }}
    app = App(tmp_path / "ws", config_overrides=config)
    assert app.icloud_sync.status()["state"] == "unavailable"
    app.close()
    assert not unsafe.exists()


def test_sanitized_name_collision_keeps_both_assets(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    project, _ = app.projects.get_or_create_project("重名安全")
    image_dir = app.workspace.artifacts_dir / "p001" / "e001" / "images"
    image_dir.mkdir(parents=True)
    for index, name in enumerate(("a:b", "a/b"), 1):
        source = image_dir / f"source_{index}.png"
        source.write_bytes(f"image-{index}".encode())
        app.assets.register(
            project["id"], "image", name, uri=str(source),
            meta={"episode_number": 1})
    app.close()
    copies = list((cloud_docs / "AIFOS").rglob("*.png"))
    assert len(copies) == 2
    assert len({path.name for path in copies}) == 2
    assert {path.read_bytes() for path in copies} == {b"image-1", b"image-2"}


def test_two_workspaces_cannot_overwrite_each_other(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    for index in (1, 2):
        app = App(tmp_path / f"ws{index}",
                  config_overrides=_config(cloud_docs))
        _make_asset(app, project_title="同名项目", episode=1,
                    name="e001_shot001",
                    payload=f"workspace-{index}".encode())
        app.close()
    copies = list((cloud_docs / "AIFOS").rglob("*.png"))
    assert len(copies) == 2
    assert len({path.parent.parent.parent.name for path in copies}) == 2
    assert {path.read_bytes() for path in copies} == {
        b"workspace-1", b"workspace-2"}
    manifest = json.loads((cloud_docs / "AIFOS"
                           / ".aifos-image-sync.json").read_text())
    assert len(manifest["entries"]) == 2
    assert len(manifest["last_runs"]) == 0

    second = App(tmp_path / "ws2", config_overrides=_config(cloud_docs))
    second.icloud_sync.backfill()
    row = dict(second.db.query_one("SELECT * FROM assets LIMIT 1"))
    second.icloud_sync._record_failure(row, "仅 workspace2 的失败")
    second.close()
    first = App(tmp_path / "ws1", config_overrides=_config(cloud_docs))
    second = App(tmp_path / "ws2", config_overrides=_config(cloud_docs))
    try:
        assert first.icloud_sync.status()["synced"] == 1
        assert first.icloud_sync.status()["state"] == "ready"
        assert second.icloud_sync.status()["synced"] == 1
        assert second.icloud_sync.status()["state"] == "partial"
    finally:
        first.close()
        second.close()


def test_long_chinese_names_stay_within_filesystem_byte_limit(
        tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    _make_asset(app, project_title="项目" * 120, episode=1,
                name="超长中文资产名称" * 60, payload=b"long-name")
    app.close()
    target = next((cloud_docs / "AIFOS").rglob("*.png"))
    assert target.read_bytes() == b"long-name"
    assert all(len(part.encode("utf-8")) <= 255
               for part in target.relative_to(cloud_docs / "AIFOS").parts)


def test_concurrent_stale_snapshot_cannot_overwrite_new_image(
        tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    ws = tmp_path / "ws"
    config = _config(cloud_docs)
    entered = threading.Event()
    release = threading.Event()
    original_copy = icloud_module.ICloudImageSync._copy_atomic
    calls = {"count": 0}

    def delayed_copy(self, source, target, digest):
        calls["count"] += 1
        if calls["count"] == 1:
            entered.set()
            assert release.wait(5)
        return original_copy(self, source, target, digest)

    monkeypatch.setattr(
        icloud_module.ICloudImageSync, "_copy_atomic", delayed_copy)
    old_app = App(ws, config_overrides=config)
    project, _ = old_app.projects.get_or_create_project("竞态项目")
    old_source = old_app.workspace.artifacts_dir / "p001/e001/images/old.png"
    old_source.parent.mkdir(parents=True)
    old_source.write_bytes(b"old-image")
    old_app.assets.register(
        project["id"], "image", "e001_shot001", uri=str(old_source))
    assert entered.wait(5)

    new_app = App(ws, config_overrides=config)
    new_source = new_app.workspace.artifacts_dir / "p001/e001/images/new.png"
    new_source.write_bytes(b"new-image")
    new_app.assets.register(
        project["id"], "image", "e001_shot001", uri=str(new_source))
    release.set()
    old_app.close()
    new_app.close()

    copies = list((cloud_docs / "AIFOS").rglob("*.png"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"new-image"


def test_older_failure_cannot_arrive_after_newer_success(
        tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    ws = tmp_path / "ws"
    config = _config(cloud_docs)
    entered = threading.Event()
    release = threading.Event()
    original_copy = icloud_module.ICloudImageSync._copy_atomic
    calls = {"count": 0}

    def fail_old_copy(self, source, target, digest):
        calls["count"] += 1
        if calls["count"] == 1:
            entered.set()
            assert release.wait(5)
            raise OSError("旧尝试失败")
        return original_copy(self, source, target, digest)

    monkeypatch.setattr(
        icloud_module.ICloudImageSync, "_copy_atomic", fail_old_copy)
    old_app = App(ws, config_overrides=config)
    project, _ = old_app.projects.get_or_create_project("失败顺序")
    old_source = old_app.workspace.artifacts_dir / "p001/e001/images/old.png"
    old_source.parent.mkdir(parents=True)
    old_source.write_bytes(b"old")
    old_app.assets.register(
        project["id"], "image", "e001_shot001", uri=str(old_source))
    assert entered.wait(5)

    new_app = App(ws, config_overrides=config)
    new_source = new_app.workspace.artifacts_dir / "p001/e001/images/new.png"
    new_source.write_bytes(b"new")
    new_app.assets.register(
        project["id"], "image", "e001_shot001", uri=str(new_source))
    release.set()
    old_app.close()
    new_app.close()

    fresh = App(ws, config_overrides=config)
    status = fresh.icloud_sync.status()
    fresh.close()
    assert status["state"] == "ready" and status["failed"] == 0
    target = next((cloud_docs / "AIFOS").rglob("*.png"))
    assert target.read_bytes() == b"new"


def test_same_size_target_corruption_is_repaired(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    row, _ = _make_asset(app, payload=b"12345678")
    app.icloud_sync.close()
    target = next((cloud_docs / "AIFOS").rglob("*.png"))
    target.write_bytes(b"87654321")
    result = app.icloud_sync.sync_asset(row)
    # 已 close 的 service 仍可显式核对；sync_asset 本身不依赖 worker。
    assert result["status"] == "synced"
    assert target.read_bytes() == b"12345678"
    app.db.close()


def test_close_timeout_does_not_use_closed_main_database(
        tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    project, _ = app.projects.get_or_create_project("关闭安全")
    image_dir = app.workspace.artifacts_dir / "p001/e001/images"
    image_dir.mkdir(parents=True)
    entered = threading.Event()
    release = threading.Event()
    original_copy = icloud_module.ICloudImageSync._copy_atomic
    calls = {"count": 0}

    def delayed_copy(self, source, target, digest):
        calls["count"] += 1
        if calls["count"] == 1:
            entered.set()
            assert release.wait(5)
            raise OSError("模拟 iCloud 写入失败")
        return original_copy(self, source, target, digest)

    monkeypatch.setattr(
        icloud_module.ICloudImageSync, "_copy_atomic", delayed_copy)
    for index in (1, 2):
        source = image_dir / f"{index}.png"
        source.write_bytes(f"image-{index}".encode())
        app.assets.register(
            project["id"], "image", f"e001_shot{index:03d}",
            uri=str(source))
    assert entered.wait(5)
    started = time.monotonic()
    app.icloud_sync.close(timeout=0.01)
    assert time.monotonic() - started < 0.5
    app.db.close()
    release.set()
    app.icloud_sync._worker.join(5)
    assert not app.icloud_sync._worker.is_alive()
    # 主 DB 已关闭后首项失败，logger 也不能杀死 worker；第二项仍须完成。
    assert len(list((cloud_docs / "AIFOS").rglob("*.png"))) == 1
    manifest = json.loads((cloud_docs / "AIFOS"
                           / ".aifos-image-sync.json").read_text())
    assert len(manifest["entries"]) == 1
    assert len(manifest["failures"]) == 1


def test_enqueue_and_close_share_one_lifecycle_gate(tmp_path, monkeypatch):
    cloud_docs = tmp_path / "CloudDocs"
    cloud_docs.mkdir()
    _use_cloud_docs(monkeypatch, cloud_docs)
    app = App(tmp_path / "ws", config_overrides=_config(cloud_docs))
    project, _ = app.projects.get_or_create_project("关闭竞态")
    source = app.workspace.artifacts_dir / "p001/e001/images/one.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"must-not-drop")
    entered = threading.Event()
    release = threading.Event()
    original_ensure = app.icloud_sync._ensure_worker

    def delayed_ensure():
        entered.set()
        assert release.wait(5)
        original_ensure()

    monkeypatch.setattr(app.icloud_sync, "_ensure_worker", delayed_ensure)
    register = threading.Thread(target=lambda: app.assets.register(
        project["id"], "image", "e001_shot001", uri=str(source)))
    register.start()
    assert entered.wait(5)
    closing = threading.Thread(target=app.close)
    closing.start()
    time.sleep(0.05)
    assert closing.is_alive()  # close 必须等 enqueue 放入队列后才能发 sentinel
    release.set()
    register.join(5)
    closing.join(5)
    assert not register.is_alive() and not closing.is_alive()
    copies = list((cloud_docs / "AIFOS").rglob("*.png"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"must-not-drop"
