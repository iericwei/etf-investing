#!/usr/bin/env bash
# restart.sh — 一键重启 ETF Investing 项目
# 用法:
#   ./restart.sh          先停后启
#   ./restart.sh stop     仅停止服务
#   ./restart.sh status   查看服务状态
set -euo pipefail

# ---- 配置 ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_PY="etf_server.py"
MARKET_DATA_PY="market_data_service.py"
WEB_PY="etf_web.py"
SERVER_LOG="$SCRIPT_DIR/logs/server.log"
MARKET_DATA_LOG="$SCRIPT_DIR/logs/market_data_service.log"
WEB_LOG="$SCRIPT_DIR/logs/web.log"
PID_DIR="$SCRIPT_DIR/.pids"

mkdir -p "$SCRIPT_DIR/logs" "$PID_DIR"

# ---- 工具函数 ----
log() { echo "[$(date '+%H:%M:%S')] $*"; }

find_pids() {
  # 查找项目目录下运行的目标进程 PID，排除 grep 和自身
  ps -ef 2>/dev/null | grep "$1" | grep -v grep | grep -v "restart.sh" | awk '{print $2}'
}

kill_pids_for() {
  local name="$1"
  local pids
  pids=$(find_pids "$name" || true)
  if [ -z "$pids" ]; then
    log "$name 未运行"
    return
  fi
  log "停止 $name (PID: $pids) ..."
  echo "$pids" | xargs kill 2>/dev/null || true
  # 等待最多 5 秒确认进程退出
  for i in 1 2 3 4 5; do
    local still_alive=false
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        still_alive=true
        break
      fi
    done
    if [ "$still_alive" = false ]; then break; fi
    sleep 1
  done
  # 如果还在跑，强制杀
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      log "$name PID $pid 未退出，强制终止"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  sleep 0.5
}

start_server() {
  local name="$1"
  local py_file="$2"
  local log_file="$3"
  local pid_file="$PID_DIR/${name}.pid"

  log "启动 $name ..."
  cd "$SCRIPT_DIR"
  nohup uv run python "$py_file" >> "$log_file" 2>&1 &
  local pid=$!
  echo "$pid" > "$pid_file"
  log "$name 已启动 (PID: $pid, 日志: $log_file)"
}

check_running() {
  local name="$1"
  local pid_file="$PID_DIR/${name}.pid"

  if [ -f "$pid_file" ]; then
    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
      echo "RUNNING (PID $pid)"
      return 0
    fi
  fi
  # 备用：通过进程名查找
  local found
  found=$(find_pids "$name" || true)
  if [ -n "$found" ]; then
    echo "RUNNING (PID: $found)"
    return 0
  fi
  echo "STOPPED"
  return 1
}

# ---- 主逻辑 ----
ACTION="${1:-restart}"

case "$ACTION" in
  stop)
    log "=== 停止服务 ==="
    kill_pids_for "$SERVER_PY"
    kill_pids_for "$MARKET_DATA_PY"
    kill_pids_for "$WEB_PY"
    log "=== 已全部停止 ==="
    ;;

  start)
    log "=== 启动服务 ==="
    start_server "server" "$SERVER_PY" "$SERVER_LOG"
    sleep 1
    start_server "market_data" "$MARKET_DATA_PY" "$MARKET_DATA_LOG"
    sleep 1
    start_server "web" "$WEB_PY" "$WEB_LOG"
    sleep 1
    log "=== 启动完成 ==="
    log "实时行情服务: http://localhost:5678"
    log "本地行情库服务: http://localhost:5680"
    log "Web Dashboard: http://localhost:8080"
    ;;

  status)
    log "=== 服务状态 ==="
    echo -n "  实时行情服务 ($SERVER_PY): "
    check_running "$SERVER_PY" || true
    echo -n "  本地行情库服务 ($MARKET_DATA_PY): "
    check_running "$MARKET_DATA_PY" || true
    echo -n "  Web Dashboard  ($WEB_PY):  "
    check_running "$WEB_PY" || true
    log "=== 状态查看完毕 ==="
    ;;

  restart|"")
    log "=== 重启 ETF Investing ==="
    kill_pids_for "$SERVER_PY"
    kill_pids_for "$MARKET_DATA_PY"
    kill_pids_for "$WEB_PY"
    sleep 1
    start_server "server" "$SERVER_PY" "$SERVER_LOG"
    sleep 1
    start_server "market_data" "$MARKET_DATA_PY" "$MARKET_DATA_LOG"
    sleep 1
    start_server "web" "$WEB_PY" "$WEB_LOG"
    sleep 2
    log "=== 重启完成 ==="
    log "实时行情服务: http://localhost:5678  (健康检查: http://localhost:5678/health)"
    log "本地行情库服务: http://localhost:5680  (健康检查: http://localhost:5680/health)"
    log "Web Dashboard: http://localhost:8080"
    ;;

  *)
    echo "用法: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
