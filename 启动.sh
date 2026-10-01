#!/usr/bin/env bash
# 图包素材审批工具 · 启动脚本（Git Bash / macOS / Linux）
cd "$(dirname "$0")"

# 注意：Git Bash 里 python3 可能指向 Microsoft Store 的占位符，所以先试能不能 import
HAS=""
for cand in python python3 py; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c "import flask, PIL" >/dev/null 2>&1; then HAS="$cand"; break; fi
done

if [ -z "$HAS" ]; then
  for cand in python python3 py; do
    command -v "$cand" >/dev/null 2>&1 || continue
    "$cand" --version >/dev/null 2>&1 || continue
    echo "缺少依赖，正在安装 flask 和 pillow ..."
    "$cand" -m pip install flask pillow || true
    if "$cand" -c "import flask, PIL" >/dev/null 2>&1; then HAS="$cand"; break; fi
  done
fi

if [ -z "$HAS" ]; then
  echo "[错误] 没找到可用的 Python（需要 3.10+，且能 import flask、PIL）。" >&2
  echo "       可手动执行：python -m pip install flask pillow" >&2
  exit 1
fi

# 进程被意外结束时自动拉起（最多 20 次）；正常退出（退出码 0）或 Ctrl+C 就停。
# 「已经在运行」「端口被别的程序占用」都会以退出码 0 结束，不会误重启。
restarts=0
while :; do
  set +e
  "$HAS" app.py "$@"
  code=$?
  set -e
  if [ "$code" = "0" ] || [ "$code" = "130" ] || [ "$code" = "2" ]; then break; fi
  restarts=$((restarts+1))
  if [ "$restarts" -ge 20 ]; then
    echo "[错误] 连续多次异常退出（最后一次退出码 $code），已停止自动重启。" >&2
    exit 1
  fi
  echo ""
  echo "[警告] 工具进程意外退出（第 $restarts 次，退出码 $code），3 秒后自动重启 …（想彻底停下就按 Ctrl+C）"
  sleep 3
done
