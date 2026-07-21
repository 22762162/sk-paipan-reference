#!/bin/bash
# macOS 双击启动 AIFOS:自动更新 → 检测本机 CLI → 体检 → 开控制台并打开浏览器。
# 服务跑在本窗口里:关闭本窗口 = 停止服务。
cd "$(dirname "$0")"
PORT="${AIFOS_PORT:-8619}"
if [ -d .git ]; then
  echo "== 检查更新 =="
  git fetch origin "$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null \
    && git pull --ff-only 2>/dev/null \
    && echo "已是最新版本(或已更新到最新)" \
    || echo "更新检查失败(离线或本地有改动),用当前版本启动"
  echo
fi
echo "== 自动检测本机 AI CLI =="
python3 -m aifos config detect --apply || true
echo
echo "== 体检:每个环节实际由谁生产 =="
python3 -m aifos doctor || true
echo
echo "== 启动控制台(关闭本窗口即停止)=="
# 等服务真的监听成功后再开浏览器,避免打开一个「拒绝连接」的空页
(
  for _ in $(seq 1 40); do
    sleep 0.5
    url="$(printf 'http://127.0.0.1:%s/' "$PORT")"
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
  read -r -p "按回车关闭窗口 …" _
fi
