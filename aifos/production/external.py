"""外部 Provider:CLI(Codex / 即梦 / 剪映 / Claude)与 API 备用。

CLI 协议:向命令 stdin 写入一行 JSON
  {"capability": "...", "payload": {...}, "out_dir": "..."}
命令 stdout 返回一行 JSON
  {"ok": true, "data": {...}, "uri": "...", "cost": 1.2}
非零退出码 / 非法输出 → ProviderError,路由器自动回退下一个 Provider。
"""

import json
import shutil
import subprocess
import urllib.request

from ..errors import ProviderError, ProviderUnavailable
from .base import Provider, ProviderResult


class CliProvider(Provider):
    """通过子进程调用外部 CLI(即梦 CLI、Codex、剪映等)。"""

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        command = self.conf.get("command") or []
        if not command:
            return False, "未配置 command"
        if shutil.which(command[0]) is None:
            return False, f"命令不存在: {command[0]}"
        return True, ""

    def generate(self, capability, payload, out_dir):
        command = self.conf["command"]
        request = json.dumps(
            {"capability": capability, "payload": payload,
             "out_dir": str(out_dir)},
            ensure_ascii=False)
        try:
            proc = subprocess.run(
                command, input=request, capture_output=True, text=True,
                timeout=self.conf.get("timeout", 600))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"{self.name} 调用失败: {exc}") from exc
        if proc.returncode != 0:
            raise ProviderError(
                f"{self.name} 退出码 {proc.returncode}: "
                f"{proc.stderr.strip()[:500]}")
        try:
            reply = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ProviderError(f"{self.name} 输出不是合法 JSON") from exc
        if not reply.get("ok"):
            raise ProviderError(
                f"{self.name} 返回失败: {reply.get('error', '未知错误')}")
        return ProviderResult(
            provider=self.name,
            cost=float(reply.get("cost", self.cost_per_call)),
            data=reply.get("data", {}),
            uri=reply.get("uri", ""),
        )


class ApiProvider(Provider):
    """API 备用能力:高并发扩容;POST JSON 到配置的 endpoint。"""

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        if not self.conf.get("endpoint") or not self.conf.get("api_key"):
            return False, "未配置 endpoint/api_key"
        return True, ""

    def generate(self, capability, payload, out_dir):
        body = json.dumps(
            {"capability": capability, "payload": payload,
             "out_dir": str(out_dir)},
            ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.conf["endpoint"], data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.conf['api_key']}",
            })
        try:
            with urllib.request.urlopen(
                    request, timeout=self.conf.get("timeout", 300)) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # 网络/协议错误统一回退
            raise ProviderError(f"{self.name} API 调用失败: {exc}") from exc
        if not reply.get("ok"):
            raise ProviderError(
                f"{self.name} 返回失败: {reply.get('error', '未知错误')}")
        return ProviderResult(
            provider=self.name,
            cost=float(reply.get("cost", self.cost_per_call)),
            data=reply.get("data", {}),
            uri=reply.get("uri", ""),
        )
