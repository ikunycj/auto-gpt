#!/usr/bin/env bash
set -euo pipefail

# Turb GPT Free Register WebUI manager.
#
# The local runtime uses two fixed listeners:
#   frontend  http://127.0.0.1:5555  (Vite preview + same-origin proxy)
#   backend   http://127.0.0.1:6666  (Flask API)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/data/runtime"
BACKEND_PID_FILE="$RUN_DIR/webui-backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/webui-frontend.pid"
LEGACY_PID_FILE="$RUN_DIR/webui.pid"
LOG_FILE="$ROOT_DIR/data/runtime/webui.log"
LOG_CATEGORY="webui_process_logs"

HOST="127.0.0.1"
FRONTEND_PORT="5555"
BACKEND_PORT="6666"
FRONTEND_URL="http://${HOST}:${FRONTEND_PORT}"
BACKEND_URL="http://${HOST}:${BACKEND_PORT}"
VITE_BIN="$ROOT_DIR/web/node_modules/.bin/vite"

OPEN_BROWSER="${OPEN_BROWSER:-0}"
VERBOSE="${VERBOSE:-0}"
AUTH_CODE="${AUTH_CODE:-}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

PYTHON_BIN=""
DETACHED_PID=""

mkdir -p "$RUN_DIR"

usage() {
  cat <<EOF
用法：$0 <command>

commands:
  start      启动前端和后端
  stop       关闭前端和后端
  restart    重启前端和后端
  status     查看前后端状态
  logs       实时查看后端日志

固定地址（不可覆盖）：
  前端  ${FRONTEND_URL}
  后端  ${BACKEND_URL}

可选环境变量：
  OPEN_BROWSER=1 VERBOSE=1 AUTH_CODE=xxx EXTRA_ARGS="..."
EOF
}

is_running() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1
}

read_pid_file() {
  local path="$1"
  [[ -f "$path" ]] && cat "$path" 2>/dev/null || true
}

listener_pids() {
  local port="$1"
  lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

process_args() {
  ps -p "$1" -o args= 2>/dev/null || true
}

is_backend_process() {
  local pid="$1"
  local args
  args="$(process_args "$pid")"
  [[ "$args" == *"$ROOT_DIR/web.py"* ]] &&
    [[ "$args" == *"--port ${BACKEND_PORT}"* || "$args" == *"--port=${BACKEND_PORT}"* ]]
}

is_frontend_process() {
  local pid="$1"
  local args
  args="$(process_args "$pid")"
  [[ "$args" == *"$ROOT_DIR/web"* ]] && [[ "$args" == *"vite"* ]]
}

unique_pids() {
  awk 'NF && !seen[$1]++ { print $1 }'
}

collect_backend_pids() {
  {
    read_pid_file "$BACKEND_PID_FILE"
    listener_pids "$BACKEND_PORT"
    pgrep -f "python.*web\.py.*--port[ =]${BACKEND_PORT}" 2>/dev/null || true
  } | unique_pids | while IFS= read -r pid; do
    is_running "$pid" && is_backend_process "$pid" && echo "$pid"
  done
}

collect_frontend_pids() {
  {
    read_pid_file "$FRONTEND_PID_FILE"
    listener_pids "$FRONTEND_PORT"
  } | unique_pids | while IFS= read -r pid; do
    is_running "$pid" && is_frontend_process "$pid" && echo "$pid"
  done
}

describe_pids() {
  local pids="$1"
  [[ -n "$pids" ]] || return 0
  ps -p "$(tr '\n' ',' <<<"$pids" | sed 's/,$//')" -o pid=,comm=,args= 2>/dev/null || true
}

ensure_port_free() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(listener_pids "$port")"
  [[ -z "$pids" ]] && return 0
  echo "无法启动${label}：${HOST}:${port} 已被其他进程占用" >&2
  describe_pids "$pids" >&2
  return 1
}

get_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "未找到 Python：请先创建 .venv 或安装 python3" >&2
    return 1
  fi
}

