#!/bin/bash
# macOS 双击启动 AIFOS:自动检测本机 CLI → 体检 → 开控制台并打开浏览器。
cd "$(dirname "$0")"
PORT="${AIFOS_PORT:-8619}"
echo "== 自动检测本机 AI CLI =="
python3 -m aifos config detect --apply || true
echo
echo "== 体检:每个环节实际由谁生产 =="
python3 -m aifos doctor || true
echo
echo "== 启动控制台 http://127.0.0.1:$PORT(关闭本窗口即停止)=="
( sleep 1.5; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT" ) &
exec python3 -m aifos serve --port "$PORT"
