"""AI 质检中心:角色一致性、镜头连续性、字幕、配音、敏感内容自动检测,
输出评分;不达标项目由 AI 导演中心按评分自动重跑。"""

from pathlib import Path

SEVERITY_PENALTY = {"error": 15, "warn": 5}


def _artifact_exists(uri):
    """产物存在性:本地文件须落盘;远程 URL(如即梦返回的 mp4)视为存在。"""
    if not uri:
        return False
    if uri.startswith("http://") or uri.startswith("https://"):
        return True
    return Path(uri).exists()


class QcCenter:
    def __init__(self, config):
        self.config = config

    def run(self, script, storyboard, ctx):
        """执行全部检查,返回质检报告。

        ctx: 导演中心流水线上下文,含 images/frames/videos/voices/subtitles
             /cast 等产物索引。
        """
        issues = []
        issues += self._check_character_consistency(script, storyboard, ctx)
        issues += self._check_continuity(script, storyboard)
        issues += self._check_subtitles(script, ctx)
        issues += self._check_voices(script, ctx)
        issues += self._check_videos(storyboard, ctx)
        issues += self._check_media_sanity(ctx)
        issues += self._check_sensitive(script, ctx)

        score = 100
        for issue in issues:
            score -= SEVERITY_PENALTY.get(issue["severity"], 5)
        score = max(0, score)
        pass_score = self.config.get("qc", "pass_score", default=80)
        rerun_shots = sorted({
            i["shot_no"] for i in issues
            if i.get("shot_no") is not None and i.get("rerunnable")})
        rerun_lines = sorted({
            i["line_no"] for i in issues
            if i.get("line_no") is not None and i.get("rerunnable")})
        return {
            "score": score,
            "passed": score >= pass_score,
            "pass_score": pass_score,
            "issues": issues,
            "rerun_shots": rerun_shots,
            "rerun_lines": rerun_lines,
        }

    # ---- 角色一致性:分镜引用的角色必须在剧本角色表(即已登记资产)中 ----
    def _check_character_consistency(self, script, storyboard, ctx):
        issues = []
        cast = set(ctx.get("cast", [])) or {
            c["name"] for c in script.get("characters", [])}
        for shot in storyboard["shots"]:
            for name in shot.get("characters", []):
                if name not in cast:
                    issues.append({
                        "check": "character_consistency",
                        "severity": "error",
                        "shot_no": shot["shot_no"],
                        "rerunnable": False,
                        "message": f"镜头{shot['shot_no']}引用未登记角色「{name}」",
                    })
        return issues

    # ---- 镜头连续性:编号连续、时长合法、每场至少一个镜头 ----
    def _check_continuity(self, script, storyboard):
        issues = []
        shots = storyboard["shots"]
        numbers = [s["shot_no"] for s in shots]
        if numbers != list(range(1, len(shots) + 1)):
            issues.append({
                "check": "continuity", "severity": "error",
                "shot_no": None, "rerunnable": False,
                "message": "镜头编号不连续",
            })
        for shot in shots:
            if not shot.get("duration") or shot["duration"] <= 0:
                issues.append({
                    "check": "continuity", "severity": "error",
                    "shot_no": shot["shot_no"], "rerunnable": False,
                    "message": f"镜头{shot['shot_no']}时长非法",
                })
        covered = {s["scene_no"] for s in shots}
        for scene in script["scenes"]:
            if scene["scene_no"] not in covered:
                issues.append({
                    "check": "continuity", "severity": "warn",
                    "shot_no": None, "rerunnable": False,
                    "message": f"场{scene['scene_no']}没有镜头",
                })
        return issues

    # ---- 字幕:每句台词有字幕且长度合规 ----
    def _check_subtitles(self, script, ctx):
        issues = []
        max_len = self.config.get("qc", "max_subtitle_len", default=30)
        subtitles = {s["line_no"]: s for s in ctx.get("subtitles", [])}
        line_no = 0
        for scene in script["scenes"]:
            for line in scene["lines"]:
                line_no += 1
                sub = subtitles.get(line_no)
                if sub is None or not sub.get("text"):
                    issues.append({
                        "check": "subtitle", "severity": "error",
                        "line_no": line_no, "rerunnable": False,
                        "message": f"台词{line_no}缺少字幕",
                    })
                elif len(sub["text"]) > max_len:
                    issues.append({
                        "check": "subtitle", "severity": "warn",
                        "line_no": line_no, "rerunnable": False,
                        "message": f"台词{line_no}字幕超长({len(sub['text'])}字)",
                    })
        return issues

    # ---- 配音:每句台词有配音文件且落盘存在 ----
    def _check_voices(self, script, ctx):
        if ctx.get("voice_carried"):
            return []          # Seedance2 有声视频:配音随视频,无独立文件
        issues = []
        voices = {v["line_no"]: v for v in ctx.get("voices", [])}
        total_lines = sum(len(s["lines"]) for s in script["scenes"])
        for line_no in range(1, total_lines + 1):
            voice = voices.get(line_no)
            if voice is None or not _artifact_exists(voice.get("uri", "")):
                issues.append({
                    "check": "voice", "severity": "error",
                    "line_no": line_no, "rerunnable": True,
                    "message": f"台词{line_no}配音缺失",
                })
        return issues

    # ---- 视频:每个镜头有视频产物且落盘存在 ----
    def _check_videos(self, storyboard, ctx):
        issues = []
        videos = {v["shot_no"]: v for v in ctx.get("videos", [])}
        for shot in storyboard["shots"]:
            video = videos.get(shot["shot_no"])
            if video is None or not _artifact_exists(video.get("uri", "")):
                issues.append({
                    "check": "video", "severity": "error",
                    "shot_no": shot["shot_no"], "rerunnable": True,
                    "message": f"镜头{shot['shot_no']}视频缺失",
                })
        return issues

    # ---- 媒体健全性:魔数与空文件(识别真实产线的损坏输出) ----
    MAGIC = {
        ".png": (b"\x89PNG",),
        ".mp4": (b"ftyp",),      # 在文件头 32 字节内出现
        ".wav": (b"RIFF",),
        ".aiff": (b"FORM",),
        ".svg": (b"<svg", b"<?xml"),
    }

    def _check_media_sanity(self, ctx):
        issues = []
        for image in ctx.get("images", []):
            issues += self._sanity(
                image.get("uri", ""), "image",
                shot_no=image.get("shot_no"), rerunnable=False)
        for video in ctx.get("videos", []):
            issues += self._sanity(
                video.get("uri", ""), "video",
                shot_no=video.get("shot_no"), rerunnable=True)
        for voice in ctx.get("voices", []):
            issues += self._sanity(
                voice.get("uri", ""), "voice",
                line_no=voice.get("line_no"), rerunnable=True)
        return issues

    def _sanity(self, uri, kind, shot_no=None, line_no=None,
                rerunnable=False):
        if not uri or uri.startswith(("http://", "https://")):
            return []
        path = Path(uri)
        suffix = path.suffix.lower()
        if suffix == ".json" or not path.exists():
            return []  # mock 描述文件不校验;缺失由存在性检查负责
        issue = {
            "check": "media_sanity", "severity": "error",
            "shot_no": shot_no, "line_no": line_no,
            "rerunnable": rerunnable,
        }
        if path.stat().st_size == 0:
            issue["message"] = f"{kind} 产物为空文件: {path.name}"
            return [issue]
        magics = self.MAGIC.get(suffix)
        if not magics:
            return []
        head = path.open("rb").read(64)
        if not any(m in head[:32] if m == b"ftyp" else head.startswith(m)
                   for m in magics):
            issue["message"] = (
                f"{kind} 产物疑似损坏(魔数不符): {path.name}")
            return [issue]
        return []

    # ---- 敏感内容:剧本与字幕全文扫描 ----
    def _check_sensitive(self, script, ctx):
        issues = []
        words = self.config.get("qc", "sensitive_words", default=[])
        texts = [script.get("logline", "")]
        for scene in script["scenes"]:
            texts.append(scene.get("action", ""))
            texts += [line["dialogue"] for line in scene["lines"]]
        texts += [s.get("text", "") for s in ctx.get("subtitles", [])]
        blob = "\n".join(texts)
        for word in words:
            if word and word in blob:
                issues.append({
                    "check": "sensitive", "severity": "error",
                    "shot_no": None, "rerunnable": False,
                    "message": f"检测到敏感词「{word}」",
                })
        return issues
