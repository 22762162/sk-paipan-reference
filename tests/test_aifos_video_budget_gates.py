"""视频层预算与时长闸门:钱只许花在明确要花的地方。

背景(2026-07-29 实账):v6 迭代验证全量 8 镜误上 vip 1080p,多花 960 积分;
而该版的主要改动(视线连贯性)用 mini 720p 完全能验。另:分镜偶发产出
1.5-3s 镜头,Seedance 全家族下限 4s,提交必被拒收,白付一次往返。
"""
import unittest
from unittest import mock

from aifos.errors import ProviderError
from aifos.production import dreamina as dreamina_module
from aifos.production.dreamina import DreaminaProvider


class _ReachedCli(RuntimeError):
    """哨兵:闸门放行、请求已走到真实 CLI 调用层。"""


def _provider(conf=None):
    provider = DreaminaProvider.__new__(DreaminaProvider)
    provider.conf = conf or {}
    return provider


def _payload(**over):
    base = {"first": "/tmp/a.png", "last": "/tmp/b.png",
            "prompt": "p", "shot_no": 1, "duration": 5}
    base.update(over)
    return base


def _generate(payload, conf=None):
    """跑到 CLI 层即抛哨兵——测试只关心闸门,不真发请求。"""
    provider = _provider(conf)
    with mock.patch.object(dreamina_module, "run_interruptible",
                           side_effect=_ReachedCli()):
        provider.generate("video", payload, "/tmp/out")


class BudgetGateTest(unittest.TestCase):
    def test_1080p_without_final_confirmation_fails_closed(self):
        with self.assertRaises(ProviderError) as ctx:
            _generate(_payload(video_resolution="1080p"))
        self.assertIn("最终成片档", str(ctx.exception))
        self.assertIn("video_final_confirmed", str(ctx.exception))

    def test_720p_needs_no_confirmation(self):
        """迭代档不设关卡——闸门只拦贵的那条路。"""
        with self.assertRaises(_ReachedCli):
            _generate(_payload(video_resolution="720p"))

    def test_confirmed_final_cut_passes_the_gate(self):
        with self.assertRaises(_ReachedCli):
            _generate(_payload(video_resolution="1080p",
                               video_final_confirmed=True))

    def test_conf_escape_hatch_for_batch_pipelines(self):
        with self.assertRaises(_ReachedCli):
            _generate(_payload(video_resolution="1080p"),
                      conf={"allow_vip_without_confirmation": True})

    def test_1080p_final_cut_auto_selects_vip_model(self):
        """1080p 只有 vip 型号支持——反选必须发生,否则静默降档白烧钱。"""
        captured = {}

        def _capture(tag, cmd, _cwd, _timeout, cancel=None):
            captured["cmd"] = cmd
            raise _ReachedCli()

        provider = _provider({"model_version": "seedance2.0mini"})
        with mock.patch.object(dreamina_module, "run_interruptible",
                               side_effect=_capture):
            with self.assertRaises(_ReachedCli):
                provider.generate(
                    "video",
                    _payload(video_resolution="1080p",
                             video_final_confirmed=True),
                    "/tmp/out")
        joined = " ".join(captured["cmd"])
        self.assertIn("--model_version=seedance2.0_vip", joined)


class DurationFloorTest(unittest.TestCase):
    def test_sub_four_second_shot_is_rejected_with_storyboard_pointer(self):
        with self.assertRaises(ProviderError) as ctx:
            _generate(_payload(duration=2.5))
        message = str(ctx.exception)
        self.assertIn("最短 4 秒", message)
        self.assertIn("分镜", message, "错误必须指回责任层,而不是让人猜")

    def test_exactly_four_seconds_passes_the_floor(self):
        with self.assertRaises(_ReachedCli):
            _generate(_payload(duration=4))


class RatioGateTest(unittest.TestCase):
    def test_multimodal_path_always_passes_ratio(self):
        """有参考图时走 multimodal2video,不传 --ratio 会被横版调度图带偏,
        9:16 竖屏实测出成 1280x720 横屏。"""
        captured = {}

        def _capture(tag, cmd, _cwd, _timeout, cancel=None):
            captured["cmd"] = cmd
            raise _ReachedCli()

        provider = _provider()
        with mock.patch.object(dreamina_module, "run_interruptible",
                               side_effect=_capture):
            with self.assertRaises(_ReachedCli):
                provider.generate(
                    "video",
                    _payload(reference_images=["/tmp/ref.png"],
                             aspect="9:16"),
                    "/tmp/out")
        joined = " ".join(captured["cmd"])
        self.assertIn("multimodal2video", joined)
        self.assertIn("--ratio=9:16", joined)


if __name__ == "__main__":
    unittest.main()


class ConfirmationWiringTest(unittest.TestCase):
    def test_manual_quality_choice_becomes_final_confirmation(self):
        """确认页亲手选高档(source=manual)= 确认;auto 推出的高档照拦。"""
        import inspect

        from aifos import director

        src = inspect.getsource(director)
        self.assertIn(
            '"video_final_confirmed": quality.get("source") == "manual"',
            src,
            "视频 payload 必须把 manual 档位翻译成确认标记,"
            "否则正常的高档生产会被预算闸门误拦")


class PublicQueueUpgradeTest(unittest.TestCase):
    """非 VIP 型号走公共排队,不是省钱是换时间(实测滞留 40 分钟~2 小时);
    且 mini 45 积分反而比 fast 25 积分更贵。一律升到对应 VIP 型号。"""

    def _model_of(self, conf, payload=None):
        captured = {}

        def _capture(tag, cmd, _cwd, _timeout, cancel=None):
            captured["cmd"] = cmd
            raise _ReachedCli()

        provider = _provider(conf)
        with mock.patch.object(dreamina_module, "run_interruptible",
                               side_effect=_capture):
            with self.assertRaises(_ReachedCli):
                provider.generate("video", _payload(**(payload or {})),
                                  "/tmp/out")
        joined = " ".join(captured["cmd"])
        match = [a for a in captured["cmd"]
                 if a.startswith("--model_version=")]
        self.assertTrue(match, joined)
        return match[0].split("=", 1)[1]

    def test_mini_upgrades_to_fast_vip(self):
        self.assertEqual(
            self._model_of({"model_version": "seedance2.0mini"}),
            "seedance2.0fast_vip")

    def test_fast_upgrades_to_fast_vip(self):
        self.assertEqual(
            self._model_of({"model_version": "seedance2.0fast"}),
            "seedance2.0fast_vip")

    def test_plain_20_upgrades_to_vip(self):
        self.assertEqual(
            self._model_of({"model_version": "seedance2.0"}),
            "seedance2.0_vip")

    def test_vip_models_are_left_alone(self):
        self.assertEqual(
            self._model_of({"model_version": "seedance2.0fast_vip"}),
            "seedance2.0fast_vip")

    def test_public_queue_can_be_opted_into_explicitly(self):
        self.assertEqual(
            self._model_of({"model_version": "seedance2.0mini",
                            "allow_public_queue": True}),
            "seedance2.0mini")

    def test_1080p_still_reverse_selects_full_vip(self):
        """分辨率反选优先级高于队列升级:1080p 只有 seedance2.0_vip 支持。"""
        self.assertEqual(
            self._model_of({"model_version": "seedance2.0mini"},
                           {"video_resolution": "1080p",
                            "video_final_confirmed": True}),
            "seedance2.0_vip")
