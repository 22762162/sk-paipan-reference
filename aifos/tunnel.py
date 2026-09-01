"""外网访问:把本机 AIFOS 通过 cloudflared 隧道暴露到公网,并把当前
公网地址落盘,让终端与网页仪表盘随时可见(带二维码),手机再也不用猜。

背景:手机端 App 存的是一条外网地址。免费的 trycloudflare 快速隧道每次
重启都会换新网址,手机里的旧地址一连就 TLS 失败。这里做两件事:

  1. `aifos tunnel` 起隧道后,自动把「当前公网地址」写进 workspace/
     public_url.json;运行中的 `serve` 进程据此在 /api/access 暴露出来,
     网页面板显示二维码,手机扫一下就拿到最新地址。
  2. 支持「命名隧道」(稳定域名):地址永不变,手机一次配置长期可用。

隧道进程本身跑在用户自己的机器上(需要本机装好 cloudflared),本模块只
负责启动、解析地址、落盘与二维码,不接管任何账号或密钥。
"""

import json
import re
import subprocess
import time
from pathlib import Path

PUBLIC_URL_FILE = "public_url.json"

# cloudflared 快速隧道会在日志里打印一行形如
#   https://<随机词>.trycloudflare.com
QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def public_url_path(root):
    return Path(root) / PUBLIC_URL_FILE


def read_public_url(root):
    """读取当前已发布的公网地址;不存在或损坏时返回 None。"""
    path = public_url_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("url"):
        return None
    return data


def write_public_url(root, url, mode="quick", note=""):
    """把当前公网地址落盘,供 serve/仪表盘展示。"""
    data = {
        "url": url.rstrip("/") + "/",
        "mode": mode,
        "note": note,
        "updated_at": time.time(),
    }
    path = public_url_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return data


def clear_public_url(root):
    path = public_url_path(root)
    if path.exists():
        path.unlink()


def parse_quick_url(text):
    """从 cloudflared 输出里提取 trycloudflare 快速隧道地址。"""
    match = QUICK_URL_RE.search(text or "")
    return match.group(0) if match else None


def build_command(cloudflared, port, name=None, extra=None):
    """拼 cloudflared 命令:命名隧道用 `tunnel run`,否则起快速隧道。"""
    cmd = [cloudflared, "tunnel", "--no-autoupdate"]
    if name:
        # 命名隧道:入口(ingress)在用户的 cloudflared 配置里指向本机端口,
        # 稳定域名固定不变
        cmd += ["run", name]
    else:
        cmd += ["--url", f"http://localhost:{port}"]
    if extra:
        cmd += list(extra)
    return cmd


def run_tunnel(root, port, cloudflared="cloudflared", name=None,
               public_url=None, on_event=None, popen=subprocess.Popen):
    """启动隧道,解析并发布当前公网地址,持续输出日志直到进程结束。

    - 快速隧道:从 cloudflared 日志里解析 trycloudflare 地址后自动发布。
    - 命名隧道:地址稳定,由 ``public_url`` 直接给出并立即发布。

    ``on_event(kind, payload)`` 便于 CLI 打印(kind: log/url/error)。
    ``popen`` 可注入,便于测试。返回子进程退出码。
    """
    def emit(kind, payload):
        if on_event:
            on_event(kind, payload)

    if name and public_url:
        write_public_url(root, public_url, mode="named", note=name)
        emit("url", public_url)

    cmd = build_command(cloudflared, port, name=name)
    emit("log", "启动隧道: " + " ".join(cmd))
    try:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, bufsize=1)
    except FileNotFoundError:
        emit("error", f"未找到 cloudflared:{cloudflared}。请先在本机安装 "
                      "cloudflared(brew install cloudflared)")
        return 127

    published = bool(name and public_url)
    try:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if line:
                emit("log", line)
            if not published and not name:
                url = parse_quick_url(line)
                if url:
                    write_public_url(root, url, mode="quick")
                    published = True
                    emit("url", url)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()
