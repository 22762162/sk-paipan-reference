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
echo "== 启动控制台 http://127.0.0.1:$PORT(Ctrl+C 停止)=="
( sleep 1.5; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT" ) &
exec python3 -m aifos serve --port "$PORT"
