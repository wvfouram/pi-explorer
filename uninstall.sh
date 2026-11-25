#!/bin/bash
# Removes File Explorer and puts the Pi's original file manager back as the default.
SRC="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user (not with sudo)."
  exit 1
fi
/usr/bin/python3 "$SRC/keybind.py" --remove
pkill -HUP -u "$USER" -x labwc 2>/dev/null || true
xdg-mime default pcmanfm.desktop inode/directory 2>/dev/null || true
xdg-mime default pcmanfm.desktop x-directory/normal 2>/dev/null || true
sudo rm -rf /opt/pi-explorer /usr/local/bin/pi-explorer /usr/share/applications/pi-explorer.desktop \
  /usr/share/icons/hicolor/scalable/apps/pi-explorer.svg
sudo update-desktop-database -q /usr/share/applications 2>/dev/null || true
echo "File Explorer removed. Your settings are still in ~/.config/pi-explorer (delete that folder to remove them)."