resolve_python() {
  if [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(get_python)"
  fi
}

validate_frontend() {
  [[ -x "$VITE_BIN" ]] || { echo "缺少前端依赖：请执行 cd web && npm ci" >&2; return 1; }
  [[ -f "$ROOT_DIR/web/dist/index.html" ]] || { echo "缺少前端构建：请执行 cd web && npm run build" >&2; return 1; }
}

launch_detached() {
  local executable="$1"
  shift
  local detach_code='import os, sys
if hasattr(os, "setsid"):
    os.setsid()
os.execv(sys.argv[1], [sys.argv[1], *sys.argv[2:]])'
  nohup "$PYTHON_BIN" -c "$detach_code" "$executable" "$@" >/dev/null 2>&1 &
  DETACHED_PID=$!
}

wait_for_listener() {
  local pid="$1"
  local port="$2"
  local label="$3"
  local attempt
  for attempt in {1..40}; do
    if ! is_running "$pid"; then
      echo "${label}进程已提前退出：PID=$pid" >&2
      return 1
    fi
    if lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "${label}未在预期时间内监听 ${HOST}:${port}：PID=$pid" >&2
  return 1
}

terminate_pids() {
  local pids=("$@")
  local pid alive
  for pid in "${pids[@]}"; do
    [[ -n "$pid" ]] && kill "$pid" >/dev/null 2>&1 || true
  done
  for _ in {1..20}; do
    alive=0
    for pid in "${pids[@]}"; do
      if is_running "$pid"; then alive=1; break; fi
    done
    [[ "$alive" == "0" ]] && return 0
    sleep 0.25
  done
  for pid in "${pids[@]}"; do
    if is_running "$pid"; then
      echo "进程未退出，强制结束：PID=$pid"
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  done
}

cmd_start() {
  local backend_pids frontend_pids backend_pid frontend_pid
  local args=("$ROOT_DIR/web.py" "--host" "$HOST" "--port" "$BACKEND_PORT")
  backend_pids="$(collect_backend_pids)"
  frontend_pids="$(collect_frontend_pids)"

  if [[ -n "$backend_pids" && -n "$frontend_pids" ]]; then
    echo "WebUI 已在运行"
    echo "前端：${FRONTEND_URL}（PID=$(tr '\n' ' ' <<<"$frontend_pids")）"
    echo "后端：${BACKEND_URL}（PID=$(tr '\n' ' ' <<<"$backend_pids")）"
    return 0
  fi
  if [[ -n "$backend_pids" || -n "$frontend_pids" ]]; then
    echo "检测到不完整的 WebUI 运行状态，请先执行 ./webui.sh stop 再启动" >&2
    return 1
  fi

  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" "$LEGACY_PID_FILE"
  ensure_port_free "$FRONTEND_PORT" "前端"
  ensure_port_free "$BACKEND_PORT" "后端"
  resolve_python
  validate_frontend

  if [[ "$VERBOSE" == "1" || "$VERBOSE" == "true" ]]; then args+=("--verbose"); fi
  if [[ -n "$AUTH_CODE" ]]; then args+=("--auth-code" "$AUTH_CODE"); fi
  if [[ -n "$EXTRA_ARGS" ]]; then
    if [[ "$EXTRA_ARGS" == *"--host"* || "$EXTRA_ARGS" == *"--port"* ]]; then
      echo "前后端地址固定为 ${FRONTEND_URL} 和 ${BACKEND_URL}，不能覆盖 host/port" >&2
      return 2
    fi
    # shellcheck disable=SC2206
    local extra_parts=($EXTRA_ARGS)
    args+=("${extra_parts[@]}")
  fi

  echo "启动后端：${BACKEND_URL}"
  launch_detached "$PYTHON_BIN" "${args[@]}"
  backend_pid="$DETACHED_PID"
  echo "$backend_pid" > "$BACKEND_PID_FILE"
  if ! wait_for_listener "$backend_pid" "$BACKEND_PORT" "后端"; then
    terminate_pids "$backend_pid"
    rm -f "$BACKEND_PID_FILE"
    return 1
  fi

  echo "启动前端：${FRONTEND_URL}"
  launch_detached "$VITE_BIN" preview "$ROOT_DIR/web" --host "$HOST" --port "$FRONTEND_PORT" --strictPort
  frontend_pid="$DETACHED_PID"
  echo "$frontend_pid" > "$FRONTEND_PID_FILE"
  if ! wait_for_listener "$frontend_pid" "$FRONTEND_PORT" "前端"; then
    terminate_pids "$frontend_pid" "$backend_pid"
    rm -f "$FRONTEND_PID_FILE" "$BACKEND_PID_FILE"
    return 1
  fi

  echo "启动成功"
  echo "前端：${FRONTEND_URL}（PID=${frontend_pid}）"
  echo "后端：${BACKEND_URL}（PID=${backend_pid}）"
  echo "后端日志：SQLite（逻辑键：data/runtime/webui.log）"
  if [[ "$OPEN_BROWSER" == "1" || "$OPEN_BROWSER" == "true" ]]; then
    "$PYTHON_BIN" -c 'import sys, webbrowser; webbrowser.open(sys.argv[1])' "$FRONTEND_URL" >/dev/null 2>&1 || true
  fi
}

cmd_stop() {
  local backend_pids frontend_pids
  local pids=()
  local pid
  backend_pids="$(collect_backend_pids)"
  frontend_pids="$(collect_frontend_pids)"
  while IFS= read -r pid; do [[ -n "$pid" ]] && pids+=("$pid"); done <<<"$frontend_pids"
  while IFS= read -r pid; do [[ -n "$pid" ]] && pids+=("$pid"); done <<<"$backend_pids"

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" "$LEGACY_PID_FILE"
    return 0
  fi

  echo "正在关闭 WebUI：PID=${pids[*]}"
  terminate_pids "${pids[@]}"
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" "$LEGACY_PID_FILE"
  echo "已关闭 WebUI"
}

cmd_restart() {
  cmd_stop
  sleep 0.5
  cmd_start
}

cmd_status() {
  local backend_pids frontend_pids
  backend_pids="$(collect_backend_pids)"
  frontend_pids="$(collect_frontend_pids)"
  if [[ -z "$backend_pids" && -z "$frontend_pids" ]]; then
    echo "WebUI 未运行"
    return 1
  fi
  if [[ -n "$frontend_pids" ]]; then
    echo "前端运行中：${FRONTEND_URL}（PID=$(tr '\n' ' ' <<<"$frontend_pids")）"
  else
    echo "前端未运行：${FRONTEND_URL}"
  fi
  if [[ -n "$backend_pids" ]]; then
    echo "后端运行中：${BACKEND_URL}（PID=$(tr '\n' ' ' <<<"$backend_pids")）"
  else
    echo "后端未运行：${BACKEND_URL}"
  fi
  echo "后端日志：$LOG_FILE"
  [[ -n "$frontend_pids" && -n "$backend_pids" ]]
}

cmd_logs() {
  resolve_python
  exec "$PYTHON_BIN" - "$LOG_FILE" "$LOG_CATEGORY" <<'PY'
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
    delta = text[len(seen):] if text.startswith(seen) else text
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
  *) echo "未知命令：$cmd" >&2; usage >&2; exit 2 ;;
esac
