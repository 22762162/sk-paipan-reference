"""候选补位的瞬时通道故障退避(12星座 j61 配额窗口固化事故回归)。

事故:候选波次失败后背靠背立即补一次;60 秒的限流/配额窗口足以让
两次尝试同时死掉,整轮零候选,16 个镜头被标记缺图。修复后瞬时错误
退避 60s/150s 重试两波;非瞬时错误维持原单次补位。
"""

import pytest

from aifos.app import App


@pytest.fixture()
def app(tmp_path):
    instance = App(tmp_path / "ws")
    yield instance
    instance.close()


def _failures(*texts):
    return {i + 1: {"error": t} for i, t in enumerate(texts)}


def test_transient_error_classification(app):
    check = app.director._is_transient_provider_error
    assert check("能力 image 没有可用 Provider(链: codex→seedream)")
    assert check("seedream5_lite API 调用失败: IncompleteRead(189400 bytes)")
    assert check("codex 退出码 1: You've hit your usage limit")
    assert check("请求超时: timed out")
    assert check("429 Too Many Requests")
    assert not check("提示词审核未通过: 同级事实互斥")
    assert not check("候选图技术完整性未通过: pixel_decode")
    assert not check("")


def test_transient_failures_get_backoff_and_extra_wave(app):
    calls = []
    failures = _failures("usage limit", "timed out")

    def wave(indices, attempt):
        calls.append((list(indices), attempt))
        failures.clear()   # 本波全部恢复

    sleeps = []
    app.director._candidate_retry_waves(
        failures, wave, sleep=sleeps.append)
    assert sleeps == [60]        # 一波即恢复,不再等第二波
    assert calls == [([1, 2], 2)]


def test_persistent_transient_uses_two_backoff_waves(app):
    calls = []
    failures = _failures("没有可用 Provider")

    def wave(indices, attempt):
        calls.append((list(indices), attempt))
        # 持续故障:failures 不变

    sleeps = []
    app.director._candidate_retry_waves(
        failures, wave, sleep=sleeps.append)
    assert sleeps == [60, 150]
    assert [c[1] for c in calls] == [2, 3]


def test_non_transient_keeps_single_immediate_retry(app):
    calls = []
    failures = _failures("提示词审核未通过: 同级事实互斥")

    def wave(indices, attempt):
        calls.append((list(indices), attempt))

    sleeps = []
    app.director._candidate_retry_waves(
        failures, wave, sleep=sleeps.append)
    assert sleeps == []          # 不退避
    assert calls == [([1], 2)]   # 原有的单次补位不变


def test_mixed_failures_do_not_backoff(app):
    failures = _failures("没有可用 Provider", "提示词审核未通过")
    calls = []
    app.director._candidate_retry_waves(
        failures, lambda i, a: calls.append(a), sleep=lambda s: None)
    assert calls == [2]          # 混合错误不退避,维持单次补位
