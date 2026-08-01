"""失败经验按当前镜头检索，不能把整库历史塞入提示词。"""

from aifos.lessons import (DOMAIN_IMAGE, DOMAIN_VIDEO, lesson_lines,
                           record_lessons, set_lesson_approval)


class FakeAssets:
    """只实现经验库所需的资产接口，避免把测试耦合到整条产线。"""

    def __init__(self):
        self.rows = {}

    def latest(self, project_id, kind, name):
        return self.rows.get((project_id, kind, name))

    def active_list(self, project_id, kind=None):
        return [row for (pid, row_kind, _name), row in self.rows.items()
                if pid == project_id and (kind is None or row_kind == kind)]

    def register(self, project_id, kind, name, uri="", meta=None,
                 new_version=False):
        key = (project_id, kind, name)
        old = self.rows.get(key) or {}
        self.rows[key] = {
            "id": old.get("id", len(self.rows) + 1),
            "name": name,
            "kind": kind,
            "uri": uri,
            "meta": dict(meta or {}),
            "version": int(old.get("version") or 0) + 1,
        }
        return self.rows[key]


def _approve_all(assets, project_id):
    for row in assets.active_list(project_id, kind="lesson"):
        set_lesson_approval(assets, project_id, row["name"], True)


def test_lesson_categories_are_inferred_and_ranked_for_current_shot():
    assets = FakeAssets()
    pid = 1
    record_lessons(
        assets, pid,
        ["古代书房出现现代笔记本电脑，时代和道具都不符合剧情"],
        category="shot_image")
    record_lessons(
        assets, pid,
        ["人物脸型和发型与定版参考图不同，发生身份漂移"],
        category="shot_image")
    record_lessons(
        assets, pid,
        ["对白口型没有与中文配音同步"],
        category="video", domain=DOMAIN_VIDEO)
    _approve_all(assets, pid)

    image = lesson_lines(
        assets, pid, domain=DOMAIN_IMAGE,
        categories=["era", "prop"], context="明代书房，案上放置交割册")
    assert len(image) == 1
    assert "笔记本电脑" in image[0]
    assert "脸型" not in image[0]

    video = lesson_lines(
        assets, pid, domain=DOMAIN_VIDEO,
        categories=["audio"], context="中文对白与口型同步")
    assert len(video) == 1
    assert "口型" in video[0]


def test_relevant_lesson_injection_is_capped_at_five():
    assets = FakeAssets()
    pid = 2
    for index in range(8):
        record_lessons(
            assets, pid,
            [f"道具物理轨迹错误编号{index}，出现漂浮或瞬移"],
            category="shot_image")
    _approve_all(assets, pid)
    lines = lesson_lines(
        assets, pid, categories=["physics", "prop"],
        context="道具移动必须服从重力和连续轨迹")
    assert len(lines) == 5
