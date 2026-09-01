"""外部 Provider:CLI(Codex / 即梦 / 剪映 / Claude)与 API 备用。

CLI 协议:向命令 stdin 写入一行 JSON
  {"capability": "...", "payload": {...}, "out_dir": "..."}
命令 stdout 返回一行 JSON
  {"ok": true, "data": {...}, "uri": "...", "cost": 1.2}
非零退出码 / 非法输出 → ProviderError,路由器自动回退下一个 Provider。
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import weakref
from pathlib import Path

from ..channel_stats import record as record_channel_stat
from ..errors import ProduceCancelled, ProviderError, ProviderUnavailable
from .base import Provider, ProviderResult


# 在途适配器子进程登记簿。服务被 TERM/异常退出时,不收割这些子进程
# 就会产生孤儿(ppid=1 的适配器 + 其 codex exec 子孙):它们继续占
# Codex 账号侧并发,产物却写进断裂的 stdout 管道被丢弃——同一张图
# 下一批还会重生成一次,额度双烧。2026-07-28 03:09 重启实测一次留下
# 3 个孤儿。WeakSet:正常退出的 Popen 被 GC 后自动出簿。
_LIVE_CHILDREN = weakref.WeakSet()


def reap_live_children(grace=5):
    """终止本进程尚存活的全部适配器子进程(先 TERM 后 KILL)。

    进程退出路径(SIGTERM/atexit)调用;幂等,子进程已退出时无操作。
    """
    reaped = 0
    for proc in list(_LIVE_CHILDREN):
        try:
            if proc.poll() is None:
                _terminate_process(proc, grace=grace)
                reaped += 1
        except Exception:
            pass
    return reaped


atexit.register(reap_live_children)


def run_interruptible(name, command, input_text, timeout, cancel=None,
                      cwd=None, env=None):
    """可中断的子进程执行:每 2 秒检查一次停止信号,先 TERM 再 KILL。

    返回 (returncode, stdout, stderr);用户停止 → ProduceCancelled。

    stdout/stderr 由独立线程边跑边收——先 wait 后 read 会在子进程
    输出超过管道缓冲(64KB)时死锁:子进程 print 大 JSON 回复被憋住,
    父进程等它退出才读,两边互相等到永远(整份 v2.2 分镜回复就踩中过)。
    """
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=env)
    except OSError as exc:
        raise ProviderError(f"{name} 调用失败: {exc}") from exc
    _LIVE_CHILDREN.add(proc)
    chunks = {"out": [], "err": []}

    def _pump(stream, key):
        try:
            for line in iter(stream.readline, ""):
                chunks[key].append(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, "out"),
                         daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, "err"),
                         daemon=True),
    ]
    for reader in readers:
        reader.start()
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
        for reader in readers:
            reader.join(timeout=10)
        return proc.returncode, "".join(chunks["out"]), "".join(chunks["err"])
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
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
        if (capability == "scene_annotate"
                and not self.conf.get("scene_image_transport", False)):
            # 通用 stdin/stdout CLI 桥只传 JSON 文本；配置里写
            # reference_images=True 并不会把全景图片真正送进子进程。
            # 禁止让它根据文件路径和提示词臆造家具坐标。
            return False, (
                "CLI 未实现全景图片附件传输；scene_annotate 必须使用"
                "支持图片输入、可真实携带全景图的视觉 API")
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
