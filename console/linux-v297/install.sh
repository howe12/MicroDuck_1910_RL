#!/usr/bin/env bash
# Install a desktop entry for the current user. Does not copy the app.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
chmod +x "$HERE/start.sh" "$HERE/ssh-askpass" 2>/dev/null || true

cat > "$APPS/microduck-console.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Microduck Console
Name[zh_CN]=Microduck 中控
Comment=Local hardware console for Microduck / Feetech HD1910
Comment[zh_CN]=Microduck 飞特 HD1910 本地中控
Exec="$HERE/start.sh"
Path=$HERE
Icon=utilities-terminal
Terminal=true
Categories=Development;Robotics;
StartupNotify=true
EOF
chmod +x "$APPS/microduck-console.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi
echo "已安装桌面入口：$APPS/microduck-console.desktop"
echo "启动命令：$HERE/start.sh"
