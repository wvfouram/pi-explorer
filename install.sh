#!/bin/bash
# Installer for File Explorer (Windows 11 style) on Raspberry Pi
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user (not with sudo). It will ask for sudo when needed."
  exit 1
fi

echo "==> Installing packages"
sudo apt-get update
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gvfs librsvg2-common adwaita-icon-theme xdg-utils
sudo apt-get install -y ffmpegthumbnailer || echo "   (ffmpegthumbnailer not available, video thumbnails disabled)"
sudo apt-get install -y fonts-open-sans || echo "   (Open Sans font not available, using system font)"

echo "==> Installing app"
sudo install -d /opt/pi-explorer
sudo install -m 644 "$SRC/pi_explorer.py" /opt/pi-explorer/pi_explorer.py
printf '#!/bin/sh\nexec /usr/bin/python3 /opt/pi-explorer/pi_explorer.py "$@"\n' | sudo tee /usr/local/bin/pi-explorer >/dev/null
sudo chmod 755 /usr/local/bin/pi-explorer

echo "==> Adding menu entry and icon"
sudo install -D -m 644 "$SRC/pi-explorer.svg" /usr/share/icons/hicolor/scalable/apps/pi-explorer.svg
sudo gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
sudo tee /usr/share/applications/pi-explorer.desktop >/dev/null <<'DESK'
[Desktop Entry]
Type=Application
Name=File Explorer
GenericName=File Manager
Comment=Browse, search and manage your files
Exec=pi-explorer %U
Icon=pi-explorer
Terminal=false
Categories=System;FileTools;FileManager;Utility;GTK;
MimeType=inode/directory;x-directory/normal;
StartupWMClass=pi-explorer
StartupNotify=true
DESK
sudo update-desktop-database -q /usr/share/applications 2>/dev/null || true

echo "==> Making File Explorer the default app for opening folders"
xdg-mime default pi-explorer.desktop inode/directory 2>/dev/null || gio mime inode/directory pi-explorer.desktop
xdg-mime default pi-explorer.desktop x-directory/normal 2>/dev/null || true

echo "==> Adding Super+E shortcut"
/usr/bin/python3 "$SRC/keybind.py"
pkill -HUP -u "$USER" -x labwc 2>/dev/null || true

echo
echo "Done. Open it with Super+E, from the menu (Accessories > File Explorer), or run: pi-explorer"
