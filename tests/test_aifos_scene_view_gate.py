"""帧链前置闸门:场景必须有 720° 全景母版才许出帧。

v6 实测(27 agent 审查,6/8 镜 fail):单角度场景母版只锁得住同方向镜头,
九张关键帧各画各的房间,视频段内出现「换布景级跳变」。全景功能
(expand_scene_views)一直在,但产线不强制,等于没有。
"""
import unittest
from unittest import mock

from aifos.director import Director
from aifos.errors import AifosError


def _director(flag):
    d = Director.__new__(Director)
    d.config = mock.Mock()
    d.config.get = lambda *a, **k: flag
    d.assets = mock.Mock()
    d.assets.latest.return_value = None       # 没有任何全景资产
    return d


def _ctx():
    return {"project": {"id": 14},
            "script": {"scenes": [{"location": "书阁内·暖金古室"}]}}


class SceneViewGateTest(unittest.TestCase):
    def test_gate_off_by_default_for_legacy_projects(self):
        d = _director(False)
        d._require_scene_views(_ctx())        # 不抛错:存量与测试不受影响

    def test_missing_panorama_fails_closed_with_remediation(self):
        d = _director(True)
        with self.assertRaises(AifosError) as ctx:
            d._require_scene_views(_ctx())
        message = str(ctx.exception)
        self.assertIn("书阁内·暖金古室", message)
        self.assertIn("expand_scene_views", message,
                      "错误必须告诉人怎么修,不能只说不行")

    def test_existing_panorama_passes(self):
        d = _director(True)
        row = {"uri": __file__}               # 指向真实存在的文件即可
        d.assets.latest.return_value = row
        d._require_scene_views(_ctx())        # 不抛错

    def test_gate_runs_before_frame_generation(self):
        import inspect
        # 同上:读模块文件,避开遗留 monkeypatch
        from aifos import director as director_module
        src = inspect.getsource(director_module)
        body = src[src.find("def _stage_frames(self, ctx):"):][:600]
        self.assertIn("_require_scene_views", body)


if __name__ == "__main__":
    unittest.main()
