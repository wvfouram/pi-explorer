#!/bin/bash
# Removes File Explorer and puts the Pi's original file manager back as the default.
SRC="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user (not with sudo)."
  exit 1
fi
/usr/bin/python3 "$SRC/keybind.py" --remove
pkill -HUP -u "$USER" -x labwc 2>/dev/null || true
# give the old file manager its icon and command back
if grep -q "X-PiExplorer=1" "$HOME/.local/share/applications/pcmanfm.desktop" 2>/dev/null; then
  rm -f "$HOME/.local/share/applications/pcmanfm.desktop"
  update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true
fi
if dpkg-divert --list /usr/bin/pcmanfm | grep -q pcmanfm.real; then
  sudo rm -f /usr/bin/pcmanfm
  sudo dpkg-divert --local --rename --remove /usr/bin/pcmanfm >/dev/null
fi
pkill -u "$USER" -x wf-panel-pi 2>/dev/null || true
xdg-mime default pcmanfm.desktop inode/directory 2>/dev/null || true
xdg-mime default pcmanfm.desktop x-directory/normal 2>/dev/null || true
sudo rm -rf /opt/pi-explorer /usr/local/lib/pi-explorer /etc/sudoers.d/pi-explorer /usr/local/bin/pi-explorer /usr/share/applications/pi-explorer.desktop \
  /usr/share/icons/hicolor/scalable/apps/pi-explorer.svg
sudo update-desktop-database -q /usr/share/applications 2>/dev/null || true
echo "Folders you shared stay shared until you remove them from /etc/samba/pi-explorer-shares.conf."
echo "File Explorer removed. Your settings are still in ~/.config/pi-explorer (delete that folder to remove them)."
