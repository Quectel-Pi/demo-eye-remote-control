#!/usr/bin/env bash
set -euo pipefail

#===============================================================================
# Eye Remote Control - 开机自启动安装脚本 (用户级 systemd)
# 用法: bash setup_autostart.sh [install|uninstall|status]
#===============================================================================

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="eye-remote-control"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="${SERVICE_DIR}/${SERVICE_NAME}.service"
LEGACY_SYSTEM_SERVICE="/etc/systemd/system/${SERVICE_NAME}.service"

install_service() {
  echo "==> 生成用户服务文件..."
  mkdir -p "${SERVICE_DIR}"
  chmod +x "${APP_DIR}/start.sh"

  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Eye Remote Control
After=graphical-session.target
PartOf=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
Environment=HOME=%h
Environment=PYTHONUNBUFFERED=1
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/%U
Environment=PATH=%h/.pyenv/shims:%h/.pyenv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/start.sh
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

  echo "==> 启用并启动用户服务..."
  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE_NAME}"
  systemctl --user restart "${SERVICE_NAME}"

  if command -v sudo >/dev/null 2>&1; then
    # 允许未登录桌面也可由 systemd 用户实例拉起（可选）
    sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  fi

  if [[ -f "${LEGACY_SYSTEM_SERVICE}" ]]; then
    echo "==> 发现旧系统级服务，正在停用避免冲突..."
    if command -v sudo >/dev/null 2>&1; then
      sudo systemctl disable --now "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
      sudo rm -f "${LEGACY_SYSTEM_SERVICE}"
      sudo systemctl daemon-reload
    fi
  fi

  echo ""
  echo "安装完成。"
  echo "查看状态: systemctl --user status ${SERVICE_NAME}"
  echo "查看日志: journalctl --user -u ${SERVICE_NAME} -f"
}

uninstall_service() {
  echo "==> 卸载用户服务..."
  systemctl --user disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
  rm -f "${SERVICE_FILE}"
  systemctl --user daemon-reload
  systemctl --user reset-failed "${SERVICE_NAME}" >/dev/null 2>&1 || true

  if [[ -f "${LEGACY_SYSTEM_SERVICE}" ]] && command -v sudo >/dev/null 2>&1; then
    echo "==> 同时清理旧系统级服务..."
    sudo systemctl disable --now "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    sudo rm -f "${LEGACY_SYSTEM_SERVICE}"
    sudo systemctl daemon-reload
  fi

  echo "已卸载。"
}

show_status() {
  echo "==> 用户服务状态"
  echo "Service file: ${SERVICE_FILE}"
  systemctl --user status "${SERVICE_NAME}" --no-pager -l || true
}

print_usage() {
  echo "用法: $0 [install|uninstall|status]"
}

ACTION="${1:-install}"

case "${ACTION}" in
  install)
    install_service
    ;;
  uninstall)
    uninstall_service
    ;;
  status)
    show_status
    ;;
  -h|--help|help)
    print_usage
    ;;
  *)
    echo "未知操作: ${ACTION}"
    print_usage
    exit 1
    ;;
esac
