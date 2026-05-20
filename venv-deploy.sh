#!/usr/bin/env bash
# venv-deploy.sh — 本地虚拟环境一键部署
# 用法:
#   ./venv-deploy.sh            创建/更新 .venv，安装依赖，并重启服务
#   ./venv-deploy.sh install    只创建/更新 .venv 和依赖
#   ./venv-deploy.sh restart    使用现有 .venv 重启服务
#   ./venv-deploy.sh status     查看服务状态
#   ./venv-deploy.sh recreate   删除并重建 .venv，然后重启服务
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-}"
ACTION="${1:-deploy}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

usage() {
  cat <<EOF
用法: $0 {deploy|install|restart|status|recreate|help}

命令:
  deploy    默认：创建/更新 .venv，安装依赖，重启服务
  install   只创建/更新 .venv 和依赖，不启动服务
  restart   使用现有 .venv 重启服务
  status    查看服务状态
  recreate  删除并重建 .venv，然后重启服务

环境变量:
  PYTHON_BIN=/path/to/python3   指定创建虚拟环境使用的 Python
  VENV_DIR=/path/to/.venv       指定虚拟环境目录，默认项目根目录 .venv
EOF
}

find_python() {
  if [ -n "$PYTHON_BIN" ]; then
    if [ ! -x "$PYTHON_BIN" ]; then
      echo "指定的 PYTHON_BIN 不可执行: $PYTHON_BIN" >&2
      exit 1
    fi
    echo "$PYTHON_BIN"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  echo "未找到 python3/python，请先安装 Python 3.10+，或设置 PYTHON_BIN。" >&2
  exit 1
}

ensure_venv() {
  local py
  py="$(find_python)"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "创建虚拟环境: $VENV_DIR"
    "$py" -m venv "$VENV_DIR"
  else
    log "使用已有虚拟环境: $VENV_DIR"
  fi

  log "Python 版本: $($VENV_DIR/bin/python --version)"
  log "升级 pip/setuptools/wheel ..."
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

  if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    log "安装 requirements.txt 依赖 ..."
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
  fi

  if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    log "以 editable 模式安装当前项目 ..."
    "$VENV_DIR/bin/python" -m pip install -e "$SCRIPT_DIR"
  fi
}

verify_imports() {
  log "验证核心依赖导入 ..."
  "$VENV_DIR/bin/python" - <<'PY'
import flask, flask_cors, pandas, numpy, requests, futu
print('core imports ok')
PY
}

restart_services() {
  log "通过 restart.sh 重启服务 ..."
  "$SCRIPT_DIR/restart.sh" restart
}

status_services() {
  "$SCRIPT_DIR/restart.sh" status
}

case "$ACTION" in
  deploy|"")
    ensure_venv
    verify_imports
    restart_services
    status_services
    ;;
  install)
    ensure_venv
    verify_imports
    log "安装完成。需要启动服务时运行: ./restart.sh restart"
    ;;
  restart)
    if [ ! -x "$VENV_DIR/bin/python" ]; then
      echo "未找到虚拟环境: $VENV_DIR。请先运行 ./venv-deploy.sh install" >&2
      exit 1
    fi
    restart_services
    status_services
    ;;
  status)
    status_services
    ;;
  recreate)
    log "删除旧虚拟环境: $VENV_DIR"
    rm -rf "$VENV_DIR"
    ensure_venv
    verify_imports
    restart_services
    status_services
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
