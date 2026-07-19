"""即梦官方 CLI(dreamina)原生适配器。

视频生成走 `dreamina frames2video`,与即梦 CLI 规范对齐:

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
from pathlib import Path

from ..errors import ProviderError
from .base import Provider, ProviderResult

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

    def generate(self, capability, payload, out_dir):
        if capability != "video":
            raise ProviderError(f"dreamina 适配器不支持能力: {capability}")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        first = payload.get("first", "")
        last = payload.get("last", "")
        if not first or not last:
            raise ProviderError("dreamina frames2video 需要首尾帧(first/last)")
        shot_no = int(payload.get("shot_no", 0))
        model_version = self.conf.get(
            "model_version", REQUIRED_MODEL_VERSION)
        cmd = self._command() + [
            "frames2video",
            f"--first={first}",
            f"--last={last}",
            f"--prompt={payload.get('prompt', '')}",
            f"--duration={int(self.conf.get('duration', 8))}",
            f"--video_resolution={self.conf.get('video_resolution', '720p')}",
            f"--model_version={model_version}",
            f"--poll={int(self.conf.get('poll', 30))}",
        ]
        cmd += list(self.conf.get("extra_args") or [])
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.conf.get("timeout", 1800))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"dreamina 调用失败: {exc}") from exc
        log_path = out_dir / f"shot_{shot_no:03d}.dreamina.log"
        log_path.write_text(
            f"$ {' '.join(cmd)}\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n", encoding="utf-8")
        if proc.returncode != 0:
            raise ProviderError(
                f"dreamina 退出码 {proc.returncode}: "
                f"{proc.stderr.strip()[:500]}")
        uri = self._extract_uri(proc.stdout)
        if not uri:
            raise ProviderError(
                f"未能从 dreamina 输出解析出视频地址(详见 {log_path})")
        return ProviderResult(
            provider=self.name,
            cost=self.cost_per_call,
            data={
                "shot_no": shot_no,
                "duration": int(self.conf.get("duration", 8)),
                "model_version": model_version,
                "log": str(log_path),
            },
            uri=uri,
        )

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
