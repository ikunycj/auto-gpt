#!/usr/bin/env bash
set -euo pipefail

# Turb GPT Free Register WebUI 管理脚本
#
# 用法：
#   ./webui.sh start      启动 WebUI
#   ./webui.sh stop       关闭 WebUI
#   ./webui.sh restart    重启 WebUI
#   ./webui.sh status     查看状态
#   ./webui.sh logs       实时查看日志
#
# 可选环境变量：
#   OPEN_BROWSER=1
#   VERBOSE=1
#   AUTH_CODE=xxx
#   EXTRA_ARGS="..."

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/data/runtime"
LOG_DIR="$ROOT_DIR/data/runtime"
PID_FILE="$RUN_DIR/webui.pid"
LOG_FILE="$LOG_DIR/webui.log"
LOG_CATEGORY="webui_process_logs"

HOST="127.0.0.1"
PORT="5000"
OPEN_BROWSER="${OPEN_BROWSER:-0}"
VERBOSE="${VERBOSE:-0}"
AUTH_CODE="${AUTH_CODE:-}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

usage() {
  cat <<EOF
用法：$0 <command>

commands:
  start      启动 WebUI
  stop       关闭 WebUI
  restart    重启 WebUI
  status     查看运行状态
  logs       实时查看日志

环境变量（地址和端口固定，不可覆盖）：
  OPEN_BROWSER=1 VERBOSE=1 AUTH_CODE=xxx EXTRA_ARGS="..."

示例：
  ./webui.sh start
  OPEN_BROWSER=1 ./webui.sh start
EOF
}

is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
  [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null || true
}

find_pids_by_port() {
  pgrep -f "python.*web\.py.*--port[ =]${PORT}" 2>/dev/null || true
}

get_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    echo "$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "未找到 Python：请先创建 .venv 或安装 python3" >&2
    return 1
  fi
}

collect_running_pids() {
  local pids=()
  local pid
  pid="$(read_pid)"
  if is_running "$pid"; then
    pids+=("$pid")
  fi

  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(find_pids_by_port)

  local unique=()
  local seen x
  for pid in "${pids[@]:-}"; do
    [[ -z "$pid" || "$pid" == "$$" ]] && continue
    seen=0
    for x in "${unique[@]:-}"; do
      [[ "$x" == "$pid" ]] && seen=1 && break
    done
    [[ "$seen" == "0" ]] && unique+=("$pid")
  done

  printf '%s\n' "${unique[@]:-}"
}

cmd_start() {
  local old_pid py pid existing_pids
  old_pid="$(read_pid)"
  if is_running "$old_pid"; then
    echo "WebUI 已在运行：PID=$old_pid，地址：http://${HOST}:${PORT}"
    return 0
  fi
  existing_pids="$(collect_running_pids)"
  if [[ -n "$existing_pids" ]]; then
    echo "WebUI 已在运行：PID=$(tr '\n' ' ' <<<"$existing_pids")，地址：http://${HOST}:${PORT}"
    return 0
  fi
  rm -f "$PID_FILE"

  py="$(get_python)"

  local args=("web.py" "--host" "$HOST" "--port" "$PORT")
  if [[ "$OPEN_BROWSER" == "1" || "$OPEN_BROWSER" == "true" ]]; then
    args+=("--open-browser")
  fi
  if [[ "$VERBOSE" == "1" || "$VERBOSE" == "true" ]]; then
    args+=("--verbose")
  fi
  if [[ -n "$AUTH_CODE" ]]; then
    args+=("--auth-code" "$AUTH_CODE")
  fi
  if [[ -n "$EXTRA_ARGS" ]]; then
    if [[ "$EXTRA_ARGS" == *"--host"* || "$EXTRA_ARGS" == *"--port"* ]]; then
      echo "WebUI 仅支持 http://127.0.0.1:5000，不能通过 EXTRA_ARGS 覆盖地址或端口" >&2
      return 2
    fi
    # shellcheck disable=SC2206
    local extra_parts=($EXTRA_ARGS)
    args+=("${extra_parts[@]}")
  fi

  echo "启动 WebUI：http://${HOST}:${PORT}"
  echo "日志存储：SQLite（逻辑键：${LOG_FILE}）"
  nohup "$py" "${args[@]}" >/dev/null 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"

  sleep 1
  if is_running "$pid"; then
    echo "启动成功：PID=$pid"
  else
    echo "启动失败，请使用 ./webui.sh logs 查看 SQLite 日志" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

cmd_stop() {
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(collect_running_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  echo "正在关闭 WebUI：PID=${pids[*]}"
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done

  local alive
  for _ in {1..15}; do
    alive=0
    for pid in "${pids[@]}"; do
      if is_running "$pid"; then
        alive=1
        break
      fi
    done
    [[ "$alive" == "0" ]] && break
    sleep 1
  done

  for pid in "${pids[@]}"; do
    if is_running "$pid"; then
      echo "进程未退出，强制结束：PID=$pid"
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  done

  rm -f "$PID_FILE"
  echo "已关闭 WebUI"
}

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

cmd_status() {
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(collect_running_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    return 1
  fi

  echo "WebUI 运行中：PID=${pids[*]}"
  echo "地址：http://${HOST}:${PORT}"
  echo "日志：$LOG_FILE"
}

cmd_logs() {
  local py
  py="$(get_python)"
  exec "$py" - "$LOG_FILE" "$LOG_CATEGORY" <<'PY'
import sys
import time
from pathlib import Path

from core import sqlite_store

path = Path(sys.argv[1])
category = sys.argv[2]
seen = ""
while True:
    try:
        text = sqlite_store.read_text_file(path, category=category, import_legacy=False)
    except FileNotFoundError:
        text = ""
    if len(text) < len(seen):
        seen = ""
    if text.startswith(seen):
        delta = text[len(seen):]
    else:
        delta = text
    if delta:
        sys.stdout.write(delta)
        sys.stdout.flush()
        seen = text
    time.sleep(0.5)
PY
}

cmd="${1:-}"
case "$cmd" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs|log) cmd_logs ;;
  -h|--help|help|"") usage ;;
  *)
    echo "未知命令：$cmd" >&2
    usage >&2
    exit 2
    ;;
esac
