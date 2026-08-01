"""Seedance 视频并行调度：默认 4 路且逐镜参考契约不串线。"""

import threading
import time

import pytest

from aifos.app import App
from aifos.production.base import ProviderResult


def test_video_stage_runs_four_in_parallel_and_accounts_once(
        tmp_path, monkeypatch):
    app = App(
        tmp_path / "ws",
        config_overrides={"routing": {"video": ["mock"]}})
    try:
        assert app.director._video_parallel_workers() == 4
        project, _ = app.projects.get_or_create_project("视频并行测试")
        episode, _ = app.projects.get_or_create_episode(project["id"], 1)
        out_root = app.workspace.artifacts_dir / "p001" / "e001"
        out_root.mkdir(parents=True)
        shots = [
            {"shot_no": number, "duration": 3.0, "prompt": f"镜头{number}"}
            for number in range(1, 7)
        ]
        ctx = {
            "project": dict(project),
            "episode": dict(episode),
            "storyboard": {"shots": shots},
            "frames": [],
            "out_root": out_root,
        }
        app.director._task_cost = 0.0
        app.director._task_providers = set()
        monkeypatch.setattr(
            app.director, "_existing_asset_uri",
            lambda *_args, **_kwargs: None)

        def prepare(_ctx, shot, _frames):
            number = int(shot["shot_no"])
            manifest = [{
                "index": 3,
                "asset_id": number,
                "kind": "spatial_blocking",
                "name": f"space-{number}",
                "version": 1,
                "uri": f"/refs/space-{number}.png",
                "binding": f"只绑定镜头{number}",
            }]
            return {
                "shot": shot,
                "payload": {
                    "shot_no": number,
                    "prompt": f"镜头{number}",
                    "reference_images": [manifest[0]["uri"]],
                    "reference_manifest": manifest,
                },
                "quality": {
                    "level": "medium",
                    "resolution": "720p",
                    "source": "default",
                },
                "reference_assets": [{
                    key: manifest[0][key]
                    for key in ("asset_id", "kind", "name", "version")
                }],
                "reference_manifest": manifest,
            }

        monkeypatch.setattr(
            app.director, "_prepare_video_call", prepare)
        lock = threading.Lock()
        first_wave = threading.Barrier(4, timeout=3)
        active = 0
        max_active = 0
        seen_payloads = []

        def fake_call(capability, payload, out_dir, cancel=None):
            nonlocal active, max_active
            assert capability == "video"
            assert not (cancel and cancel())
            with lock:
                active += 1
                max_active = max(max_active, active)
                seen_payloads.append(payload)
            try:
                if int(payload["shot_no"]) <= 4:
                    first_wave.wait()
                time.sleep(0.02)
                target = out_dir / f"shot_{int(payload['shot_no']):03d}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
                return ProviderResult(
                    provider="mock", cost=0.1, uri=str(target),
                    data={"voice": "jimeng_builtin", "lip_sync": True})
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(app.router, "call", fake_call)
        result = app.director._stage_videos(ctx)

        assert result == {
            "count": 6,
            "reused": 0,
            "generated": 6,
            "parallel_workers": 4,
            "contract_incomplete": False,
            "contract_incomplete_shots": [],
        }
        assert max_active == 4
        assert [item["shot_no"] for item in ctx["videos"]] == list(range(1, 7))
        assert len(seen_payloads) == 6
        assert all(
            payload["reference_manifest"][0]["uri"]
            == f"/refs/space-{payload['shot_no']}.png"
            for payload in seen_payloads)
        assert app.director._task_cost == pytest.approx(0.6)
        assert app.projects.get_episode(episode["id"])["cost"] \
            == pytest.approx(0.6)
        assert len(app.assets.list(project["id"], "video")) == 6
    finally:
        app.close()
