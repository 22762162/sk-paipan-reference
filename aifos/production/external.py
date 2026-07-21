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
import time
import urllib.request

from ..errors import ProduceCancelled, ProviderError, ProviderUnavailable
from .base import Provider, ProviderResult


def run_interruptible(name, command, input_text, timeout, cancel=None,
                      cwd=None):
    """可中断的子进程执行:每 2 秒检查一次停止信号,收到即 kill。

    返回 (returncode, stdout, stderr);用户停止 → ProduceCancelled。
    """
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd)
    except OSError as exc:
        raise ProviderError(f"{name} 调用失败: {exc}") from exc
    try:
        if input_text is not None:
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        deadline = time.monotonic() + (timeout or 600)
        while True:
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel():
                    proc.kill()
                    proc.wait()
                    raise ProduceCancelled(
                        f"已手动停止(终止 {name} 外部调用)")
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.wait()
                    raise ProviderError(f"{name} 调用超时({timeout}s)")
        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        return proc.returncode, stdout, stderr
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


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

    def generate(self, capability, payload, out_dir, cancel=None):
        command = self.conf["command"]
        request = json.dumps(
            {"capability": capability, "payload": payload,
             "out_dir": str(out_dir)},
            ensure_ascii=False)
        returncode, stdout, stderr = run_interruptible(
            self.name, command, request,
            self.conf.get("timeout", 600), cancel=cancel)
        if returncode != 0:
            raise ProviderError(
                f"{self.name} 退出码 {returncode}: "
                f"{stderr.strip()[:500]}")
        try:
            reply = json.loads(stdout.strip().splitlines()[-1])
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
            model=(reply.get("model") or self.conf.get("model", "")),
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

    def generate(self, capability, payload, out_dir, cancel=None):
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
