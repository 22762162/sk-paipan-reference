"""剪映真实草稿产线测试:注入假 pyJianYingDraft 验证草稿装配与回退。"""

import sys
import types
from pathlib import Path

import pytest

from aifos.app import App
from aifos.errors import ProviderError
from aifos.production.jianying_draft import JianyingDraftProvider

MP4 = b"\x00\x00\x00 ftypisom" + b"\x00" * 32


class _FakeScript:
    def __init__(self, name, width, height, root):
        self.name, self.width, self.height = name, width, height
        self.root = root
        self.tracks, self.segments = [], []
        self.saved = False

    def add_track(self, kind):
        self.tracks.append(kind)

    def add_segment(self, segment, *args):
        self.segments.append(segment)

    def save(self):
        (self.root / self.name).mkdir(parents=True, exist_ok=True)
        (self.root / self.name / "draft_content.json").write_text(
            "{}", encoding="utf-8")
        self.saved = True


def _fake_module(created):
    """构造与 pyJianYingDraft 新版同名的假模块。"""
    mod = types.ModuleType("pyJianYingDraft")

    class DraftFolder:
        def __init__(self, path):
            self.path = Path(path)

        def create_draft(self, name, width, height):
            script = _FakeScript(name, width, height, self.path)
            created.append(script)
            return script

    class TrackType:
        video, text, audio = "video", "text", "audio"

    def trange(start, duration):
        return (start, duration)

    mod.DraftFolder = DraftFolder
    mod.TrackType = TrackType
    mod.trange = trange
    mod.VideoSegment = lambda src, span: ("video", str(src), span)
    mod.TextSegment = lambda text, span: ("text", text, span)
    mod.AudioSegment = lambda src, span: ("audio", str(src), span)
    return mod


@pytest.fixture()
def fake_jd(monkeypatch):
    created = []
    monkeypatch.setitem(sys.modules, "pyJianYingDraft",
                        _fake_module(created))
    return created


def _provider(draft_dir):
    return JianyingDraftProvider("jianying", {
        "type": "jianying_draft", "enabled": True,
        "capabilities": ["edit"], "draft_dir": str(draft_dir),
        "cost_per_call": 0.3})


def _payload(tmp_path):
    shot1 = tmp_path / "shot_001.mp4"
    shot2 = tmp_path / "shot_002.mp4"
    shot1.write_bytes(MP4)
    shot2.write_bytes(MP4)
    return {
        "project_title": "万妖图录", "episode_number": 3,
        "aspect": "9:16", "width": 1080, "height": 1920,
        "shots": [
            {"shot_no": 1, "uri": str(shot1), "duration": 2.5,
             "dialogue": None},
            {"shot_no": 2, "uri": str(shot2), "duration": 3.0,
             "dialogue": {"character": "林昭", "dialogue": "妖气不对劲"}},
        ],
        "voices": [],
        "subtitles": [{"line_no": 1, "character": "林昭",
                       "text": "妖气不对劲"}],
    }


def test_draft_build(fake_jd, tmp_path):
    draft_dir = tmp_path / "JianyingPro Drafts"
    draft_dir.mkdir()
    provider = _provider(draft_dir)
    assert provider.available("edit") == (True, "")
    result = provider.generate("edit", _payload(tmp_path), tmp_path / "out")
    script = fake_jd[0]
    assert script.width == 1080 and script.height == 1920
    assert "万妖图录_第3集" in script.name
    assert script.saved
    # 2 段视频 + 1 段字幕;无独立配音(有声视频方案)则不建音频轨
    kinds = [s[0] for s in script.segments]
    assert kinds.count("video") == 2 and kinds.count("text") == 1
    assert "audio" not in script.tracks
    subtitle = next(s for s in script.segments if s[0] == "text")
    assert subtitle[1] == "妖气不对劲"
    assert result.uri.endswith(script.name)
    assert "打开剪映" in result.data["note"]


def test_draft_with_voice_files(fake_jd, tmp_path):
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    voice = tmp_path / "line_001.mp3"
    voice.write_bytes(b"ID3xx")
    payload = _payload(tmp_path)
    payload["voices"] = [{"line_no": 1, "uri": str(voice),
                          "duration": 1.4}]
    provider = _provider(draft_dir)
    provider.generate("edit", payload, tmp_path / "out")
    script = fake_jd[0]
    assert "audio" in script.tracks
    assert any(s[0] == "audio" for s in script.segments)


def test_available_without_library(tmp_path, monkeypatch):
    import importlib.util as iu
    monkeypatch.setattr(iu, "find_spec", lambda name: None)
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    provider = _provider(draft_dir)
    ok, reason = provider.available("edit")
    assert not ok and "pyJianYingDraft" in reason


def test_available_without_draft_dir(fake_jd, tmp_path):
    provider = _provider(tmp_path / "不存在的目录")
    ok, reason = provider.available("edit")
    assert not ok and "草稿" in reason


def test_missing_video_noted(fake_jd, tmp_path):
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    payload = _payload(tmp_path)
    payload["shots"].append(
        {"shot_no": 3, "uri": "/不存在/shot_003.mp4", "duration": 2.0,
         "dialogue": None})
    provider = _provider(draft_dir)
    result = provider.generate("edit", payload, tmp_path / "out")
    assert result.data["missing_shots"] == [3]


def test_produce_routes_edit_to_jianying(fake_jd, tmp_path):
    """整集制作:edit 环节走剪映草稿,产出真实草稿并计入阶段。"""
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    app = App(tmp_path / "ws", config_overrides={
        "providers": {"jianying": {"enabled": True,
                                       "draft_dir": str(draft_dir)}}})
    try:
        summary = app.director.produce("草稿联测", 1)
        assert summary["status"] == "done"
        project = app.projects.get_project("草稿联测")
        episode = app.db.query_one(
            "SELECT * FROM episodes WHERE project_id=? AND number=1",
            (project["id"],))
        script, _ = app.projects.latest_document(episode["id"], "script")
        for character in script["characters"]:
            app.director.select_character_candidate(
                "草稿联测", 1, character["name"], 1)
        summary = app.director.produce("草稿联测", 1)
        edit_stage = next(s for s in summary["stages"]
                          if s["stage"] == "edit")
        assert edit_stage["status"] == "done"
        assert "jianying" in edit_stage["providers"]
    finally:
        app.close()
    assert fake_jd and fake_jd[0].saved
    assert "草稿联测_第1集" in fake_jd[0].name


def test_generate_error_is_provider_error(fake_jd, tmp_path):
    """草稿库目录消失等异常 → ProviderError,路由可回退内置合成。"""
    provider = _provider(tmp_path / "gone")
    with pytest.raises(ProviderError):
        provider.generate("edit", _payload(tmp_path), tmp_path / "out")
