"""AIFOS 内置 3D 空间查看器:全景 × blocking 三维调度叠加。"""
import inspect
import unittest
from pathlib import Path

from aifos.web import server as web_server


class Scene3dPayloadTest(unittest.TestCase):
    def test_payload_builder_exists_and_merges_pano_with_blocking(self):
        src = inspect.getsource(web_server._scene3d_payload)
        self.assertIn('"blocking"', src)
        self.assertIn("::view:panorama", src,
                      "必须按场景名取 720° 全景母版")
        self.assertIn("/artifacts/", src,
                      "全景必须转成可被页面访问的 /artifacts/ 相对 URL")

    def test_routes_registered(self):
        src = inspect.getsource(web_server)
        self.assertIn('route == "/scene3d"', src)
        self.assertIn('route == "/api/scene3d"', src)

    def test_page_ships_with_static_assets(self):
        page = (Path(web_server.__file__).parent
                / "static" / "scene3d.html")
        self.assertTrue(page.exists())
        html = page.read_text(encoding="utf-8")
        # 三层结合缺一不可:全景投影 / 火柴人 / 机位视锥
        self.assertIn("stickman", html, "火柴人必须按真人比例(height_m)画")
        self.assertIn("CAPTURE", html, "全景必须从拍摄点采样投到真实房间盒")
        self.assertIn("进入本镜机位", html)
        # 等距采样公式自带垂直方向,再翻转=天地颠倒(用户实测抓过)
        self.assertIn("UNPACK_FLIP_Y_WEBGL,0", html)


if __name__ == "__main__":
    unittest.main()
