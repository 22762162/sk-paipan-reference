"""即梦官方 CLI(dreamina)原生适配器。

视频生成默认走 `dreamina frames2video`；选了资产中心参考图时改走
`dreamina multimodal2video`，首帧、尾帧和资产参考图一起提交。

  dreamina frames2video \
    --first=/path/start.png --last=/path/end.png \
    --prompt="提示词" --duration=8 \
    --video_resolution=720p \
    --model_version=seedance2.0fast_vip \
    --poll=30

注意:model_version 必须使用 seedance2.0fast_vip(Fast VIP),
不要使用旧脚本中的 seedance2.0_vip。
`dreamina user_credit` 用于查询订阅额度余额。
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path



from ..errors import ProduceCancelled, ProviderError
from ..seedance_policy import (
    DEFAULT_TIER,
    UPGRADE_TIER,
    SeedancePolicyError,
    configured_model_alias,
    probe_cli_help,
    requested_tier,
    validate_runtime_request,
)
from .base import Provider, ProviderResult
from .external import run_interruptible

REQUIRED_MODEL_VERSION = "seedance2.0fast_vip"
SEEDANCE_PROMPT_CHAR_LIMIT = 4000
SEEDANCE_PROMPT_TARGET_CHAR_LIMIT = 4000
SEEDANCE_PHYSICS_CLAUSE = (
    "【物理运动硬约束】人和物品道具的运动轨迹必须符合真实物理世界的运动轨迹和逻辑；"
    "人物动作必须服从重力、惯性、关节活动范围、重心和支撑关系；物品的位移、旋转、碰撞、"
    "抛落、接触与交接必须连续、有明确受力来源且前后状态一致；禁止漂浮、瞬移、穿模、"
    "无支撑运动或违背因果。"
)
_MP4_PATTERN = re.compile(r"(https?://\S+?\.mp4|/\S+?\.mp4)")
_PROMPT_SECTION_PATTERN = re.compile(r"(?=【[^】\n]{1,48}】)")


def _prompt_section_label(block):
    match = re.match(r"【([^】\n]{1,48})】", block or "")
    return match.group(1) if match else ""


def _shorten_prompt_block(block, limit):
    """Shorten one labelled block while keeping its execution label visible."""
    block = str(block or "").strip()
    if len(block) <= limit:
        return block
    match = re.match(r"(【[^】\n]{1,48}】)", block)
    marker = match.group(1) if match else ""
    if limit <= len(marker) + 1:
        return marker[:limit]
    content = block[len(marker):]
    return marker + content[:limit - len(marker) - 1].rstrip() + "…"


def fit_seedance_prompt(prompt, limit=SEEDANCE_PROMPT_TARGET_CHAR_LIMIT):
    """Return the exact final Seedance prompt, including every suffix, <= limit.

    The full audit contract can be much longer than the provider field.  This
    last-mile guard keeps labelled execution facts and proportionally compacts
    long sections; it counts Unicode characters after dialogue/reference
    clauses have been appended, so punctuation and symbols are included.
    """
    limit = int(limit)
    if limit < len(SEEDANCE_PHYSICS_CLAUSE):
        raise ProviderError(
            "Seedance 提示词上限小于物理运动硬约束本身，禁止提交")
    source = str(prompt or "").strip()
    # The final clause is appended exactly once and held outside the flexible
    # budget, so compaction can never truncate the user's physics requirement.
    source = re.sub(
        r"(?:\n|^)?【物理运动硬约束】[^\n]*", "", source).strip()
    available = limit - len(SEEDANCE_PHYSICS_CLAUSE) - 1
    if len(source) <= available:
        fitted = f"{source}\n{SEEDANCE_PHYSICS_CLAUSE}" if source else \
            SEEDANCE_PHYSICS_CLAUSE
        assert len(fitted) <= limit
        return fitted

    blocks = [
        part.strip() for part in _PROMPT_SECTION_PATTERN.split(source)
        if part.strip()
    ]
    if len(blocks) <= 1:
        fitted = source[:max(0, available - 1)].rstrip() + "…"
        fitted = f"{fitted}\n{SEEDANCE_PHYSICS_CLAUSE}"
        return fitted[:limit]

    # Cap repetition-heavy sections first.  Every retained labelled block gets
    # a readable minimum; the allocator then shrinks the longest blocks until
    # the complete prompt (newlines included) fits the hard provider limit.
    section_caps = {
        "镜头合同v2.2": 90, "输入": 130,
        "首尾帧职责": 300, "首尾帧硬绑定": 260, "主体": 260,
        "人物状态合同": 220, "硬状态·强制执行": 300,
        "非现实内心Q版叠层": 260, "场景": 220,
        "过肩构图": 220, "单人过肩构图": 220,
        "起点": 420, "单一主动作": 380, "表演": 180,
        "镜头": 360, "光影": 160, "空间裁决": 240,
        "空间站位": 220, "屏幕方向": 180, "终点": 420,
        "道具定格": 300, "道具状态变化": 300,
        "物理/空间逻辑": 360, "空间关系": 260,
        "视觉媒介": 150, "画风": 150, "风格导演执行": 180,
        "对白": 220, "有声对白": 260, "文字": 100,
        "参考图职责": 360, "模型约束": 340, "硬约束": 340,
        "多图参考边界": 240, "参考视频边界": 280,
    }
    caps = [min(len(block), section_caps.get(
        _prompt_section_label(block), 180)) for block in blocks]
    minimums = []
    for block, cap in zip(blocks, caps):
        marker = re.match(r"(【[^】\n]{1,48}】)", block)
        marker_len = len(marker.group(1)) if marker else 0
        minimums.append(min(cap, marker_len + 36))
    while sum(caps) + max(0, len(caps) - 1) > available:
        reducible = [
            (caps[index] - minimums[index], index)
            for index in range(len(caps))
            if caps[index] > minimums[index]
        ]
        if not reducible:
            break
        room, index = max(reducible)
        overflow = sum(caps) + len(caps) - 1 - available
        caps[index] -= min(room, max(1, overflow))
    compact = "\n".join(
        _shorten_prompt_block(block, cap)
        for block, cap in zip(blocks, caps)
    )
    if len(compact) > available:
        compact = compact[:max(0, available - 1)].rstrip() + "…"
    fitted = f"{compact}\n{SEEDANCE_PHYSICS_CLAUSE}"
    if len(fitted) > limit:
        raise ProviderError(
            f"Seedance 提示词压缩后仍有 {len(fitted)} 字符，超过 {limit}")
    return fitted


class DreaminaProvider(Provider):
    def _command(self):
        """完整命令前缀(支持绝对路径与全局旗标),子命令追加其后。"""
        return list(self.conf.get("command") or ["dreamina"])

    def _upgrade_help_probe(self, alias):
        """Probe once per provider/alias; every video still validates limits."""
        key = (tuple(self._command()), str(alias))
        cache = getattr(self, "_seedance_help_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._seedance_help_cache = cache
        if key not in cache:
            cache[key] = probe_cli_help(
                self._command(), alias,
                timeout=float(self.conf.get("help_timeout", 10) or 10))
        return cache[key]

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        binary = self._command()[0]
        if shutil.which(binary) is None and not Path(binary).exists():
            return False, f"命令不存在: {binary}"
        min_credit = self.conf.get("min_credit")
        if min_credit:
            balance = self._parse_credit(self._safe_credit())
            if balance is not None and balance < int(min_credit):
                return False, f"订阅额度不足({balance} < {min_credit})"
        return True, ""

    def generate(self, capability, payload, out_dir, cancel=None):
        if capability != "video":
            raise ProviderError(f"dreamina 适配器不支持能力: {capability}")
        started_at = time.monotonic()
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        first = payload.get("first", "")
        last = payload.get("last", "")
        if not first or not last:
            raise ProviderError("dreamina frames2video 需要首尾帧(first/last)")
        # 首尾帧以绝对路径交给外部 CLI,避免随子进程 cwd 漂移
        if not first.startswith(("http://", "https://")):
            first = str(Path(first).resolve())
        if not last.startswith(("http://", "https://")):
            last = str(Path(last).resolve())
        references = []
        for uri in payload.get("reference_images") or []:
            value = str(uri)
            if not value.startswith(("http://", "https://")):
                value = str(Path(value).resolve())
            if value not in references and value not in (first, last):
                references.append(value)

        def _media_refs(key, label, limit):
            """参考视频/音频(multimodal2video 在 2.0 全家族即支持):
            CLI 硬上限 video<=3 / audio<=3,超限 fail-closed 不静默丢弃
            (2.5 才放宽到各 10 条)。"""
            rows = []
            for uri in payload.get(key) or []:
                value = str(uri)
                if not value.startswith(("http://", "https://")):
                    value = str(Path(value).resolve())
                if value and value not in rows:
                    rows.append(value)
            if len(rows) > limit:
                raise ProviderError(
                    f"Seedance 2.0 单次最多 {limit} 条{label},"
                    f"当前 {len(rows)} 条;请删减,或按镜升级 seedance2_5"
                    f"(上限 10 条)后再提交,禁止静默丢弃")
            return rows
        reference_videos = _media_refs("reference_videos", "参考视频", 3)
        reference_audios = _media_refs("reference_audios", "参考音频", 3)
        shot_no = int(payload.get("shot_no", 0))
        try:
            tier = requested_tier(payload)
            runtime = validate_runtime_request(
                {**payload, "first": first, "last": last,
                 "reference_images": references},
                self.conf, command=self._command(),
                help_probe=(
                    lambda: self._upgrade_help_probe(
                        configured_model_alias(self.conf, tier))
                    if tier == UPGRADE_TIER else None))
        except SeedancePolicyError as exc:
            raise ProviderError(str(exc)) from exc
        model_version = runtime["model_alias"]
        # 导演同时保存完整审计提示词与镜头合同；真实请求使用合同短版，
        # 避免全局故事背景和重复禁词稀释首尾帧/单一动作。
        prompt = payload.get("prompt_compact") or payload.get("prompt", "")
        dialogue = payload.get("dialogue") or {}
        if self.conf.get("audio_in_video", True) and dialogue.get("dialogue"):
            # Seedance2 有声视频:台词随视频自动配音,免单独 TTS
            prompt += (f"\n【有声对白】让角色开口说出这句台词并自动配音"
                       f"(中文自然人声,口型对应):「{dialogue['dialogue']}」")
        requested_duration = runtime["requested_duration"]
        video_quality = str(payload.get("video_quality") or "medium")
        video_resolution = str(payload.get(
            "video_resolution") or self.conf.get("video_resolution", "720p"))
        if video_resolution.lower() not in ("480p", "720p", "1080p"):
            raise ProviderError(
                "Seedance video_resolution 只允许 480p/720p/1080p")
        if tier == UPGRADE_TIER and video_resolution.lower() != "720p":
            raise ProviderError(
                "seedance2_5 当前运行时合同只验证了 720p；"
                "未证明的分辨率禁止提交")
        # 只有 seedance2.0_vip 支持 1080p/4k;其余型号(含 mini、fast_vip)
        # 一律只出 720p。不按分辨率反选型号,「高档 1080P」就是个不可达
        # 的假选项:用户选了 1080p,请求照发,即梦静默降回 720p——而关键帧
        # 母图是 1080x1920,线性分辨率白丢 33%,全程不报错。
        if (tier == DEFAULT_TIER
                and video_resolution.lower() in ("1080p", "4k")
                and model_version != "seedance2.0_vip"):
            model_version = "seedance2.0_vip"
        # 非 VIP 型号走**公共排队**,不是省钱是换时间——实测 mini 单段滞留
        # 40 分钟(v5 镜5)到 2 小时(v7 镜2),而同批 vip 段 2-3 分钟就出;
        # 且 mini 45 积分反而比 fast 25 积分更贵。VIP 通道不排队,fast_vip
        # 是 720p 迭代的正确档位(本模块开头的注释一直这么写,是 workspace
        # 配置把它覆盖成了 mini)。自动升到对应 VIP 型号,不静默排队。
        public_queue_upgrade = {
            "seedance2.0mini": "seedance2.0fast_vip",
            "seedance2.0fast": "seedance2.0fast_vip",
            "seedance2.0": "seedance2.0_vip",
        }
        if (tier == DEFAULT_TIER and model_version in public_queue_upgrade
                and not self.conf.get("allow_public_queue")):
            model_version = public_queue_upgrade[model_version]
        # 预算规则(用户 2026-07-29 定):一切迭代验证用 720p fast_vip,
        # vip 1080p(165/段)只给「用户确认过的最终成片」。vip 提交必须带显式
        # final 标记,否则宁可报错也不静默花钱——一次全量 8 镜误上 vip 的
        # 代价是 960 积分,而报错的代价是零。
        if (video_resolution.lower() in ("1080p", "4k")
                and not payload.get("video_final_confirmed")
                and not self.conf.get("allow_vip_without_confirmation")):
            raise ProviderError(
                "1080p/4k 属最终成片档(seedance2.0_vip, 165积分/段)，"
                "迭代验证请用 720p Fast VIP。确为最终成片时在"
                " payload 带 video_final_confirmed=true 再提交")
        # Policy validates the requested maximum before whole-second rounding,
        # validates the minimum against the actual rounded submission, and
        # never clips an over-limit duration or drops excess references.
        duration = runtime["submitted_duration"]
        if references or reference_videos or reference_audios:
            if references:
                prompt += (
                    "\n【多图参考边界】图1仅为动作起点，图2仅为动作终点；"
                    "图3及之后必须逐张服从提示词内的“资产图单一职责”，"
                    "禁止把一张图同时当人物、服装、场景和画风依据，"
                    "禁止跨人物复制属性或把参考图拼贴进画面。")
            if reference_videos:
                # 多人身份泄漏实测教训:实拍参考的外观先验会压过身份图。
                # 参考视频只授权运动骨架,外观一律以图片资产为准。
                prompt += (
                    "\n【参考视频边界】视频只授权动作节奏、运动轨迹与镜头"
                    "运动(运动骨架)，画面外观、人物身份、发型服装、场景"
                    "与光线一律以图片资产和文字提示词为准；禁止从参考"
                    "视频复制人脸、服饰、道具或场景元素。")
            cmd = self._command() + ["multimodal2video"]
            cmd.extend(f"--image={uri}" for uri in [first, last, *references])
            cmd.extend(f"--video={uri}" for uri in reference_videos)
            cmd.extend(f"--audio={uri}" for uri in reference_audios)
            # frames2video 从首帧尺寸推断画幅,multimodal2video 不会——
            # 不显式传 --ratio,画幅会被参考图里的横版空间调度图带偏,
            # 9:16 竖屏短剧实测出成 1280x720 横屏。而「勾了资产参考图」
            # 几乎是每一镜的常态(空间调度图与出场人物立绘是强制参考)。
            ratio = str(payload.get("aspect")
                        or self.conf.get("ratio", "9:16")).strip()
            if ratio:
                cmd.append(f"--ratio={ratio}")
        else:
            cmd = self._command() + [
                "frames2video", f"--first={first}", f"--last={last}"]
        # 最终完整字符串才是 Seedance 真正收到的提示词。长度闸门必须放在
        # 对白、多图与参考视频条款之后，4000 计数包含所有标点和符号。
        prompt = fit_seedance_prompt(prompt)
        cmd += [
            f"--prompt={prompt}",
            f"--duration={duration}",
            f"--video_resolution={video_resolution}",
            f"--model_version={model_version}",
            f"--poll={int(self.conf.get('poll', 30))}",
        ]
        cmd += list(self.conf.get("extra_args") or [])
        log_path = out_dir / f"shot_{shot_no:03d}.dreamina.log"
        recovered_existing = False
        submit_id = ""
        if payload.get("director_autonomy_mode"):
            submit_id = self._latest_submit_id(log_path)
        if submit_id and not (out_dir / f"shot_{shot_no:03d}.mp4").exists():
            recovered_existing = True
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    "\n--- recover existing submit (no regeneration) ---\n"
                    f"submit_id={submit_id}\n")
            uri = self._wait_for_video(
                submit_id, out_dir, log_path, time.monotonic(), cancel)
        else:
            # 可中断执行:用户点「停止生成」时 2 秒内终止即梦调用
            returncode, stdout, stderr = run_interruptible(
                "dreamina", cmd, None, self.conf.get("timeout", 1800),
                cancel=cancel)
            log_path.write_text(
                f"$ {' '.join(cmd)}\n--- stdout ---\n{stdout}\n"
                f"--- stderr ---\n{stderr}\n", encoding="utf-8")
            if returncode != 0:
                raise ProviderError(
                    f"dreamina 退出码 {returncode}: "
                    f"{stderr.strip()[:500]}")
            uri = self._extract_uri(stdout)
            if not uri:
                reply = self._json_reply(stdout)
                submit_id = str(reply.get("submit_id") or "").strip()
                status = str(
                    reply.get("gen_status")
                    or reply.get("status") or "").lower()
                if (submit_id
                        and status not in ("failed", "error", "cancelled")):
                    uri = self._wait_for_video(
                        submit_id, out_dir, log_path, started_at, cancel)
        if not uri:
            raise ProviderError(
                f"未能从 dreamina 输出解析出视频地址(详见 {log_path})")
        uri = self._ingest(uri, out_dir, shot_no)
        return ProviderResult(
            provider=self.name,
            cost=0.0 if recovered_existing else self.cost_per_call,
            data={
                "shot_no": shot_no,
                "duration": duration,
                "requested_duration": requested_duration,
                "video_model_tier": tier,
                "video_model_reason": runtime["upgrade_reason"],
                "model_version": model_version,
                "video_quality": video_quality,
                "video_resolution": video_resolution,
                "voice": payload.get("voice", "jimeng_builtin"),
                "lip_sync": bool(payload.get("lip_sync", True)),
                "forbid_subtitles": bool(payload.get("forbid_subtitles", True)),
                "reference_images_used": references,
                "reference_videos_used": reference_videos,
                "reference_audios_used": reference_audios,
                "material_reference_count": runtime[
                    "material_reference_count"],
                "total_reference_count": runtime[
                    "total_reference_count"],
                "seedance_limits": runtime["limits"],
                "reference_assets": list(payload.get("reference_assets") or []),
                "prompt_used": prompt,
                "prompt_char_count": len(prompt),
                "prompt_char_limit": SEEDANCE_PROMPT_CHAR_LIMIT,
                "prompt_target_char_limit": SEEDANCE_PROMPT_TARGET_CHAR_LIMIT,
                "recovered_existing_submit": recovered_existing,
                "log": str(log_path),
            },
            uri=uri,
            model=model_version,
        )

    @staticmethod
    def _json_reply(stdout):
        """兼容 CLI 的单行或缩进 JSON 输出。"""
        text = (stdout or "").strip()
        try:
            reply = json.loads(text)
        except (TypeError, ValueError):
            return {}
        return reply if isinstance(reply, dict) else {}

    @staticmethod
    def _latest_submit_id(log_path):
        """从旧日志恢复已提交任务，下载超时后不得重新消耗一次生成。"""
        try:
            text = Path(log_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        matches = re.findall(r'"submit_id"\s*:\s*"([^"]+)"', text)
        return matches[-1] if matches else ""

    def _wait_for_video(self, submit_id, out_dir, log_path, started_at,
                        cancel=None):
        """异步任务返回 querying 时持续查到成片，不把排队误判为失败。

        dreamina 的 ``--poll`` 只会短暂等待，超时后正常返回 submit_id；
        这里接管后续查询，并把每次状态写入同一个镜头日志。
        """
        timeout = float(self.conf.get("timeout", 1800) or 1800)
        interval = max(
            0.05, float(self.conf.get("query_interval", 5) or 5))
        deadline = started_at + timeout
        query_no = 0
        while time.monotonic() < deadline:
            if cancel is not None and cancel():
                raise ProduceCancelled(
                    "已手动停止(终止 dreamina 异步视频查询)")
            remaining = max(1, int(deadline - time.monotonic()))
            command = self._command() + [
                "query_result",
                f"--submit_id={submit_id}",
                f"--download_dir={out_dir}",
            ]
            returncode, stdout, stderr = run_interruptible(
                "dreamina", command, None, min(60, remaining),
                cancel=cancel)
            query_no += 1
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"\n--- query_result #{query_no} ---\n{stdout}\n"
                    f"--- query stderr ---\n{stderr}\n")
            if returncode != 0:
                transient = any(token in str(stderr).lower() for token in (
                    "context deadline exceeded", "client.timeout",
                    "timeout", "timed out", "connection reset"))
                if transient and time.monotonic() < deadline:
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(
                            "--- transient download error; retry same "
                            f"submit_id ---\n{stderr}\n")
                    time.sleep(min(interval, max(
                        0.0, deadline - time.monotonic())))
                    continue
                raise ProviderError(
                    f"dreamina query_result 退出码 {returncode}: "
                    f"{stderr.strip()[:500]}")
            uri = self._extract_uri(stdout)
            if uri:
                return uri
            reply = self._json_reply(stdout)
            status = str(
                reply.get("gen_status") or reply.get("status") or "").lower()
            if status in ("fail", "failed", "error", "cancelled"):
                detail = reply.get("message") or reply.get("error") or status
                raise ProviderError(
                    f"dreamina 任务 {submit_id} 生成失败: {detail}")
            sleep_until = min(deadline, time.monotonic() + interval)
            while time.monotonic() < sleep_until:
                if cancel is not None and cancel():
                    raise ProduceCancelled(
                        "已手动停止(终止 dreamina 异步视频查询)")
                time.sleep(min(0.5, sleep_until - time.monotonic()))
        raise ProviderError(
            f"dreamina 异步视频等待超时({int(timeout)}s): {submit_id}")

    @staticmethod
    def _ingest(uri, out_dir, shot_no):
        """把即梦落在外部路径的成片归档进平台产物目录(资产沉淀),
        以便 Web 端服务、质检与跨次复用;远程 URL 原样保留。"""
        if uri.startswith(("http://", "https://")):
            return uri
        source = Path(uri).resolve()
        managed = (Path(out_dir) / f"shot_{shot_no:03d}.mp4").resolve()
        if source == managed:
            return str(managed)
        if not source.exists():
            raise ProviderError(f"dreamina 返回的视频不存在: {source}")
        shutil.copy2(source, managed)
        return str(managed)

    # ---- 输出解析:优先 JSON 行,回退正则找 .mp4 地址 ----
    @staticmethod
    def _extract_uri(stdout):
        for line in stdout.splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            for key in ("video_path", "video_url", "path", "url", "result"):
                value = reply.get(key)
                if isinstance(value, str) and value.endswith(".mp4"):
                    return value
        match = _MP4_PATTERN.search(stdout)
        return match.group(1) if match else ""

    # ---- 订阅额度 ----
    def credit(self):
        """`dreamina user_credit` 原样返回,供 stats 展示与额度判断。"""
        try:
            proc = subprocess.run(
                self._command() + ["user_credit"], capture_output=True,
                text=True, timeout=self.conf.get("credit_timeout", 60))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"dreamina user_credit 失败: {exc}") from exc
        if proc.returncode != 0:
            raise ProviderError(
                f"dreamina user_credit 退出码 {proc.returncode}")
        return proc.stdout.strip()

    def _safe_credit(self):
        try:
            return self.credit()
        except ProviderError:
            return ""

    @staticmethod
    def _parse_credit(text):
        match = re.search(r"\d+", text or "")
        return int(match.group(0)) if match else None
