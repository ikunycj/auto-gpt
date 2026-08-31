#!/usr/bin/env bash
set -euo pipefail

# Turb GPT Free Register WebUI manager.
#
# The local runtime uses two fixed listeners:
#   frontend  http://127.0.0.1:5555  (Vite preview + same-origin proxy)
#   backend   http://127.0.0.1:6666  (Flask API)
#
# Usage:
#   ./webui.sh start
#   ./webui.sh stop
#   ./webui.sh restart
#   ./webui.sh status
#   ./webui.sh logs

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/data/runtime"
BACKEND_PID_FILE="$RUN_DIR/webui-backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/webui-frontend.pid"
LEGACY_PID_FILE="$RUN_DIR/webui.pid"
# Stable SQLite source key used by the backend logger; no log file is created.
LOG_FILE="$RUN_DIR/webui.log"
LOG_CATEGORY="webui_process_logs"

HOST="127.0.0.1"
FRONTEND_PORT="5555"
BACKEND_PORT="6666"
FRONTEND_URL="http://${HOST}:${FRONTEND_PORT}"
BACKEND_URL="http://${HOST}:${BACKEND_PORT}"
VITE_BIN="$ROOT_DIR/web/node_modules/.bin/vite"

OPEN_BROWSER="${OPEN_BROWSER:-0}"
VERBOSE="${VERBOSE:-0}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

PYTHON_MODE=""
PYTHON_BIN=""
UV_MODE=""
UV_BIN=""
UV_PYTHON=""
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
  OPEN_BROWSER=1 VERBOSE=1 EXTRA_ARGS="..."

Python 环境：
  优先使用 uv 项目环境（存在 uv.lock 时锁定依赖）
  无 uv 时回退到 .venv/bin/python 或系统 python3

示例：
  ./webui.sh start
  OPEN_BROWSER=1 ./webui.sh start
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
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true
  fi
}

is_backend_process() {
  local pid="$1"
  local args
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$args" == *"$ROOT_DIR/web.py"* ]] &&
    [[ "$args" == *"--port ${BACKEND_PORT}"* || "$args" == *"--port=${BACKEND_PORT}"* ]]
}

is_frontend_process() {
  local pid="$1"
  local args
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$args" == *"$ROOT_DIR/web"* && "$args" == *"vite"* ]]
}

find_backend_pids() {
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && is_backend_process "$pid" && echo "$pid"
  done < <(pgrep -f "python.*web\.py.*--port[ =]${BACKEND_PORT}" 2>/dev/null || true)
  return 0
}

find_frontend_pids() {
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && is_frontend_process "$pid" && echo "$pid"
  done < <(listener_pids "$FRONTEND_PORT")
  return 0
}

unique_pids() {
  awk 'NF && !seen[$1]++ { print $1 }'
}

collect_backend_pids() {
  {
    read_pid_file "$BACKEND_PID_FILE"
    find_backend_pids
  } | unique_pids | while IFS= read -r pid; do
    is_running "$pid" && is_backend_process "$pid" && echo "$pid"
  done
  return 0
}

collect_frontend_pids() {
  {
    read_pid_file "$FRONTEND_PID_FILE"
    find_frontend_pids
  } | unique_pids | while IFS= read -r pid; do
    is_running "$pid" && is_frontend_process "$pid" && echo "$pid"
  done
  return 0
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
  if [[ -z "$pids" ]]; then
    return 0
  fi

  echo "无法启动${label}：${HOST}:${port} 已被其他进程占用" >&2
  describe_pids "$pids" >&2
  return 1
}

find_uv() {
  [[ -n "$UV_MODE" ]] && return 0

  if command -v uv >/dev/null 2>&1; then
    UV_MODE="binary"
    UV_BIN="$(command -v uv)"
  elif command -v python3 >/dev/null 2>&1 && "$(command -v python3)" -m uv --version >/dev/null 2>&1; then
    UV_MODE="module"
    UV_PYTHON="$(command -v python3)"
  else
    UV_MODE="none"
  fi
}

run_uv() {
  find_uv
  case "$UV_MODE" in
    binary)
      "$UV_BIN" "$@"
      ;;
    module)
      "$UV_PYTHON" -m uv "$@"
      ;;
    *)
      echo "未找到 uv" >&2
      return 1
      ;;
  esac
}

