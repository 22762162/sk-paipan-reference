"""AI 质检中心:五维生产合同、角色/状态连续性、内置声画、无字幕、
复核证据与敏感内容自动检测；不达标项目由 AI 导演中心自动重跑。"""

from pathlib import Path

from .prompt_contract import readable_text_required

# Warnings are director suggestions, not disguised blockers. They remain in
# the review report but cannot make a technically valid episode fail.
SEVERITY_PENALTY = {"error": 15, "warn": 0}


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
        professional = storyboard.get("pipeline_version") == "sk-manju-v5"
        issues += self._check_character_consistency(script, storyboard, ctx)
        issues += self._check_continuity(script, storyboard)
        if professional:
            issues += self._check_professional_contract(
                script, storyboard, ctx)
            issues += self._check_no_subtitles(ctx)
            issues += self._check_integrated_audio(ctx)
            issues += self._check_review_evidence(ctx)
        else:
            # 兼容旧项目/单元测试输入；新产线强制走无字幕与内置声画同步。
            issues += self._check_subtitles(script, ctx)
            issues += self._check_voices(script, ctx)
        issues += self._check_videos(storyboard, ctx)
        issues += self._check_media_sanity(ctx)
        issues += self._check_sensitive(script, ctx)

        if professional:
            profile = ctx.get("production_profile") or storyboard.get("profile") or {}
            settings = {
                item.get("id"): item
                for item in profile.get("rules", {}).get("quality_gates", [])
                if isinstance(item, dict) and item.get("id")
            }
            check_to_gate = {
                "continuity": "continuity",
                "state_continuity": "continuity",
                "five_dimensions": "five_dimensions",
                "duration": "duration",
                "dialogue_integrity": "dialogue",
                "performance": "performance",
                "camera": "camera",
                "character_consistency": "people",
                "text_asset": "text",
                "integrated_audio": "audio",
            }
            filtered = []
            for issue in issues:
                gate_id = check_to_gate.get(issue.get("check"))
                setting = settings.get(gate_id, {}) if gate_id else {}
                if gate_id and setting.get("enabled", True) is False:
                    continue
                if gate_id and setting.get("severity") == "warning":
                    issue = dict(issue, severity="warn")
                filtered.append(issue)
            issues = filtered

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
        hard_failed = professional and any(
            issue["severity"] == "error" for issue in issues)
        domains = {
            "structure": not any(i["check"] in (
                "continuity", "five_dimensions", "duration",
                "dialogue_integrity", "performance", "camera")
                and i["severity"] == "error" for i in issues),
            "visual_continuity": not any(i["check"] in (
                "character_consistency", "state_continuity", "text_asset")
                and i["severity"] == "error" for i in issues),
            "audio_delivery": not any(i["check"] in (
                "voice", "integrated_audio", "subtitle", "review_evidence")
                and i["severity"] == "error" for i in issues),
        }
        return {
            "score": score,
            "passed": score >= pass_score and not hard_failed,
            "pass_score": pass_score,
            "issues": issues,
            "rerun_shots": rerun_shots,
            "rerun_lines": rerun_lines,
            "pipeline_version": storyboard.get("pipeline_version", "legacy"),
            "domains": domains,
        }

    def _check_professional_contract(self, script, storyboard, ctx):
        issues = []
        profile = (ctx.get("production_profile")
                   or storyboard.get("profile") or {})
        rules = profile.get("rules", {})
        gate_settings = {
            item.get("id"): item for item in rules.get("quality_gates", [])
            if isinstance(item, dict) and item.get("id")
        }

        def enabled(gate_id):
            return gate_settings.get(gate_id, {}).get("enabled", True)

        max_seconds = float(profile.get("max_segment_seconds", 15))
        precision = float(profile.get("time_precision_seconds", 0.5))
        required_columns = rules.get("storyboard", {}).get(
            "required_columns", [])
        required = (
            "unit_id", "character_count", "start_state", "end_state",
            "shot_function", "script_reference", "readable_text",
            "visual_hook", "performance", "five_dimensions",
            "seedance_prompt", "shot_contract", "sound_design", "timecode",
        )
        shots = storyboard.get("shots", [])
        if (enabled("continuity") and storyboard.get("standard_fingerprint")
                != profile.get("standard_fingerprint")):
            issues.append({
                "check": "preflight", "severity": "error",
                "shot_no": None, "rerunnable": False,
                "message": "分镜与本集制作标准快照不一致",
            })
        for shot in shots:
            missing = ([field for field in required if field not in shot]
                       if enabled("five_dimensions") else [])
            if missing:
                issues.append({
                    "check": "duration", "severity": "error",
                    "shot_no": shot.get("shot_no"), "rerunnable": False,
                    "message": f"{shot.get('unit_id', '镜头')}缺少生产字段:"
                    + "、".join(missing),
                })
            duration = float(shot.get("duration") or 0)
            if (enabled("duration") and (duration > max_seconds
                    or abs(duration / precision
                           - round(duration / precision)) > 1e-7)):
                issues.append({
                    "check": "five_dimensions", "severity": "error",
                    "shot_no": shot.get("shot_no"), "rerunnable": False,
                    "message": f"{shot.get('unit_id')}时长须为{precision:g}秒粒度"
                    f"且不超过{max_seconds:g}秒",
                })
            if (enabled("people") and shot.get("character_count")
                    != len(shot.get("characters", []))):
                issues.append({
                    "check": "character_consistency", "severity": "error",
                    "shot_no": shot.get("shot_no"), "rerunnable": False,
                    "message": f"{shot.get('unit_id')}人物名单与数量不一致",
                })
            text_asset = shot.get("readable_text") or {}
            if enabled("text") and readable_text_required(text_asset) and not (
                    text_asset.get("locked_by") and text_asset.get("keyframe_uri")):
                issues.append({
                    "check": "text_asset", "severity": "error",
                    "shot_no": shot.get("shot_no"), "rerunnable": False,
                    "message": f"{shot.get('unit_id')}可读文字未绑定ChatGPT关键帧",
                })
            if enabled("five_dimensions") and required_columns and not all(
                    column in (shot.get("shot_contract") or {})
                    for column in required_columns):
                issues.append({
                    "check": "five_dimensions", "severity": "error",
                    "shot_no": shot.get("shot_no"), "rerunnable": False,
                    "message": f"{shot.get('unit_id')}镜头合同未覆盖全部"
                    f"{len(required_columns)}列",
                })
            if (enabled("audio")
                    and not (shot.get("sound_design") or {}).get("environment")):
                issues.append({
                    "check": "integrated_audio", "severity": "error",
                    "shot_no": shot.get("shot_no"), "rerunnable": False,
                    "message": f"{shot.get('unit_id')}缺少环境声设计",
                })

        # 同名角色跨单元出现时，下一单元起始状态必须继承上一结尾状态。
        if enabled("continuity"):
            previous = {}
            previous_scene = None
            for shot in shots:
                for name, start in (shot.get("start_state") or {}).items():
                    if (shot.get("scene_no") == previous_scene
                            and name in previous
                            and start != previous[name]):
                        issues.append({
                            "check": "state_continuity", "severity": "error",
                            "shot_no": shot.get("shot_no"), "rerunnable": False,
                            "message": f"{shot.get('unit_id')}的{name}未继承上一单元结尾状态",
                        })
                previous.update(shot.get("end_state") or {})
                previous_scene = shot.get("scene_no")

        script_lines = [
            line.get("dialogue", "")
            for scene in script.get("scenes", [])
            for line in scene.get("lines", [])
        ]
        storyboard_lines = []
        for shot in shots:
            if not shot.get("dialogue"):
                continue
            part = shot.get("dialogue_part") or {"index": 1}
            if part.get("index", 1) == 1:
                storyboard_lines.append(
                    shot.get("dialogue_source")
                    or shot["dialogue"].get("dialogue", ""))
        if enabled("dialogue") and script_lines != storyboard_lines:
            issues.append({
                "check": "dialogue_integrity", "severity": "error",
                "shot_no": None, "rerunnable": False,
                "message": "分镜台词未逐字、逐句覆盖原剧本",
            })
        preflight = ctx.get("preflight") or {}
        if not preflight.get("passed"):
            issues.append({
                "check": "preflight", "severity": "error",
                "shot_no": None, "rerunnable": False,
                "message": "生产前硬门禁没有通过记录",
            })
        return issues

    @staticmethod
    def _check_no_subtitles(ctx):
        subtitles = ctx.get("subtitles") or []
        edit_subs = ((ctx.get("edit_data") or {}).get("tracks") or {}).get(
            "subtitle", [])
        if not subtitles and not edit_subs:
            return []
        return [{
            "check": "subtitle", "severity": "error",
            "shot_no": None, "line_no": None, "rerunnable": False,
            "message": "无字幕版本检测到对白字幕轨或字幕条",
        }]

    @staticmethod
    def _check_integrated_audio(ctx):
        requested = (ctx.get("voice_mode") == "jimeng_builtin"
                     and bool(ctx.get("lip_sync")))
        carried = ctx.get("voice_carried") is True
        videos = ctx.get("videos") or []
        # Mock 的 *.video.json 是离线契约产物，用于无真实模型时
        # 验证整条工业流；实产 mp4/URL 不使用这个例外。
        mock_evidence = bool(videos) and all(
            video.get("provider") == "mock"
            or str(video.get("uri", "")).endswith(".video.json")
            for video in videos)
        if requested and (carried or mock_evidence):
            return []
        return [{
            "check": "integrated_audio", "severity": "error",
            "shot_no": None, "line_no": None, "rerunnable": False,
            "message": "缺少实际随视频配音证据(voice_carried)"
                       "或未锁定即梦对口型",
        }]

    @staticmethod
    def _check_review_evidence(ctx):
        board = ctx.get("review_board", "")
        review = ctx.get("content_review") or {}
        board_ok = bool(board) and _artifact_exists(board)
        if board_ok and review.get("passed"):
            return []
        return [{
            "check": "review_evidence", "severity": "error",
            "shot_no": None, "line_no": None, "rerunnable": False,
            "message": "抽帧检查板或逐段内容复核缺失/未通过",
        }]

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
