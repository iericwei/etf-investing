#!/usr/bin/env bash
# venv-deploy.sh — 本地虚拟环境一键部署
# 用法:
#   ./venv-deploy.sh            创建/更新 .venv，安装依赖，并重启服务
#   ./venv-deploy.sh install    只创建/更新 .venv 和依赖
#   ./venv-deploy.sh restart    使用现有 .venv 重启服务
#   ./venv-deploy.sh status     查看服务状态
#   ./venv-deploy.sh check      检查 .venv 版本和核心依赖，不安装、不重启
#   ./venv-deploy.sh recreate   删除并重建 .venv，然后重启服务
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-}"
REQUIRED_PYTHON_MAJOR="${REQUIRED_PYTHON_MAJOR:-3}"
REQUIRED_PYTHON_MINOR="${REQUIRED_PYTHON_MINOR:-13}"
REQUIRED_PIP_VERSION="${REQUIRED_PIP_VERSION:-26.1.1}"
REQUIRED_SETUPTOOLS_VERSION="${REQUIRED_SETUPTOOLS_VERSION:-82.0.1}"
REQUIRED_WHEEL_VERSION="${REQUIRED_WHEEL_VERSION:-0.47.0}"
AUTO_INSTALL_PYTHON="${AUTO_INSTALL_PYTHON:-1}"
AUTO_RECREATE_VENV="${AUTO_RECREATE_VENV:-1}"
ACTION="${1:-deploy}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "错误: $*" >&2; exit 1; }

usage() {
  cat <<EOF
用法: $0 {deploy|install|restart|status|check|recreate|help}

命令:
  deploy    默认：创建/更新 .venv，安装依赖，重启服务
  install   只创建/更新 .venv 和依赖，不启动服务
  restart   使用现有 .venv 重启服务
  status    查看服务状态
  check     检查 .venv 版本和核心依赖，不安装、不重启
  recreate  删除并重建 .venv，然后重启服务

环境变量:
  PYTHON_BIN=/path/to/python3.13  指定创建虚拟环境使用的 Python
  VENV_DIR=/path/to/.venv         指定虚拟环境目录，默认项目根目录 .venv
  REQUIRED_PYTHON_MAJOR=3         要求的 Python major 版本
  REQUIRED_PYTHON_MINOR=13        要求的 Python minor 版本
  REQUIRED_PIP_VERSION=26.1.1     要求安装的 pip 版本
  REQUIRED_SETUPTOOLS_VERSION=82.0.1  要求安装的 setuptools 版本
  REQUIRED_WHEEL_VERSION=0.47.0   要求安装的 wheel 版本
  AUTO_INSTALL_PYTHON=1           未找到 Python 时尝试自动安装；设为 0 则只报错
  AUTO_RECREATE_VENV=1            .venv 版本不匹配时自动重建；设为 0 则报错
EOF
}

required_python_label() {
  echo "${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}"
}

python_version_tuple() {
  "$1" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

python_matches_required() {
  "$1" - "$REQUIRED_PYTHON_MAJOR" "$REQUIRED_PYTHON_MINOR" <<'PY'
import sys
major = int(sys.argv[1])
minor = int(sys.argv[2])
raise SystemExit(0 if sys.version_info[:2] == (major, minor) else 1)
PY
}

assert_python_matches_required() {
  local py="$1"
  local version
  version="$(python_version_tuple "$py")"
  if ! python_matches_required "$py"; then
    die "$py 版本为 $version，项目要求 Python $(required_python_label).x。"
  fi
}

find_matching_python() {
  local candidate resolved
  for candidate in "python$(required_python_label)" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      resolved="$(command -v "$candidate")"
      if python_matches_required "$resolved"; then
        echo "$resolved"
        return 0
      fi
    fi
  done

  if command -v uv >/dev/null 2>&1; then
    resolved="$(uv python find "$(required_python_label)" 2>/dev/null || true)"
    if [ -n "$resolved" ] && [ -x "$resolved" ] && python_matches_required "$resolved"; then
      echo "$resolved"
      return 0
    fi
  fi

  return 1
}

install_python_with_uv() {
  command -v uv >/dev/null 2>&1 || return 1
  log "尝试通过 uv 安装 Python $(required_python_label) ..."
  uv python install "$(required_python_label)"
}

install_python_with_brew() {
  command -v brew >/dev/null 2>&1 || return 1
  log "尝试通过 Homebrew 安装 python@$(required_python_label) ..."
  brew install "python@$(required_python_label)" || brew upgrade "python@$(required_python_label)"
}

install_python_with_apt() {
  command -v apt-get >/dev/null 2>&1 || return 1
  local package="python$(required_python_label)"
  local venv_package="${package}-venv"
  local dev_package="${package}-dev"

  log "尝试通过 apt 安装 $package ..."
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update
    apt-get install -y "$package" "$venv_package" "$dev_package"
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y "$package" "$venv_package" "$dev_package"
  else
    return 1
  fi
}

install_required_python() {
  [ "$AUTO_INSTALL_PYTHON" = "1" ] || return 1

  if install_python_with_uv; then
    return 0
  fi

  case "$(uname -s)" in
    Darwin)
      install_python_with_brew
      ;;
    Linux)
      install_python_with_apt
      ;;
    *)
      return 1
      ;;
  esac
}

