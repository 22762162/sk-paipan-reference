"""多集剧本文档解析：文件 → 可预览、可逐集排队的标准单集。

文档不会直接进入图片或视频模型。先按「第 N 集 / EP N」拆分，再把每集
识别为已有剧本或剧情梗概；已有剧本进入人工审阅，梗概则按集调用编剧并在
剧本审阅处暂停。支持 TXT、Markdown、JSON、DOCX，PDF 在本机有可用提取器
时支持。
"""

import copy
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .adapters.claude_script import validate_script
from .script_import import ScriptImportError, parse_any
from .smart_input import cn_to_int


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_EPISODES = 200
SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".docx", ".pdf"}

_EPISODE_RE = re.compile(
    r"^\s*(?:"
    r"第\s*(?P<cn>[0-9零一二两三四五六七八九十百]+)\s*[集话回]"
    r"|[Ee][Pp](?:[Ii][Ss][Oo][Dd][Ee])?\.?\s*0*(?P<ep>\d+)"
    r")\s*(?:[：:·.\-—]\s*)?(?P<title>.*?)\s*$"
)


class SeriesImportError(ValueError):
    pass


def _chinese_number(value):
    if value.isdigit():
        return int(value)
    direct = cn_to_int(value)
    if direct is not None:
        return direct
    # smart_input 覆盖到九十九；这里补最常见的一百到九百九十九集写法。
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "百" not in value:
        return None
    hundreds, _, rest = value.partition("百")
    h = digits.get(hundreds or "一")
    if h is None:
        return None
    if not rest:
        return h * 100
    tail = cn_to_int(rest.lstrip("零"))
    return h * 100 + (tail or 0)


def _decode_text(data):
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise SeriesImportError("无法识别文档编码；请另存为 UTF-8、DOCX 或 PDF 后重试")


