"""外部 Provider:CLI(Codex / 即梦 / 剪映 / Claude)与 API 备用。

CLI 协议:向命令 stdin 写入一行 JSON
  {"capability": "...", "payload": {...}, "out_dir": "..."}
命令 stdout 返回一行 JSON
  {"ok": true, "data": {...}, "uri": "...", "cost": 1.2}
非零退出码 / 非法输出 → ProviderError,路由器自动回退下一个 Provider。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from ..channel_stats import record as record_channel_stat
from ..errors import ProduceCancelled, ProviderError, ProviderUnavailable
from .base import Provider, ProviderResult


def run_interruptible(name, command, input_text, timeout, cancel=None,
                      cwd=None, env=None):
    """可中断的子进程执行:每 2 秒检查一次停止信号,先 TERM 再 KILL。

    返回 (returncode, stdout, stderr);用户停止 → ProduceCancelled。
    """
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=env)
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
                    _terminate_process(proc)
                    raise ProduceCancelled(
                        f"已手动停止(终止 {name} 外部调用)")
                if time.monotonic() >= deadline:
                    _terminate_process(proc)
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


def _terminate_process(proc, grace=5):
    """给适配器清理子进程组的机会，超时后再强制终止。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class CliProvider(Provider):
    """通过子进程调用外部 CLI(即梦 CLI、Codex、剪映等)。"""

    @staticmethod
    def _real_binary(command):
        """桥接命令真正调用的可执行文件:``--claude/--codex/--say`` 等旗标
        后面的值,否则命令本体。避免只查到启动器 python3 就误报“就绪”。"""
        for flag in ("--claude", "--codex", "--say", "--dreamina",
                     "--jianying"):
            if flag in command:
                index = command.index(flag)
                if index + 1 < len(command):
                    return command[index + 1]
        if "aifos.adapters.say_voice" in command:
            return "say"
        return command[0]

    def available(self, capability):
        ok, reason = super().available(capability)
        if not ok:
            return ok, reason
        command = self.conf.get("command") or []
        if not command:
            return False, "未配置 command"
        binary = self._real_binary(command)
        if shutil.which(binary) is None and not Path(binary).exists():
            return False, f"命令不存在: {binary}"
        return True, ""

    def generate(self, capability, payload, out_dir, cancel=None):
        command = list(self.conf["command"])
        profile_id = ""
        profile = None
        if self.name == "codex":
            profile_id = str(payload.get("_codex_profile") or "").strip()
            profiles = self.conf.get("codex_profiles") or []
            profile = next((item for index, item in enumerate(profiles)
                            if isinstance(item, dict)
                            and str(item.get("id") or (
                                "codex_a" if index == 0
                                else f"codex_{index + 1}")) == profile_id),
                           None) if profile_id else None
            if profile is not None and profile.get("command"):
                command = list(profile["command"])
            # 设置页允许只填写 `codex` 或一个绝对路径；这仍需经过
            # AIFOS 桥接协议，不能把 JSON 请求直接喂给 Codex 交互 CLI。
            if len(command) == 1:
                command = [sys.executable, "-m",
                           "aifos.adapters.codex_image", "--codex",
                           command[0]]
        request = json.dumps(
            {"capability": capability, "payload": payload,
             "out_dir": str(out_dir)},
            ensure_ascii=False)
        env = None
        # 两个 Codex 账号共用同一个桥接命令，但由 CODEX_HOME 选择
        # 独立登录态。只允许导演层传入已配置 profile id，避免任意任务
        # 通过 payload 注入外部环境变量或凭据路径。
        if self.name == "codex":
            if profile_id:
                profiles = self.conf.get("codex_profiles") or []
                if profile is None:
                    raise ProviderError(
                        f"Codex profile 不存在或未配置: {profile_id}")
                home_value = str(profile.get("codex_home") or "").strip()
                home = Path(home_value).expanduser()
                if not home.is_dir():
                    raise ProviderError(
                        f"Codex profile {profile_id} 的 CODEX_HOME 不存在: {home}")
                env = os.environ.copy()
                env["CODEX_HOME"] = str(home.resolve())
                env["AIFOS_CODEX_PROFILE"] = profile_id
        started = time.monotonic()

        def _stat(ok, error=""):
            # 只统计 Codex 通道;用户手动停止(ProduceCancelled)不计入。
            if self.name != "codex":
                return
            record_channel_stat(
                out_dir, self.name, profile_id, capability,
                time.monotonic() - started, ok, error=error)

        try:
            returncode, stdout, stderr = run_interruptible(
                self.name, command, request,
                self.conf.get("timeout", 600), cancel=cancel, env=env)
        except ProviderError as exc:
            _stat(False, error=str(exc))   # 超时/启动失败也计入通道账
            raise
        if returncode != 0:
            _stat(False, error=f"退出码 {returncode}")
            raise ProviderError(
                f"{self.name} 退出码 {returncode}: "
                f"{stderr.strip()[:500]}")
        try:
            reply = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            _stat(False, error="输出不是合法 JSON")
            raise ProviderError(f"{self.name} 输出不是合法 JSON") from exc
        if not reply.get("ok"):
            _stat(False, error=str(reply.get("error", "未知错误")))
            raise ProviderError(
                f"{self.name} 返回失败: {reply.get('error', '未知错误')}")
        _stat(True)
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
        outbound = dict(payload or {})
        if capability in {"image", "frames", "cover"}:
            prompt_used = (
                outbound.get("prompt_compact") or outbound.get("prompt") or "")
            outbound["prompt"] = prompt_used
            outbound["prompt_compact"] = prompt_used
            # Unknown external endpoints must not get planning biographies and
            # episode context then accidentally choose those over the shot.
            for key in (
                    "prompt_full", "prompt_audit", "_reference_prompt_base",
                    "story_world", "story_background", "character_background"):
                outbound.pop(key, None)
        body = json.dumps(
            {"capability": capability, "payload": outbound,
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
