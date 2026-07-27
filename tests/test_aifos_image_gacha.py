"""连抽选优:按重要度分档、首过早停、全败恢复最优、成本不漏账。"""

import pathlib
import tempfile

from aifos.director import Director


class _Log:
    def info(self, *args):
        pass


class _Cfg:
    def get(self, *args, default=None):
        return {"important": 2, "final": 3}


class _Res:
    def __init__(self, uri, cost, passed, score):
        self.uri, self.cost = uri, cost
        self.qc = {"passed": passed, "score": score}
        self.data, self.provider = {}, "test"


def _director():
    director = Director.__new__(Director)
    director.log = _Log()
    director.config = _Cfg()
    director._image_qc_enabled = lambda: True
    return director


def test_gacha_pulls_by_task_class():
    director = _director()
    assert director._gacha_pulls({"image_task_class": "batch"}) == 1
    assert director._gacha_pulls({"image_task_class": "important"}) == 2
    assert director._gacha_pulls({"image_task_class": "final"}) == 3
    assert director._gacha_pulls({}) == 1          # 未分类不加抽


def test_gacha_stops_on_first_pass_and_sums_cost(tmp_path):
    director = _director()
    target = tmp_path / "shot_009.keyframe.png"
    calls = {"n": 0}

    def fake(cap, payload, out_dir, cancel, qc_spec):
        calls["n"] += 1
        target.write_bytes(b"IMG%d" % calls["n"])
        return _Res(str(target), 1.0, calls["n"] == 2, 50)

    director._generate_image_with_qc = fake
    result = director._generate_image_gacha(
        "image", {"image_task_class": "final"}, tmp_path, None, {"s": 1})
    assert calls["n"] == 2                 # 第2抽过即停,不抽第3张
    assert result.qc["passed"] and result.cost == 2.0
    assert not list(tmp_path.glob("*.gacha*"))


def test_gacha_restores_best_when_all_fail(tmp_path):
    director = _director()
    target = tmp_path / "shot_009.keyframe.png"
    calls = {"n": 0}
    scores = [90, 50, 30]

    def fake(cap, payload, out_dir, cancel, qc_spec):
        calls["n"] += 1
        target.write_bytes(b"PULL%d" % calls["n"])
        return _Res(str(target), 1.0, False, scores[calls["n"] - 1])

    director._generate_image_with_qc = fake
    result = director._generate_image_gacha(
        "image", {"image_task_class": "final"}, tmp_path, None, {"s": 1})
    assert calls["n"] == 3 and not result.qc["passed"]
    assert target.read_bytes() == b"PULL1"     # 分最高的第1抽被恢复
    assert result.cost == 3.0
    assert not list(tmp_path.glob("*.gacha*"))


def test_gacha_skips_batch_and_frames(tmp_path):
    director = _director()
    target = tmp_path / "x.png"
    calls = {"n": 0}

    def fake(cap, payload, out_dir, cancel, qc_spec):
        calls["n"] += 1
        target.write_bytes(b"1")
        return _Res(str(target), 1.0, False, 10)

    director._generate_image_with_qc = fake
    director._generate_image_gacha(
        "image", {"image_task_class": "batch"}, tmp_path, None, {"s": 1})
    assert calls["n"] == 1
    calls["n"] = 0
    director._generate_image_gacha(
        "frames", {"image_task_class": "final"}, tmp_path, None, {"s": 1})
    assert calls["n"] == 1                 # frames 成对语义,不参与连抽
