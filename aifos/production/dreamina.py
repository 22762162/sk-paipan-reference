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
from .base import Provider, ProviderResult
from .external import run_interruptible

REQUIRED_MODEL_VERSION = "seedance2.0fast_vip"
_MP4_PATTERN = re.compile(r"(https?://\S+?\.mp4|/\S+?\.mp4)")


class DreaminaProvider(Provider):
    def _command(self):
        """完整命令前缀(支持绝对路径与全局旗标),子命令追加其后。"""
        return list(self.conf.get("command") or ["dreamina"])

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
        # 图片总上限 9 = 首帧 + 尾帧 + 7 张资产参考(与导演端一致)
        references = references[
            :int(self.conf.get("max_reference_assets", 7))]
        shot_no = int(payload.get("shot_no", 0))
        model_version = self.conf.get(
            "model_version", REQUIRED_MODEL_VERSION)
        # 导演同时保存完整审计提示词与镜头合同；真实请求使用合同短版，
        # 避免全局故事背景和重复禁词稀释首尾帧/单一动作。
        prompt = payload.get("prompt_compact") or payload.get("prompt", "")
        dialogue = payload.get("dialogue") or {}
        if self.conf.get("audio_in_video", True) and dialogue.get("dialogue"):
            # Seedance2 有声视频:台词随视频自动配音,免单独 TTS
            prompt += (f"。让角色开口说出这句台词并自动配音"
                       f"(中文自然人声,口型对应):「{dialogue['dialogue']}」")
        requested_duration = float(payload.get(
            "duration", self.conf.get("duration", 8)))
        video_quality = str(payload.get("video_quality") or "medium")
        video_resolution = str(payload.get(
            "video_resolution") or self.conf.get("video_resolution", "720p"))
        if video_resolution.lower() not in ("480p", "720p", "1080p"):
            raise ProviderError(
                "Seedance video_resolution 只允许 480p/720p/1080p")
        # 即梦 CLI 接受整秒；0.5 秒分镜用常规四舍五入，避免 Python
        # bankers rounding 把 2.5 秒意外压成 2 秒。
        duration = max(1, min(15, int(requested_duration + 0.5)))
        if references:
            prompt += (
                "。多图边界：图1仅为动作起点，图2仅为动作终点；"
                "图3及之后必须逐张服从提示词内的“资产图单一职责”，"
                "禁止把一张图同时当人物、服装、场景和画风依据，"
                "禁止跨人物复制属性或把参考图拼贴进画面。")
            cmd = self._command() + ["multimodal2video"]
            cmd.extend(f"--image={uri}" for uri in [first, last, *references])
        else:
            cmd = self._command() + [
                "frames2video", f"--first={first}", f"--last={last}"]
        cmd += [
            f"--prompt={prompt}",
            f"--duration={duration}",
            f"--video_resolution={video_resolution}",
            f"--model_version={model_version}",
            f"--poll={int(self.conf.get('poll', 30))}",
        ]
        cmd += list(self.conf.get("extra_args") or [])
        # 可中断执行:用户点「停止生成」时 2 秒内终止即梦调用
        returncode, stdout, stderr = run_interruptible(
            "dreamina", cmd, None, self.conf.get("timeout", 1800),
            cancel=cancel)
        log_path = out_dir / f"shot_{shot_no:03d}.dreamina.log"
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
                reply.get("gen_status") or reply.get("status") or "").lower()
            if submit_id and status not in ("failed", "error", "cancelled"):
                uri = self._wait_for_video(
                    submit_id, out_dir, log_path, started_at, cancel)
        if not uri:
            raise ProviderError(
                f"未能从 dreamina 输出解析出视频地址(详见 {log_path})")
        uri = self._ingest(uri, out_dir, shot_no)
        return ProviderResult(
            provider=self.name,
            cost=self.cost_per_call,
            data={
                "shot_no": shot_no,
                "duration": duration,
                "model_version": model_version,
                "video_quality": video_quality,
                "video_resolution": video_resolution,
                "voice": payload.get("voice", "jimeng_builtin"),
                "lip_sync": bool(payload.get("lip_sync", True)),
                "forbid_subtitles": bool(payload.get("forbid_subtitles", True)),
                "reference_images_used": references,
                "reference_assets": list(payload.get("reference_assets") or []),
                "log": str(log_path),
            },
            uri=uri,
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
                raise ProviderError(
                    f"dreamina query_result 退出码 {returncode}: "
                    f"{stderr.strip()[:500]}")
            uri = self._extract_uri(stdout)
            if uri:
                return uri
            reply = self._json_reply(stdout)
            status = str(
                reply.get("gen_status") or reply.get("status") or "").lower()
            if status in ("failed", "error", "cancelled"):
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
