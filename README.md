# pi-explorer

A Windows 11 style File Explorer for the Raspberry Pi (built for the Pi 5, works on other Pis running Raspberry Pi OS). Native GTK app, dark theme, opens with **Super+E**, and fully replaces the Pi's old file manager: the folder icon on the taskbar, the menu entry, the `pcmanfm` command and every "open folder" link open File Explorer instead.

![Details view](screenshots/details.png)

## Features

- **Tabs** in the title bar: Ctrl+T, Ctrl+W, Ctrl+Tab, middle-click a folder to open it in a new tab, drag files over a tab to switch to it
- **Address bar** with clickable breadcrumbs. Click a **>** arrow to jump to a subfolder, or click the empty part of the bar to type a path (with folder auto-complete)
- **Search** the current folder and everything inside it as you type. Wildcards like `*.stl` work too
- **Views**: Details, Medium, Large and Extra large icons, with picture and video thumbnails. Ctrl+mouse wheel changes the icon size. Each folder remembers its own view
- **Home** page with Quick access folders and recently opened files
- **This PC** page with your folders plus every drive, its free space bar, and Eject for USB drives
- **Network** page:
  - **Shared from this Pi**: every folder the Pi shares (Samba), plus **Share a folder** / right-click a folder > **Share on network** to share it in one step, and **Stop sharing**
  - **Computers and devices**: other computers on your network (Windows PCs, Macs, other Pis, NAS boxes). Click one to see its shared folders
  - **Open a network folder**: type `\\server\share` or `smb://server/share`, sign in with a Windows-style credentials box, and browse it like a normal folder (copy, paste, drag and drop all work)
  - Connected network folders also show in the navigation pane and on This PC, with **Disconnect**
- **Navigation pane** with pinned folders and expandable This PC tree. You can drop files on any folder in it
- **Recycle Bin** with Restore, Restore all and Empty Recycle Bin (uses the normal Linux trash, so it matches the old file manager)
- **File operations**: copy, cut, paste, drag and drop, rename in place (F2), new folder, new text/Python/shell file, delete to Recycle Bin, Shift+Delete to delete permanently
- **Replace or Skip Files** dialog when names clash (Replace, Skip, or Keep both) and a Windows-style progress window with speed and time remaining
- **Compress to ZIP file** and **Extract all** (zip, tar.gz, tar.xz and more)
- **Details pane** (Alt+Shift+P) with a large preview, image dimensions and dates
- **Properties** (Alt+Enter) with size and file/folder counts, a used/free chart for drives, Read-only and Hidden attributes, and a Permissions tab
- **Open with** menu listing your installed apps, **Open in Terminal**, **Copy as path**
- Colored Windows 11 style icons for folders and file types, including 3D printing files (STL, 3MF, STEP, G-code)

![Large icons](screenshots/icons.png)
![This PC](screenshots/thispc.png)
![Network](screenshots/network.png)
![Context menu](screenshots/menu.png)
![Search](screenshots/search.png)
![Home](screenshots/home.png)

## Install

In a terminal on the Pi (as your normal user, not with sudo):

```
git clone https://github.com/wvfouram/pi-explorer.git && bash pi-explorer/install.sh
```

Then press **Super+E**, click the folder icon on the taskbar, open **Accessories > File Explorer** from the menu, or run `pi-explorer`.

To update later:

```
cd ~/pi-explorer && git pull && bash install.sh
```

## What the installer does

- Installs `python3-gi`, `python3-gi-cairo`, GTK 3, `gvfs`, `gvfs-backends`, `gvfs-fuse` (network folders), `avahi-daemon` and `wsdd` (finding computers on the network), `ffmpegthumbnailer` (video thumbnails) and the Open Sans font
- Puts the app in `/opt/pi-explorer` and a launcher at `/usr/local/bin/pi-explorer`
- Installs a small root helper at `/usr/local/lib/pi-explorer/pie-helper` and a sudo rule for it, so you can share folders without typing a password. It only adds or removes shares in `/etc/samba/pi-explorer-shares.conf`, only for folders you own in `/home` (or folders in `/srv`, `/mnt`, `/media`, `/opt`, `/data`), and checks the Samba config before reloading it
- Replaces the old file manager:
  - Makes File Explorer the default app for opening folders (`xdg-mime`)
  - Puts a `pcmanfm.desktop` in `~/.local/share/applications`, so the taskbar/dock folder icon and the menu entry open File Explorer
  - Swaps the `pcmanfm` command for a small wrapper (using `dpkg-divert`, so Pi OS updates don't undo it). Running `pcmanfm` opens File Explorer. The desktop background and desktop icons still use the real program (`pcmanfm --desktop`)
- Adds a menu entry and icon
- Adds Super+E to `~/.config/labwc/rc.xml` (or Wayfire / Openbox config), keeping a backup

## Keyboard shortcuts

| Keys | Action |
|---|---|
| Super+E | Open File Explorer |
| Ctrl+T / Ctrl+W | New tab / close tab |
| Ctrl+Tab | Next tab |
| Ctrl+N | New window |
| Alt+Left / Alt+Right / Alt+Up | Back / Forward / Up |
| Backspace | Back |
| Ctrl+L or Alt+D | Type in the address bar |
| Ctrl+F or Ctrl+E | Search |
| F5 | Refresh |
| Ctrl+C / Ctrl+X / Ctrl+V | Copy / Cut / Paste |
| Ctrl+A | Select all |
| F2 | Rename |
| Delete / Shift+Delete | Recycle Bin / delete permanently |
| Ctrl+Shift+N | New folder |
| Ctrl+Shift+C | Copy as path |
| Alt+Enter | Properties |
| Alt+Shift+P | Details pane |
| Ctrl+H | Show hidden files |
| Ctrl+Shift+1 / 2 / 3 / 6 | Extra large / Large / Medium icons / Details |

## Settings

Open **... > Options** to pick where File Explorer opens (Home, This PC or your home folder), show hidden files, show file name extensions, turn thumbnails off, hide recent files, ask before deleting, and clear recent file history. Settings are stored in `~/.config/pi-explorer/settings.json`.

## Uninstall

```
bash pi-explorer/uninstall.sh
```

This removes the app, the Super+E shortcut and the sharing helper, and gives the Pi's original file manager (PCManFM) its taskbar icon, menu entry, command and folder default back.

## License

MIT
