"""生产中心路由测试:降级回退与订阅额度。"""

import pytest

from aifos.app import App
from aifos.errors import ProviderUnavailable

OK_CLI = ["python3", "-c",
          "import sys,json; sys.stdin.read(); "
          "print(json.dumps({'ok':True,'data':{'src':'cli'},"
          "'uri':'','cost':2.0}))"]


@pytest.fixture()
def make_app(tmp_path):
    apps = []

    def factory(overrides):
        app = App(tmp_path / f"ws{len(apps)}", config_overrides=overrides)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()


def test_disabled_provider_falls_back_to_mock(make_app):
    app = make_app({})  # 默认:codex/jimeng 未启用
    result = app.router.call("video", {"shot_no": 1, "duration": 1.0},
                             app.workspace.artifacts_dir)
    assert result.provider == "mock"


def test_missing_command_falls_back(make_app):
    app = make_app({"providers": {"codex": {
        "enabled": True, "command": ["definitely-missing-cmd-xyz"]}}})
    result = app.router.call("image", {"shot_no": 1},
                             app.workspace.artifacts_dir)
    assert result.provider == "mock"


def test_failing_command_falls_back(make_app):
    app = make_app({"providers": {"codex": {
        "enabled": True, "command": ["false"]}}})
    result = app.router.call("image", {"shot_no": 1},
                             app.workspace.artifacts_dir)
    assert result.provider == "mock"


def test_enabled_cli_provider_is_used(make_app):
    app = make_app({"providers": {"jimeng": {
        "enabled": True, "command": OK_CLI, "quota": 10}}})
    result = app.router.call("video", {"shot_no": 1},
                             app.workspace.artifacts_dir)
    assert result.provider == "jimeng"
    assert result.cost == 2.0
    assert app.router.quota_remaining("jimeng") == 9


def test_quota_exhausted_falls_back(make_app):
    app = make_app({"providers": {"jimeng": {
        "enabled": True, "command": OK_CLI, "quota": 1}}})
    first = app.router.call("video", {"shot_no": 1},
                            app.workspace.artifacts_dir)
    second = app.router.call("video", {"shot_no": 2},
                             app.workspace.artifacts_dir)
    assert first.provider == "jimeng"
    # 订阅额度耗尽 → 回退链下一个可用 Provider(api 未配置 → mock)
    assert second.provider == "mock"
    assert app.router.quota_remaining("jimeng") == 0


def test_no_provider_available_raises(make_app):
    app = make_app({
        "providers": {"mock": {"enabled": False}},
        "routing": {"video": ["jimeng", "api", "mock"]},
    })
    with pytest.raises(ProviderUnavailable):
        app.router.call("video", {"shot_no": 1},
                        app.workspace.artifacts_dir)
