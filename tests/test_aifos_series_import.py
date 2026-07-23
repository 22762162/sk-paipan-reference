"""多集文档解析与串行队列测试。"""

import io
import json
import zipfile

import pytest

from aifos.app import App
from aifos.errors import AifosError
from aifos.series_import import (
    SeriesImportError, extract_document, parse_series_document,
    split_episode_text)


SERIES_TEXT = """第1集 初见
第1场 旧办公室
林昭:这里就是新公司?
团长:设备旧，账是干净的。

第2集 反转
林昭发现直播榜单被人动过，决定连夜追查数据来源。
"""


def _docx_bytes(paragraphs):
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = ("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
           "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
           f"<w:body>{body}</w:body></w:document>")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return stream.getvalue()


def test_split_and_classify_script_vs_outline():
    split = split_episode_text(SERIES_TEXT)
    assert [item["source_number"] for item in split] == [1, 2]
    assert [item["title"] for item in split] == ["初见", "反转"]
    parsed = parse_series_document(
        "十集剧本.txt", SERIES_TEXT.encode(), "逆光成团", start_number=8)
    assert parsed["total"] == 2
    assert [item["episode_number"] for item in parsed["episodes"]] == [8, 9]
    assert parsed["episodes"][0]["mode"] == "script"
    assert parsed["episodes"][0]["script"]["episode_title"] == "初见"
    assert parsed["episodes"][1]["mode"] == "outline"
    assert parsed["episodes"][1]["script"] is None


def test_novel_dialogue_is_classified_as_script_not_outline():
    novel = """第1集 宫门急报
乾清宫内，王承恩快步入殿。
朱慈烺咬牙道：“父皇，儿臣请战！”
“你可知此去凶险？”崇祯沉声问道。
"""
    parsed = parse_series_document(
        "大明小说.txt", novel.encode(), "大明", start_number=1)
    episode = parsed["episodes"][0]
    assert episode["mode"] == "script"
    assert episode["import_analysis"]["source_format"] == "novel"
    assert episode["import_analysis"]["dialogue_count"] == 2
    assert episode["characters"] == ["朱慈烺", "崇祯"]


def test_docx_and_json_series_are_supported():
    raw = _docx_bytes(SERIES_TEXT.splitlines())
    text, kind, digest = extract_document("series.docx", raw)
    assert kind == "docx" and "第1集 初见" in text and len(digest) == 64
    parsed = parse_series_document("series.docx", raw, "逆光成团", 1)
    assert parsed["total"] == 2

    script = parsed["episodes"][0]["script"]
    payload = {"episodes": [script, {
        "title": "只有梗概", "premise": "主角决定公开所有证据。"}]}
    parsed_json = parse_series_document(
        "series.json", json.dumps(payload, ensure_ascii=False).encode(),
        "逆光成团", 10)
    assert [item["mode"] for item in parsed_json["episodes"]] == [
        "script", "outline"]
    assert parsed_json["episodes"][0]["script"]["episode_number"] == 10


def test_document_format_and_size_gates():
    with pytest.raises(SeriesImportError, match="不支持"):
        extract_document("series.pages", b"hello")
    with pytest.raises(SeriesImportError, match="内容为空"):
        extract_document("series.txt", b"")


def test_series_center_imports_all_but_activates_one(tmp_path):
    app = App(tmp_path / "ws")
    try:
        parsed = parse_series_document(
            "series.txt", SERIES_TEXT.encode(), "逆光成团", 1)
        batch = app.series.import_batch(parsed, auto_advance=True)
        assert batch["total"] == 2
        assert [item["episode_status"] for item in batch["items"]] == [
            "queued_script", "queued_script"]

        first = app.series.activate_next(batch["id"])
        assert first["mode"] == "script" and first["number"] == 1
        assert app.projects.get_episode(first["episode_id"])["status"] == \
            "awaiting_script"
        with pytest.raises(AifosError, match="尚未完成"):
            app.series.activate_next(batch["id"])

        app.projects.set_episode_status(first["episode_id"], "done")
        second = app.series.activate_next(batch["id"])
        assert second["mode"] == "outline" and second["number"] == 2
        assert app.projects.get_episode(second["episode_id"])["status"] == \
            "created"
        source, version = app.projects.latest_document(
            second["episode_id"], "series_source")
        assert version == 1 and source["filename"] == "series.txt"
        assert source["mode"] == "outline"
    finally:
        app.close()


def test_series_import_is_atomic_on_episode_conflict(tmp_path):
    app = App(tmp_path / "ws")
    try:
        parsed = parse_series_document(
            "series.txt", SERIES_TEXT.encode(), "逆光成团", 1)
        app.series.import_batch(parsed)
        with pytest.raises(AifosError, match="已经存在"):
            app.series.import_batch(parsed)
        project = app.projects.get_project("逆光成团")
        assert len(app.projects.list_episodes(project["id"])) == 2
        assert len(app.series.list_batches(project["id"])) == 1
    finally:
        app.close()
