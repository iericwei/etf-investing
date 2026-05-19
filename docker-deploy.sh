#!/usr/bin/env bash
# docker-deploy.sh — ETF Investing Docker 一键部署
# 用法:
#   ./docker-deploy.sh           构建并后台启动
#   ./docker-deploy.sh restart   重新构建并重启
#   ./docker-deploy.sh stop      停止并移除容器
#   ./docker-deploy.sh status    查看容器状态
#   ./docker-deploy.sh logs      查看实时日志
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "未找到 docker compose 或 docker-compose，请先安装 Docker Desktop。" >&2
    exit 1
  fi
}

ensure_runtime_files() {
  for file in holdings.json watchlist.json; do
    if [ ! -f "$file" ]; then
      printf '[]\n' > "$file"
      log "已创建 $file"
    fi
  done
  if [ ! -f config.json ]; then
    log "未找到 config.json，将使用代码内默认配置"
  fi
}

wait_http() {
  local url="$1"
  local name="$2"
  for _ in {1..30}; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log "$name 已就绪: $url"
      return 0
    fi
    sleep 2
  done
  log "$name 健康检查超时: $url"
  return 1
}

ACTION="${1:-deploy}"

case "$ACTION" in
  deploy|up|start)
    ensure_runtime_files
    log "构建并启动 Docker 服务 ..."
    compose up -d --build
    compose ps
    wait_http "http://localhost:5678/health" "实时行情服务" || true
    wait_http "http://localhost:8080/health" "Web Dashboard" || true
    log "部署完成："
    log "  Web Dashboard: http://localhost:8080"
    log "  实时行情 API: http://localhost:5678"
    ;;
  restart)
    ensure_runtime_files
    log "重启 Docker 服务 ..."
    compose down
    compose up -d --build
    compose ps
    wait_http "http://localhost:5678/health" "实时行情服务" || true
    wait_http "http://localhost:8080/health" "Web Dashboard" || true
    ;;
  stop|down)
    log "停止 Docker 服务 ..."
    compose down
    ;;
  status|ps)
    compose ps
    ;;
  logs)
    compose logs -f --tail=200
    ;;
  *)
    echo "用法: $0 {deploy|start|restart|stop|status|logs}" >&2
    exit 1
    ;;
esac