select_python() {
  [[ -n "$PYTHON_MODE" ]] && return 0

  if [[ -f "$ROOT_DIR/pyproject.toml" ]]; then
    find_uv
    if [[ "$UV_MODE" != "none" ]]; then
      if [[ -f "$ROOT_DIR/uv.lock" ]]; then
        PYTHON_MODE="uv-locked"
      else
        PYTHON_MODE="uv"
        echo "警告：未找到 uv.lock，暂使用未锁定的 uv 环境；建议执行 uv lock" >&2
      fi
      return 0
    fi
  fi

  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_MODE="venv"
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
    echo "警告：uv 不可用，回退到 .venv/bin/python；建议安装 uv 并执行 uv sync --locked" >&2
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    PYTHON_MODE="system"
    PYTHON_BIN="$(command -v python3)"
    echo "警告：uv 不可用，回退到系统 Python；建议安装 uv 并执行 uv sync --locked" >&2
    return 0
  fi

  echo "未找到可用 Python：请安装 uv 后执行 uv sync --locked" >&2
  return 1
}

resolve_python_bin() {
  [[ -n "$PYTHON_BIN" ]] && return 0
  select_python

  case "$PYTHON_MODE" in
    uv-locked)
      PYTHON_BIN="$(run_uv run --locked python -c 'import sys; print(sys.executable)')"
      ;;
    uv)
      PYTHON_BIN="$(run_uv run python -c 'import sys; print(sys.executable)')"
      ;;
  esac

  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "无法解析项目 Python 解释器：$PYTHON_BIN" >&2
    return 1
  fi

  if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 版本过低：项目要求 Python 3.10 或更高版本" >&2
    return 1
  fi
}

validate_frontend() {
  if [[ ! -x "$VITE_BIN" ]]; then
    echo "缺少前端依赖：请执行 cd web && npm ci" >&2
    return 1
  fi
  if [[ ! -f "$ROOT_DIR/web/dist/index.html" ]]; then
    echo "缺少前端构建：请执行 cd web && npm run build" >&2
    return 1
  fi
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
    if ! command -v lsof >/dev/null 2>&1 || lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
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
      if is_running "$pid"; then
        alive=1
        break
      fi
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
    [[ -n "$frontend_pids" ]] && echo "前端 PID：$(tr '\n' ' ' <<<"$frontend_pids")" >&2
    [[ -n "$backend_pids" ]] && echo "后端 PID：$(tr '\n' ' ' <<<"$backend_pids")" >&2
    return 1
  fi

  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" "$LEGACY_PID_FILE"
  ensure_port_free "$FRONTEND_PORT" "前端"
  ensure_port_free "$BACKEND_PORT" "后端"
  resolve_python_bin
  validate_frontend

  if [[ "$VERBOSE" == "1" || "$VERBOSE" == "true" ]]; then
    args+=("--verbose")
  fi
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
    echo "后端启动失败，请使用 ./webui.sh logs 查看 SQLite 日志" >&2
    return 1
  fi

  echo "启动前端：${FRONTEND_URL}"
  launch_detached "$VITE_BIN" preview "$ROOT_DIR/web" --host "$HOST" --port "$FRONTEND_PORT" --strictPort
  frontend_pid="$DETACHED_PID"
  echo "$frontend_pid" > "$FRONTEND_PID_FILE"
  if ! wait_for_listener "$frontend_pid" "$FRONTEND_PORT" "前端"; then
    terminate_pids "$frontend_pid" "$backend_pid"
    rm -f "$FRONTEND_PID_FILE" "$BACKEND_PID_FILE"
    echo "前端启动失败，请确认已执行 cd web && npm ci && npm run build" >&2
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
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done <<<"$frontend_pids"
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done <<<"$backend_pids"

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    rm -f "$FRONTEND_PID_FILE" "$BACKEND_PID_FILE" "$LEGACY_PID_FILE"
    return 0
  fi

  echo "正在关闭 WebUI：PID=${pids[*]}"
  terminate_pids "${pids[@]}"
  rm -f "$FRONTEND_PID_FILE" "$BACKEND_PID_FILE" "$LEGACY_PID_FILE"
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
  resolve_python_bin
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
  *)
    echo "未知命令：$cmd" >&2
    usage >&2
    exit 2
    ;;
esac
