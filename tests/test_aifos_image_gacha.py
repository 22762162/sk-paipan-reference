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


def test_repair_round_draws_all_three_then_selects_best_pass(tmp_path):
    director = _director()
    target = tmp_path / "shot_015.keyframe.png"
    calls = {"n": 0}

    def fake(cap, payload, out_dir, cancel, qc_spec):
        calls["n"] += 1
        target.write_bytes(b"PULL%d" % calls["n"])
        result = _Res(
            str(target), 1.0, calls["n"] in (1, 2), 0)
        if calls["n"] == 1:
            result.qc.update({
                "identity_checked": True, "identity_match": True,
                "count_checked": True, "count_match": True,
            })
        elif calls["n"] == 2:
            result.qc.update({
                "image_passed": True,
                "identity_checked": True, "identity_match": True,
                "gender_checked": True, "gender_match": True,
                "wardrobe_checked": True, "wardrobe_match": True,
                "count_checked": True, "count_match": True,
                "physical_logic_checked": True,
                "physical_logic_match": True,
                "spatial_logic_checked": True,
                "spatial_logic_match": True,
                "input_contract_passed": True,
            })
        return result

    director._generate_image_with_qc = fake
    result = director._generate_image_gacha(
        "image",
        {
            "_gacha_pulls_override": 3,
            "_gacha_select_best_after_all": True,
        },
        tmp_path, None, {"s": 1})

    assert calls["n"] == 3              # 第1张通过也必须抽满3张
    assert result.qc["passed"] is True
    assert result.qc["gacha"]["selected_pull"] == 2
    assert target.read_bytes() == b"PULL2"
    assert result.cost == 3.0
    assert not list(tmp_path.glob("*.gacha*"))


def test_repair_round_freezes_first_reviewed_prompt_for_all_three(tmp_path):
    director = _director()
    target = tmp_path / "shot_018.keyframe.png"
    seen = []

    def fake(cap, payload, out_dir, cancel, qc_spec):
        seen.append({
            "prompt": payload.get("prompt"),
            "prompt_review": payload.get("prompt_review"),
            "feedback": payload.get("feedback"),
        })
        target.write_bytes(b"PULL%d" % len(seen))
        result = _Res(str(target), 1.0, True, 0)
        optimized = (
            "Codex冻结后的同一修订提示词"
            if len(seen) == 1 else payload.get("prompt"))
        result.data = {
            "prompt_optimized": optimized,
            "prompt_review": {
                "approved": True,
                "reviewed_input_hash": "frozen-review",
            },
        }
        return result

    director._generate_image_with_qc = fake
    result = director._generate_image_gacha(
        "image",
        {
            "prompt": "第一次失败后的原始定向修订",
            "feedback": "首图失败后 Codex 给出的定向修改意见",
            "_gacha_pulls_override": 3,
            "_gacha_select_best_after_all": True,
        },
        tmp_path, None, {"s": 1})

    assert len(seen) == 3
    assert seen[0]["prompt"] == "第一次失败后的原始定向修订"
    assert seen[1]["prompt"] == "Codex冻结后的同一修订提示词"
    assert seen[2]["prompt"] == "Codex冻结后的同一修订提示词"
    assert seen[0]["feedback"] == "首图失败后 Codex 给出的定向修改意见"
    assert seen[1]["feedback"] == ""
    assert seen[2]["feedback"] == ""
    assert seen[1]["prompt_review"]["approved"] is True
    assert seen[2]["prompt_review"]["approved"] is True
    assert result.qc["gacha"]["same_prompt"] is True
    hashes = {
        row["prompt_hash"]
        for row in result.qc["gacha"]["candidates"]
    }
    assert hashes == {result.qc["gacha"]["frozen_prompt_hash"]}


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


def test_repair_gacha_reuse_requires_three_identical_prompt_hashes():
    frozen = "frozen-hash"
    valid = {
        "qc": {
            "gacha": {
                "pulls": 3,
                "select_after_all": True,
                "same_prompt": True,
                "frozen_prompt_hash": frozen,
                "candidates": [
                    {"pull": index, "prompt_hash": frozen}
                    for index in range(1, 4)
                ],
            },
        },
    }
    assert Director._repair_gacha_prompt_invariant_valid(valid) is True
    drifted = {
        "qc": {
            "gacha": {
                **valid["qc"]["gacha"],
                "same_prompt": False,
                "candidates": [
                    {"pull": 1, "prompt_hash": frozen},
                    {"pull": 2, "prompt_hash": "drifted-hash"},
                    {"pull": 3, "prompt_hash": frozen},
                ],
            },
        },
    }
    assert Director._repair_gacha_prompt_invariant_valid(drifted) is False
    assert Director._repair_gacha_prompt_invariant_valid(
        {"qc": {"passed": True}}) is True
