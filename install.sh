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
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gvfs gvfs-backends gvfs-fuse librsvg2-common \
  adwaita-icon-theme xdg-utils avahi-daemon
# wsdd lets the Network page find Windows PCs (skip it if wsdd2 is already doing that job)
if ! command -v wsdd >/dev/null && ! dpkg -s wsdd2 >/dev/null 2>&1; then
  sudo apt-get install -y wsdd || echo "   (wsdd not available, Windows PCs won't be listed automatically)"
fi
sudo apt-get install -y ffmpegthumbnailer || echo "   (ffmpegthumbnailer not available, video thumbnails disabled)"
sudo apt-get install -y fonts-open-sans || echo "   (Open Sans font not available, using system font)"

echo "==> Installing app"
sudo install -d /opt/pi-explorer
sudo install -m 644 "$SRC/pi_explorer.py" /opt/pi-explorer/pi_explorer.py
printf '#!/bin/sh\nexec /usr/bin/python3 /opt/pi-explorer/pi_explorer.py "$@"\n' | sudo tee /usr/local/bin/pi-explorer >/dev/null
sudo chmod 755 /usr/local/bin/pi-explorer

echo "==> Allowing network sharing (share folders with Samba)"
sudo install -D -m 755 -o root -g root "$SRC/pie-helper" /usr/local/lib/pi-explorer/pie-helper
TMP=$(mktemp)
echo "$USER ALL=(root) NOPASSWD: /usr/local/lib/pi-explorer/pie-helper" > "$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
  sudo install -m 440 -o root -g root "$TMP" /etc/sudoers.d/pi-explorer
else
  echo "   sudoers check failed, sharing from File Explorer not enabled"
fi
rm -f "$TMP"

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
NoDisplay=true
Categories=System;FileTools;FileManager;Utility;GTK;
MimeType=inode/directory;x-directory/normal;
StartupWMClass=pi-explorer
StartupNotify=true
DESK
sudo update-desktop-database -q /usr/share/applications 2>/dev/null || true

echo "==> Replacing the old file manager (taskbar/dock icon, menu entry and the pcmanfm command)"
# The taskbar, dock and menu all use pcmanfm.desktop. A copy in your own applications folder wins,
# so this points that same icon at File Explorer.
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/pcmanfm.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=File Explorer
GenericName=File Manager
Comment=Browse, search and manage your files
Exec=pi-explorer %U
Icon=pi-explorer
Terminal=false
Categories=Utility;FileTools;FileManager;System;
MimeType=inode/directory;x-directory/normal;
StartupWMClass=pi-explorer
StartupNotify=true
X-PiExplorer=1
DESK
update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true
# Anything that runs the "pcmanfm" command to open a folder now gets File Explorer.
# The desktop background and icons still use the real pcmanfm (pcmanfm --desktop ...).
if [ -x /usr/bin/pcmanfm ] && ! dpkg-divert --list /usr/bin/pcmanfm | grep -q pcmanfm.real; then
  sudo dpkg-divert --local --rename --add --divert /usr/bin/pcmanfm.real /usr/bin/pcmanfm >/dev/null
fi
if [ -x /usr/bin/pcmanfm.real ]; then
  sudo install -m 755 "$SRC/pcmanfm-wrapper" /usr/bin/pcmanfm
fi

echo "==> Making File Explorer the default app for opening folders"
xdg-mime default pi-explorer.desktop inode/directory 2>/dev/null || gio mime inode/directory pi-explorer.desktop
xdg-mime default pi-explorer.desktop x-directory/normal 2>/dev/null || true

echo "==> Adding Super+E shortcut"
/usr/bin/python3 "$SRC/keybind.py"
pkill -HUP -u "$USER" -x labwc 2>/dev/null || true

echo
# reload the taskbar so its folder icon picks up the change
pkill -u "$USER" -x wf-panel-pi 2>/dev/null || true

echo
echo "Done. Open it with Super+E, the folder icon on the taskbar, the menu (Accessories > File Explorer),"
echo "or run: pi-explorer"
