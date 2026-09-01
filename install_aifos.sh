#!/bin/bash
# AIFOS 一键安装 + 启动(macOS / Linux)。已装过则自动更新。
#
#   curl -fsSL https://raw.githubusercontent.com/22762162/sk-paipan-reference/claude/sk-manga-drama-platform-b8nbe3/install_aifos.sh | bash
#
# 安装位置默认 ~/AIFOS(可用环境变量 AIFOS_HOME 改),零第三方依赖,
# 只需要系统自带的 python3 与 git。
set -e
BRANCH="claude/sk-manga-drama-platform-b8nbe3"
REPO="https://github.com/22762162/sk-paipan-reference"
DIR="${AIFOS_HOME:-$HOME/AIFOS}"
PORT="${AIFOS_PORT:-8619}"

command -v python3 >/dev/null 2>&1 || {
  echo "需要 python3(macOS 自带;缺失时 xcode-select --install 或 brew install python3)"; exit 1; }
command -v git >/dev/null 2>&1 || {
  echo "需要 git(macOS 运行 xcode-select --install)"; exit 1; }

if [ -d "$DIR/.git" ]; then
  echo "== 更新 AIFOS($DIR)=="
  git -C "$DIR" fetch origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  echo "== 安装 AIFOS 到 $DIR =="
  git clone -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

echo
echo "== 自动检测本机 AI CLI(claude / codex / dreamina / 剪映)=="
python3 -m aifos config detect --apply || true
echo
echo "== 体检:每个环节实际由谁生产 =="
python3 -m aifos doctor || true
echo
echo "== 启动控制台(Ctrl+C 或关闭本窗口即停止)=="
# 等服务真的监听成功后再开浏览器,避免打开「拒绝连接」的空页
(
  for _ in $(seq 1 40); do
    sleep 0.5
    for p in $(seq "$PORT" $((PORT + 9))); do
      if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$p/"; then
        command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$p/"
        exit 0
      fi
    done
  done
) &
python3 -m aifos serve --port "$PORT"
status=$?
if [ "$status" -ne 0 ]; then
  echo
  echo "!! 控制台启动失败(退出码 $status)。请把上面的报错完整发给开发助手。"
fi
exit "$status"