def _extract_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise SeriesImportError("DOCX 文件损坏或不是有效的 Word 文档") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise SeriesImportError("DOCX 正文 XML 无法解析") from exc
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(ns + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == ns + "t" and node.text:
                parts.append(node.text)
            elif node.tag == ns + "tab":
                parts.append("\t")
            elif node.tag in (ns + "br", ns + "cr"):
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    if not paragraphs:
        raise SeriesImportError("Word 文档中没有可读取的正文")
    return "\n".join(paragraphs)


def _extract_pdf(data):
    for package in ("pypdf", "PyPDF2"):
        try:
            module = __import__(package, fromlist=["PdfReader"])
            reader = module.PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if text.strip():
                return text
        except (ImportError, OSError, ValueError):
            continue
    if shutil.which("pdftotext"):
        source = target = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(data)
                source = Path(handle.name)
            target = source.with_suffix(".txt")
            proc = subprocess.run(
                ["pdftotext", "-layout", str(source), str(target)],
                capture_output=True, timeout=60)
            if proc.returncode == 0 and target.exists():
                text = _decode_text(target.read_bytes())
                if text.strip():
                    return text
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            for path in (source, target):
                if path:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
    raise SeriesImportError(
        "当前环境无法读取该 PDF，或它是扫描图片。请优先上传 DOCX/TXT；"
        "扫描版 PDF 请先做 OCR")


def extract_document(filename, data):
    """返回 ``(正文, 格式, sha256)``，并执行大小/扩展名门禁。"""
    if not isinstance(data, (bytes, bytearray)):
        raise SeriesImportError("文档数据无效")
    if not data:
        raise SeriesImportError("文档内容为空")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise SeriesImportError("文档超过 20MB；请按卷拆分后导入")
    suffix = Path(filename or "script.txt").suffix.lower() or ".txt"
    if suffix not in SUPPORTED_SUFFIXES:
        raise SeriesImportError(
            "不支持该格式；可上传 TXT、Markdown、JSON、DOCX 或 PDF")
    if suffix == ".docx":
        text, source_format = _extract_docx(bytes(data)), "docx"
    elif suffix == ".pdf":
        text, source_format = _extract_pdf(bytes(data)), "pdf"
    else:
        text = _decode_text(bytes(data))
        source_format = "json" if suffix == ".json" else "text"
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise SeriesImportError("文档中没有可读取的正文")
    return text, source_format, hashlib.sha256(data).hexdigest()


def split_episode_text(text):
    """按集标题拆分；没有集标题时作为单集返回。"""
    items = []
    current = None
    prefix = []
    for raw in text.splitlines():
        match = _EPISODE_RE.match(raw)
        if match:
            if current is not None:
                current["text"] = "\n".join(current.pop("lines")).strip()
                items.append(current)
            elif any(line.strip() for line in prefix):
                # 封面/总纲等集前文字不丢失，附到第一集的剧情来源前。
                prefix = [line for line in prefix if line.strip()]
            raw_number = match.group("cn") or match.group("ep")
            current = {
                "source_number": _chinese_number(raw_number),
                "title": (match.group("title") or "").strip(" ：:·.-—"),
                "lines": list(prefix),
            }
            prefix = []
        elif current is None:
            prefix.append(raw)
        else:
            current["lines"].append(raw)
    if current is not None:
        current["text"] = "\n".join(current.pop("lines")).strip()
        items.append(current)
    else:
        items = [{"source_number": None, "title": "", "text": text.strip()}]
    items = [item for item in items if item["text"]]
    if not items:
        raise SeriesImportError("检测到分集标题，但每集正文都是空的")
    if len(items) > MAX_EPISODES:
        raise SeriesImportError(f"一次最多导入 {MAX_EPISODES} 集，请分批导入")
    return items


def _standard_script(value, project_title, episode_number, title=""):
    script = copy.deepcopy(value)
    error = validate_script(
        script, {"project_title": project_title,
                 "episode_number": episode_number})
    if error:
        raise ScriptImportError(error)
    script["project_title"] = project_title
    script["episode_number"] = episode_number
    if title:
        script["episode_title"] = title
    return script


def _classify_text(text, project_title, episode_number, title=""):
    try:
        script = parse_any(text, project_title, episode_number)
    except ScriptImportError:
        return "outline", None
    script["project_title"] = project_title
    script["episode_number"] = episode_number
    if title:
        script["episode_title"] = title
    return "script", script


def _json_entries(text):
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SeriesImportError(f"JSON 文档解析失败：{exc}") from exc
    if isinstance(payload, dict) and payload.get("scenes"):
        return [payload]
    if isinstance(payload, dict) and isinstance(payload.get("episodes"), list):
        return payload["episodes"]
    if isinstance(payload, list):
        return payload
    raise SeriesImportError(
        "多集 JSON 需要是数组或 {\"episodes\": [...]}；单集需包含 scenes")


def _parse_json_series(text, project_title, start_number):
    parsed = []
    for index, entry in enumerate(_json_entries(text), start=0):
        number = start_number + index
        if isinstance(entry, str):
            title, source_number, source_text = "", None, entry
            mode, script = _classify_text(
                source_text, project_title, number, title)
        elif isinstance(entry, dict):
            title = str(entry.get("episode_title") or entry.get("title") or "")
            source_number = entry.get("episode_number") or entry.get("number")
            candidate = entry.get("script")
            if isinstance(candidate, dict):
                source_text = json.dumps(candidate, ensure_ascii=False)
                try:
                    script = _standard_script(
                        candidate, project_title, number, title)
                    mode = "script"
                except ScriptImportError as exc:
                    raise SeriesImportError(
                        f"第 {index + 1} 项剧本结构无效：{exc}") from exc
            elif entry.get("scenes"):
                source_text = json.dumps(entry, ensure_ascii=False)
                try:
                    script = _standard_script(
                        entry, project_title, number, title)
                    mode = "script"
                except ScriptImportError as exc:
                    raise SeriesImportError(
                        f"第 {index + 1} 项剧本结构无效：{exc}") from exc
            else:
                source_text = str(
                    candidate or entry.get("script_text") or entry.get("content")
                    or entry.get("plot") or entry.get("premise") or "").strip()
                if not source_text:
                    raise SeriesImportError(f"第 {index + 1} 项没有剧情或剧本正文")
                mode, script = _classify_text(
                    source_text, project_title, number, title)
        else:
            raise SeriesImportError(f"第 {index + 1} 项必须是文本或对象")
        parsed.append({
            "episode_number": number,
            "source_number": int(source_number) if str(source_number).isdigit() else None,
            "title": title or f"第{number}集",
            "source_text": source_text.strip(),
            "mode": mode,
            "script": script,
        })
    return parsed


def parse_series_document(filename, data, project_title, start_number=1):
    """提取并拆分文档，返回批次元数据及逐集标准化结果。"""
    try:
        start_number = int(start_number)
    except (TypeError, ValueError) as exc:
        raise SeriesImportError("起始集数必须是正整数") from exc
    if start_number < 1:
        raise SeriesImportError("起始集数必须是正整数")
    project_title = str(project_title or "").strip()
    if not project_title:
        raise SeriesImportError("作品名不能为空")
    text, source_format, digest = extract_document(filename, data)
    if source_format == "json":
        episodes = _parse_json_series(text, project_title, start_number)
    else:
        episodes = []
        for index, item in enumerate(split_episode_text(text)):
            number = start_number + index
            title = item["title"] or f"第{number}集"
            mode, script = _classify_text(
                item["text"], project_title, number, title)
            episodes.append({
                "episode_number": number,
                "source_number": item["source_number"],
                "title": title,
                "source_text": item["text"],
                "mode": mode,
                "script": script,
            })
    if not episodes:
        raise SeriesImportError("没有从文档中识别到任何剧集")
    if len(episodes) > MAX_EPISODES:
        raise SeriesImportError(f"一次最多导入 {MAX_EPISODES} 集，请分批导入")
    for episode in episodes:
        script = episode.get("script") or {}
        episode["char_count"] = len(episode["source_text"])
        episode["scene_count"] = len(script.get("scenes", []))
        episode["characters"] = [
            character.get("name", "")
            for character in script.get("characters", [])
            if character.get("name")]
        episode["excerpt"] = re.sub(
            r"\s+", " ", episode["source_text"]).strip()[:160]
    return {
        "filename": Path(filename or "script.txt").name,
        "source_format": source_format,
        "source_sha256": digest,
        "project_title": project_title,
        "start_number": start_number,
        "total": len(episodes),
        "episodes": episodes,
    }


def preview_payload(parsed):
    """移除完整正文/标准剧本，只把审阅所需摘要返回浏览器。"""
    return {
        key: value for key, value in parsed.items() if key != "episodes"
    } | {
        "episodes": [{
            key: episode[key] for key in (
                "episode_number", "source_number", "title", "mode",
                "char_count", "scene_count", "characters", "excerpt")
        } for episode in parsed["episodes"]]
    }