find_python() {
  if [ -n "$PYTHON_BIN" ]; then
    [ -x "$PYTHON_BIN" ] || die "指定的 PYTHON_BIN 不可执行: $PYTHON_BIN"
    assert_python_matches_required "$PYTHON_BIN"
    echo "$PYTHON_BIN"
    return
  fi

  if find_matching_python; then
    return
  fi

  if install_required_python && find_matching_python; then
    return
  fi

  die "未找到 Python $(required_python_label).x，且自动安装失败。请安装 python$(required_python_label)，或设置 PYTHON_BIN=/path/to/python$(required_python_label)。"
}

ensure_venv_python_version() {
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    return
  fi

  local version
  version="$(python_version_tuple "$VENV_DIR/bin/python")"
  if python_matches_required "$VENV_DIR/bin/python"; then
    log "虚拟环境 Python 版本符合要求: $version"
    return
  fi

  if [ "$AUTO_RECREATE_VENV" = "1" ]; then
    log "虚拟环境 Python 版本为 $version，不符合 Python $(required_python_label).x；自动重建 $VENV_DIR"
    rm -rf "$VENV_DIR"
    return
  fi

  die "虚拟环境 Python 版本为 $version，不符合 Python $(required_python_label).x。请运行 ./venv-deploy.sh recreate，或设置 AUTO_RECREATE_VENV=1。"
}

ensure_pip_bootstrap() {
  if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    log "venv 中未检测到 pip，尝试通过 ensurepip 安装 ..."
    "$VENV_DIR/bin/python" -m ensurepip --upgrade
  fi
}

ensure_pip_consistency() {
  local pip_output
  pip_output="$($VENV_DIR/bin/python -m pip --version)"
  log "pip 版本: $pip_output"

  "$VENV_DIR/bin/python" - "$REQUIRED_PIP_VERSION" "$REQUIRED_SETUPTOOLS_VERSION" "$REQUIRED_WHEEL_VERSION" <<'PY'
import re
import subprocess
import sys
from importlib import metadata

expected_python = f"python {sys.version_info.major}.{sys.version_info.minor}"
pip_output = subprocess.check_output([sys.executable, "-m", "pip", "--version"], text=True).strip()
if expected_python not in pip_output:
    raise SystemExit(f"pip 未绑定到当前 venv Python: {pip_output}")

match = re.search(r"pip (\d+)\.(\d+)(?:\.(\d+))?", pip_output)
if not match:
    raise SystemExit(f"无法解析 pip 版本: {pip_output}")

pip_version = ".".join(part for part in match.groups() if part is not None)
expected = {
    "pip": sys.argv[1],
    "setuptools": sys.argv[2],
    "wheel": sys.argv[3],
}
actual = {
    "pip": pip_version,
    "setuptools": metadata.version("setuptools"),
    "wheel": metadata.version("wheel"),
}
for package, expected_version in expected.items():
    if actual[package] != expected_version:
        raise SystemExit(f"{package} 版本为 {actual[package]}，要求 {expected_version}")
PY
}

install_packaging_tools() {
  log "安装指定版本的 pip/setuptools/wheel ..."
  "$VENV_DIR/bin/python" -m pip install \
    "pip==$REQUIRED_PIP_VERSION" \
    "setuptools==$REQUIRED_SETUPTOOLS_VERSION" \
    "wheel==$REQUIRED_WHEEL_VERSION"
}

ensure_venv() {
  ensure_venv_python_version
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    local py
    py="$(find_python)"
    log "创建虚拟环境: $VENV_DIR"
    "$py" -m venv "$VENV_DIR"
  else
    log "使用已有虚拟环境: $VENV_DIR"
  fi

  assert_python_matches_required "$VENV_DIR/bin/python"
  log "Python 版本: $($VENV_DIR/bin/python --version)"
  ensure_pip_bootstrap
  install_packaging_tools
  ensure_pip_consistency

  if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    log "安装 requirements.txt 依赖 ..."
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
  fi

  if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    log "以 editable 模式安装当前项目 ..."
    "$VENV_DIR/bin/python" -m pip install --no-build-isolation -e "$SCRIPT_DIR"
  fi
}

verify_imports() {
  log "验证核心依赖导入 ..."
  "$VENV_DIR/bin/python" - <<'PY'
import importlib
import importlib.util

safe_imports = ["flask", "flask_cors", "pandas", "numpy", "requests", "mootdx", "akshare"]
spec_only = ["futu"]

missing = []
for name in safe_imports:
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")

for name in spec_only:
    if importlib.util.find_spec(name) is None:
        missing.append(f"{name}: module not found")

if missing:
    raise SystemExit("核心依赖验证失败:\n" + "\n".join(missing))

print("core imports ok")
PY
}

restart_services() {
  log "通过 restart.sh 重启服务 ..."
  "$SCRIPT_DIR/restart.sh" restart
}

status_services() {
  "$SCRIPT_DIR/restart.sh" status
}

check_environment() {
  [ -x "$VENV_DIR/bin/python" ] || die "未找到虚拟环境: $VENV_DIR。请先运行 ./venv-deploy.sh install"
  assert_python_matches_required "$VENV_DIR/bin/python"
  log "Python 版本: $($VENV_DIR/bin/python --version)"
  ensure_pip_bootstrap
  ensure_pip_consistency
  verify_imports
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
    assert_python_matches_required "$VENV_DIR/bin/python"
    ensure_pip_bootstrap
    ensure_pip_consistency
    restart_services
    status_services
    ;;
  status)
    status_services
    ;;
  check)
    check_environment
    log "检查完成。"
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
