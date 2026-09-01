"""剪映真实草稿产线:剪辑结果直接进剪映草稿库。

剪映官方没有 CLI,也不支持新版自动导出;最稳的本地自动化是生成
剪映真实草稿格式(经 pyJianYingDraft)写入草稿库目录:
剪辑完成后打开剪映,该集草稿(镜头视频 + 配音 + 字幕已铺好时间轴)
直接出现在首页,人工微调后点导出即可。

依赖(仅此 Provider 需要):pip3 install pyJianYingDraft
新版剪映(10.8+/11.x)支持草稿生成;自动导出仅旧版(≤6)支持。

pyJianYingDraft 不同版本类名有 CamelCase / snake_case 两套,
这里做双名解析,新旧版本都能用。
"""

import importlib
import importlib.util
import sys
import time
from pathlib import Path

from ..errors import ProviderError
from .base import Provider, ProviderResult

# macOS 剪映草稿库常见位置(留空 draft_dir 时按顺序找)
DRAFT_DIR_CANDIDATES = [
    "~/Movies/JianyingPro Drafts",
    "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft",
    "~/Movies/CapCut Drafts",
    "~/Movies/CapCut/User Data/Projects/com.lveditor.draft",
]


def _find_draft_dir(conf):
    configured = (conf.get("draft_dir") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_dir() else None
    for candidate in DRAFT_DIR_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return None


def _api(module, *names):
    """新旧版本类名双解析(如 ScriptFile / Script_file)。"""
    for name in names:
        if hasattr(module, name):
            return getattr(module, name)
    raise ProviderError(
        f"pyJianYingDraft 缺少 {'/'.join(names)}(请升级:"
        "pip3 install -U pyJianYingDraft)")


class JianyingDraftProvider(Provider):
    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if "pyJianYingDraft" not in sys.modules:
            try:
                found = importlib.util.find_spec(
                    "pyJianYingDraft") is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                return False, "未安装依赖:pip3 install pyJianYingDraft"
        if _find_draft_dir(self.conf) is None:
            return False, ("找不到剪映草稿库目录(先装剪映专业版,"
                           "或在设置的「草稿目录」里手动指定)")
        return True, ""

    def generate(self, capability, payload, out_dir, cancel=None):
        if capability != "edit":
            raise ProviderError(f"剪映草稿产线不支持能力: {capability}")
        try:
            jd = importlib.import_module("pyJianYingDraft")
        except ImportError as exc:
            raise ProviderError(
                f"pyJianYingDraft 导入失败: {exc}") from exc
        draft_dir = _find_draft_dir(self.conf)
        if draft_dir is None:
            raise ProviderError("剪映草稿库目录不存在")
        try:
            return self._build(jd, draft_dir, payload)
        except ProviderError:
            raise
        except Exception as exc:   # 库/格式差异统一转为可回退错误
            raise ProviderError(f"剪映草稿生成失败: {exc}") from exc

    def _build(self, jd, draft_dir, payload):
        width = int(payload.get("width", 1080))
        height = int(payload.get("height", 1920))
        title = payload.get("project_title", "AIFOS")
        episode = int(payload.get("episode_number", 0))
        draft_name = f"{title}_第{episode}集_{time.strftime('%m%d%H%M')}"

        DraftFolder = _api(jd, "DraftFolder", "Draft_folder")
        TrackType = _api(jd, "TrackType", "Track_type")
        VideoSegment = _api(jd, "VideoSegment", "Video_segment")
        TextSegment = _api(jd, "TextSegment", "Text_segment")
        trange = _api(jd, "trange")
        folder = DraftFolder(str(draft_dir))
        script = folder.create_draft(draft_name, width, height)
        script.add_track(TrackType.video)
        script.add_track(TrackType.text)

        def _material(cls, source):
            kind = "Video" if cls is VideoSegment else "Audio"
            material_cls = _api(jd, f"{kind}Material", f"{kind}_material")
            return material_cls(source)

        def _real_seconds(cls, source, planned):
            """素材真实时长(秒);读不到就退回计划时长。

            分镜里的计划时长是「想要几秒」,Seedance 实际吐出来的是
            5.017 秒这种零头。旧代码直接拿计划时长去切素材,只要多出
            一丝(5.09 > 5.017)剪映草稿就整份失败——一集里任意一段
            对不上,全集都进不了剪映。
            """
            try:
                micros = int(getattr(_material(cls, source), "duration", 0))
            except Exception:
                return planned
            return round(micros / 1_000_000, 3) if micros > 0 else planned

        def _segment(cls, source, span):
            """新版本可直接传路径,旧版本需要先包 Material。"""
            try:
                return cls(source, span)
            except Exception:
                return cls(_material(cls, source), span)

        # 镜头视频顺序铺满视频轨;有台词的镜头同步铺字幕
        shots = payload.get("shots") or []
        subtitles = {s["line_no"]: s
                     for s in payload.get("subtitles") or []}
        cursor = 0.0
        line_no = 0
        missing = []
        clamped = []
        for shot in shots:
            uri = shot.get("uri", "")
            duration = float(shot.get("duration") or 3.0)
            playable = uri and Path(uri).exists() and uri.endswith(".mp4")
            if playable:
                # 时间轴长度以素材真实时长为准:超出会被剪映判「截取范围
                # 超出素材时长」而整份草稿失败,短于素材则只是不用完。
                real = _real_seconds(VideoSegment, uri, duration)
                if real < duration:
                    clamped.append(
                        {"shot_no": shot.get("shot_no"),
                         "planned": duration, "actual": real})
                    duration = real
            span = trange(f"{cursor}s", f"{duration}s")
            if playable:
                script.add_segment(_segment(VideoSegment, uri, span))
            else:
                missing.append(shot.get("shot_no"))
            dialogue = shot.get("dialogue")
            if dialogue and dialogue.get("dialogue"):
                line_no += 1
                sub = subtitles.get(line_no, {})
                text = sub.get("text") or dialogue["dialogue"]
                script.add_segment(TextSegment(text, span))
            cursor += duration

        # 独立配音文件(未用有声视频时)铺到音频轨
        voices = [v for v in (payload.get("voices") or [])
                  if v.get("uri", "").endswith((".mp3", ".wav"))
                  and Path(v.get("uri", "")).exists()]
        if voices:
            AudioSegment = _api(jd, "AudioSegment", "Audio_segment")
            script.add_track(TrackType.audio)
            audio_cursor = 0.0
            for voice in voices:
                duration = float(voice.get("duration") or 1.0)
                script.add_segment(_segment(
                    AudioSegment, voice["uri"],
                    trange(f"{audio_cursor}s", f"{duration}s")))
                audio_cursor += duration

        # 保存:经 DraftFolder 创建的草稿优先 save(),旧版回退 dump()
        draft_path = Path(draft_dir) / draft_name
        if hasattr(script, "save"):
            script.save()
        else:
            script.dump(str(draft_path / "draft_content.json"))
        note = f"草稿已进剪映:{draft_name}(打开剪映导出成片)"
        if missing:
            note += f";缺视频镜头 {missing} 未上轨"
        if clamped:
            note += (f";{len(clamped)} 段按素材真实时长收敛"
                     f"(计划与实际有零头,已避免草稿整份失败)")
        return ProviderResult(
            provider=self.name, cost=self.cost_per_call,
            data={"draft": str(draft_path), "draft_name": draft_name,
                  "total_duration": round(cursor, 2), "note": note,
                  "missing_shots": missing,
                  "clamped_segments": clamped},
            uri=str(draft_path))
