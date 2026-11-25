#!/usr/bin/python3
# File Explorer for Raspberry Pi, styled after the Windows 11 File Explorer (dark).
import os
import sys
import re
import json
import time
import math
import stat
import shutil
import signal
import hashlib
import datetime
import threading
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Pango, Gio, GObject  # noqa: E402
import cairo  # noqa: E402

APP_ID = "io.github.piexplorer"
VERSION = "1.0"
HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".config", "pi-explorer")
CONF_FILE = os.path.join(CONF_DIR, "settings.json")
TRASH_DIR = os.path.join(GLib.get_user_data_dir(), "Trash")
RECENT_FILE = os.path.join(GLib.get_user_data_dir(), "recently-used.xbel")

# special locations
LOC_HOME = "::home"
LOC_PC = "::pc"
LOC_TRASH = "::trash"
LOC_NET = "::net"
LOC_NAMES = {LOC_HOME: "Home", LOC_PC: "This PC", LOC_TRASH: "Recycle Bin", LOC_NET: "Network"}
PAGE_MODES = ("home", "pc", "net", "nethost")
HELPER = "/usr/local/lib/pi-explorer/pie-helper"
SHARES_CONF = "/etc/samba/pi-explorer-shares.conf"


def is_special(loc):
    return bool(loc) and loc.startswith("::")


def host_loc(uri):
    return "::net/" + uri


def loc_host_uri(loc):
    return loc[6:] if loc and loc.startswith("::net/") else None


def uri_host(uri):
    try:
        return urllib.parse.urlparse(uri).hostname or uri
    except ValueError:
        return uri


def loc_kind(loc):
    if loc in (LOC_HOME,):
        return "home"
    if loc == LOC_PC:
        return "pc"
    if loc == LOC_TRASH:
        return "recycle"
    if loc == LOC_NET:
        return "net"
    if loc_host_uri(loc):
        return "netpc"
    if loc == "/":
        return "drive-sys"
    if loc == USER_DIRS[0][1]:
        return "desktop"
    if is_gvfs_path(loc):
        return "netshare"
    return USER_DIR_KIND.get(loc, "folder")


def is_gvfs_path(p):
    return bool(p) and re.search(r"/\.?gvfs/[^/]+:", p + "/") is not None


def gvfs_label(p):
    """Readable name for a gvfs fuse mount folder like smb-share:server=nas,share=media."""
    base = os.path.basename(p.rstrip("/"))
    m = re.match(r"smb-share:(.*)", base)
    if m:
        kv = dict(x.split("=", 1) for x in m.group(1).split(",") if "=" in x)
        if "share" in kv:
            return f"{kv['share']} ({kv.get('server', '')})"
    return base

C_ACCENT = "#4CC2FF"


def hexc(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a)


def special_dir(kind, fallback):
    try:
        p = GLib.get_user_special_dir(kind)
    except Exception:
        p = None
    if not p or os.path.realpath(p) == os.path.realpath(HOME):
        p = os.path.join(HOME, fallback)
    return p


USER_DIRS = [
    ("Desktop", special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP, "Desktop"), "desktop"),
    ("Downloads", special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD, "Downloads"), "downloads"),
    ("Documents", special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS, "Documents"), "documents"),
    ("Pictures", special_dir(GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures"), "pictures"),
    ("Music", special_dir(GLib.UserDirectory.DIRECTORY_MUSIC, "Music"), "music"),
    ("Videos", special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS, "Videos"), "videos"),
]
USER_DIR_KIND = {p: k for _, p, k in USER_DIRS}
USER_DIR_KIND[HOME] = "homefolder"


# ----------------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------------
def fmt_size(b):
    if b is None:
        return ""
    b = float(b)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(b)} bytes"
            return f"{b:.2f} {unit}" if b < 10 else (f"{b:.1f} {unit}" if b < 100 else f"{b:.0f} {unit}")
        b /= 1024


def fmt_kb(b):
    """Windows Details view size column: always KB, rounded up."""
    if b is None:
        return ""
    return f"{max(0 if b == 0 else 1, math.ceil(b / 1024)):,} KB"


def fmt_bytes_long(b):
    return f"{fmt_size(b)} ({b:,} bytes)"


def fmt_date(ts):
    if not ts:
        return ""
    d = datetime.datetime.fromtimestamp(ts)
    h = d.hour % 12 or 12
    return f"{d.month}/{d.day}/{d.year} {h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def fmt_date_long(ts):
    if not ts:
        return ""
    d = datetime.datetime.fromtimestamp(ts)
    h = d.hour % 12 or 12
    return (f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}, "
            f"{h}:{d.minute:02d}:{d.second:02d} {'AM' if d.hour < 12 else 'PM'}")


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.casefold())]


def esc(s):
    return GLib.markup_escape_text(s)


def disp_name(path):
    if path in LOC_NAMES:
        return LOC_NAMES[path]
    if loc_host_uri(path):
        return uri_host(loc_host_uri(path))
    if is_gvfs_path(path) and os.path.dirname(path.rstrip("/")).endswith("gvfs"):
        return gvfs_label(path)
    if path == "/":
        return "System (/)"
    if os.path.realpath(path) == os.path.realpath(HOME):
        return os.path.basename(HOME) or HOME
    return os.path.basename(path.rstrip("/")) or path


def split_ext(name, is_dir=False):
    if is_dir or name.startswith(".") and name.count(".") == 1:
        return name, ""
    stem, ext = os.path.splitext(name)
    if stem.lower().endswith(".tar") and ext.lower() in (".gz", ".xz", ".bz2", ".zst"):
        stem, e2 = os.path.splitext(stem)
        ext = e2 + ext
    return stem, ext


def unique_name(folder, name, is_dir=False, style="paren"):
    """Return a name not existing in folder. style 'paren': 'x (2).txt', 'copy': 'x - Copy.txt'."""
    if not os.path.lexists(os.path.join(folder, name)):
        return name
    stem, ext = split_ext(name, is_dir)
    if style == "copy":
        cand = f"{stem} - Copy{ext}"
        if not os.path.lexists(os.path.join(folder, cand)):
            return cand
        n = 2
        while True:
            cand = f"{stem} - Copy ({n}){ext}"
            if not os.path.lexists(os.path.join(folder, cand)):
                return cand
            n += 1
    n = 2
    while True:
        cand = f"{stem} ({n}){ext}"
        if not os.path.lexists(os.path.join(folder, cand)):
            return cand
        n += 1


def path_to_uri(p):
    return Gio.File.new_for_path(p).get_uri()


def uri_to_path(u):
    u = u.strip()
    if u.startswith("file://"):
        return Gio.File.new_for_uri(u).get_path()
    if u.startswith("/"):
        return u
    return None


def is_inside(child, parent):
    c = os.path.realpath(child)
    p = os.path.realpath(parent)
    return c == p or c.startswith(p.rstrip("/") + "/")


def same_device(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


# ----------------------------------------------------------------------------
# File kinds and type names
# ----------------------------------------------------------------------------
KIND_EXT = {
    "image": "jpg jpeg png gif bmp webp tif tiff svg ico heic avif raw cr2 nef xcf",
    "video": "mp4 mkv avi mov webm m4v wmv flv mpg mpeg 3gp h264 ts",
    "audio": "mp3 wav flac ogg m4a aac wma opus mid midi",
    "archive": "zip tar gz tgz xz bz2 7z rar zst deb iso img",
    "pdf": "pdf",
    "code": ("py c h cpp hpp cc js ts jsx tsx html htm css json xml yaml yml toml ini conf cfg java rs go rb php lua "
             "kt swift ino scad sql vue svelte"),
    "script": "sh bash run bat cmd ps1 appimage",
    "text": "txt md log csv rtf nfo readme",
    "doc": "doc docx odt",
    "sheet": "xls xlsx ods",
    "slides": "ppt pptx odp",
    "model": "stl 3mf step stp obj gcode bgcode amf ply fbx blend f3d",
    "font": "ttf otf woff woff2",
}
EXT_KIND = {}
for _k, _v in KIND_EXT.items():
    for _e in _v.split():
        EXT_KIND[_e] = _k


def file_kind(name, is_dir=False, mode=0):
    if is_dir:
        return "folder"
    low = name.lower()
    if low.endswith(".desktop"):
        return "app"
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    k = EXT_KIND.get(ext)
    if k:
        return k
    if mode & 0o111 and not ext:
        return "script"
    return "file"


_type_cache = {}
TYPE_OVERRIDE = {
    "txt": "Text Document", "md": "Markdown File", "py": "Python File", "sh": "Shell Script",
    "stl": "STL 3D Model", "3mf": "3MF 3D Model", "step": "STEP 3D Model", "stp": "STEP 3D Model",
    "gcode": "G-code File", "bgcode": "Binary G-code File", "scad": "OpenSCAD File", "ino": "Arduino Sketch",
    "zip": "Compressed (zipped) Folder", "json": "JSON File", "log": "Text Document", "csv": "CSV File",
    "conf": "Configuration File", "ini": "Configuration Settings", "desktop": "Application Shortcut",
    "img": "Disk Image File", "iso": "Disc Image File", "jpg": "JPG File", "jpeg": "JPEG File",
    "png": "PNG File", "gif": "GIF File", "webp": "WEBP File", "svg": "SVG Image", "mp4": "MP4 File",
    "mp3": "MP3 File", "wav": "WAV File", "pdf": "PDF Document", "html": "HTML Document",
}


def type_name(name, is_dir=False):
    if is_dir:
        return "File folder"
    low = name.lower()
    if low.endswith(".desktop"):
        return "Application Shortcut"
    ext = os.path.splitext(low)[1][1:]
    if ext in TYPE_OVERRIDE:
        return TYPE_OVERRIDE[ext]
    if ext in _type_cache:
        return _type_cache[ext]
    if not ext:
        return "File"
    try:
        ct, _ = Gio.content_type_guess(name, None)
        desc = Gio.content_type_get_description(ct) if ct else ""
    except Exception:
        desc = ""
    if not desc or desc.lower() in ("unknown", "binary") or "application/octet" in (desc or ""):
        desc = f"{ext.upper()} File"
    desc = desc[0].upper() + desc[1:]
    _type_cache[ext] = desc
    return desc


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
DEFAULTS = {
    "width": 1180, "height": 760, "maximized": False, "view": "details", "folder_views": {},
    "sort_col": "name", "sort_desc": False, "show_hidden": False, "show_ext": True, "thumbnails": True,
    "details_pane": False, "nav_width": 230, "confirm_delete": False, "start": LOC_HOME,
    "quick_access": None, "show_recent": True, "compact": False,
}


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            with open(CONF_FILE) as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass
        if self.get("quick_access") is None:
            self["quick_access"] = [p for _, p, _ in USER_DIRS if os.path.isdir(p)]

    def save(self):
        try:
            os.makedirs(CONF_DIR, exist_ok=True)
            tmp = CONF_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self, f, indent=1)
            os.replace(tmp, CONF_FILE)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# Styles (Windows 11 dark)
# ----------------------------------------------------------------------------
CSS = """
* { font-family: "Segoe UI Variable Text", "Segoe UI", "Selawik", "Open Sans", "Noto Sans", sans-serif; }
window.fx, window.fx-dialog { background-color: #202020; color: #ffffff; font-size: 12px; }
.fx label { color: #ffffff; }
headerbar.fx-title { background: #202020; border: none; box-shadow: none; min-height: 40px; padding: 0; margin: 0; }
headerbar.fx-title:backdrop { background: #202020; }
button.wc { background: transparent; border: none; border-radius: 0; min-width: 46px; min-height: 32px; padding: 0; box-shadow: none; }
button.wc:hover { background-color: rgba(255,255,255,0.06); }
button.wc.close:hover { background-color: #C42B1C; }

/* tabs */
.tabstrip { margin-top: 6px; margin-left: 6px; }
button.tab { outline-color: transparent; background: transparent; border: none; border-radius: 8px 8px 0 0; min-height: 34px; padding: 0 6px 0 12px;
   box-shadow: none; margin-right: 1px; }
button.tab:hover { background-color: rgba(255,255,255,0.04); }
button.tab.active { background-color: #2C2C2C; }
button.tab label { font-size: 12px; }
button.tab-x { background: transparent; border: none; border-radius: 4px; min-width: 24px; min-height: 24px; padding: 0; box-shadow: none; }
button.tab-x:hover { background-color: rgba(255,255,255,0.08); }
button.tab-new { background: transparent; border: none; border-radius: 4px; min-width: 32px; min-height: 28px; padding: 0;
   box-shadow: none; margin-top: 4px; margin-left: 2px; }
button.tab-new:hover { background-color: rgba(255,255,255,0.06); }

/* address row + command bar */
.navrow { background-color: #2C2C2C; padding: 6px 10px 6px 6px; }
.cmdbar { background-color: #2C2C2C; padding: 4px 10px 6px 10px; border-bottom: 1px solid rgba(255,255,255,0.06); }
button.nb { background: transparent; border: none; border-radius: 4px; min-width: 34px; min-height: 32px; padding: 0; box-shadow: none; }
button.nb:hover { background-color: rgba(255,255,255,0.06); }
button.nb:disabled { opacity: 0.4; }
.addr { background-color: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.07); border-radius: 4px; min-height: 30px; }
.addr:hover { background-color: rgba(255,255,255,0.08); }
button.crumb { background: transparent; border: none; border-radius: 3px; min-height: 26px; padding: 0 6px; box-shadow: none; }
button.crumb:hover { background-color: rgba(255,255,255,0.08); }
button.crumb label { font-size: 12px; }
button.crumb-sep { background: transparent; border: none; border-radius: 3px; min-height: 26px; min-width: 18px; padding: 0 1px; box-shadow: none; }
button.crumb-sep:hover { background-color: rgba(255,255,255,0.08); }
entry.addr-entry, entry.fx-search, entry.field { background-color: rgba(255,255,255,0.06); color: #ffffff;
   border: 1px solid rgba(255,255,255,0.07); border-bottom: 1px solid rgba(255,255,255,0.54); border-radius: 4px;
   min-height: 30px; padding: 0 10px; box-shadow: none; font-size: 12px; caret-color: #ffffff; }
entry.addr-entry:focus, entry.fx-search:focus, entry.field:focus { background-color: #1c1c1c; border-bottom: 2px solid #4CC2FF; }
entry selection, entry selection:focus { background-color: #0078D4; color: #ffffff; }
entry.fx-search image { color: rgba(255,255,255,0.786); }
button.tb { background: transparent; border: none; border-radius: 4px; min-height: 32px; padding: 0 9px; box-shadow: none; color: #ffffff; }
button.tb:hover { background-color: rgba(255,255,255,0.06); }
button.tb:active { background-color: rgba(255,255,255,0.04); }
button.tb:disabled label { color: rgba(255,255,255,0.36); }
button.tb label { font-size: 12px; }
.vsep { background-color: rgba(255,255,255,0.08); min-width: 1px; margin: 6px 6px; }

/* panes */
.navpane, .navpane treeview.view { background-color: #191919; }
.navpane treeview.view { color: #ffffff; font-size: 12px; }
.navpane treeview.view:selected { background-color: rgba(255,255,255,0.08); color: #ffffff; }
.navpane treeview.view:hover { background-color: rgba(255,255,255,0.05); }
.navpane treeview.view.expander { color: rgba(255,255,255,0.6); -gtk-icon-source: -gtk-icontheme("pan-end-symbolic"); }
.navpane treeview.view.expander:checked { -gtk-icon-source: -gtk-icontheme("pan-down-symbolic"); }
.content { background-color: #191919; }
.navpane treeview.view separator { color: rgba(255,255,255,0.08); min-height: 1px; margin: 4px 8px; }
treeview.view.separator, .navpane treeview.view.separator { color: rgba(255,255,255,0.08); }
paned > separator { background-color: rgba(255,255,255,0.06); min-width: 1px; background-image: none; }
.detailspane { background-color: #191919; border-left: 1px solid rgba(255,255,255,0.06); padding: 20px 18px; }
.dp-name { font-size: 16px; font-weight: 600; }
.status { background-color: #191919; padding: 4px 12px 4px 12px; }
.status label { font-size: 12px; color: rgba(255,255,255,0.786); }
button.sv { background: transparent; border: none; border-radius: 4px; min-width: 28px; min-height: 24px; padding: 0; box-shadow: none; }
button.sv:hover { background-color: rgba(255,255,255,0.06); }
button.sv.on { background-color: rgba(255,255,255,0.09); }

/* file views */
treeview.view.files { background-color: #191919; color: #ffffff; font-size: 12px; }
treeview.view.files:selected { background-color: rgba(76,194,255,0.20); color: #ffffff; }
treeview.view.files:hover { background-color: rgba(255,255,255,0.05); }
treeview.view.files:selected:hover { background-color: rgba(76,194,255,0.26); }
treeview.view.files header button { background: #191919; border: none; border-right: 1px solid rgba(255,255,255,0.08);
   box-shadow: none; padding: 4px 8px 6px 8px; color: rgba(255,255,255,0.786); }
treeview.view.files header button:hover { background-color: rgba(255,255,255,0.05); }
treeview.view.files header button label { font-size: 12px; color: rgba(255,255,255,0.786); }
treeview.view.files.rubberband, iconview.view.rubberband, .rubberband { background-color: rgba(76,194,255,0.18); border: 1px solid #4CC2FF; }
iconview.view.files { background-color: #191919; color: #ffffff; font-size: 12px; outline-color: transparent; }
treeview.view.files { outline-color: transparent; }
iconview.view.files:selected { background-color: rgba(76,194,255,0.22); color: #ffffff; border-radius: 4px; }
iconview.view.files cell:hover { background-color: rgba(255,255,255,0.05); }
.files entry, treeview entry, iconview entry { background-color: #1c1c1c; color: #ffffff; border: 1px solid #4CC2FF; border-radius: 2px; box-shadow: none; padding: 0 4px; }
.sect-title { font-size: 14px; font-weight: 600; }
.home-head { padding: 14px 20px 6px 20px; }
button.tile { outline-color: transparent; background: transparent; border: none; border-radius: 4px; padding: 8px; box-shadow: none; }
button.tile:hover { background-color: rgba(255,255,255,0.05); }
button.tile:focus { background-color: rgba(255,255,255,0.07); }
.tile-name { font-size: 12px; }
.tile-sub { font-size: 11px; color: rgba(255,255,255,0.6); }
flowbox, flowboxchild { background: transparent; padding: 0; }
flowboxchild:selected { background: transparent; }
.empty-msg { color: rgba(255,255,255,0.6); font-size: 12px; }
.sec { color: rgba(255,255,255,0.786); }
.ter { color: rgba(255,255,255,0.545); }
scrollbar { background: transparent; border: none; }
scrollbar slider { background-color: rgba(255,255,255,0.45); border-radius: 6px; min-width: 3px; min-height: 3px; border: none; }
scrollbar:hover slider { min-width: 6px; min-height: 6px; background-color: rgba(255,255,255,0.55); }
scrollbar trough { background: transparent; border: none; }
progressbar trough { background-color: rgba(255,255,255,0.12); border: none; border-radius: 2px; min-height: 4px; }
progressbar progress { background-color: #4CC2FF; border: none; border-radius: 2px; min-height: 4px; }
levelbar trough { background-color: rgba(255,255,255,0.12); border: none; border-radius: 2px; min-height: 6px; padding: 0; }
levelbar block.filled { background-color: #4CC2FF; border: none; border-radius: 2px; min-height: 6px; }
levelbar block.empty { background-color: transparent; border: none; }
levelbar.full block.filled { background-color: #FF99A4; }

/* menus and dialogs */
menu, .menu, .context-menu { background-color: #2C2C2C; color: #ffffff; border: 1px solid rgba(0,0,0,0.45); border-radius: 8px; padding: 4px; }
menu menuitem { padding: 6px 12px; border-radius: 4px; color: #ffffff; min-height: 18px; }
menu menuitem:hover { background-color: rgba(255,255,255,0.06); }
menu menuitem:disabled label { color: rgba(255,255,255,0.36); }
menu menuitem label { color: #ffffff; font-size: 12px; }
menu menuitem accelerator { color: rgba(255,255,255,0.55); font-size: 11px; }
menu menuitem arrow { color: rgba(255,255,255,0.786); }
menu separator { background-color: rgba(255,255,255,0.08); margin: 4px 0; min-height: 1px; }
menu menuitem check, menu menuitem radio { color: #ffffff; background: transparent; border: none; }
popover.fx-pop, popover.fx-pop.background { background-color: #2C2C2C; border: 1px solid rgba(0,0,0,0.45); border-radius: 8px; padding: 4px; }
window.fx button, window.fx-dialog button { background-image: none; text-shadow: none; -gtk-icon-shadow: none; }
button.primary { background-color: #4CC2FF; color: #000000; border: none; border-radius: 4px; min-height: 30px; padding: 0 16px; box-shadow: none; }
button.primary label { color: #000000; }
button.primary:hover { background-color: #48B1E8; }
button.secondary { background-color: rgba(255,255,255,0.06); color: #ffffff; border: 1px solid rgba(255,255,255,0.09); border-radius: 4px; min-height: 30px; padding: 0 16px; box-shadow: none; }
button.secondary:hover { background-color: rgba(255,255,255,0.09); }
button.choice { background-color: transparent; border: 1px solid transparent; border-radius: 4px; padding: 8px 10px; box-shadow: none; }
button.choice label { color: #ffffff; font-size: 13px; }
button.choice label.ter { color: rgba(255,255,255,0.6); font-size: 12px; }
button.choice:hover { background-color: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.08); }
.dlg-body { background-color: #2B2B2B; padding: 24px; }
.fx-dialog label { color: #ffffff; }
.fx-dialog label.sec { color: rgba(255,255,255,0.786); }
.fx-dialog label.ter { color: rgba(255,255,255,0.545); }
.dlg-foot { background-color: #202020; padding: 22px 24px; border-top: 1px solid rgba(0,0,0,0.3); }
.dlg-title { font-size: 20px; font-weight: 600; }
.prog-title { font-size: 14px; }
.prop-big { font-size: 14px; }
notebook.fx-tabs, notebook.fx-tabs > header, notebook.fx-tabs > stack { border: none; box-shadow: none; }
notebook.fx-tabs > header tab { outline-color: transparent; }
notebook.fx-tabs header { background: #2B2B2B; border: none; }
notebook.fx-tabs header tab { padding: 6px 14px; border: none; color: rgba(255,255,255,0.786); }
notebook.fx-tabs header tab:checked { color: #ffffff; box-shadow: inset 0 -2px #4CC2FF; }
notebook.fx-tabs stack { background: #2B2B2B; }
checkbutton check { background-color: rgba(0,0,0,0.1); border: 1px solid rgba(255,255,255,0.6); border-radius: 4px; min-width: 18px; min-height: 18px; -gtk-icon-source: none; color: #000000; }
checkbutton check:checked { background-color: #4CC2FF; border-color: #4CC2FF; -gtk-icon-source: -gtk-icontheme("object-select-symbolic"); }
checkbutton label { color: #ffffff; font-size: 12px; margin-left: 6px; }
radiobutton radio { background-color: rgba(0,0,0,0.1); border: 1px solid rgba(255,255,255,0.6); border-radius: 50%; min-width: 18px; min-height: 18px; -gtk-icon-source: none; }
radiobutton radio:checked { background-color: #000000; border: 5px solid #4CC2FF; }
radiobutton label { color: #ffffff; font-size: 12px; margin-left: 6px; }
combobox button, combobox box { background-color: rgba(255,255,255,0.06); color: #ffffff; border: 1px solid rgba(255,255,255,0.07); border-radius: 4px; box-shadow: none; min-height: 30px; }
combobox button cellview, combobox button label, combobox arrow { color: #ffffff; }
tooltip, tooltip.background { background-color: #2C2C2C; border: 1px solid rgba(0,0,0,0.4); border-radius: 4px; }
tooltip label { color: #ffffff; font-size: 12px; }
spinner { color: #4CC2FF; }
"""


# ----------------------------------------------------------------------------
# Icons drawn with cairo (16 unit grid)
# ----------------------------------------------------------------------------
def _rr(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _line(cr, *pts):
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.stroke()


def draw_glyph(cr, name, s, col):
    """Monochrome line icons for buttons."""
    cr.save()
    cr.scale(s / 16.0, s / 16.0)
    cr.set_source_rgba(*col)
    cr.set_line_width(1.15)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    pi = math.pi
    L = lambda *p: _line(cr, *p)  # noqa: E731
    if name == "back":
        L((13.5, 8), (2.5, 8))
        L((7, 3.5), (2.5, 8), (7, 12.5))
    elif name == "forward":
        L((2.5, 8), (13.5, 8))
        L((9, 3.5), (13.5, 8), (9, 12.5))
    elif name == "up":
        L((8, 13.5), (8, 2.5))
        L((3.5, 7), (8, 2.5), (12.5, 7))
    elif name == "refresh":
        cr.arc(8, 8, 5.5, -pi * 0.35, pi * 1.55)
        cr.stroke()
        L((12.9, 1.8), (12.6, 4.9), (9.6, 4.6))
    elif name == "plus":
        L((8, 2.5), (8, 13.5))
        L((2.5, 8), (13.5, 8))
    elif name == "new":
        cr.set_source_rgba(*hexc(C_ACCENT))
        cr.arc(8, 8, 6.5, 0, 2 * pi)
        cr.stroke()
        L((8, 4.8), (8, 11.2))
        L((4.8, 8), (11.2, 8))
    elif name == "cut":
        cr.arc(4.5, 12, 2, 0, 2 * pi)
        cr.stroke()
        cr.arc(11.5, 12, 2, 0, 2 * pi)
        cr.stroke()
        L((5.8, 10.5), (11, 1.8))
        L((10.2, 10.5), (5, 1.8))
    elif name == "copy":
        _rr(cr, 5, 1.5, 9, 10.5, 1.5)
        cr.stroke()
        cr.move_to(3, 4.5)
        cr.line_to(3, 13)
        cr.arc_negative(4.5, 13, 1.5, pi, pi / 2)
        cr.line_to(11, 14.5)
        cr.stroke()
    elif name == "paste":
        _rr(cr, 2.5, 3, 11, 11.5, 1.5)
        cr.stroke()
        _rr(cr, 5.5, 1.5, 5, 3, 1)
        cr.stroke()
        L((5.5, 8), (10.5, 8))
        L((5.5, 11), (9, 11))
    elif name == "rename":
        _rr(cr, 1.5, 4.5, 13, 7, 1.3)
        cr.stroke()
        L((4, 8), (8, 8))
        L((10.5, 2.5), (10.5, 13.5))
        L((9, 2.5), (12, 2.5))
        L((9, 13.5), (12, 13.5))
    elif name == "delete" or name == "trash":
        L((2.5, 4), (13.5, 4))
        L((6, 4), (6.5, 2), (9.5, 2), (10, 4))
        cr.move_to(4, 4)
        cr.line_to(4.8, 14)
        cr.line_to(11.2, 14)
        cr.line_to(12, 4)
        cr.stroke()
        L((6.8, 6.8), (7, 11.5))
        L((9.2, 6.8), (9, 11.5))
    elif name == "sort":
        L((4.5, 2.5), (4.5, 13.5))
        L((2, 5), (4.5, 2.5), (7, 5))
        L((11.5, 2.5), (11.5, 13.5))
        L((9, 11), (11.5, 13.5), (14, 11))
    elif name == "view":
        for y in (3, 8, 13):
            L((2, y), (14, y))
    elif name == "more":
        for x in (3, 8, 13):
            cr.arc(x, 8, 1.1, 0, 2 * pi)
            cr.fill()
    elif name == "chevdown":
        L((4, 6), (8, 10), (12, 6))
    elif name == "chevright":
        L((6, 4), (10, 8), (6, 12))
    elif name == "close":
        L((3.5, 3.5), (12.5, 12.5))
        L((12.5, 3.5), (3.5, 12.5))
    elif name == "min":
        L((3, 8), (13, 8))
    elif name == "max":
        _rr(cr, 3, 3, 10, 10, 1.2)
        cr.stroke()
    elif name == "restore":
        _rr(cr, 3, 5, 8, 8, 1.2)
        cr.stroke()
        L((5, 3), (12, 3), (13, 4), (13, 11))
    elif name == "search":
        cr.arc(6.6, 6.6, 4.6, 0, 2 * pi)
        cr.stroke()
        L((10, 10), (14, 14))
    elif name == "pane":
        _rr(cr, 1.5, 2.5, 13, 11, 1.5)
        cr.stroke()
        L((10, 2.5), (10, 13.5))
    elif name == "terminal":
        _rr(cr, 1.5, 2.5, 13, 11, 1.5)
        cr.stroke()
        L((4, 6), (6.5, 8), (4, 10))
        L((8, 10.5), (11.5, 10.5))
    elif name == "properties":
        _rr(cr, 2.5, 1.5, 11, 13, 1.5)
        cr.stroke()
        for y in (5, 8, 11):
            cr.arc(5.2, y, 0.8, 0, 2 * pi)
            cr.fill()
            L((7.2, y), (11, y))
    elif name == "pin":
        cr.move_to(6, 2)
        cr.line_to(10, 2)
        cr.line_to(9.5, 6.5)
        cr.line_to(12, 9)
        cr.line_to(4, 9)
        cr.line_to(6.5, 6.5)
        cr.close_path()
        cr.stroke()
        L((8, 9), (8, 14))
    elif name == "pin-fill":
        cr.set_line_width(1.0)
        cr.translate(8, 8)
        cr.rotate(pi / 4)
        cr.translate(-8, -8)
        cr.move_to(6, 2)
        cr.line_to(10, 2)
        cr.line_to(9.5, 6.5)
        cr.line_to(12, 9)
        cr.line_to(4, 9)
        cr.line_to(6.5, 6.5)
        cr.close_path()
        cr.stroke()
        L((8, 9), (8, 14))
    elif name == "folder":
        cr.move_to(1.5, 4)
        cr.line_to(6, 4)
        cr.line_to(7.5, 5.5)
        cr.line_to(14.5, 5.5)
        cr.line_to(14.5, 13)
        cr.line_to(1.5, 13)
        cr.close_path()
        cr.stroke()
    elif name == "file":
        cr.move_to(3.5, 1.5)
        cr.line_to(9.5, 1.5)
        cr.line_to(12.5, 4.5)
        cr.line_to(12.5, 14.5)
        cr.line_to(3.5, 14.5)
        cr.close_path()
        cr.stroke()
        L((9.5, 1.5), (9.5, 4.5), (12.5, 4.5))
    elif name == "zip":
        _rr(cr, 2.5, 1.5, 11, 13, 1.5)
        cr.stroke()
        for y in (3, 5, 7):
            L((7, y), (9, y))
        _rr(cr, 6.5, 9, 3, 3.5, 0.6)
        cr.stroke()
    elif name == "extract":
        _rr(cr, 2.5, 6.5, 11, 8, 1.5)
        cr.stroke()
        L((8, 1.5), (8, 10))
        L((5.5, 7.5), (8, 10), (10.5, 7.5))
    elif name == "restoreitem":
        cr.arc(8.5, 8.5, 5, -pi * 0.9, pi * 0.6)
        cr.stroke()
        L((2, 3.5), (3.6, 6.6), (6.6, 5.4))
    elif name == "open":
        _rr(cr, 1.5, 1.5, 13, 13, 2)
        cr.stroke()
        L((7, 9), (12.5, 3.5))
        L((8.5, 3.5), (12.5, 3.5), (12.5, 7.5))
    elif name == "newtab":
        _rr(cr, 1.5, 3.5, 13, 10, 1.5)
        cr.stroke()
        L((1.5, 6), (8, 6))
        L((8, 3.5), (8, 6))
    elif name == "check":
        L((3, 8.5), (6.5, 12), (13, 4.5))
    elif name == "eject":
        cr.move_to(8, 3)
        cr.line_to(13, 9)
        cr.line_to(3, 9)
        cr.close_path()
        cr.stroke()
        L((3, 12.5), (13, 12.5))
    elif name == "details-view":
        for y in (3.5, 8, 12.5):
            _rr(cr, 1.5, y - 1.2, 2.4, 2.4, 0.5)
            cr.stroke()
            L((6, y), (14.5, y))
    elif name == "icons-view":
        for x in (2, 9):
            for y in (2, 9):
                _rr(cr, x, y, 5, 5, 1.2)
                cr.stroke()
    elif name == "selectall":
        _rr(cr, 2, 2, 12, 12, 1.5)
        cr.stroke()
        L((5, 8), (7.2, 10.2), (11, 6))
    elif name == "info":
        cr.arc(8, 8, 6, 0, 2 * pi)
        cr.stroke()
        L((8, 7), (8, 11.5))
        cr.arc(8, 4.8, 0.9, 0, 2 * pi)
        cr.fill()
    elif name == "share":
        cr.arc(4, 8, 1.8, 0, 2 * pi)
        cr.stroke()
        cr.arc(12, 3.8, 1.8, 0, 2 * pi)
        cr.stroke()
        cr.arc(12, 12.2, 1.8, 0, 2 * pi)
        cr.stroke()
        L((5.6, 7.1), (10.4, 4.7))
        L((5.6, 8.9), (10.4, 11.3))
    elif name == "network":
        _rr(cr, 5, 1.5, 6, 4.5, 1)
        cr.stroke()
        L((8, 6), (8, 9))
        L((3, 9), (13, 9))
        L((3, 9), (3, 11))
        L((13, 9), (13, 11))
        _rr(cr, 1, 11, 4, 3.5, 0.8)
        cr.stroke()
        _rr(cr, 11, 11, 4, 3.5, 0.8)
        cr.stroke()
    elif name == "link":
        cr.arc(5.5, 10.5, 3, pi * 0.25, pi * 1.75)
        cr.stroke()
        cr.arc(10.5, 5.5, 3, pi * 1.25, pi * 0.75)
        cr.stroke()
        L((6.5, 9.5), (9.5, 6.5))
    cr.restore()


# ---- colored file / place icons ---------------------------------------------
def _folder(cr, open_=False):
    g = cairo.LinearGradient(0, 2, 0, 14)
    g.add_color_stop_rgb(0, *hexc("#E6A82C")[:3])
    g.add_color_stop_rgb(1, *hexc("#C98A12")[:3])
    cr.set_source(g)
    cr.move_to(1.0, 3.2)
    cr.arc(2.0, 3.2, 1.0, math.pi, 1.5 * math.pi)
    cr.line_to(5.6, 2.2)
    cr.line_to(7.0, 3.6)
    cr.line_to(14.0, 3.6)
    cr.arc(14.0, 4.6, 1.0, 1.5 * math.pi, 2 * math.pi)
    cr.line_to(15.0, 12.6)
    cr.arc(14.0, 12.6, 1.0, 0, math.pi / 2)
    cr.line_to(2.0, 13.6)
    cr.arc(2.0, 12.6, 1.0, math.pi / 2, math.pi)
    cr.close_path()
    cr.fill()
    g = cairo.LinearGradient(0, 5, 0, 14)
    g.add_color_stop_rgb(0, *hexc("#FFD867")[:3])
    g.add_color_stop_rgb(1, *hexc("#F9BD2E")[:3])
    cr.set_source(g)
    _rr(cr, 1.0, 5.0, 14.0, 8.6, 1.0)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 0.45)
    cr.set_line_width(0.35)
    cr.move_to(1.6, 5.35)
    cr.line_to(14.4, 5.35)
    cr.stroke()


def _page(cr, col="#F4F4F4", fold="#C9C9C9"):
    cr.set_source_rgba(*hexc(col))
    cr.move_to(2.5, 1.5)
    cr.line_to(9.8, 1.5)
    cr.line_to(13.5, 5.2)
    cr.line_to(13.5, 14.0)
    cr.arc(12.8, 14.0, 0.7, 0, math.pi / 2)
    cr.line_to(3.2, 14.7)
    cr.arc(3.2, 14.0, 0.7, math.pi / 2, math.pi)
    cr.line_to(2.5, 2.2)
    cr.arc(3.2, 2.2, 0.7, math.pi, 1.5 * math.pi)
    cr.close_path()
    cr.fill()
    cr.set_source_rgba(0, 0, 0, 0.18)
    cr.set_line_width(0.3)
    cr.move_to(2.5, 1.5)
    cr.line_to(9.8, 1.5)
    cr.line_to(13.5, 5.2)
    cr.line_to(13.5, 14.7)
    cr.line_to(2.5, 14.7)
    cr.close_path()
    cr.stroke()
    cr.set_source_rgba(*hexc(fold))
    cr.move_to(9.8, 1.5)
    cr.line_to(9.8, 5.2)
    cr.line_to(13.5, 5.2)
    cr.close_path()
    cr.fill()


def _badge_glyph(cr, kind, cx, cy, sc):
    """Small colored emblem used on special folders."""
    cr.save()
    cr.translate(cx, cy)
    cr.scale(sc, sc)
    pi = math.pi
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind == "downloads":
        cr.set_source_rgba(*hexc("#2E9E4F"))
        cr.set_line_width(1.4)
        _line(cr, (0, -3), (0, 2.2))
        _line(cr, (-2.4, 0), (0, 2.4), (2.4, 0))
        _line(cr, (-3, 3.6), (3, 3.6))
    elif kind == "documents":
        cr.set_source_rgba(*hexc("#2F7ED8"))
        _rr(cr, -2.6, -3.4, 5.2, 6.8, 0.6)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(0.6)
        for y in (-1.6, 0, 1.6):
            _line(cr, (-1.5, y), (1.5, y))
    elif kind == "pictures":
        cr.set_source_rgba(*hexc("#2F8FE0"))
        _rr(cr, -3.4, -2.6, 6.8, 5.2, 0.7)
        cr.fill()
        cr.set_source_rgba(*hexc("#B8E986"))
        cr.move_to(-3.4, 2.6)
        cr.line_to(-1, -0.4)
        cr.line_to(0.8, 1.4)
        cr.line_to(1.8, 0.4)
        cr.line_to(3.4, 2.6)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(1, 0.9, 0.4, 1)
        cr.arc(1.6, -1.2, 0.7, 0, 2 * pi)
        cr.fill()
    elif kind == "music":
        cr.set_source_rgba(*hexc("#E8642C"))
        cr.arc(-1.6, 2.2, 1.3, 0, 2 * pi)
        cr.fill()
        cr.arc(2.0, 1.4, 1.3, 0, 2 * pi)
        cr.fill()
        cr.set_line_width(0.8)
        _line(cr, (-0.4, 2.2), (-0.4, -2.8), (3.2, -3.6), (3.2, 1.4))
    elif kind == "videos":
        cr.set_source_rgba(*hexc("#8E5BD9"))
        _rr(cr, -3.4, -2.6, 6.8, 5.2, 0.7)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.move_to(-0.9, -1.4)
        cr.line_to(1.6, 0)
        cr.line_to(-0.9, 1.4)
        cr.close_path()
        cr.fill()
    elif kind == "homefolder":
        cr.set_source_rgba(*hexc("#2F7ED8"))
        cr.arc(0, -1.2, 1.5, 0, 2 * pi)
        cr.fill()
        cr.arc(0, 3.4, 2.9, pi, 2 * pi)
        cr.fill()
    cr.restore()


def draw_place(cr, kind, s):
    """Colored icons: folders, files by kind, drives and places."""
    cr.save()
    cr.scale(s / 16.0, s / 16.0)
    pi = math.pi
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind in ("folder", "downloads", "documents", "pictures", "music", "videos", "homefolder"):
        _folder(cr)
        if kind != "folder":
            _badge_glyph(cr, kind, 8, 9.4, 0.72)
    elif kind == "desktop":
        g = cairo.LinearGradient(0, 2, 0, 12)
        g.add_color_stop_rgb(0, *hexc("#3AA0F2")[:3])
        g.add_color_stop_rgb(1, *hexc("#1F6FD1")[:3])
        cr.set_source_rgba(*hexc("#3A3A3A"))
        _rr(cr, 1, 2, 14, 9.6, 1.2)
        cr.fill()
        cr.set_source(g)
        _rr(cr, 1.8, 2.8, 12.4, 8.0, 0.6)
        cr.fill()
        cr.set_source_rgba(*hexc("#9A9A9A"))
        cr.rectangle(6.6, 11.6, 2.8, 1.6)
        cr.fill()
        _rr(cr, 4.5, 13, 7, 1.3, 0.6)
        cr.fill()
    elif kind == "pc":
        cr.set_source_rgba(*hexc("#4A4A4A"))
        _rr(cr, 1, 2, 14, 9.6, 1.2)
        cr.fill()
        g = cairo.LinearGradient(0, 3, 16, 11)
        g.add_color_stop_rgb(0, *hexc("#56B4F5")[:3])
        g.add_color_stop_rgb(1, *hexc("#1E64C8")[:3])
        cr.set_source(g)
        _rr(cr, 1.8, 2.8, 12.4, 8.0, 0.6)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.move_to(1.8, 10.8)
        cr.curve_to(6, 6, 10, 5, 14.2, 4.5)
        cr.line_to(14.2, 10.8)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(*hexc("#A0A0A0"))
        cr.rectangle(6.6, 11.6, 2.8, 1.6)
        cr.fill()
        _rr(cr, 4.5, 13, 7, 1.3, 0.6)
        cr.fill()
    elif kind in ("net", "netpc"):
        def mon(x, y, w, h, screen):
            cr.set_source_rgba(*hexc("#4A4A4A"))
            _rr(cr, x, y, w, h, 0.8)
            cr.fill()
            cr.set_source_rgba(*hexc(screen))
            _rr(cr, x + 0.6, y + 0.6, w - 1.2, h - 1.2, 0.4)
            cr.fill()
        if kind == "netpc":
            mon(1, 2, 14, 9.6, "#56B4F5")
            cr.set_source_rgba(*hexc("#A0A0A0"))
            cr.rectangle(6.6, 11.6, 2.8, 1.6)
            cr.fill()
            _rr(cr, 4.5, 13, 7, 1.3, 0.6)
            cr.fill()
        else:
            mon(1, 1.5, 8.5, 6.5, "#56B4F5")
            mon(6.5, 6, 8.5, 6.5, "#3A8DDE")
            cr.set_source_rgba(*hexc("#6CCB5F"))
            cr.set_line_width(1.0)
            _line(cr, (3.5, 8), (3.5, 14.5), (10.5, 14.5), (10.5, 12.5))
    elif kind == "netshare":
        _folder(cr)
        cr.set_source_rgba(*hexc("#2F7ED8"))
        cr.set_line_width(0.9)
        _line(cr, (8, 8), (8, 12))
        _line(cr, (5, 12), (11, 12))
        for x in (5, 11):
            cr.arc(x, 12, 0.9, 0, 2 * pi)
            cr.fill()
        _rr(cr, 6.4, 6.8, 3.2, 2.2, 0.4)
        cr.fill()
    elif kind == "home":
        cr.set_source_rgba(*hexc("#F2C14E"))
        cr.move_to(8, 1.6)
        cr.line_to(15, 7.6)
        cr.line_to(13.2, 7.6)
        cr.line_to(13.2, 14.4)
        cr.line_to(2.8, 14.4)
        cr.line_to(2.8, 7.6)
        cr.line_to(1, 7.6)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(*hexc("#D98C1E"))
        cr.move_to(8, 1.6)
        cr.line_to(15, 7.6)
        cr.line_to(13.2, 7.6)
        cr.line_to(8, 3.2)
        cr.line_to(2.8, 7.6)
        cr.line_to(1, 7.6)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(*hexc("#2F7ED8"))
        _rr(cr, 6.4, 9.2, 3.2, 5.2, 0.5)
        cr.fill()
    elif kind in ("recycle", "recycle-full"):
        g = cairo.LinearGradient(0, 3, 0, 15)
        g.add_color_stop_rgba(0, 0.75, 0.85, 0.95, 0.9)
        g.add_color_stop_rgba(1, 0.45, 0.6, 0.78, 0.9)
        if kind == "recycle-full":
            cr.set_source_rgba(*hexc("#F4F4F4"))
            _rr(cr, 4.5, 1.2, 5, 5, 0.4)
            cr.fill()
            cr.set_source_rgba(*hexc("#F9C64A"))
            _rr(cr, 7, 0.8, 5, 4.5, 0.4)
            cr.fill()
        cr.set_source(g)
        cr.move_to(2.5, 4)
        cr.line_to(13.5, 4)
        cr.line_to(12.3, 14.2)
        cr.curve_to(12.2, 14.8, 11.8, 15, 11.3, 15)
        cr.line_to(4.7, 15)
        cr.curve_to(4.2, 15, 3.8, 14.8, 3.7, 14.2)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.set_line_width(0.5)
        for x in (5.6, 8, 10.4):
            _line(cr, (x, 6), (x + (x - 8) * 0.05, 13.2))
        cr.set_source_rgba(*hexc("#8FA9C4"))
        _rr(cr, 1.8, 3, 12.4, 1.6, 0.5)
        cr.fill()
    elif kind in ("drive", "drive-sys", "sdcard", "usb", "nvme", "optical", "network"):
        if kind == "sdcard":
            cr.set_source_rgba(*hexc("#3D5A80"))
            cr.move_to(4, 1.5)
            cr.line_to(12, 1.5)
            cr.line_to(12, 14.5)
            cr.line_to(3, 14.5)
            cr.line_to(3, 4.5)
            cr.close_path()
            cr.fill()
            cr.set_source_rgba(*hexc("#E8C34A"))
            for i in range(4):
                cr.rectangle(5 + i * 1.6, 2.5, 0.9, 2.4)
                cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.85)
            _rr(cr, 4.5, 8, 6, 4, 0.5)
            cr.fill()
        elif kind == "usb":
            cr.set_source_rgba(*hexc("#B8B8B8"))
            cr.rectangle(5.5, 1, 5, 4)
            cr.fill()
            cr.set_source_rgba(*hexc("#555555"))
            cr.rectangle(6.5, 2, 1, 1)
            cr.rectangle(8.5, 2, 1, 1)
            cr.fill()
            cr.set_source_rgba(*hexc("#2F7ED8"))
            _rr(cr, 4, 5, 8, 10, 1.4)
            cr.fill()
        elif kind == "nvme":
            cr.set_source_rgba(*hexc("#1E6B3A"))
            _rr(cr, 1, 5, 14, 6, 0.6)
            cr.fill()
            cr.set_source_rgba(*hexc("#222222"))
            cr.rectangle(3, 6.2, 4, 3.6)
            cr.rectangle(8, 6.2, 3, 3.6)
            cr.fill()
            cr.set_source_rgba(*hexc("#E8C34A"))
            for i in range(5):
                cr.rectangle(12 + i * 0.55, 6, 0.3, 4)
            cr.fill()
        else:
            g = cairo.LinearGradient(0, 4, 0, 13)
            g.add_color_stop_rgb(0, *hexc("#D8D8D8")[:3])
            g.add_color_stop_rgb(1, *hexc("#9C9C9C")[:3])
            cr.set_source(g)
            _rr(cr, 1, 5, 14, 7.5, 1.4)
            cr.fill()
            cr.set_source_rgba(*hexc("#5E5E5E"))
            _rr(cr, 1, 9.5, 14, 3, 1.2)
            cr.fill()
            cr.set_source_rgba(*hexc("#6CCB5F"))
            cr.arc(12.6, 11, 0.7, 0, 2 * pi)
            cr.fill()
            if kind == "drive-sys":
                cr.set_source_rgba(*hexc("#C51A4A"))
                for (x, y) in ((4.2, 6.6), (5.6, 6.6), (4.9, 7.8)):
                    cr.arc(x, y, 0.7, 0, 2 * pi)
                    cr.fill()
            if kind == "network":
                cr.set_source_rgba(*hexc("#2F7ED8"))
                cr.set_line_width(0.9)
                _line(cr, (8, 12.5), (8, 15))
                _line(cr, (4, 15), (12, 15))
    else:
        # files
        if kind == "script":
            cr.set_source_rgba(*hexc("#2D2D2D"))
            _rr(cr, 1.5, 2.5, 13, 11, 1.3)
            cr.fill()
            cr.set_source_rgba(*hexc("#555555"))
            _rr(cr, 1.5, 2.5, 13, 2.2, 1.0)
            cr.fill()
            cr.set_source_rgba(*hexc("#6CCB5F"))
            cr.set_line_width(1.0)
            _line(cr, (4, 7), (6, 8.6), (4, 10.2))
            _line(cr, (7.4, 10.4), (11, 10.4))
            cr.restore()
            return
        if kind == "app":
            g = cairo.LinearGradient(0, 1, 0, 15)
            g.add_color_stop_rgb(0, *hexc("#56B4F5")[:3])
            g.add_color_stop_rgb(1, *hexc("#1E64C8")[:3])
            cr.set_source(g)
            _rr(cr, 1.5, 1.5, 13, 13, 3)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(8, 4)
            cr.line_to(12, 8)
            cr.line_to(8, 12)
            cr.line_to(4, 8)
            cr.close_path()
            cr.fill()
            cr.restore()
            return
        _page(cr)
        cr.set_line_width(0.8)
        if kind == "image":
            cr.set_source_rgba(*hexc("#3D9BEA"))
            _rr(cr, 4, 6.2, 8, 6.4, 0.6)
            cr.fill()
            cr.set_source_rgba(*hexc("#6CCB5F"))
            cr.move_to(4, 12.6)
            cr.line_to(6.6, 9)
            cr.line_to(8.6, 11)
            cr.line_to(9.8, 9.8)
            cr.line_to(12, 12.6)
            cr.close_path()
            cr.fill()
            cr.set_source_rgba(1, 0.88, 0.35, 1)
            cr.arc(10, 8, 0.8, 0, 2 * pi)
            cr.fill()
        elif kind == "video":
            cr.set_source_rgba(*hexc("#8E5BD9"))
            _rr(cr, 4, 6.4, 8, 6, 0.8)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(7.1, 7.9)
            cr.line_to(9.8, 9.4)
            cr.line_to(7.1, 10.9)
            cr.close_path()
            cr.fill()
        elif kind == "audio":
            cr.set_source_rgba(*hexc("#E8642C"))
            cr.arc(6.2, 11.6, 1.3, 0, 2 * pi)
            cr.fill()
            cr.arc(10.2, 10.8, 1.3, 0, 2 * pi)
            cr.fill()
            cr.set_line_width(0.9)
            _line(cr, (7.4, 11.6), (7.4, 6.8), (11.4, 6), (11.4, 10.8))
        elif kind == "archive":
            cr.set_source_rgba(*hexc("#F2B53C"))
            cr.rectangle(2.5, 8.4, 11, 2.4)
            cr.fill()
            cr.set_source_rgba(*hexc("#7A7A7A"))
            for i in range(6):
                cr.rectangle(7 + (i % 2) * 1, 1.8 + i * 1.1, 1, 1)
            cr.fill()
            _rr(cr, 6.6, 8.0, 2.8, 3.2, 0.4)
            cr.fill()
        elif kind == "pdf":
            cr.set_source_rgba(*hexc("#D93A3A"))
            _rr(cr, 2.0, 8.6, 9.5, 4.6, 0.8)
            cr.fill()
            if s >= 32:
                cr.set_source_rgba(1, 1, 1, 1)
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(3.2)
                cr.move_to(3.1, 12.1)
                cr.show_text("PDF")
        elif kind == "code":
            cr.set_source_rgba(*hexc("#2F7ED8"))
            cr.set_line_width(1.0)
            _line(cr, (6, 7.2), (4.2, 9.4), (6, 11.6))
            _line(cr, (10, 7.2), (11.8, 9.4), (10, 11.6))
            _line(cr, (8.8, 6.8), (7.2, 12))
        elif kind == "text":
            cr.set_source_rgba(*hexc("#8A8A8A"))
            for y in (6.5, 8.5, 10.5, 12.5):
                _line(cr, (4.5, y), (11.5 if y < 12 else 9, y))
        elif kind == "doc":
            cr.set_source_rgba(*hexc("#2F7ED8"))
            for y in (6.5, 8.5, 10.5, 12.5):
                _line(cr, (4.5, y), (11.5 if y < 12 else 9, y))
        elif kind == "sheet":
            cr.set_source_rgba(*hexc("#21A366"))
            cr.set_line_width(0.6)
            for y in (6.5, 8.5, 10.5, 12.5):
                _line(cr, (4, y), (12, y))
            for x in (4, 7, 9.5, 12):
                _line(cr, (x, 6.5), (x, 12.5))
        elif kind == "slides":
            cr.set_source_rgba(*hexc("#E46C2C"))
            _rr(cr, 4, 6.6, 8, 5.6, 0.6)
            cr.fill()
        elif kind == "model":
            cr.set_line_width(0.6)
            top = [(8, 5.6), (12, 7.6), (8, 9.6), (4, 7.6)]
            cr.set_source_rgba(*hexc("#4FD1C5"))
            cr.move_to(*top[0])
            for p in top[1:]:
                cr.line_to(*p)
            cr.close_path()
            cr.fill()
            cr.set_source_rgba(*hexc("#2A9D8F"))
            cr.move_to(4, 7.6)
            cr.line_to(8, 9.6)
            cr.line_to(8, 13.8)
            cr.line_to(4, 11.8)
            cr.close_path()
            cr.fill()
            cr.set_source_rgba(*hexc("#1F7A6F"))
            cr.move_to(12, 7.6)
            cr.line_to(8, 9.6)
            cr.line_to(8, 13.8)
            cr.line_to(12, 11.8)
            cr.close_path()
            cr.fill()
        elif kind == "font":
            cr.set_source_rgba(*hexc("#333333"))
            cr.select_font_face("Serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(6)
            cr.move_to(3.8, 12.4)
            cr.show_text("Aa")
    cr.restore()


_pix_cache = {}


def place_pixbuf(kind, size):
    key = ("p", kind, size)
    pb = _pix_cache.get(key)
    if pb is None:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surf)
        draw_place(cr, kind, size)
        pb = Gdk.pixbuf_get_from_surface(surf, 0, 0, size, size)
        _pix_cache[key] = pb
    return pb


def glyph_pixbuf(name, size=16, col=(1, 1, 1, 0.786)):
    key = ("g", name, size, col)
    pb = _pix_cache.get(key)
    if pb is None:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surf)
        draw_glyph(cr, name, size, col)
        pb = Gdk.pixbuf_get_from_surface(surf, 0, 0, size, size)
        _pix_cache[key] = pb
    return pb


def theme_pixbuf(icon, size):
    """Load a themed icon (Gio.Icon or name) at size, or None."""
    if icon is None:
        return None
    key = ("t", str(icon), size)
    if key in _pix_cache:
        return _pix_cache[key]
    pb = None
    theme = Gtk.IconTheme.get_default()
    try:
        if isinstance(icon, str):
            if os.path.isabs(icon) and os.path.exists(icon):
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(icon, size, size)
            else:
                pb = theme.load_icon(icon, size, Gtk.IconLookupFlags.FORCE_SIZE)
        else:
            info = theme.lookup_by_gicon(icon, size, Gtk.IconLookupFlags.FORCE_SIZE)
            if info:
                pb = info.load_icon()
    except Exception:
        pb = None
    _pix_cache[key] = pb
    return pb


class Icon(Gtk.DrawingArea):
    """Glyph (monochrome) or place (colored) icon widget."""

    def __init__(self, name, size=16, color=None, place=False):
        super().__init__()
        self.name = name
        self.size = size
        self.color = color
        self.place = place
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def set_icon(self, name, place=None):
        self.name = name
        if place is not None:
            self.place = place
        self.queue_draw()

    def _draw(self, w, cr):
        if self.place:
            draw_place(cr, self.name, self.size)
            return
        col = self.color or (1, 1, 1, 1)
        p = self.get_parent()
        if not self.is_sensitive() or (p and not p.is_sensitive()):
            col = (1, 1, 1, 0.36)
        draw_glyph(cr, self.name, self.size, col)


# ----------------------------------------------------------------------------
# Directory listing
# ----------------------------------------------------------------------------
class Entry:
    __slots__ = ("name", "path", "is_dir", "size", "mtime", "ctime", "atime", "mode", "is_link", "hidden",
                 "kind", "extra")

    def __init__(self, path, st=None, name=None, is_dir=None):
        self.path = path
        self.name = name if name is not None else (os.path.basename(path.rstrip("/")) or path)
        self.extra = None
        if st is None:
            try:
                st = os.stat(path)
            except OSError:
                try:
                    st = os.lstat(path)
                except OSError:
                    st = None
        if st is not None:
            self.is_dir = stat.S_ISDIR(st.st_mode) if is_dir is None else is_dir
            self.size = None if self.is_dir else st.st_size
            self.mtime = st.st_mtime
            self.ctime = st.st_ctime
            self.atime = st.st_atime
            self.mode = st.st_mode
        else:
            self.is_dir = bool(is_dir)
            self.size = None
            self.mtime = self.ctime = self.atime = 0
            self.mode = 0
        try:
            self.is_link = os.path.islink(path)
        except OSError:
            self.is_link = False
        self.hidden = self.name.startswith(".")
        self.kind = file_kind(self.name, self.is_dir, self.mode)


def list_dir(path, show_hidden):
    out = []
    with os.scandir(path) as it:
        for de in it:
            if not show_hidden and de.name.startswith("."):
                continue
            try:
                st = de.stat()
            except OSError:
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    st = None
            out.append(Entry(de.path, st, de.name))
    return out


def dir_summary(path, cancel=None, cb=None):
    """Recursive size/count for properties. Returns (bytes, files, folders)."""
    total = files = folders = 0
    last = time.time()
    stack = [path]
    while stack:
        if cancel and cancel.is_set():
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for de in it:
                    try:
                        if de.is_dir(follow_symlinks=False):
                            folders += 1
                            stack.append(de.path)
                        else:
                            files += 1
                            total += de.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
        if cb and time.time() - last > 0.25:
            last = time.time()
            cb(total, files, folders)
    return total, files, folders


# ----------------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------------
THUMB_DIRS = [os.path.join(GLib.get_user_cache_dir(), "thumbnails", d) for d in ("x-large", "large", "normal")]
HAVE_FFMPEGTHUMB = shutil.which("ffmpegthumbnailer") is not None


class Thumbnailer:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.cache = OrderedDict()
        self.pending = set()
        self.lock = threading.Lock()

    def get(self, path, mtime, size, kind, cb):
        key = (path, mtime, size)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            if key in self.pending:
                return None
            self.pending.add(key)
        self.pool.submit(self._work, key, kind, cb)
        return None

    def _store(self, key, pb):
        with self.lock:
            self.pending.discard(key)
            self.cache[key] = pb
            while len(self.cache) > 800:
                self.cache.popitem(last=False)

    def _work(self, key, kind, cb):
        path, mtime, size = key
        pb = None
        try:
            uri = path_to_uri(path)
            md5 = hashlib.md5(uri.encode()).hexdigest() + ".png"
            for d in THUMB_DIRS:
                tp = os.path.join(d, md5)
                if os.path.exists(tp) and os.path.getmtime(tp) >= mtime - 1:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(tp, size, size, True)
                    break
            if pb is None and kind == "image":
                if os.path.getsize(path) < 80 * 1024 * 1024:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
                    pb = pb.apply_embedded_orientation() or pb
            if pb is None and kind == "video" and HAVE_FFMPEGTHUMB:
                d = THUMB_DIRS[1]
                os.makedirs(d, exist_ok=True)
                tp = os.path.join(d, md5)
                subprocess.run(["ffmpegthumbnailer", "-i", path, "-o", tp, "-s", "256", "-t", "15%"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                if os.path.exists(tp):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(tp, size, size, True)
        except Exception:
            pb = None
        if pb is not None:
            pb = self._frame(pb, kind)
        self._store(key, pb)
        if pb is not None:
            GLib.idle_add(cb, path, pb)

    @staticmethod
    def _frame(pb, kind):
        w, h = pb.get_width(), pb.get_height()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w + 2, h + 2)
        cr = cairo.Context(surf)
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.rectangle(0, 0, w + 2, h + 2)
        cr.fill()
        Gdk.cairo_set_source_pixbuf(cr, pb, 1, 1)
        cr.paint()
        if kind == "video":
            r = max(6, min(w, h) * 0.14)
            cx, cy = w / 2 + 1, h / 2 + 1
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(cx - r * 0.35, cy - r * 0.5)
            cr.line_to(cx + r * 0.55, cy)
            cr.line_to(cx - r * 0.35, cy + r * 0.5)
            cr.close_path()
            cr.fill()
        return Gdk.pixbuf_get_from_surface(surf, 0, 0, w + 2, h + 2)


# ----------------------------------------------------------------------------
# Recent files and mounts
# ----------------------------------------------------------------------------
def recent_files(limit=20):
    out = []
    try:
        root = ET.parse(RECENT_FILE).getroot()
    except Exception:
        return out
    items = []
    for bm in root.iter("bookmark"):
        href = bm.get("href", "")
        if not href.startswith("file://"):
            continue
        p = uri_to_path(href)
        when = bm.get("visited") or bm.get("modified") or ""
        items.append((when, p))
    items.sort(reverse=True)
    seen = set()
    for _, p in items:
        if p in seen or not p or not os.path.isfile(p):
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def add_recent(path):
    try:
        mgr = Gtk.RecentManager.get_default()
        mgr.add_item(path_to_uri(path))
    except Exception:
        pass


def block_kind(dev):
    """Guess drive icon from a /dev path."""
    base = os.path.basename(dev)
    if base.startswith("mmcblk"):
        return "sdcard"
    if base.startswith("nvme"):
        return "nvme"
    if base.startswith("sd"):
        m = re.match(r"(sd[a-z]+)", base)
        if m:
            rem = f"/sys/block/{m.group(1)}/removable"
            try:
                if open(rem).read().strip() == "1":
                    return "usb"
            except OSError:
                pass
            if "usb" in os.path.realpath(f"/sys/block/{m.group(1)}"):
                return "usb"
        return "drive"
    if base.startswith("sr"):
        return "optical"
    return "drive"


SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "pstore", "bpf", "debugfs",
           "tracefs", "securityfs", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc",
           "rpc_pipefs", "overlay", "squashfs", "nsfs", "efivarfs", "ramfs", "fuse.gvfsd-fuse", "fuse.portal"}


def drives():
    """List of dicts: name, path, kind, total, free, dev, mounted, volume."""
    out = []
    seen = set()
    try:
        with open("/proc/self/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fs in SKIP_FS or mnt in seen:
            continue
        if not (dev.startswith("/dev/") or fs in ("cifs", "nfs", "nfs4", "smb3", "fuse.sshfs")):
            continue
        if mnt.startswith(("/snap", "/var/lib/docker", "/run/user", "/proc", "/sys")):
            continue
        seen.add(mnt)
        try:
            sv = os.statvfs(mnt)
            total = sv.f_blocks * sv.f_frsize
            free = sv.f_bavail * sv.f_frsize
        except OSError:
            total = free = 0
        if mnt == "/":
            name, kind = "System", "drive-sys"
        elif mnt in ("/boot/firmware", "/boot"):
            name, kind = "Boot", block_kind(dev)
        else:
            name = os.path.basename(mnt) or mnt
            kind = "network" if fs in ("cifs", "nfs", "nfs4", "smb3", "fuse.sshfs") else block_kind(dev)
        out.append({"name": name, "path": mnt, "kind": kind, "total": total, "free": free, "dev": dev,
                    "mounted": True, "volume": None, "fs": fs})
    # unmounted volumes (USB sticks etc.)
    try:
        vm = Gio.VolumeMonitor.get()
        for vol in vm.get_volumes():
            if vol.get_mount() is not None:
                continue
            dev = vol.get_identifier("unix-device") or ""
            if not vol.can_mount():
                continue
            out.append({"name": vol.get_name(), "path": None, "kind": block_kind(dev) if dev else "usb",
                        "total": 0, "free": 0, "dev": dev, "mounted": False, "volume": vol, "fs": ""})
    except Exception:
        pass
    return out


def drive_label(d):
    if d["path"] == "/":
        return "System (/)"
    if d["path"]:
        return f"{d['name']} ({d['path']})"
    return d["name"]


# ----------------------------------------------------------------------------
# Trash (freedesktop spec, home trash)
# ----------------------------------------------------------------------------
def trash_items():
    files_dir = os.path.join(TRASH_DIR, "files")
    info_dir = os.path.join(TRASH_DIR, "info")
    out = []
    try:
        names = os.listdir(files_dir)
    except OSError:
        return out
    for n in names:
        p = os.path.join(files_dir, n)
        orig, deleted = "", 0
        try:
            with open(os.path.join(info_dir, n + ".trashinfo")) as f:
                for ln in f:
                    if ln.startswith("Path="):
                        orig = urllib.parse.unquote(ln[5:].strip())
                    elif ln.startswith("DeletionDate="):
                        try:
                            deleted = datetime.datetime.strptime(ln[13:].strip(), "%Y-%m-%dT%H:%M:%S").timestamp()
                        except ValueError:
                            pass
        except OSError:
            pass
        e = Entry(p, name=os.path.basename(orig) if orig else n)
        e.extra = {"orig": orig, "deleted": deleted, "tname": n}
        out.append(e)
    return out


def trash_count():
    try:
        return len(os.listdir(os.path.join(TRASH_DIR, "files")))
    except OSError:
        return 0


def trash_restore(e):
    orig = e.extra["orig"]
    if not orig:
        raise OSError("Original location unknown")
    parent = os.path.dirname(orig)
    os.makedirs(parent, exist_ok=True)
    target = orig
    if os.path.lexists(target):
        target = os.path.join(parent, unique_name(parent, os.path.basename(orig), e.is_dir))
    shutil.move(e.path, target)
    try:
        os.remove(os.path.join(TRASH_DIR, "info", e.extra["tname"] + ".trashinfo"))
    except OSError:
        pass
    return target


def rm_any(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    else:
        os.remove(p)


def trash_delete(e):
    rm_any(e.path)
    try:
        os.remove(os.path.join(TRASH_DIR, "info", e.extra["tname"] + ".trashinfo"))
    except OSError:
        pass


def trash_empty():
    for sub in ("files", "info"):
        d = os.path.join(TRASH_DIR, sub)
        try:
            for n in os.listdir(d):
                try:
                    rm_any(os.path.join(d, n))
                except OSError:
                    pass
        except OSError:
            pass
    try:
        os.remove(os.path.join(TRASH_DIR, "directorysizes"))
    except OSError:
        pass


# ----------------------------------------------------------------------------
# File operations (run in a thread)
# ----------------------------------------------------------------------------
class Cancelled(Exception):
    pass


class FileJob(threading.Thread):
    VERB = {"copy": "Copying", "move": "Moving", "delete": "Deleting", "trash": "Deleting",
            "compress": "Compressing", "extract": "Extracting"}

    def __init__(self, op, items, dest=None, policy="replace", done=None, out_name=None):
        super().__init__(daemon=True)
        self.op = op
        self.items = list(items)
        self.dest = dest
        self.policy = policy          # replace | skip | keep
        self.done_cb = done
        self.out_name = out_name
        self.cancel = threading.Event()
        self.total_bytes = 0
        self.total_files = 0
        self.done_bytes = 0
        self.done_files = 0
        self.current = ""
        self.errors = []
        self.not_trashable = []
        self.created = []
        self.started = time.time()
        self.finished = False
        self.scanning = True

    # -- helpers
    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _scan(self, paths):
        for p in paths:
            self._check()
            if os.path.isdir(p) and not os.path.islink(p):
                for root, dirs, files in os.walk(p):
                    self._check()
                    for fn in files:
                        self.total_files += 1
                        try:
                            self.total_bytes += os.lstat(os.path.join(root, fn)).st_size
                        except OSError:
                            pass
            else:
                self.total_files += 1
                try:
                    self.total_bytes += os.lstat(p).st_size
                except OSError:
                    pass

    def _copy_file(self, src, dst):
        self.current = os.path.basename(src)
        if os.path.islink(src):
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(os.readlink(src), dst)
            self.done_files += 1
            return
        try:
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    self._check()
                    buf = fi.read(1024 * 1024)
                    if not buf:
                        break
                    fo.write(buf)
                    self.done_bytes += len(buf)
        except Cancelled:
            try:
                os.remove(dst)
            except OSError:
                pass
            raise
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass
        self.done_files += 1

    def _copy_any(self, src, dst):
        """Copy src to dst. Directories are merged into an existing dst."""
        self._check()
        if os.path.isdir(src) and not os.path.islink(src):
            if os.path.lexists(dst) and not os.path.isdir(dst):
                os.remove(dst)
            os.makedirs(dst, exist_ok=True)
            for de in os.scandir(src):
                try:
                    self._copy_any(de.path, os.path.join(dst, de.name))
                except Cancelled:
                    raise
                except OSError as e:
                    self.errors.append((de.path, e.strerror or str(e)))
            try:
                shutil.copystat(src, dst)
            except OSError:
                pass
        else:
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            self._copy_file(src, dst)

    def _dest_for(self, src):
        name = os.path.basename(src.rstrip("/"))
        dst = os.path.join(self.dest, name)
        if self.op == "copy" and os.path.realpath(os.path.dirname(src)) == os.path.realpath(self.dest):
            return os.path.join(self.dest, unique_name(self.dest, name, os.path.isdir(src), "copy"))
        if os.path.lexists(dst):
            if self.policy == "skip":
                return None
            if self.policy == "keep":
                return os.path.join(self.dest, unique_name(self.dest, name, os.path.isdir(src)))
        return dst

    # -- ops
    def run(self):
        try:
            getattr(self, "_run_" + self.op)()
        except Cancelled:
            pass
        except Exception as e:  # keep the UI alive whatever happens
            self.errors.append(("", str(e)))
        self.finished = True
        if self.done_cb:
            GLib.idle_add(self.done_cb, self)

    def _run_copy(self):
        self._scan(self.items)
        self.scanning = False
        for src in self.items:
            self._check()
            dst = self._dest_for(src)
            if dst is None:
                continue
            if is_inside(dst, src) and os.path.isdir(src):
                self.errors.append((src, "The destination folder is inside the source folder."))
                continue
            try:
                self._copy_any(src, dst)
                self.created.append(dst)
            except Cancelled:
                raise
            except OSError as e:
                self.errors.append((src, e.strerror or str(e)))

    def _run_move(self):
        slow = [s for s in self.items if not same_device(s, self.dest)]
        self._scan(slow)
        self.scanning = False
        for src in self.items:
            self._check()
            if os.path.realpath(os.path.dirname(src)) == os.path.realpath(self.dest):
                continue
            dst = self._dest_for(src)
            if dst is None:
                continue
            if os.path.isdir(src) and is_inside(dst, src):
                self.errors.append((src, "The destination folder is inside the source folder."))
                continue
            try:
                if same_device(src, self.dest):
                    self.current = os.path.basename(src)
                    if os.path.lexists(dst):
                        if os.path.isdir(dst) and os.path.isdir(src) and not os.path.islink(src):
                            self._copy_any(src, dst)
                            rm_any(src)
                        else:
                            if os.path.isdir(dst) and not os.path.islink(dst):
                                shutil.rmtree(dst)
                            os.replace(src, dst)
                    else:
                        os.rename(src, dst)
                    self.done_files += 1
                else:
                    self._copy_any(src, dst)
                    rm_any(src)
                self.created.append(dst)
            except Cancelled:
                raise
            except OSError as e:
                self.errors.append((src, e.strerror or str(e)))

    def _run_trash(self):
        self.total_files = len(self.items)
        self.scanning = False
        for src in self.items:
            self._check()
            self.current = os.path.basename(src)
            try:
                Gio.File.new_for_path(src).trash(None)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                    self.not_trashable.append(src)
                else:
                    self.errors.append((src, e.message))
            self.done_files += 1

    def _run_delete(self):
        self.total_files = len(self.items)
        self.scanning = False
        for src in self.items:
            self._check()
            self.current = os.path.basename(src)
            try:
                rm_any(src)
            except OSError as e:
                self.errors.append((src, e.strerror or str(e)))
            self.done_files += 1

    def _run_compress(self):
        import zipfile
        self._scan(self.items)
        self.scanning = False
        out = os.path.join(self.dest, unique_name(self.dest, self.out_name))
        self.created.append(out)
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for src in self.items:
                    base = os.path.dirname(src.rstrip("/"))
                    if os.path.isdir(src) and not os.path.islink(src):
                        for root, dirs, files in os.walk(src):
                            self._check()
                            rel = os.path.relpath(root, base)
                            if not files and not dirs:
                                zf.writestr(rel + "/", "")
                            for fn in files:
                                self._check()
                                fp = os.path.join(root, fn)
                                self.current = fn
                                try:
                                    zf.write(fp, os.path.relpath(fp, base))
                                    self.done_bytes += os.lstat(fp).st_size
                                except OSError as e:
                                    self.errors.append((fp, e.strerror or str(e)))
                                self.done_files += 1
                    else:
                        self.current = os.path.basename(src)
                        zf.write(src, os.path.relpath(src, base))
                        self.done_bytes += os.lstat(src).st_size
                        self.done_files += 1
        except Cancelled:
            try:
                os.remove(out)
            except OSError:
                pass
            raise

    def _run_extract(self):
        import zipfile
        import tarfile
        for src in self.items:
            self._check()
            name = os.path.basename(src)
            stem, _ = split_ext(name)
            out = os.path.join(self.dest, unique_name(self.dest, stem, True))
            os.makedirs(out)
            self.created.append(out)
            self.current = name
            try:
                if zipfile.is_zipfile(src):
                    with zipfile.ZipFile(src) as zf:
                        infos = zf.infolist()
                        self.total_files += len(infos)
                        self.total_bytes += sum(i.file_size for i in infos)
                        self.scanning = False
                        for i in infos:
                            self._check()
                            self.current = os.path.basename(i.filename.rstrip("/"))
                            zf.extract(i, out)
                            self.done_bytes += i.file_size
                            self.done_files += 1
                elif tarfile.is_tarfile(src):
                    with tarfile.open(src) as tf:
                        members = tf.getmembers()
                        self.total_files += len(members)
                        self.total_bytes += sum(m.size for m in members)
                        self.scanning = False
                        for m in members:
                            self._check()
                            self.current = os.path.basename(m.name)
                            try:
                                tf.extract(m, out, filter="data")
                            except TypeError:
                                tf.extract(m, out)
                            self.done_bytes += m.size
                            self.done_files += 1
                else:
                    self.errors.append((src, "This file is not a supported archive."))
            except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
                self.errors.append((src, str(e)))


# ----------------------------------------------------------------------------
# Network: mounted network shares, Samba shares on this Pi
# ----------------------------------------------------------------------------
def network_mounts():
    """Gio mounts of network shares (smb, sftp, nfs...) that have a local fuse path."""
    out = []
    try:
        for m in Gio.VolumeMonitor.get().get_mounts():
            root = m.get_root()
            uri = root.get_uri() or ""
            if uri.startswith("file://") or not root.get_path():
                continue
            path = root.get_path()
            name = m.get_name()
            if uri.startswith("smb://"):
                u = urllib.parse.urlparse(uri)
                share = u.path.strip("/").split("/")[0]
                name = f"{share} ({u.hostname})" if share else name
            out.append({"name": name, "path": path, "uri": uri, "mount": m})
    except Exception:
        pass
    return out


def samba_installed():
    return shutil.which("testparm") is not None or os.path.exists("/usr/sbin/smbd")


def _testparm():
    exe = shutil.which("testparm") or "/usr/bin/testparm"
    try:
        r = subprocess.run([exe, "-s", "--suppress-prompt"], capture_output=True, text=True, timeout=8)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        try:
            with open("/etc/samba/smb.conf") as f:
                return f.read()
        except OSError:
            return ""


def samba_config():
    """Return (global dict, {share: dict}) from the Samba config."""
    glob, shares, cur = {}, {}, None
    for ln in _testparm().splitlines():
        ln = ln.strip()
        if not ln or ln[0] in "#;":
            continue
        m = re.match(r"^\[(.+)\]$", ln)
        if m:
            name = m.group(1)
            cur = glob if name.lower() == "global" else shares.setdefault(name, {})
            continue
        if cur is not None and "=" in ln:
            k, v = ln.split("=", 1)
            cur[k.strip().lower()] = v.strip()
    return glob, shares


def our_shares():
    names = set()
    try:
        with open(SHARES_CONF) as f:
            for ln in f:
                m = re.match(r"^\s*\[(.+)\]\s*$", ln)
                if m:
                    names.add(m.group(1))
    except OSError:
        pass
    return names


def list_shares():
    """Folders this Pi shares over the network. List of dicts: name, path, comment, readonly, ours."""
    glob, shares = samba_config()
    mine = our_shares()
    out = []
    for name, d in shares.items():
        if name.lower() in ("printers", "print$", "homes") or d.get("printable", "no").lower() in ("yes", "true"):
            continue
        path = d.get("path", "")
        if not path:
            continue
        ro = d.get("read only", d.get("readonly", "yes")).lower() in ("yes", "true", "1")
        if d.get("writable", d.get("writeable", "")).lower() in ("yes", "true", "1"):
            ro = False
        out.append({"name": name, "path": path, "comment": d.get("comment", ""), "readonly": ro,
                    "ours": name in mine})
    return out


def share_for_path(path):
    rp = os.path.realpath(path)
    for s in list_shares():
        if os.path.realpath(s["path"]) == rp:
            return s
    return None


def server_name():
    glob, _ = samba_config()
    nb = glob.get("netbios name")
    if nb:
        return nb.upper()
    return (os.uname().nodename.split(".")[0] or "raspberrypi").upper()


def helper_available():
    return os.path.exists(HELPER)


def run_helper(*args):
    """Run the root helper through sudo. Returns (ok, message)."""
    try:
        r = subprocess.run(["sudo", "-n", HELPER] + list(args), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    msg = (r.stdout + r.stderr).strip()
    return r.returncode == 0, msg


def share_name_from(path):
    base = os.path.basename(path.rstrip("/")) or "share"
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-") or "share"
    return name[:40]


# ----------------------------------------------------------------------------
# Small widget helpers
# ----------------------------------------------------------------------------
def label(text="", cls=None, xalign=0.0, ellipsize=False, wrap=False, markup=False):
    lb = Gtk.Label()
    if markup:
        lb.set_markup(text)
    else:
        lb.set_text(text)
    lb.set_xalign(xalign)
    if cls:
        for c in cls.split():
            lb.get_style_context().add_class(c)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        lb.set_line_wrap(True)
        lb.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return lb


def add_cls(w, *cls):
    ctx = w.get_style_context()
    for c in cls:
        ctx.add_class(c)
    return w


def tb_button(icon, text, cb=None, chev=False, tip=None):
    b = Gtk.Button()
    add_cls(b, "tb")
    box = Gtk.Box(spacing=7)
    if icon:
        box.pack_start(Icon(icon), False, False, 0)
    if text:
        box.pack_start(Gtk.Label(label=text), False, False, 0)
    if chev:
        box.pack_start(Icon("chevdown", 10), False, False, 0)
    b.add(box)
    b.set_relief(Gtk.ReliefStyle.NONE)
    b.set_tooltip_text(tip or text or None)
    if cb:
        b.connect("clicked", lambda w: cb(w))
    return b


def icon_button(icon, cb, cls="nb", tip=None, size=16):
    b = Gtk.Button()
    add_cls(b, cls)
    b.add(Icon(icon, size))
    b.set_relief(Gtk.ReliefStyle.NONE)
    if tip:
        b.set_tooltip_text(tip)
    b.connect("clicked", lambda w: cb(w))
    return b


def build_menu(spec):
    """spec: list of None (separator) or dicts: label, cb, icon, place, accel, sensitive, checked, sub."""
    menu = Gtk.Menu()
    add_cls(menu, "context-menu")
    for it in spec:
        if it is None:
            menu.append(Gtk.SeparatorMenuItem())
            continue
        mi = Gtk.MenuItem()
        box = Gtk.Box(spacing=12)
        ic = None
        if it.get("checked"):
            ic = Icon("check", 16)
        elif it.get("place"):
            ic = Icon(it["place"], 16, place=True)
        elif it.get("pixbuf") is not None:
            ic = Gtk.Image.new_from_pixbuf(it["pixbuf"])
        elif it.get("icon"):
            ic = Icon(it["icon"], 16)
        if ic is None:
            ic = Gtk.Box()
            ic.set_size_request(16, 16)
        box.pack_start(ic, False, False, 0)
        lb = Gtk.Label(label=it["label"])
        lb.set_xalign(0)
        box.pack_start(lb, True, True, 0)
        if it.get("accel"):
            al = Gtk.Label(label=it["accel"])
            al.get_style_context().add_class("dim-label")
            al.set_opacity(0.55)
            al.set_margin_start(24)
            box.pack_end(al, False, False, 0)
        mi.add(box)
        mi.set_sensitive(it.get("sensitive", True))
        if it.get("sub") is not None:
            mi.set_submenu(build_menu(it["sub"]))
        elif it.get("cb"):
            cb = it["cb"]
            mi.connect("activate", lambda w, cb=cb: GLib.idle_add(cb))
        menu.append(mi)
    menu.show_all()
    return menu


def popup_menu(menu, widget, event=None):
    menu.attach_to_widget(widget, None)
    if event is not None:
        menu.popup_at_pointer(event)
    else:
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)


# ----------------------------------------------------------------------------
# Windows 11 style dialogs
# ----------------------------------------------------------------------------
class WinDialog(Gtk.Window):
    def __init__(self, parent, title, body=None, primary="OK", secondary="Cancel", width=440, extra=None,
                 markup=False):
        super().__init__(transient_for=parent, modal=True)
        add_cls(self, "fx-dialog")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(width, -1)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_title(title)
        self.cb = None
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        add_cls(self.body, "dlg-body")
        self.body.pack_start(label(title, "dlg-title"), False, False, 0)
        if body:
            lb = label(body, wrap=True, markup=markup)
            lb.set_max_width_chars(60)
            self.body.pack_start(lb, False, False, 0)
        if extra:
            self.body.pack_start(extra, False, False, 0)
        outer.pack_start(self.body, True, True, 0)
        self.foot = Gtk.Box(spacing=8, homogeneous=True)
        add_cls(self.foot, "dlg-foot")
        self.primary_btn = None
        if primary:
            pb = Gtk.Button(label=primary)
            add_cls(pb, "primary")
            pb.connect("clicked", lambda *_: self.done(True))
            self.foot.pack_start(pb, True, True, 0)
            pb.set_can_default(True)
            self.set_default(pb)
            self.primary_btn = pb
        if secondary:
            sb = Gtk.Button(label=secondary)
            add_cls(sb, "secondary")
            sb.connect("clicked", lambda *_: self.done(False))
            self.foot.pack_start(sb, True, True, 0)
        if primary or secondary:
            outer.pack_start(self.foot, False, False, 0)
        self.add(outer)
        self.connect("key-press-event", self._key)

    def _key(self, w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.done(False)
            return True
        return False

    def done(self, ok):
        cb = self.cb
        self.cb = None
        self.hide()
        if cb:
            cb(ok)
        self.destroy()

    def ask(self, cb=None):
        self.cb = cb
        self.show_all()
        if self.primary_btn:
            self.primary_btn.grab_focus()


def info_dialog(parent, title, body):
    WinDialog(parent, title, body, primary="OK", secondary=None).ask()


def confirm(parent, title, body, yes, cb, no="Cancel"):
    WinDialog(parent, title, body, primary=yes, secondary=no).ask(cb)


def error_list_dialog(parent, title, errors):
    lines = []
    for p, msg in errors[:12]:
        lines.append(f"{os.path.basename(p) or p}: {msg}" if p else msg)
    if len(errors) > 12:
        lines.append(f"...and {len(errors) - 12} more")
    info_dialog(parent, title, "\n".join(lines))


def ask_text(parent, title, prompt, initial, cb, select_stem=True, ok="OK"):
    ent = Gtk.Entry()
    add_cls(ent, "field")
    ent.set_text(initial)
    ent.set_activates_default(True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    if prompt:
        box.pack_start(label(prompt), False, False, 0)
    box.pack_start(ent, False, False, 0)
    dlg = WinDialog(parent, title, None, primary=ok, extra=box)

    def done(okk):
        if okk:
            cb(ent.get_text())
    dlg.ask(done)
    ent.grab_focus()
    if select_stem:
        stem, _ = split_ext(initial)
        ent.select_region(0, len(stem))
    return dlg


class ChoiceDialog(WinDialog):
    """Dialog with large choice buttons (like the Windows replace/skip dialog)."""

    def __init__(self, parent, title, body, choices, cb):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        super().__init__(parent, title, body, primary=None, secondary="Cancel", width=480, extra=box,
                         markup=True)
        self.choice_cb = cb
        for key, text, sub in choices:
            b = Gtk.Button()
            add_cls(b, "choice")
            row = Gtk.Box(spacing=12)
            row.pack_start(Icon("check", 16, color=hexc(C_ACCENT)), False, False, 0)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.pack_start(label(text), False, False, 0)
            if sub:
                col.pack_start(label(sub, "ter", wrap=True), False, False, 0)
            row.pack_start(col, True, True, 0)
            b.add(row)
            b.connect("clicked", lambda w, k=key: self._pick(k))
            box.pack_start(b, False, False, 0)
        self.ask(lambda ok: cb(None))

    def _pick(self, key):
        self.cb = None
        self.destroy()
        self.choice_cb(key)


class ProgressWindow(Gtk.Window):
    def __init__(self, parent, job, title, src_desc, dst_desc):
        super().__init__(transient_for=parent, modal=False)
        add_cls(self, "fx-dialog")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(460, -1)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.job = job
        self.last_bytes = 0
        self.last_t = time.time()
        self.speed = 0.0
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        add_cls(body, "dlg-body")
        body.pack_start(label(title, "prog-title"), False, False, 0)
        if src_desc:
            m = f"from <b>{esc(src_desc)}</b>" + (f" to <b>{esc(dst_desc)}</b>" if dst_desc else "")
            body.pack_start(label(m, "sec", markup=True, ellipsize=True), False, False, 0)
        self.pct = label("0% complete", "dlg-title")
        self.pct.set_margin_top(8)
        body.pack_start(self.pct, False, False, 0)
        self.bar = Gtk.ProgressBar()
        body.pack_start(self.bar, False, False, 4)
        self.l_name = label("", "sec", ellipsize=True)
        self.l_rem = label("", "sec")
        self.l_speed = label("", "sec")
        for w in (self.l_name, self.l_rem, self.l_speed):
            body.pack_start(w, False, False, 0)
        outer.pack_start(body, True, True, 0)
        foot = Gtk.Box(spacing=8)
        add_cls(foot, "dlg-foot")
        cb = Gtk.Button(label="Cancel")
        add_cls(cb, "secondary")
        cb.connect("clicked", lambda *_: self.job.cancel.set())
        foot.pack_end(cb, False, False, 0)
        outer.pack_start(foot, False, False, 0)
        self.add(outer)
        GLib.timeout_add(150, self._tick)

    def _tick(self):
        j = self.job
        if j.finished:
            self.destroy()
            return False
        now = time.time()
        dt = now - self.last_t
        if dt >= 0.5:
            inst = (j.done_bytes - self.last_bytes) / dt
            self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
            self.last_bytes = j.done_bytes
            self.last_t = now
        if j.scanning:
            self.bar.pulse()
            self.pct.set_text("Calculating...")
            return True
        if j.total_bytes > 0:
            frac = min(1.0, j.done_bytes / j.total_bytes)
        elif j.total_files > 0:
            frac = min(1.0, j.done_files / j.total_files)
        else:
            frac = 0
        self.bar.set_fraction(frac)
        self.pct.set_text(f"{int(frac * 100)}% complete")
        self.l_name.set_text(f"Name: {j.current}")
        rem_items = max(0, j.total_files - j.done_files)
        rem_bytes = max(0, j.total_bytes - j.done_bytes)
        self.l_rem.set_text(f"Items remaining: {rem_items:,}" + (f" ({fmt_size(rem_bytes)})" if j.total_bytes else ""))
        if self.speed > 0 and j.total_bytes:
            secs = rem_bytes / self.speed
            t = (f"{int(secs // 60)} min {int(secs % 60)} sec" if secs >= 60 else
                 (f"{int(secs)} sec" if secs >= 2 else "a few seconds"))
            self.l_speed.set_text(f"Speed: {fmt_size(self.speed)}/s    Time remaining: {'about ' + t if secs >= 2 else t}")
        return True


class NetMountOp(Gio.MountOperation):
    """Windows-style 'Enter network credentials' prompt for mounting network folders."""
    _live = set()

    def __init__(self, parent):
        super().__init__()
        self.parent_win = parent
        NetMountOp._live.add(self)
        GLib.timeout_add_seconds(300, lambda: NetMountOp._live.discard(self) and False)

    def do_ask_password(self, message, default_user, default_domain, flags):
        op = self
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        first = (message or "").splitlines()[0] if message else ""
        if first:
            box.pack_start(label(first, "sec", wrap=True), False, False, 0)
        user = Gtk.Entry()
        add_cls(user, "field")
        user.set_placeholder_text("User name")
        if default_user and default_user.lower() not in ("guest", "anonymous"):
            user.set_text(default_user)
        pw = Gtk.Entry()
        add_cls(pw, "field")
        pw.set_placeholder_text("Password")
        pw.set_visibility(False)
        pw.set_activates_default(True)
        user.set_activates_default(True)
        need_user = bool(flags & Gio.AskPasswordFlags.NEED_USERNAME)
        need_pw = bool(flags & Gio.AskPasswordFlags.NEED_PASSWORD)
        if need_user:
            box.pack_start(user, False, False, 0)
        if need_pw:
            box.pack_start(pw, False, False, 0)
        guest = None
        if flags & Gio.AskPasswordFlags.ANONYMOUS_SUPPORTED:
            guest = Gtk.CheckButton(label="Connect as guest (no password)")
            box.pack_start(guest, False, False, 0)
        remember = None
        if flags & Gio.AskPasswordFlags.SAVING_SUPPORTED:
            remember = Gtk.CheckButton(label="Remember my credentials")
            remember.set_active(True)
            box.pack_start(remember, False, False, 0)
        dlg = WinDialog(self.parent_win, "Enter network credentials", None, primary="OK", extra=box)

        def done(ok):
            if not ok:
                op.reply(Gio.MountOperationResult.ABORTED)
                return
            if guest is not None and guest.get_active():
                op.set_anonymous(True)
            else:
                op.set_anonymous(False)
                if need_user:
                    op.set_username(user.get_text())
                if need_pw:
                    op.set_password(pw.get_text())
                if default_domain:
                    op.set_domain(default_domain)
            op.set_password_save(Gio.PasswordSave.PERMANENTLY if remember is not None and remember.get_active()
                                 else Gio.PasswordSave.NEVER)
            op.reply(Gio.MountOperationResult.HANDLED)
        dlg.ask(done)
        (user if need_user and not user.get_text() else pw).grab_focus()

    def do_ask_question(self, message, choices):
        op = self
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dlg = WinDialog(self.parent_win, "File Explorer", message, primary=None, secondary="Cancel", extra=box)
        for i, c in enumerate(choices):
            b = Gtk.Button(label=c)
            add_cls(b, "secondary")

            def pick(w, i=i):
                dlg.cb = None
                dlg.destroy()
                op.set_choice(i)
                op.reply(Gio.MountOperationResult.HANDLED)
            b.connect("clicked", pick)
            box.pack_start(b, False, False, 0)
        dlg.ask(lambda ok: op.reply(Gio.MountOperationResult.ABORTED))


# ----------------------------------------------------------------------------
# Launching
# ----------------------------------------------------------------------------
def launch_ctx():
    try:
        return Gdk.Display.get_default().get_app_launch_context()
    except Exception:
        return None


def terminal_cmd(cwd, run=None):
    import shlex
    for t in ("lxterminal", "x-terminal-emulator", "gnome-terminal", "xfce4-terminal", "kgx", "konsole", "xterm"):
        exe = shutil.which(t)
        if not exe:
            continue
        if run:
            inner = f"{shlex.quote(run)}; echo; read -p 'Press Enter to close...' _"
            if t in ("gnome-terminal", "kgx"):
                return [exe, "--", "bash", "-c", inner]
            return [exe, "-e", "bash -c " + shlex.quote(inner)] if t == "lxterminal" else [exe, "-e", "bash", "-c", inner]
        if t in ("lxterminal", "gnome-terminal", "xfce4-terminal", "kgx"):
            return [exe, f"--working-directory={cwd}"]
        return [exe]
    return None


def open_terminal(cwd, parent=None, run=None):
    cmd = terminal_cmd(cwd, run)
    if not cmd:
        if parent:
            info_dialog(parent, "No terminal found", "Install a terminal app such as lxterminal.")
        return
    try:
        subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
    except OSError as e:
        if parent:
            info_dialog(parent, "Couldn't open terminal", str(e))


def content_type(path):
    try:
        info = Gio.File.new_for_path(path).query_info("standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
        return info.get_content_type()
    except GLib.Error:
        ct, _ = Gio.content_type_guess(os.path.basename(path), None)
        return ct


def apps_for(path):
    ct = content_type(path)
    apps = []
    try:
        default = Gio.AppInfo.get_default_for_type(ct, False) if ct else None
        lst = Gio.AppInfo.get_all_for_type(ct) if ct else []
    except Exception:
        default, lst = None, []
    seen = set()
    for a in ([default] if default else []) + list(lst):
        if a and a.get_id() not in seen and a.should_show():
            seen.add(a.get_id())
            apps.append(a)
    return apps, default


def launch_with(app, paths, parent=None):
    try:
        app.launch([Gio.File.new_for_path(p) for p in paths], launch_ctx())
        for p in paths:
            add_recent(p)
    except GLib.Error as e:
        if parent:
            info_dialog(parent, "Couldn't open the file", e.message)


def choose_app(parent, path):
    dlg = Gtk.AppChooserDialog.new(parent, Gtk.DialogFlags.MODAL, Gio.File.new_for_path(path))
    dlg.set_title("How do you want to open this file?")
    if dlg.run() == Gtk.ResponseType.OK:
        app = dlg.get_app_info()
        if app:
            ct = content_type(path)
            try:
                if ct:
                    app.set_as_last_used_for_type(ct)
            except GLib.Error:
                pass
            dlg.destroy()
            launch_with(app, [path], parent)
            return
    dlg.destroy()


def pointer_mods(widget):
    try:
        seat = Gdk.Display.get_default().get_default_seat()
        win = widget.get_window()
        _, _, _, mask = win.get_device_position(seat.get_pointer())
        return mask
    except Exception:
        return 0


# ----------------------------------------------------------------------------
# Tiles for Home / This PC
# ----------------------------------------------------------------------------
class Tile(Gtk.Button):
    def __init__(self, kind, name, sub=None, bar=None, width=250, pixbuf=None):
        super().__init__()
        add_cls(self, "tile")
        self.set_relief(Gtk.ReliefStyle.NONE)
        row = Gtk.Box(spacing=12)
        if pixbuf is not None:
            row.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
        else:
            row.pack_start(Icon(kind, 48, place=True), False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        col.set_valign(Gtk.Align.CENTER)
        col.pack_start(label(name, "tile-name", ellipsize=True), False, False, 0)
        if bar is not None:
            lv = Gtk.LevelBar()
            lv.set_min_value(0)
            lv.set_max_value(1)
            lv.set_value(bar)
            lv.set_size_request(width - 90, 6)
            for off in ("low", "high", "full"):
                lv.remove_offset_value(off)
            if bar > 0.9:
                add_cls(lv, "full")
            col.pack_start(lv, False, False, 2)
        if sub:
            col.pack_start(label(sub, "tile-sub", ellipsize=True), False, False, 0)
        row.pack_start(col, True, True, 0)
        self.add(row)
        self.set_size_request(width, -1)


def section(title):
    lb = label(title, "sect-title")
    box = Gtk.Box(spacing=8)
    add_cls(box, "home-head")
    box.pack_start(lb, False, False, 0)
    return box


def flow():
    fb = Gtk.FlowBox()
    fb.set_selection_mode(Gtk.SelectionMode.NONE)
    fb.set_homogeneous(True)
    fb.set_max_children_per_line(8)
    fb.set_min_children_per_line(1)
    fb.set_column_spacing(4)
    fb.set_row_spacing(4)
    fb.set_margin_start(14)
    fb.set_margin_end(14)
    fb.set_valign(Gtk.Align.START)
    return fb


# ----------------------------------------------------------------------------
# Folder view (one per tab)
# ----------------------------------------------------------------------------
(C_NAME, C_PATH, C_DIR, C_SIZE, C_MTIME, C_TYPE, C_ICON, C_BIG, C_SIZES, C_DATES, C_X1, C_X2S, C_X3,
 C_X2, C_FG, C_KIND, C_FNAME) = range(17)
VIEW_SIZES = {"details": 20, "small": 20, "medium": 48, "large": 96, "xl": 192}
VIEW_ITEMW = {"medium": 92, "large": 128, "xl": 216}
FG_NORMAL = Gdk.RGBA(1, 1, 1, 1)
FG_DIM = Gdk.RGBA(1, 1, 1, 0.5)
SORT_IDS = {"name": C_NAME, "date": C_MTIME, "type": C_TYPE, "size": C_SIZE, "folder": C_X1, "deleted": C_X2}


class FolderView(Gtk.Box):
    def __init__(self, win, loc, select=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.app = win.app
        self.st = win.app.settings
        self.loc = None
        self.history = []
        self.hpos = -1
        self.mode = "folder"
        self.query = ""
        self.gen = 0
        self.iters = {}
        self.monitor = None
        self.refresh_pending = False
        self.searching = False
        self.search_cancel = None
        self.error = None
        self.count = 0
        self.typeahead = ""
        self.typeahead_t = 0
        self.view_mode = self.st["view"]
        self.model = Gtk.ListStore(str, str, bool, GObject.TYPE_INT64, float, str, GdkPixbuf.Pixbuf,
                                   GdkPixbuf.Pixbuf, str, str, str, str, str, float, Gdk.RGBA, str, str)
        for name, cid in SORT_IDS.items():
            self.model.set_sort_func(cid, self._sort, cid)
        self.stack = Gtk.Stack()
        self.pack_start(self.stack, True, True, 0)
        self._build_details()
        self._build_icons()
        self.home_box = Gtk.ScrolledWindow()
        self.pc_box = Gtk.ScrolledWindow()
        self.net_box = Gtk.ScrolledWindow()
        for sw in (self.home_box, self.pc_box, self.net_box):
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            add_cls(sw, "content")
        self.stack.add_named(self.home_box, "home")
        self.stack.add_named(self.pc_box, "pc")
        self.stack.add_named(self.net_box, "net")
        self.show_all()
        self.navigate(loc, select=select)

    # ---------------- construction
    def _overlay(self, child):
        ov = Gtk.Overlay()
        ov.add(child)
        msg = label("", "empty-msg", xalign=0.5, wrap=True)
        msg.set_halign(Gtk.Align.CENTER)
        msg.set_valign(Gtk.Align.START)
        msg.set_margin_top(60)
        msg.set_no_show_all(True)
        ov.add_overlay(msg)
        ov.set_overlay_pass_through(msg, True)
        return ov, msg

    def _build_details(self):
        tv = Gtk.TreeView(model=self.model)
        add_cls(tv, "files")
        tv.set_enable_search(False)
        tv.set_rubber_banding(True)
        tv.set_headers_clickable(True)
        tv.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        tv.set_activate_on_single_click(False)
        self.tree = tv
        # name column
        col = Gtk.TreeViewColumn("Name")
        pr = Gtk.CellRendererPixbuf()
        pr.set_padding(6, 5)
        col.pack_start(pr, False)
        col.add_attribute(pr, "pixbuf", C_ICON)
        tr = Gtk.CellRendererText()
        tr.set_property("ellipsize", Pango.EllipsizeMode.END)
        tr.connect("edited", self._edited)
        tr.connect("editing-started", self._edit_started)
        tr.connect("editing-canceled", self._edit_canceled)
        col.pack_start(tr, True)
        col.add_attribute(tr, "text", C_NAME)
        col.add_attribute(tr, "foreground-rgba", C_FG)
        col.set_resizable(True)
        col.set_expand(True)
        col.set_min_width(220)
        col.set_sort_column_id(C_NAME)
        tv.append_column(col)
        self.name_col = col
        self.name_cell = tr
        self.cols = {"name": col}

        def mk(title, cid, width, key, xalign=0.0):
            r = Gtk.CellRendererText()
            r.set_property("xalign", xalign)
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            r.set_padding(8, 0)
            c = Gtk.TreeViewColumn(title, r, text=cid)
            c.add_attribute(r, "foreground-rgba", C_FG)
            c.set_resizable(True)
            c.set_fixed_width(width)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            c.set_sort_column_id(SORT_IDS.get(key, cid))
            # dim secondary columns
            c.set_cell_data_func(r, self._dim_cell)
            tv.append_column(c)
            self.cols[key] = c
            return c
        mk("Original location", C_X1, 260, "orig")
        mk("Date deleted", C_X2S, 160, "deleted")
        mk("Date modified", C_DATES, 160, "date")
        mk("Type", C_TYPE, 170, "type")
        mk("Size", C_SIZES, 100, "size", 1.0)
        mk("Folder", C_X1, 280, "folder")
        tv.connect("row-activated", lambda t, p, c: self.open_selected())
        tv.connect("button-press-event", self._button)
        tv.get_selection().connect("changed", lambda *_: self._sel_changed())
        tv.connect("key-press-event", self._key)
        self._setup_dnd(tv)
        sw = Gtk.ScrolledWindow()
        sw.add(tv)
        add_cls(sw, "content")
        ov, self.msg_details = self._overlay(sw)
        self.sw_details = sw
        self.stack.add_named(ov, "details")

    def _dim_cell(self, col, cell, model, it, data=None):
        fg = model.get_value(it, C_FG)
        a = 0.5 if fg and fg.alpha < 1 else 0.786
        cell.set_property("foreground-rgba", Gdk.RGBA(1, 1, 1, a))

    def _build_icons(self):
        iv = Gtk.IconView(model=self.model)
        add_cls(iv, "files")
        iv.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        iv.set_item_orientation(Gtk.Orientation.VERTICAL)
        iv.set_margin(12)
        iv.set_column_spacing(4)
        iv.set_row_spacing(4)
        iv.set_item_padding(6)
        iv.set_activate_on_single_click(False)
        pr = Gtk.CellRendererPixbuf()
        pr.set_property("yalign", 1.0)
        iv.pack_start(pr, False)
        iv.add_attribute(pr, "pixbuf", C_BIG)
        tr = Gtk.CellRendererText()
        tr.set_property("alignment", Pango.Alignment.CENTER)
        tr.set_property("xalign", 0.5)
        tr.set_property("yalign", 0.0)
        tr.set_property("wrap-mode", Pango.WrapMode.WORD_CHAR)
        tr.set_property("ellipsize", Pango.EllipsizeMode.END)
        tr.connect("edited", self._edited)
        tr.connect("editing-started", self._edit_started)
        tr.connect("editing-canceled", self._edit_canceled)
        iv.pack_start(tr, False)
        iv.add_attribute(tr, "text", C_NAME)
        iv.add_attribute(tr, "foreground-rgba", C_FG)
        self.icon_cell = tr
        self.icon_pix = pr
        self.iview = iv
        iv.connect("item-activated", lambda v, p: self.open_selected())
        iv.connect("button-press-event", self._button)
        iv.connect("selection-changed", lambda *_: self._sel_changed())
        iv.connect("key-press-event", self._key)
        self._setup_dnd(iv)
        sw = Gtk.ScrolledWindow()
        sw.add(iv)
        add_cls(sw, "content")
        ov, self.msg_icons = self._overlay(sw)
        self.sw_icons = sw
        self.stack.add_named(ov, "icons")

    def _apply_icon_size(self):
        n = VIEW_SIZES.get(self.view_mode, 48)
        w = VIEW_ITEMW.get(self.view_mode, 92)
        self.iview.set_item_width(w)
        self.icon_pix.set_fixed_size(n + 2, n + 2)
        lines = 2 if self.view_mode == "medium" else 3
        self.icon_cell.set_property("wrap-width", w - 8)
        self.icon_cell.set_property("max-width-chars", -1)
        self.icon_cell.set_fixed_size(w - 8, -1)
        self._lines = lines

    # ---------------- sorting
    def _sort(self, model, a, b, cid):
        try:
            order = model.get_sort_column_id()[1]
        except TypeError:
            order = Gtk.SortType.ASCENDING
        desc = order == Gtk.SortType.DESCENDING
        da, db = model.get_value(a, C_DIR), model.get_value(b, C_DIR)
        if da != db:
            r = -1 if da else 1
            return -r if desc else r
        if cid == C_NAME:
            ka, kb = natural_key(model.get_value(a, C_FNAME) or ""), natural_key(model.get_value(b, C_FNAME) or "")
        elif cid in (C_TYPE, C_X1):
            ka, kb = (model.get_value(a, cid) or "").casefold(), (model.get_value(b, cid) or "").casefold()
        else:
            ka, kb = model.get_value(a, cid), model.get_value(b, cid)
        if ka == kb:
            ka = natural_key(model.get_value(a, C_FNAME) or "")
            kb = natural_key(model.get_value(b, C_FNAME) or "")
            if desc:
                ka, kb = kb, ka
        return (ka > kb) - (ka < kb)

    def apply_sort(self):
        key = self.st.get("sort_col", "name")
        if self.mode == "trash" and key == "date":
            key = "deleted"
        cid = SORT_IDS.get(key, C_NAME)
        self.model.set_sort_column_id(cid, Gtk.SortType.DESCENDING if self.st.get("sort_desc") else
                                      Gtk.SortType.ASCENDING)

    def sort_by(self, key, desc=None):
        self.st["sort_col"] = key
        if desc is not None:
            self.st["sort_desc"] = desc
        self.apply_sort()

    # ---------------- navigation
    def navigate(self, loc, push=True, select=None):
        if not is_special(loc):
            loc = os.path.abspath(os.path.expanduser(loc))
        if push:
            self.history = self.history[:self.hpos + 1]
            if not self.history or self.history[-1] != loc:
                self.history.append(loc)
            self.hpos = len(self.history) - 1
        self.query = ""
        self._cancel_search()
        self.loc = loc
        self.load(select=select)
        self.win.on_location(self)

    def can_back(self):
        return self.hpos > 0

    def can_forward(self):
        return self.hpos < len(self.history) - 1

    def back(self):
        if self.query:
            self.set_search("")
            self.win.on_location(self)
            return
        if self.can_back():
            prev = self.loc
            self.hpos -= 1
            self.navigate(self.history[self.hpos], push=False,
                          select=[prev] if not is_special(prev) else None)

    def forward(self):
        if self.can_forward():
            self.hpos += 1
            self.navigate(self.history[self.hpos], push=False)

    def parent(self):
        if self.loc in (LOC_TRASH, LOC_HOME, LOC_NET):
            return None
        if loc_host_uri(self.loc):
            return LOC_NET
        if self.loc == LOC_PC:
            return None
        if self.loc == "/":
            return LOC_PC
        return os.path.dirname(self.loc.rstrip("/")) or "/"

    def up(self):
        p = self.parent()
        if p:
            self.navigate(p, select=[self.loc])

    def is_folder(self):
        return not is_special(self.loc)

    # ---------------- loading
    def _view_mode_for(self, loc):
        fv = self.st.get("folder_views", {})
        if self.query:
            return fv.get("::search", "details")
        if loc in fv:
            return fv[loc]
        if loc in (USER_DIRS[3][1], USER_DIRS[5][1]):
            return "large"
        return self.st["view"]

    def set_view(self, mode):
        self.view_mode = mode
        fv = self.st.setdefault("folder_views", {})
        if self.loc:
            fv[self.loc] = mode
            if len(fv) > 400:
                for k in list(fv)[:100]:
                    fv.pop(k, None)
        if self.mode == "search":
            fv.pop(self.loc, None)
            fv["::search"] = mode
        sel = self.selected_paths()
        self._show_files_view()
        self._repixbuf()
        self.select_paths(sel)

    def _show_files_view(self):
        if self.mode in PAGE_MODES:
            self.stack.set_visible_child_name("net" if self.mode == "nethost" else self.mode)
            return
        if self.view_mode == "details" or self.mode == "trash":
            self.stack.set_visible_child_name("details")
        else:
            self._apply_icon_size()
            self.stack.set_visible_child_name("icons")

    def cur_view(self):
        return self.stack.get_visible_child_name()

    def _set_columns(self):
        m = self.mode
        vis = {"name": True, "date": m in ("folder", "search"), "type": True, "size": True,
               "folder": m == "search", "orig": m == "trash", "deleted": m == "trash"}
        for k, c in self.cols.items():
            c.set_visible(vis.get(k, True))

    def load(self, select=None):
        self.gen += 1
        gen = self.gen
        self._stop_monitor()
        self.error = None
        loc = self.loc
        if loc == LOC_HOME:
            self.mode = "home"
            self._build_home()
            self._show_files_view()
            self.win.on_items(self)
            return
        if loc == LOC_PC:
            self.mode = "pc"
            self._build_pc()
            self._show_files_view()
            self.win.on_items(self)
            return
        if loc == LOC_NET or loc_host_uri(loc):
            self.mode = "net" if loc == LOC_NET else "nethost"
            self._show_files_view()
            (self._build_net if loc == LOC_NET else self._build_nethost)()
            self.win.on_items(self)
            return
        self.view_mode = self._view_mode_for(loc)
        if loc == LOC_TRASH:
            self.mode = "trash"
            self._set_columns()
            self._show_files_view()
            self._fill(trash_items(), select)
            self._start_monitor(os.path.join(TRASH_DIR, "files"))
            return
        self.mode = "search" if self.query else "folder"
        self._set_columns()
        self._show_files_view()
        if self.query:
            self._start_search()
            return
        self._set_msg("")
        show_hidden = self.st["show_hidden"]

        def work():
            try:
                ents = list_dir(loc, show_hidden)
                err = None
            except PermissionError:
                ents, err = [], "You don't currently have permission to access this folder."
            except FileNotFoundError:
                ents, err = [], f"Location is not available\n\n{loc} could not be found."
            except NotADirectoryError:
                ents, err = [], f"{loc} is not a folder."
            except OSError as e:
                ents, err = [], e.strerror or str(e)
            GLib.idle_add(self._loaded, gen, ents, err, select)
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, gen, ents, err, select):
        if gen != self.gen:
            return False
        self.error = err
        self._fill(ents, select)
        if err:
            self._set_msg(err)
        else:
            self._start_monitor(self.loc)
        return False

    def _row(self, e):
        size = self.icon_size()
        name = e.name if (self.st["show_ext"] or e.is_dir) else split_ext(e.name)[0]
        small, big = self._pixbufs(e, size)
        fg = FG_DIM if (e.hidden or self.app.is_cut(e.path)) else FG_NORMAL
        x1 = x2s = ""
        x2 = 0.0
        if self.mode == "trash" and e.extra:
            x1 = os.path.dirname(e.extra["orig"])
            x2 = e.extra["deleted"]
            x2s = fmt_date(x2)
        elif self.mode == "search":
            x1 = os.path.dirname(e.path)
        return [name, e.path, e.is_dir, -1 if e.size is None else e.size, e.mtime, type_name(e.name, e.is_dir),
                small, big, "" if e.is_dir else fmt_kb(e.size), fmt_date(e.mtime), x1, x2s, "", x2, fg, e.kind,
                e.name]

    def icon_size(self):
        if self.mode == "trash" or self.view_mode == "details":
            return 20
        return VIEW_SIZES.get(self.view_mode, 48)

    def _kind_icon(self, path, kind):
        if kind == "folder":
            return USER_DIR_KIND.get(path, "folder")
        return kind

    def _pixbufs(self, e, size):
        kind = self._kind_icon(e.path, e.kind)
        small = None
        big = None
        if kind == "app":
            try:
                info = Gio.DesktopAppInfo.new_from_filename(e.path)
                gi_ = info.get_icon() if info else None
            except Exception:
                gi_ = None
            small = theme_pixbuf(gi_, 20) if gi_ else None
            big = theme_pixbuf(gi_, size) if gi_ else None
        if small is None:
            small = place_pixbuf(kind, 20)
        if big is None:
            big = place_pixbuf(kind, size) if size != 20 else small
        if size >= 48 and self.st["thumbnails"] and e.kind in ("image", "video") and self.mode != "trash":
            tb = self.app.thumbs.get(e.path, e.mtime, size, e.kind, self._thumb_ready)
            if tb is not None:
                big = tb
        return small, big

    def _thumb_ready(self, path, pb):
        it = self.iters.get(path)
        if it is not None and self.model.iter_is_valid(it):
            n = VIEW_SIZES.get(self.view_mode, 48)
            if self.view_mode != "details" and max(pb.get_width(), pb.get_height()) <= n + 2:
                self.model.set_value(it, C_BIG, pb)
        return False

    def _repixbuf(self):
        size = self.icon_size()
        for row in self.model:
            path = row[C_PATH]
            try:
                e = Entry(path)
            except Exception:
                continue
            small, big = self._pixbufs(e, size)
            row[C_ICON] = small
            row[C_BIG] = big

    def _fill(self, ents, select=None, keep_scroll=None):
        self.tree.set_model(None)
        self.iview.set_model(None)
        self.model.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID, Gtk.SortType.ASCENDING)
        self.model.clear()
        self.iters = {}
        for e in ents:
            self.iters[e.path] = self.model.append(self._row(e))
        self.apply_sort()
        self.tree.set_model(self.model)
        self.iview.set_model(self.model)
        self.count = len(ents)
        if not ents and not self.error:
            if self.mode == "trash":
                self._set_msg("Recycle Bin is empty.")
            elif self.mode == "search":
                self._set_msg("" if self.searching else "No items match your search.")
            else:
                self._set_msg("This folder is empty.")
        elif not self.error:
            self._set_msg("")
        if select:
            self.select_paths(select)
        if keep_scroll is not None:
            sw = self.sw_details if self.cur_view() == "details" else self.sw_icons
            GLib.idle_add(lambda: sw.get_vadjustment().set_value(keep_scroll) and False)
        self.win.on_items(self)

    def _set_msg(self, text):
        for m in (self.msg_details, self.msg_icons):
            m.set_text(text)
            m.set_visible(bool(text))

    def refresh(self):
        if self.mode in PAGE_MODES:
            self.load()
            return
        sel = self.selected_paths()
        sw = self.sw_details if self.cur_view() == "details" else self.sw_icons
        scroll = sw.get_vadjustment().get_value()
        if self.mode == "trash":
            self._fill(trash_items(), sel, scroll)
            return
        if self.mode == "search":
            self._start_search()
            return
        try:
            ents = list_dir(self.loc, self.st["show_hidden"])
            self.error = None
        except OSError as e:
            ents = []
            self.error = e.strerror or str(e)
        self._fill(ents, sel, scroll)
        if self.error:
            self._set_msg(self.error)

    # ---------------- monitoring
    def _start_monitor(self, path):
        try:
            gf = Gio.File.new_for_path(path)
            self.monitor = gf.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.set_rate_limit(400)
            self.monitor.connect("changed", self._changed)
        except GLib.Error:
            self.monitor = None

    def _stop_monitor(self):
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None

    def _changed(self, *a):
        if self.refresh_pending or self._editing():
            return
        self.refresh_pending = True

        def go():
            self.refresh_pending = False
            if not self._editing():
                self.refresh()
            return False
        GLib.timeout_add(250, go)

    def _editing(self):
        return getattr(self, "_rename_path", None) is not None

    # ---------------- search
    def set_search(self, q):
        q = q.strip()
        if q == self.query:
            return
        if self.mode not in ("folder", "search"):
            if not q:
                return
        self.query = q
        self.load()
        self.win.on_location(self)

    def _cancel_search(self):
        if self.search_cancel:
            self.search_cancel.set()
            self.search_cancel = None
        self.searching = False

    def _start_search(self):
        import fnmatch
        self._cancel_search()
        cancel = threading.Event()
        self.search_cancel = cancel
        self.searching = True
        gen = self.gen
        root = self.loc
        q = self.query.casefold()
        show_hidden = self.st["show_hidden"]
        wild = any(ch in q for ch in "*?[")
        words = q.split()
        self._fill([])
        self._set_msg("Searching...")
        self.win.on_items(self)

        def match(name):
            n = name.casefold()
            if wild:
                return fnmatch.fnmatch(n, q)
            return all(w in n for w in words)

        def work():
            batch = []
            found = 0
            last = time.time()
            stack = [root]
            while stack and not cancel.is_set():
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for de in it:
                            if not show_hidden and de.name.startswith("."):
                                continue
                            try:
                                isd = de.is_dir(follow_symlinks=False)
                            except OSError:
                                isd = False
                            if isd:
                                stack.append(de.path)
                            if match(de.name):
                                try:
                                    st = de.stat()
                                except OSError:
                                    st = None
                                batch.append(Entry(de.path, st, de.name))
                                found += 1
                except OSError:
                    pass
                if batch and (time.time() - last > 0.3 or len(batch) > 300):
                    GLib.idle_add(self._search_batch, gen, batch)
                    batch = []
                    last = time.time()
                if found >= 5000:
                    break
            GLib.idle_add(self._search_batch, gen, batch, True)
        threading.Thread(target=work, daemon=True).start()

    def _search_batch(self, gen, batch, done=False):
        if gen != self.gen or self.mode != "search":
            return False
        for e in batch:
            if e.path not in self.iters:
                self.iters[e.path] = self.model.append(self._row(e))
        self.count = len(self.iters)
        if done:
            self.searching = False
            self._set_msg("" if self.count else "No items match your search.")
        else:
            self._set_msg("")
        self.win.on_items(self)
        return False

    # ---------------- selection
    def selected_paths(self):
        if self.mode in PAGE_MODES:
            return []
        out = []
        if self.cur_view() == "details":
            model, rows = self.tree.get_selection().get_selected_rows()
            for r in rows:
                out.append(self.model[r][C_PATH])
        else:
            for r in self.iview.get_selected_items():
                out.append(self.model[r][C_PATH])
        return out

    def selected_entries(self):
        out = []
        if self.mode == "trash":
            sel = set(self.selected_paths())
            return [e for e in trash_items() if e.path in sel]
        for p in self.selected_paths():
            out.append(Entry(p))
        return out

    def select_paths(self, paths, focus=True):
        paths = [p for p in (paths or []) if p in self.iters]
        tsel = self.tree.get_selection()
        tsel.unselect_all()
        self.iview.unselect_all()
        first = None
        for p in paths:
            it = self.iters[p]
            if not self.model.iter_is_valid(it):
                continue
            tp = self.model.get_path(it)
            tsel.select_path(tp)
            self.iview.select_path(tp)
            if first is None:
                first = tp
        if first is not None:
            if self.cur_view() == "details":
                self.tree.set_cursor(first, None, False)
                tsel.unselect_all()
                for p in paths:
                    if p in self.iters:
                        tsel.select_iter(self.iters[p])
                self.tree.scroll_to_cell(first, None, True, 0.3, 0)
            else:
                self.iview.set_cursor(first, None, False)
                self.iview.scroll_to_path(first, True, 0.3, 0)
        if focus:
            self.focus_view()

    def select_all(self):
        if self.cur_view() == "details":
            self.tree.get_selection().select_all()
        elif self.cur_view() == "icons":
            self.iview.select_all()

    def select_none(self):
        self.tree.get_selection().unselect_all()
        self.iview.unselect_all()

    def invert_selection(self):
        sel = set(self.selected_paths())
        self.select_paths([r[C_PATH] for r in self.model if r[C_PATH] not in sel], focus=False)

    def focus_view(self):
        v = self.cur_view()
        if v == "details":
            self.tree.grab_focus()
        elif v == "icons":
            self.iview.grab_focus()

    def _sel_changed(self):
        self.win.on_selection(self)

    # ---------------- typing to jump
    def _key(self, w, ev):
        if self._editing():
            return False
        st = ev.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK)
        if st:
            return False
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.open_selected()
            return True
        if ev.keyval == Gdk.KEY_BackSpace:
            self.back()
            return True
        ch = Gdk.keyval_to_unicode(ev.keyval)
        now = time.time()
        if ch and chr(ch) == " " and not (self.typeahead and now - self.typeahead_t <= 1.0):
            return True
        if ch and chr(ch).isprintable():
            if now - self.typeahead_t > 1.0:
                self.typeahead = ""
            self.typeahead_t = now
            self.typeahead += chr(ch).casefold()
            for r in self.model:
                if r[C_FNAME].casefold().startswith(self.typeahead):
                    self.select_paths([r[C_PATH]])
                    break
            return True
        return False

    # ---------------- mouse
    def _path_at(self, w, x, y):
        if w is self.tree:
            res = self.tree.get_path_at_pos(int(x), int(y))
            return res[0] if res else None
        return self.iview.get_path_at_pos(int(x), int(y))

    def _button(self, w, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        tp = self._path_at(w, ev.x, ev.y)
        if ev.button == 8:
            self.back()
            return True
        if ev.button == 9:
            self.forward()
            return True
        if ev.button == 2 and tp is not None:
            p = self.model[tp][C_PATH]
            if self.model[tp][C_DIR]:
                self.win.new_tab(p, switch=False)
            return True
        if ev.button != 3:
            if tp is None and ev.button == 1 and not (ev.state & (Gdk.ModifierType.CONTROL_MASK |
                                                                  Gdk.ModifierType.SHIFT_MASK)):
                self.select_none()
            return False
        w.grab_focus()
        if tp is None:
            self.select_none()
            self.win.bg_menu(self, w, ev)
            return True
        selected = (self.tree.get_selection().path_is_selected(tp) if w is self.tree
                    else self.iview.path_is_selected(tp))
        if not selected:
            if w is self.tree:
                self.tree.get_selection().unselect_all()
                self.tree.set_cursor(tp, None, False)
            else:
                self.iview.unselect_all()
                self.iview.select_path(tp)
                self.iview.set_cursor(tp, None, False)
        self.win.item_menu(self, w, ev)
        return True

    # ---------------- open
    def open_selected(self, new_tab=False):
        paths = self.selected_paths()
        if not paths:
            return
        if self.mode == "trash":
            self.win.properties(self)
            return
        dirs = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if not os.path.isdir(p)]
        if dirs:
            if len(dirs) == 1 and not files and not new_tab:
                self.navigate(dirs[0])
                return
            for d in dirs:
                self.win.new_tab(d, switch=False)
        for f in files[:20]:
            self.open_file(f)

    def open_file(self, path):
        win = self.win
        if path.lower().endswith(".desktop"):
            try:
                info = Gio.DesktopAppInfo.new_from_filename(path)
                if info:
                    info.launch([], launch_ctx())
                    return
            except GLib.Error:
                pass
        try:
            mode = os.stat(path).st_mode
        except OSError:
            mode = 0
        ct = content_type(path) or ""
        is_script = (mode & 0o111) and (path.endswith(".sh") or Gio.content_type_is_a(ct, "text/plain")
                                       or ct in ("application/x-shellscript", "application/x-executable",
                                                 "application/x-pie-executable", "application/vnd.appimage"))
        if is_script:
            def pick(k):
                if k == "term":
                    open_terminal(os.path.dirname(path), win, run=path)
                elif k == "run":
                    try:
                        subprocess.Popen([path], cwd=os.path.dirname(path), start_new_session=True)
                    except OSError as e:
                        info_dialog(win, "Couldn't run the file", str(e))
                elif k == "open":
                    self._open_default(path, force_text=True)
            choices = [("term", "Run in Terminal", "See the output in a terminal window"),
                       ("run", "Run", "Run it in the background")]
            if Gio.content_type_is_a(ct, "text/plain") or path.endswith(".sh"):
                choices.append(("open", "Open", "Open it in the text editor"))
            ChoiceDialog(win, "This file is executable",
                         f"What do you want to do with <b>{esc(os.path.basename(path))}</b>?", choices, pick)
            return
        self._open_default(path)

    def _open_default(self, path, force_text=False):
        ct = content_type(path)
        app = None
        try:
            if force_text:
                app = Gio.AppInfo.get_default_for_type("text/plain", False)
            elif ct:
                app = Gio.AppInfo.get_default_for_type(ct, False)
        except Exception:
            app = None
        if app is None:
            choose_app(self.win, path)
            return
        launch_with(app, [path], self.win)

    # ---------------- rename (inline)
    def start_rename(self, path=None):
        if self.mode not in ("folder", "search"):
            return
        if path is None:
            sel = self.selected_paths()
            if not sel:
                return
            path = sel[0]
        it = self.iters.get(path)
        if it is None:
            return
        tp = self.model.get_path(it)
        self._rename_path = path
        if self.cur_view() == "details":
            self.name_cell.set_property("editable", True)
            self.tree.scroll_to_cell(tp, None, False, 0, 0)
            self.tree.set_cursor_on_cell(tp, self.name_col, self.name_cell, True)
        else:
            self.icon_cell.set_property("editable", True)
            self.iview.scroll_to_path(tp, False, 0, 0)
            self.iview.set_cursor(tp, self.icon_cell, True)

    def _edit_started(self, cell, editable, tp):
        if isinstance(editable, Gtk.Entry):
            name = self.model[tp][C_FNAME]
            editable.set_text(name)
            stem, _ = split_ext(name, self.model[tp][C_DIR])
            GLib.idle_add(lambda: editable.select_region(0, len(stem)) or False)

    def _edit_canceled(self, cell):
        cell.set_property("editable", False)
        self._rename_path = None

    def _edited(self, cell, tp, new):
        cell.set_property("editable", False)
        path = self._rename_path
        self._rename_path = None
        if not path:
            return
        self.rename(path, new)

    def rename(self, path, new):
        new = new.strip()
        old = os.path.basename(path)
        if not new or new == old:
            return
        if "/" in new or new in (".", ".."):
            info_dialog(self.win, "Invalid name", "A file name can't contain / or be . or ..")
            return
        folder = os.path.dirname(path)
        dst = os.path.join(folder, new)
        if os.path.lexists(dst) and new.casefold() != old.casefold():
            info_dialog(self.win, "Rename",
                        f"There is already a file or folder named \"{new}\" in this location.")
            return

        def do():
            try:
                os.rename(path, dst)
            except OSError as e:
                info_dialog(self.win, "Couldn't rename", e.strerror or str(e))
                return
            self.app.rename_pinned(path, dst)
            self.refresh()
            self.select_paths([dst])
        _, oe = split_ext(old, os.path.isdir(path))
        _, ne = split_ext(new, os.path.isdir(path))
        if not os.path.isdir(path) and oe.lower() != ne.lower() and oe:
            confirm(self.win, "Rename",
                    "If you change a file name extension, the file might become unusable.\n\n"
                    "Are you sure you want to change it?", "Yes", lambda ok: ok and do(), no="No")
        else:
            do()

    # ---------------- drag and drop
    def _setup_dnd(self, w):
        targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]
        acts = Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK
        w.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, targets, acts)
        w.enable_model_drag_dest(targets, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        w.connect_after("drag-begin", self._drag_begin)
        w.connect("drag-data-get", self._drag_get)
        w.connect("drag-data-received", self._drag_recv)
        w.connect("drag-data-delete", lambda w, c: w.stop_emission_by_name("drag-data-delete"))

    def _drag_begin(self, w, ctx):
        paths = self.selected_paths()
        if not paths:
            return
        it = self.iters.get(paths[0])
        pb = self.model.get_value(it, C_BIG) if it is not None else None
        if pb is None or pb.get_width() < 40:
            e = Entry(paths[0])
            pb = place_pixbuf(self._kind_icon(e.path, e.kind), 64)
        if max(pb.get_width(), pb.get_height()) > 96:
            sc = 96 / max(pb.get_width(), pb.get_height())
            pb = pb.scale_simple(int(pb.get_width() * sc), int(pb.get_height() * sc), GdkPixbuf.InterpType.BILINEAR)
        w_, h_ = pb.get_width() + 14, pb.get_height() + 14
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w_, h_)
        cr = cairo.Context(surf)
        cr.set_source_rgba(0.3, 0.76, 1.0, 0.25)
        _rr(cr, 0, 0, w_, h_, 6)
        cr.fill()
        Gdk.cairo_set_source_pixbuf(cr, pb, 7, 7)
        cr.paint_with_alpha(0.9)
        if len(paths) > 1:
            txt = str(len(paths))
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12)
            ext = cr.text_extents(txt)
            bw = max(20, ext.width + 12)
            cr.set_source_rgba(*hexc(C_ACCENT))
            _rr(cr, w_ - bw - 2, 2, bw, 20, 10)
            cr.fill()
            cr.set_source_rgba(0, 0, 0, 1)
            cr.move_to(w_ - bw - 2 + (bw - ext.width) / 2 - ext.x_bearing, 16)
            cr.show_text(txt)
        Gtk.drag_set_icon_surface(ctx, surf)

    def _drag_get(self, w, ctx, data, info, time_):
        paths = self.selected_paths()
        data.set_uris([path_to_uri(p) for p in paths])
        w.stop_emission_by_name("drag-data-get")

    def _drag_recv(self, w, ctx, x, y, data, info, time_):
        w.stop_emission_by_name("drag-data-received")
        uris = data.get_uris() or []
        paths = [p for p in (uri_to_path(u) for u in uris) if p]
        dest = None
        if w is self.tree:
            res = self.tree.get_dest_row_at_pos(x, y)
            if res:
                tp, pos = res
                if self.model[tp][C_DIR] and pos in (Gtk.TreeViewDropPosition.INTO_OR_BEFORE,
                                                     Gtk.TreeViewDropPosition.INTO_OR_AFTER):
                    dest = self.model[tp][C_PATH]
        else:
            ok, tp, pos = self.iview.get_dest_item_at_pos(x, y)
            if ok and tp is not None and self.model[tp][C_DIR] and pos in (
                    Gtk.IconViewDropPosition.DROP_INTO,):
                dest = self.model[tp][C_PATH]
        if dest is None:
            dest = self.loc if self.mode == "folder" else None
        if dest in paths:
            dest = None
        ctx.finish(dest is not None and bool(paths), False, time_)
        if dest is None or not paths:
            return
        if dest == LOC_TRASH:
            self.app.delete(self.win, paths)
            return
        self.win.drop_files(paths, dest, w)


# ----------------------------------------------------------------------------
# Home and This PC pages (FolderView methods)
# ----------------------------------------------------------------------------
def _clear(sw):
    child = sw.get_child()
    if child:
        sw.remove(child)
        child.destroy()


def _tile_menu(view, tile, path, ev, extra=None):
    spec = [
        {"label": "Open", "icon": "open", "cb": lambda: view.navigate(path)},
        {"label": "Open in new tab", "icon": "newtab", "cb": lambda: view.win.new_tab(path, switch=False)},
        {"label": "Open in new window", "icon": "open", "cb": lambda: view.app.new_window(path)},
        None,
    ]
    if extra:
        spec += extra + [None]
    if path in view.st["quick_access"]:
        spec.append({"label": "Unpin from Quick access", "icon": "pin", "cb": lambda: view.app.unpin(path)})
    else:
        spec.append({"label": "Pin to Quick access", "icon": "pin", "cb": lambda: view.app.pin(path)})
    spec += [
        {"label": "Copy as path", "icon": "link", "cb": lambda: view.app.copy_text(path)},
        {"label": "Open in Terminal", "icon": "terminal", "cb": lambda: open_terminal(path, view.win)},
        None,
        {"label": "Properties", "icon": "properties", "accel": "Alt+Enter",
         "cb": lambda: PropertiesDialog(view.win, [path]).show_all()},
    ]
    m = build_menu(spec)
    view.win._menu = m
    popup_menu(m, tile, ev)


def _tile_events(view, tile, path, extra=None):
    def press(w, ev):
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 3:
            _tile_menu(view, w, path, ev, extra)
            return True
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 2:
            view.win.new_tab(path, switch=False)
            return True
        return False
    tile.connect("button-press-event", press)
    tile.connect("clicked", lambda *_: view.navigate(path))
    # tiles accept drops too
    tile.drag_dest_set(Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                       Gdk.DragAction.COPY | Gdk.DragAction.MOVE)

    def recv(w, ctx, x, y, data, info, t):
        paths = [p for p in (uri_to_path(u) for u in (data.get_uris() or [])) if p]
        if paths:
            view.win.drop_files(paths, path, w)
    tile.connect("drag-data-received", recv)


def _build_home(self):
    _clear(self.home_box)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_bottom(20)
    box.pack_start(section("Quick access"), False, False, 0)
    fb = flow()
    pins = [p for p in self.st["quick_access"] if os.path.isdir(p)]
    for p in pins:
        kind = USER_DIR_KIND.get(p, "folder")
        if p == USER_DIRS[0][1]:
            kind = "desktop"
        sub = "Stored locally" if is_inside(p, HOME) else os.path.dirname(p)
        t = Tile(kind, disp_name(p), sub, width=230)
        _tile_events(self, t, p)
        fb.add(t)
    if not pins:
        fb.add(label("Pin folders here with right-click > Pin to Quick access.", "empty-msg"))
    box.pack_start(fb, False, False, 0)

    if self.st.get("show_recent", True):
        box.pack_start(section("Recent"), False, False, 8)
        files = recent_files(25)
        if not files:
            lb = label("After you've opened some files, we'll show the most recent ones here.", "empty-msg")
            lb.set_margin_start(22)
            box.pack_start(lb, False, False, 0)
        else:
            store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, str)
            for p in files:
                e = Entry(p)
                store.append([place_pixbuf(e.kind, 20), e.name, fmt_date(e.mtime), os.path.dirname(p), p])
            tv = Gtk.TreeView(model=store)
            add_cls(tv, "files")
            tv.set_enable_search(False)
            tv.set_margin_start(14)
            tv.set_margin_end(14)
            col = Gtk.TreeViewColumn("Name")
            pr = Gtk.CellRendererPixbuf()
            pr.set_padding(6, 5)
            col.pack_start(pr, False)
            col.add_attribute(pr, "pixbuf", 0)
            tr = Gtk.CellRendererText()
            tr.set_property("ellipsize", Pango.EllipsizeMode.END)
            col.pack_start(tr, True)
            col.add_attribute(tr, "text", 1)
            col.set_expand(True)
            col.set_min_width(240)
            tv.append_column(col)
            for title, cid, w in (("Date modified", 2, 170), ("File location", 3, 320)):
                r = Gtk.CellRendererText()
                r.set_property("ellipsize", Pango.EllipsizeMode.END)
                r.set_property("foreground-rgba", Gdk.RGBA(1, 1, 1, 0.786))
                r.set_padding(8, 0)
                c = Gtk.TreeViewColumn(title, r, text=cid)
                c.set_fixed_width(w)
                c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
                c.set_resizable(True)
                tv.append_column(c)
            tv.connect("row-activated", lambda t, tp, c: self.open_file(store[tp][4]))

            def press(w, ev):
                if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 3:
                    res = tv.get_path_at_pos(int(ev.x), int(ev.y))
                    if not res:
                        return True
                    tv.set_cursor(res[0], None, False)
                    p = store[res[0]][4]
                    m = build_menu([
                        {"label": "Open", "icon": "open", "cb": lambda: self.open_file(p)},
                        {"label": "Open file location", "icon": "folder",
                         "cb": lambda: self.navigate(os.path.dirname(p), select=[p])},
                        None,
                        {"label": "Copy as path", "icon": "link", "cb": lambda: self.app.copy_text(p)},
                        {"label": "Properties", "icon": "properties",
                         "cb": lambda: PropertiesDialog(self.win, [p]).show_all()},
                    ])
                    self.win._menu = m
                    popup_menu(m, tv, ev)
                    return True
                return False
            tv.connect("button-press-event", press)
            box.pack_start(tv, False, False, 0)
    self.home_box.add(box)
    self.home_box.show_all()


def _build_pc(self):
    _clear(self.pc_box)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_bottom(20)
    box.pack_start(section("Folders"), False, False, 0)
    fb = flow()
    folders = [(os.path.basename(HOME), HOME, "homefolder")] + [(n, p, k) for n, p, k in USER_DIRS
                                                                  if os.path.isdir(p)]
    for name, p, k in folders:
        kind = "desktop" if k == "desktop" else k
        t = Tile(kind, name, None, width=220)
        _tile_events(self, t, p)
        fb.add(t)
    box.pack_start(fb, False, False, 0)
    box.pack_start(section("Devices and drives"), False, False, 8)
    fb2 = flow()
    for d in drives():
        if d["mounted"] and d["total"]:
            used = 1 - d["free"] / d["total"] if d["total"] else 0
            t = Tile(d["kind"], drive_label(d), f"{fmt_size(d['free'])} free of {fmt_size(d['total'])}",
                     bar=used, width=280)
        else:
            t = Tile(d["kind"], drive_label(d), "Click to open" if not d["mounted"] else d["fs"], width=280)
        if d["mounted"]:
            extra = []
            mnt = d["path"]
            if mnt not in ("/", "/boot/firmware", "/boot") and d["dev"]:
                extra.append({"label": "Eject", "icon": "eject", "cb": lambda m=mnt: self.app.eject(self.win, m)})
            _tile_events(self, t, d["path"], extra)
        else:
            vol = d["volume"]
            t.connect("clicked", lambda w, v=vol: self.app.mount_volume(self.win, v, self))
        fb2.add(t)
    box.pack_start(fb2, False, False, 0)
    mounts = network_mounts()
    if mounts:
        box.pack_start(section("Network locations"), False, False, 8)
        fb3 = flow()
        for m in mounts:
            t = Tile("netshare", m["name"], m["uri"], width=280)
            _tile_events(self, t, m["path"], [{"label": "Disconnect", "icon": "eject",
                                               "cb": lambda p=m["path"]: self.app.eject(self.win, p)}])
            fb3.add(t)
        box.pack_start(fb3, False, False, 0)
    self.pc_box.add(box)
    self.pc_box.show_all()


FolderView._build_home = _build_home
FolderView._build_pc = _build_pc


# ----------------------------------------------------------------------------
# Network pages
# ----------------------------------------------------------------------------
def _menu_at(view, widget, ev, spec):
    m = build_menu(spec)
    view.win._menu = m
    popup_menu(m, widget, ev)


def _right_click(widget, cb):
    def press(w, ev):
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 3:
            cb(w, ev)
            return True
        return False
    widget.connect("button-press-event", press)


def _build_net(self):
    _clear(self.net_box)
    gen = self.gen
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_bottom(20)
    srv = server_name()

    # --- shared from this Pi
    head = section(f"Shared from this Pi  (\\\\{srv})")
    sb = tb_button("share", "Share a folder", lambda w: self.app.pick_and_share(self.win))
    head.pack_end(sb, False, False, 0)
    box.pack_start(head, False, False, 0)
    fb = flow()
    if not samba_installed():
        lb = label("Sharing folders needs Samba. Install it with:  sudo apt install samba", "empty-msg")
        lb.set_selectable(True)
        lb.set_margin_start(22)
        box.pack_start(lb, False, False, 0)
        sb.set_sensitive(False)
    else:
        shares = list_shares()
        for s in shares:
            sub = s["path"] + ("   (read-only)" if s["readonly"] else "")
            t = Tile("netshare", s["name"], sub, width=280)
            t.set_tooltip_text(f"\\\\{srv}\\{s['name']}")
            t.connect("clicked", lambda w, p=s["path"]: self.navigate(p))

            def menu(w, ev, s=s):
                spec = [
                    {"label": "Open", "icon": "open", "cb": lambda: self.navigate(s["path"])},
                    {"label": "Open in new tab", "icon": "newtab", "cb": lambda: self.win.new_tab(s["path"], switch=False)},
                    None,
                    {"label": "Copy network path", "icon": "link",
                     "cb": lambda: self.app.copy_text(f"\\\\{srv}\\{s['name']}")},
                    {"label": "Stop sharing", "icon": "close", "sensitive": s["ours"],
                     "cb": lambda: self.app.unshare(self.win, s["name"])},
                    None,
                    {"label": "Properties", "icon": "properties",
                     "cb": lambda: PropertiesDialog(self.win, [s["path"]]).show_all()},
                ]
                _menu_at(self, w, ev, spec)
            _right_click(t, menu)
            fb.add(t)
        if not shares:
            fb.add(label("No folders are shared yet. Right-click a folder > Share on network.", "empty-msg"))
        box.pack_start(fb, False, False, 0)

    # --- connected network folders
    mounts = network_mounts()
    if mounts:
        box.pack_start(section("Connected network folders"), False, False, 8)
        fb3 = flow()
        for m in mounts:
            t = Tile("netshare", m["name"], m["uri"], width=280)
            t.connect("clicked", lambda w, p=m["path"]: self.navigate(p))
            _right_click(t, lambda w, ev, m=m: _menu_at(self, w, ev, [
                {"label": "Open", "icon": "open", "cb": lambda: self.navigate(m["path"])},
                {"label": "Open in new tab", "icon": "newtab", "cb": lambda: self.win.new_tab(m["path"], switch=False)},
                {"label": "Pin to Quick access", "icon": "pin", "cb": lambda: self.app.pin(m["path"])},
                None,
                {"label": "Disconnect", "icon": "eject", "cb": lambda: self.app.eject(self.win, m["path"])},
            ]))
            fb3.add(t)
        box.pack_start(fb3, False, False, 0)

    # --- computers on the network
    box.pack_start(section("Computers and devices"), False, False, 8)
    fb2 = flow()
    status = label("Looking for computers on your network...", "empty-msg")
    status.set_margin_start(22)
    box.pack_start(status, False, False, 0)
    box.pack_start(fb2, False, False, 0)

    # --- connect by address
    box.pack_start(section("Open a network folder"), False, False, 8)
    row = Gtk.Box(spacing=8)
    row.set_margin_start(22)
    row.set_margin_end(22)
    ent = Gtk.Entry()
    add_cls(ent, "field")
    ent.set_placeholder_text("\\\\server\\share   or   smb://server/share")
    ent.set_size_request(380, -1)
    go = Gtk.Button(label="Open")
    add_cls(go, "primary")
    act = lambda *_: ent.get_text().strip() and self.app.open_network_path(self.win, self, ent.get_text().strip())  # noqa: E731
    ent.connect("activate", act)
    go.connect("clicked", act)
    row.pack_start(ent, False, False, 0)
    row.pack_start(go, False, False, 0)
    box.pack_start(row, False, False, 0)
    hint = label("You'll be asked for a username and password if the folder needs one.", "ter")
    hint.set_margin_start(22)
    hint.set_margin_top(4)
    box.pack_start(hint, False, False, 0)

    self.net_box.add(box)
    self.net_box.show_all()

    def found(items, err):
        if gen != self.gen or self.mode != "net":
            return False
        for name, uri in items:
            t = Tile("netpc", name, uri_host(uri) if uri_host(uri) != name else None, width=240)
            t.connect("clicked", lambda w, u=uri: self.navigate(host_loc(u)))
            fb2.add(t)
        fb2.show_all()
        if items:
            status.hide()
        else:
            status.set_text("No computers found. If you know the address, type it below." +
                            (f"\n({err})" if err else ""))
        return False

    def work():
        items, err = [], None
        try:
            f = Gio.File.new_for_uri("network:///")
            en = f.enumerate_children("standard::display-name,standard::target-uri,standard::name",
                                      Gio.FileQueryInfoFlags.NONE, None)
            for info in en:
                uri = info.get_attribute_string("standard::target-uri") or ""
                if not uri or uri.startswith("network:"):
                    continue
                items.append((info.get_display_name(), uri))
        except GLib.Error as e:
            err = e.message
        items.sort(key=lambda x: x[0].casefold())
        GLib.idle_add(found, items, err)
    threading.Thread(target=work, daemon=True).start()


def _build_nethost(self):
    _clear(self.net_box)
    gen = self.gen
    uri = loc_host_uri(self.loc)
    host = uri_host(uri)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_bottom(20)
    box.pack_start(section(f"Shared folders on {host}"), False, False, 0)
    status = label("Loading...", "empty-msg")
    status.set_margin_start(22)
    box.pack_start(status, False, False, 0)
    fb = flow()
    box.pack_start(fb, False, False, 0)
    self.net_box.add(box)
    self.net_box.show_all()

    def show(items, err):
        if gen != self.gen:
            return False
        for name, suri in items:
            t = Tile("netshare", name, f"\\\\{host}\\{name}", width=260)
            t.connect("clicked", lambda w, u=suri: self.app.open_uri(self.win, self, u))
            _right_click(t, lambda w, ev, u=suri, n=name: _menu_at(self, w, ev, [
                {"label": "Open", "icon": "open", "cb": lambda: self.app.open_uri(self.win, self, u)},
                {"label": "Open in new tab", "icon": "newtab",
                 "cb": lambda: self.app.open_uri(self.win, self.win.new_tab(LOC_NET, switch=True), u)},
                {"label": "Copy network path", "icon": "link", "cb": lambda: self.app.copy_text(f"\\\\{host}\\{n}")},
            ]))
            fb.add(t)
        fb.show_all()
        if items:
            status.hide()
        else:
            status.set_text(err or "This computer isn't sharing any folders.")
        return False

    def need_mount(err_msg):
        if gen != self.gen:
            return False
        gf = Gio.File.new_for_uri(uri)

        def done(f, res):
            try:
                f.mount_enclosing_volume_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    show([], f"Couldn't connect to {host}: {e.message}")
                    return
            start(False)
        gf.mount_enclosing_volume(Gio.MountMountFlags.NONE, NetMountOp(self.win), None, done)
        return False

    def start(allow_mount=True):
        def work():
            items, err = [], None
            try:
                f = Gio.File.new_for_uri(uri)
                en = f.enumerate_children("standard::display-name,standard::target-uri,standard::name,"
                                          "standard::type", Gio.FileQueryInfoFlags.NONE, None)
                for info in en:
                    name = info.get_display_name()
                    if name.endswith("$"):
                        continue
                    target = info.get_attribute_string("standard::target-uri") or \
                        uri.rstrip("/") + "/" + urllib.parse.quote(info.get_name())
                    items.append((name, target))
            except GLib.Error as e:
                if allow_mount and e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_MOUNTED):
                    GLib.idle_add(need_mount, e.message)
                    return
                err = f"Couldn't open {host}: {e.message}"
            items.sort(key=lambda x: x[0].casefold())
            GLib.idle_add(show, items, err)
        threading.Thread(target=work, daemon=True).start()
    start()


FolderView._build_net = _build_net
FolderView._build_nethost = _build_nethost


# ----------------------------------------------------------------------------
# Navigation pane
# ----------------------------------------------------------------------------
N_ICON, N_LABEL, N_TARGET, N_PIN = range(4)
DUMMY = "::dummy"
SEP = "::sep"


def has_subdirs(path, show_hidden):
    try:
        with os.scandir(path) as it:
            for de in it:
                if not show_hidden and de.name.startswith("."):
                    continue
                try:
                    if de.is_dir():
                        return True
                except OSError:
                    pass
    except OSError:
        pass
    return False


class NavPane(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.app = win.app
        self.syncing = False
        add_cls(self, "navpane")
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.store = Gtk.TreeStore(GdkPixbuf.Pixbuf, str, str, GdkPixbuf.Pixbuf)
        tv = Gtk.TreeView(model=self.store)
        tv.set_headers_visible(False)
        tv.set_enable_search(False)
        tv.set_show_expanders(True)
        tv.set_level_indentation(4)
        tv.set_row_separator_func(lambda m, it: m.get_value(it, N_TARGET) == SEP)
        col = Gtk.TreeViewColumn()
        pr = Gtk.CellRendererPixbuf()
        pr.set_padding(4, 6)
        col.pack_start(pr, False)
        col.add_attribute(pr, "pixbuf", N_ICON)
        tr = Gtk.CellRendererText()
        tr.set_property("ellipsize", Pango.EllipsizeMode.END)
        tr.set_padding(4, 0)
        col.pack_start(tr, True)
        col.add_attribute(tr, "text", N_LABEL)
        pin = Gtk.CellRendererPixbuf()
        pin.set_padding(8, 0)
        col.pack_end(pin, False)
        col.add_attribute(pin, "pixbuf", N_PIN)
        col.set_expand(True)
        tv.append_column(col)
        tv.connect("test-expand-row", self._expand)
        tv.get_selection().connect("changed", self._sel)
        tv.connect("button-press-event", self._press)
        tv.connect("key-press-event", self._keypress)
        tv.connect("focus-in-event", lambda *_: GLib.idle_add(self._resync) and False)
        self._kbd = False
        targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]
        tv.enable_model_drag_dest(targets, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        tv.connect("drag-data-received", self._drop)
        tv.connect("drag-motion", self._motion)
        self.tv = tv
        self.add(tv)
        self.rebuild()

    def rebuild(self):
        self.syncing = True
        self.store.clear()
        sh = self.app.settings["show_hidden"]
        self.store.append(None, [place_pixbuf("home", 16), "Home", LOC_HOME, None])
        self.store.append(None, [None, "", SEP, None])
        pin = glyph_pixbuf("pin-fill", 14, (1, 1, 1, 0.5))
        for p in self.app.settings["quick_access"]:
            if not os.path.isdir(p):
                continue
            kind = USER_DIR_KIND.get(p, "folder")
            if p == USER_DIRS[0][1]:
                kind = "desktop"
            it = self.store.append(None, [place_pixbuf(kind, 16), disp_name(p), p, pin])
            if has_subdirs(p, sh):
                self.store.append(it, [None, "", DUMMY, None])
        self.store.append(None, [None, "", SEP, None])
        pc = self.store.append(None, [place_pixbuf("pc", 16), "This PC", LOC_PC, None])
        for d in drives():
            if not d["mounted"]:
                continue
            it = self.store.append(pc, [place_pixbuf(d["kind"], 16), drive_label(d), d["path"], None])
            if has_subdirs(d["path"], sh):
                self.store.append(it, [None, "", DUMMY, None])
        net = self.store.append(None, [place_pixbuf("net", 16), "Network", LOC_NET, None])
        for m in network_mounts():
            self.store.append(net, [place_pixbuf("netshare", 16), m["name"], m["path"], None])
        self.store.append(None, [place_pixbuf("recycle-full" if trash_count() else "recycle", 16), "Recycle Bin",
                                 LOC_TRASH, None])
        self.syncing = False

    def _expand(self, tv, it, tp):
        child = self.store.iter_children(it)
        if child is None or self.store.get_value(child, N_TARGET) != DUMMY:
            return False
        path = self.store.get_value(it, N_TARGET)
        sh = self.app.settings["show_hidden"]
        subs = []
        try:
            with os.scandir(path) as itr:
                for de in itr:
                    if not sh and de.name.startswith("."):
                        continue
                    try:
                        if de.is_dir():
                            subs.append(de)
                    except OSError:
                        pass
        except OSError:
            pass
        subs.sort(key=lambda d: natural_key(d.name))
        self.syncing = True
        for de in subs[:400]:
            kind = USER_DIR_KIND.get(de.path, "folder")
            c = self.store.append(it, [place_pixbuf(kind, 16), de.name, de.path, None])
            if has_subdirs(de.path, sh):
                self.store.append(c, [None, "", DUMMY, None])
        self.store.remove(child)
        self.syncing = False
        return not subs

    def _resync(self):
        if self.win.cur:
            self.sync(self.win.cur.loc)
        return False

    def _keypress(self, tv, ev):
        if ev.keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Home, Gdk.KEY_End, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            self._kbd = True
            GLib.idle_add(self._kbd_off)
        elif ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            model, it = tv.get_selection().get_selected()
            if it is not None:
                self._go(model.get_value(it, N_TARGET), focus=True)
            return True
        return False

    def _kbd_off(self):
        self._kbd = False
        return False

    def _go(self, t, focus=False):
        if t in (SEP, DUMMY):
            return
        v = self.win.cur
        if v and v.loc != t:
            v.navigate(t)
        if focus and v:
            v.focus_view()

    def _sel(self, sel):
        if self.syncing or not self._kbd:
            return
        model, it = sel.get_selected()
        if it is None:
            return
        self._go(model.get_value(it, N_TARGET))

    def sync(self, loc):
        """Highlight the row for loc if it is visible."""
        self.syncing = True
        sel = self.tv.get_selection()
        found = []

        def walk(model, tp, it):
            if model.get_value(it, N_TARGET) == loc:
                parent = tp.copy()
                if parent.get_depth() == 1 or self.tv.row_expanded(self._parent_path(tp)):
                    found.append(tp.copy())
                    return True
            return False
        self.store.foreach(walk)
        if found:
            sel.select_path(found[0])
        else:
            sel.unselect_all()
        self.syncing = False

    @staticmethod
    def _parent_path(tp):
        p = tp.copy()
        p.up()
        return p

    def _press(self, tv, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        res = tv.get_path_at_pos(int(ev.x), int(ev.y))
        if ev.button == 1:
            if res:
                tp, col = res[0], res[1]
                area = tv.get_cell_area(tp, col)
                if ev.x >= area.x:
                    t = self.store[tp][N_TARGET]
                    GLib.idle_add(lambda: self._go(t) or False)
            return False
        if ev.button not in (2, 3):
            return False
        if not res:
            return True
        t = self.store[res[0]][N_TARGET]
        if t in (SEP, DUMMY):
            return True
        if ev.button == 2:
            self.win.new_tab(t, switch=False)
            return True
        if t == LOC_TRASH:
            m = build_menu([{"label": "Open", "icon": "open", "cb": lambda: self.win.cur.navigate(t)},
                            {"label": "Open in new tab", "icon": "newtab", "cb": lambda: self.win.new_tab(t)},
                            None,
                            {"label": "Empty Recycle Bin", "icon": "trash", "sensitive": trash_count() > 0,
                             "cb": lambda: self.app.empty_trash(self.win)}])
        elif t in (LOC_HOME, LOC_PC, LOC_NET):
            m = build_menu([{"label": "Open", "icon": "open", "cb": lambda: self.win.cur.navigate(t)},
                            {"label": "Open in new tab", "icon": "newtab", "cb": lambda: self.win.new_tab(t)},
                            {"label": "Open in new window", "icon": "open", "cb": lambda: self.app.new_window(t)}])
        else:
            m = None
            _tile_menu(self.win.cur, tv, t, ev)
            return True
        self.win._menu = m
        popup_menu(m, tv, ev)
        return True

    def _motion(self, tv, ctx, x, y, t):
        res = tv.get_dest_row_at_pos(x, y)
        if res:
            tp, pos = res
            tgt = self.store[tp][N_TARGET]
            if tgt in (SEP, DUMMY, LOC_HOME, LOC_PC, LOC_NET):
                Gdk.drag_status(ctx, 0, t)
                tv.set_drag_dest_row(None, Gtk.TreeViewDropPosition.INTO_OR_AFTER)
                return True
            tv.set_drag_dest_row(tp, Gtk.TreeViewDropPosition.INTO_OR_AFTER)
            Gdk.drag_status(ctx, Gdk.DragAction.MOVE if tgt == LOC_TRASH else ctx.get_suggested_action(), t)
            return True
        return False

    def _drop(self, tv, ctx, x, y, data, info, t):
        tv.stop_emission_by_name("drag-data-received")
        paths = [p for p in (uri_to_path(u) for u in (data.get_uris() or [])) if p]
        res = tv.get_dest_row_at_pos(x, y)
        ctx.finish(bool(res and paths), False, t)
        if not res or not paths:
            return
        tgt = self.store[res[0]][N_TARGET]
        if tgt == LOC_TRASH:
            self.app.delete(self.win, paths)
        elif tgt not in (SEP, DUMMY, LOC_HOME, LOC_PC, LOC_NET):
            self.win.drop_files(paths, tgt, tv)


# ----------------------------------------------------------------------------
# Address bar
# ----------------------------------------------------------------------------
class AddressBar(Gtk.Box):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.loc = None
        self.stack = Gtk.Stack()
        self.pack_start(self.stack, True, True, 0)
        # crumbs
        eb = Gtk.EventBox()
        add_cls(eb, "addr")
        self.crumb_row = Gtk.Box(spacing=0)
        self.crumb_row.set_margin_start(6)
        self.loc_icon = Icon("pc", 16, place=True)
        self.crumb_row.pack_start(self.loc_icon, False, False, 4)
        self.crumbs = Gtk.Box(spacing=0)
        self.crumb_row.pack_start(self.crumbs, False, False, 0)
        eb.add(self.crumb_row)
        eb.connect("button-press-event", self._bg_click)
        self.stack.add_named(eb, "crumbs")
        # entry
        self.entry = Gtk.Entry()
        add_cls(self.entry, "addr-entry")
        self.entry.connect("activate", self._go)
        self.entry.connect("key-press-event", self._ekey)
        self.entry.connect("focus-out-event", self._focus_out)
        self.comp_store = Gtk.ListStore(str)
        comp = Gtk.EntryCompletion()
        comp.set_model(self.comp_store)
        comp.set_text_column(0)
        comp.set_inline_completion(False)
        comp.set_popup_single_match(True)
        comp.set_minimum_key_length(1)
        self.entry.set_completion(comp)
        self.entry.connect("changed", self._complete)
        self._comp_dir = None
        self.stack.add_named(self.entry, "edit")

    def _focus_out(self, *a):
        def check():
            if self.win.get_focus() is not self.entry or not self.win.is_active():
                if self.win.get_focus() is not self.entry:
                    self.stack.set_visible_child_name("crumbs")
            return False
        GLib.timeout_add(200, check)
        return False

    def _bg_click(self, w, ev):
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS:
            self.edit()
            return True
        return False

    def edit(self):
        loc = self.loc
        if loc_host_uri(loc):
            self.entry.set_text("\\\\" + uri_host(loc_host_uri(loc)))
        else:
            self.entry.set_text(LOC_NAMES[loc] if loc in LOC_NAMES else loc)
        self._comp_dir = None
        self.stack.set_visible_child_name("edit")

        def focus():
            self.entry.grab_focus()
            self.entry.select_region(0, -1)
            return False
        GLib.idle_add(focus)

    def _ekey(self, w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.stack.set_visible_child_name("crumbs")
            self.win.cur.focus_view()
            return True
        return False

    def _complete(self, e):
        t = e.get_text()
        if not t.startswith(("/", "~")):
            return
        d = os.path.dirname(os.path.expanduser(t)) or "/"
        if d == self._comp_dir:
            return
        self._comp_dir = d
        self.comp_store.clear()
        try:
            subs = sorted((de.name for de in os.scandir(d) if de.is_dir() and not de.name.startswith(".")),
                          key=natural_key)[:300]
        except OSError:
            subs = []
        prefix = t[:len(t) - len(os.path.basename(t))]
        for s in subs:
            self.comp_store.append([prefix + s])

    def _go(self, *a):
        t = self.entry.get_text().strip()
        self.stack.set_visible_child_name("crumbs")
        v = self.win.cur
        low = t.lower()
        for k, n in LOC_NAMES.items():
            if low == n.lower():
                v.navigate(k)
                return
        if low in ("recycle bin", "trash", "trash:///", "trash:"):
            v.navigate(LOC_TRASH)
            return
        if low.startswith("file://"):
            t = uri_to_path(t) or t
        if t.startswith("\\\\") or low.startswith(("smb://", "//")):
            self.win.app.open_network_path(self.win, v, t)
            return
        p = os.path.abspath(os.path.expanduser(t)) if t else HOME
        if os.path.isdir(p):
            v.navigate(p)
        elif os.path.exists(p):
            v.navigate(os.path.dirname(p), select=[p])
        else:
            info_dialog(self.win, "File Explorer",
                        f"File Explorer can't find '{t}'. Check the spelling and try again.")
        v.focus_view()

    def set_location(self, loc, query=""):
        self.loc = loc
        for c in self.crumbs.get_children():
            self.crumbs.remove(c)
            c.destroy()
        self.loc_icon.set_icon(loc_kind(loc))
        if loc in LOC_NAMES:
            parts = [(LOC_NAMES[loc], loc)]
        elif loc_host_uri(loc):
            parts = [("Network", LOC_NET), (disp_name(loc), loc)]
        else:
            parts = []
            gv = None
            if is_gvfs_path(loc):
                cur = loc
                while cur and os.path.dirname(cur.rstrip("/")) and not os.path.dirname(cur).endswith("gvfs"):
                    cur = os.path.dirname(cur)
                gv = cur if os.path.dirname(cur).endswith("gvfs") else None
            if gv:
                parts.append(("Network", LOC_NET))
                parts.append((gvfs_label(gv), gv))
                cur = gv
                rel = os.path.relpath(loc, gv)
                if rel != ".":
                    for seg in rel.split("/"):
                        cur = os.path.join(cur, seg)
                        parts.append((seg, cur))
            elif is_inside(loc, HOME):
                rel = os.path.relpath(loc, HOME)
                parts.append((os.path.basename(HOME), HOME))
                cur = HOME
                if rel != ".":
                    for seg in rel.split("/"):
                        cur = os.path.join(cur, seg)
                        parts.append((seg, cur))
            else:
                parts.append(("System (/)", "/"))
                cur = "/"
                for seg in [s for s in loc.split("/") if s]:
                    cur = os.path.join(cur, seg)
                    parts.append((seg, cur))
            if not gv:
                parts.insert(0, ("This PC", LOC_PC))
        hidden = []
        if len(parts) > 6:
            hidden = parts[:-5]
            parts = parts[-5:]
            ob = Gtk.Button()
            add_cls(ob, "crumb-sep")
            ob.add(label("«", xalign=0.5))
            ob.connect("clicked", lambda w: self._list_menu(w, hidden))
            self.crumbs.pack_start(ob, False, False, 0)
        for i, (name, target) in enumerate(parts):
            if i > 0 or hidden:
                sep = Gtk.Button()
                add_cls(sep, "crumb-sep")
                sep.add(Icon("chevright", 10))
                prev = parts[i - 1][1] if i > 0 else hidden[-1][1]
                sep.connect("clicked", lambda w, p=prev: self._sub_menu(w, p))
                self.crumbs.pack_start(sep, False, False, 0)
            b = Gtk.Button()
            add_cls(b, "crumb")
            b.add(label(name))
            b.connect("clicked", lambda w, t=target: self.win.cur.navigate(t))
            self.crumbs.pack_start(b, False, False, 0)
        if query:
            sep = Gtk.Box()
            sep.pack_start(Icon("chevright", 10), False, False, 4)
            self.crumbs.pack_start(sep, False, False, 0)
            self.crumbs.pack_start(label(f"Search Results in {disp_name(loc)}"), False, False, 6)
        self.crumbs.show_all()
        self.stack.set_visible_child_name("crumbs")

    def _list_menu(self, w, items):
        m = build_menu([{"label": n, "place": "folder", "cb": (lambda t=t: self.win.cur.navigate(t))}
                        for n, t in reversed(items)])
        self.win._menu = m
        popup_menu(m, w)

    def _sub_menu(self, w, parent):
        sh = self.win.app.settings["show_hidden"]
        spec = []
        if parent == LOC_PC:
            for d in drives():
                if d["mounted"]:
                    spec.append({"label": drive_label(d), "place": d["kind"],
                                 "cb": (lambda t=d["path"]: self.win.cur.navigate(t))})
        else:
            try:
                subs = sorted((de for de in os.scandir(parent) if de.is_dir() and (sh or not de.name.startswith("."))),
                              key=lambda d: natural_key(d.name))
            except OSError:
                subs = []
            for de in subs[:60]:
                spec.append({"label": de.name, "place": USER_DIR_KIND.get(de.path, "folder"),
                             "cb": (lambda t=de.path: self.win.cur.navigate(t))})
        if not spec:
            spec = [{"label": "No subfolders", "sensitive": False}]
        m = build_menu(spec)
        self.win._menu = m
        popup_menu(m, w)


# ----------------------------------------------------------------------------
# Details pane
# ----------------------------------------------------------------------------
class DetailsPane(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        add_cls(self, "detailspane")
        self.win = win
        self.set_size_request(280, -1)
        self.img = Gtk.Image()
        self.img.set_size_request(240, 200)
        self.pack_start(self.img, False, False, 6)
        self.name = label("", "dp-name", wrap=True)
        self.pack_start(self.name, False, False, 0)
        self.sub = label("", "sec", wrap=True)
        self.pack_start(self.sub, False, False, 0)
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.grid.set_margin_top(10)
        self.pack_start(self.grid, False, False, 0)
        self.gen = 0

    def update(self, view):
        self.gen += 1
        for c in self.grid.get_children():
            self.grid.remove(c)
            c.destroy()
        paths = view.selected_paths() if view else []
        rows = []
        if not view:
            return
        if not paths:
            loc = view.loc
            kind = loc_kind(loc)
            self.img.set_from_pixbuf(place_pixbuf(kind, 96))
            self.name.set_text(disp_name(loc))
            self.sub.set_text(f"{view.count} items" if view.mode not in PAGE_MODES else "")
        elif len(paths) == 1:
            e = Entry(paths[0])
            kind = view._kind_icon(e.path, e.kind)
            pb = None
            if e.kind == "image":
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(e.path, 240, 200, True)
                    pb = pb.apply_embedded_orientation() or pb
                except GLib.Error:
                    pb = None
            self.img.set_from_pixbuf(pb or place_pixbuf(kind, 96))
            self.name.set_text(e.name)
            self.sub.set_text(type_name(e.name, e.is_dir))
            if not e.is_dir:
                rows.append(("Size", fmt_size(e.size)))
            if e.kind == "image":
                try:
                    fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(e.path)
                    if fmt:
                        rows.append(("Dimensions", f"{w} x {h}"))
                except Exception:
                    pass
            rows.append(("Date modified", fmt_date(e.mtime)))
            rows.append(("Date accessed", fmt_date(e.atime)))
            rows.append(("Location", os.path.dirname(e.path)))
        else:
            self.img.set_from_pixbuf(place_pixbuf("file", 96))
            self.name.set_text(f"{len(paths)} items selected")
            total = 0
            for p in paths:
                try:
                    if not os.path.isdir(p):
                        total += os.path.getsize(p)
                except OSError:
                    pass
            self.sub.set_text("")
            rows.append(("Total size", fmt_size(total)))
        for i, (k, v) in enumerate(rows):
            self.grid.attach(label(k, "sec"), 0, i, 1, 1)
            vl = label(v, wrap=True)
            vl.set_selectable(True)
            vl.set_max_width_chars(24)
            self.grid.attach(vl, 1, i, 1, 1)
        self.grid.show_all()


# ----------------------------------------------------------------------------
# Properties
# ----------------------------------------------------------------------------
class DonutChart(Gtk.DrawingArea):
    def __init__(self, used_frac):
        super().__init__()
        self.f = max(0.0, min(1.0, used_frac))
        self.set_size_request(120, 120)
        self.connect("draw", self._draw)

    def _draw(self, w, cr):
        a = w.get_allocation()
        cx, cy = a.width / 2, a.height / 2
        r = min(cx, cy) - 8
        cr.set_line_width(16)
        cr.set_source_rgba(*hexc("#4A4A4A"))
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgba(*hexc(C_ACCENT))
        cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * self.f)
        cr.stroke()


class PropertiesDialog(Gtk.Window):
    def __init__(self, parent, paths, trash_entries=None):
        super().__init__(transient_for=parent, modal=False)
        add_cls(self, "fx-dialog")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(420, -1)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.parent_win = parent
        self.paths = paths
        self.cancel = threading.Event()
        self.connect("destroy", lambda *_: self.cancel.set())
        self.connect("key-press-event", lambda w, ev: (ev.keyval == Gdk.KEY_Escape and self.destroy()) or False)
        single = len(paths) == 1
        e = Entry(paths[0]) if single else None
        self.e = e
        title = (e.name if single else f"{len(paths)} items") + " Properties"
        self.set_title(title)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=8)
        add_cls(head, "dlg-body")
        head.set_margin_bottom(0)
        head.pack_start(label(title, "dlg-title", ellipsize=True), True, True, 0)
        outer.pack_start(head, False, False, 0)
        nb = Gtk.Notebook()
        add_cls(nb, "fx-tabs")
        outer.pack_start(nb, True, True, 0)

        # --- General
        g = Gtk.Grid(column_spacing=16, row_spacing=9)
        g.set_margin_start(24)
        g.set_margin_end(24)
        g.set_margin_top(18)
        g.set_margin_bottom(18)
        self.grid = g
        self.row = 0
        top = Gtk.Box(spacing=14)
        if single:
            kind = USER_DIR_KIND.get(e.path, e.kind)
            if os.path.ismount(e.path):
                kind = "drive-sys" if e.path == "/" else "drive"
            top.pack_start(Icon(kind, 40, place=True), False, False, 0)
            self.name_entry = Gtk.Entry()
            add_cls(self.name_entry, "field")
            self.name_entry.set_text(e.name)
            self.name_entry.set_activates_default(True)
            self.name_entry.set_sensitive(not trash_entries and e.path not in ("/",) and not os.path.ismount(e.path))
            top.pack_start(self.name_entry, True, True, 0)
        else:
            top.pack_start(Icon("file", 40, place=True), False, False, 0)
            top.pack_start(label(f"{sum(os.path.isfile(p) for p in paths)} Files, "
                                 f"{sum(os.path.isdir(p) for p in paths)} Folders", "prop-big"), True, True, 0)
            self.name_entry = None
        g.attach(top, 0, 0, 2, 1)
        self.row = 1
        self._sep()
        if single:
            if os.path.ismount(e.path):
                self._kv("Type:", "Local Disk" if e.path.startswith("/") else "Drive")
                try:
                    with open("/proc/self/mounts") as f:
                        for ln in f:
                            pp = ln.split()
                            if len(pp) > 2 and pp[1] == e.path:
                                self._kv("File system:", pp[2])
                                break
                except OSError:
                    pass
                sv = os.statvfs(e.path)
                total = sv.f_blocks * sv.f_frsize
                free = sv.f_bavail * sv.f_frsize
                used = total - sv.f_bfree * sv.f_frsize
                self._sep()
                self._kv("Used space:", fmt_bytes_long(used))
                self._kv("Free space:", fmt_bytes_long(free))
                self._sep()
                self._kv("Capacity:", fmt_bytes_long(total))
                ch = DonutChart(used / total if total else 0)
                g.attach(ch, 0, self.row, 2, 1)
                self.row += 1
            else:
                self._kv("Type of file:" if not e.is_dir else "Type:", type_name(e.name, e.is_dir) +
                         ("" if e.is_dir else f" ({os.path.splitext(e.name)[1] or 'no extension'})"))
                if not e.is_dir and not trash_entries:
                    apps, default = apps_for(e.path)
                    if default:
                        self._kv("Opens with:", default.get_display_name())
                if e.is_link:
                    try:
                        self._kv("Target:", os.readlink(e.path))
                    except OSError:
                        pass
                self._sep()
                if trash_entries:
                    self._kv("Original location:", os.path.dirname(trash_entries[0].extra["orig"]))
                    self._kv("Date deleted:", fmt_date_long(trash_entries[0].extra["deleted"]))
                else:
                    self._kv("Location:", os.path.dirname(e.path))
                self.size_lbl = self._kv("Size:", "Calculating..." if e.is_dir else fmt_bytes_long(e.size or 0))
                if e.is_dir:
                    self.cont_lbl = self._kv("Contains:", "Calculating...")
                if e.kind == "image":
                    try:
                        fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(e.path)
                        if fmt:
                            self._kv("Dimensions:", f"{w} x {h}")
                    except Exception:
                        pass
                self._sep()
                created = None
                try:
                    info = Gio.File.new_for_path(e.path).query_info("time::created", Gio.FileQueryInfoFlags.NONE,
                                                                    None)
                    if info.has_attribute("time::created"):
                        created = info.get_attribute_uint64("time::created")
                except GLib.Error:
                    pass
                if created:
                    self._kv("Created:", fmt_date_long(created))
                self._kv("Modified:", fmt_date_long(e.mtime))
                self._kv("Accessed:", fmt_date_long(e.atime))
                if not trash_entries:
                    self._sep()
                    ab = Gtk.Box(spacing=18)
                    self.ro = Gtk.CheckButton(label="Read-only")
                    self.ro.set_active(not (e.mode & stat.S_IWUSR))
                    self.hid = Gtk.CheckButton(label="Hidden")
                    self.hid.set_active(e.hidden)
                    ab.pack_start(self.ro, False, False, 0)
                    ab.pack_start(self.hid, False, False, 0)
                    self._kv("Attributes:", None, ab)
            if e.is_dir and not os.path.ismount(e.path):
                self._start_count([e.path])
        else:
            parents = {os.path.dirname(p) for p in paths}
            self._kv("Type:", "Multiple Types")
            self._kv("Location:", parents.pop() if len(parents) == 1 else "Various folders")
            self.size_lbl = self._kv("Size:", "Calculating...")
            self.cont_lbl = self._kv("Contains:", "Calculating...")
            self._start_count(paths)
        nb.append_page(g, label("General"))

        # --- Permissions
        self.perm_checks = None
        if single and not trash_entries:
            pg = Gtk.Grid(column_spacing=16, row_spacing=10)
            pg.set_margin_start(24)
            pg.set_margin_end(24)
            pg.set_margin_top(18)
            pg.set_margin_bottom(18)
            try:
                st = os.lstat(e.path)
                import pwd
                import grp
                try:
                    owner = pwd.getpwuid(st.st_uid).pw_name
                except KeyError:
                    owner = str(st.st_uid)
                try:
                    group = grp.getgrgid(st.st_gid).gr_name
                except KeyError:
                    group = str(st.st_gid)
                mine = st.st_uid == os.getuid()
                pg.attach(label("Owner:", "sec"), 0, 0, 1, 1)
                pg.attach(label(owner), 1, 0, 3, 1)
                pg.attach(label("Group:", "sec"), 0, 1, 1, 1)
                pg.attach(label(group), 1, 1, 3, 1)
                for c, t in enumerate(("Read", "Write", "Run" if not e.is_dir else "Open")):
                    pg.attach(label(t, "sec", xalign=0.5), c + 1, 2, 1, 1)
                bits = [[stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR], [stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP],
                        [stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH]]
                self.perm_checks = []
                for r, who in enumerate(("Owner", "Group", "Everyone else")):
                    pg.attach(label(who), 0, r + 3, 1, 1)
                    for c in range(3):
                        cb = Gtk.CheckButton()
                        cb.set_halign(Gtk.Align.CENTER)
                        cb.set_active(bool(st.st_mode & bits[r][c]))
                        cb.set_sensitive(mine)
                        pg.attach(cb, c + 1, r + 3, 1, 1)
                        self.perm_checks.append((cb, bits[r][c]))
                self.orig_mode = stat.S_IMODE(st.st_mode)
                note = ("" if mine else "Only the owner can change these permissions.")
                if note:
                    pg.attach(label(note, "ter", wrap=True), 0, 6, 4, 1)
                pg.attach(label(f"Mode: {oct(stat.S_IMODE(st.st_mode))[2:]}", "ter"), 0, 7, 4, 1)
            except OSError:
                pass
            nb.append_page(pg, label("Permissions"))

        foot = Gtk.Box(spacing=8, homogeneous=True)
        add_cls(foot, "dlg-foot")
        ok = Gtk.Button(label="OK")
        add_cls(ok, "primary")
        ok.connect("clicked", lambda *_: self._apply(True))
        ok.set_can_default(True)
        cancel = Gtk.Button(label="Cancel")
        add_cls(cancel, "secondary")
        cancel.connect("clicked", lambda *_: self.destroy())
        foot.pack_start(ok, True, True, 0)
        foot.pack_start(cancel, True, True, 0)
        outer.pack_start(foot, False, False, 0)
        self.add(outer)
        self.set_default(ok)

    def _sep(self):
        s = Gtk.Separator()
        s.set_margin_top(2)
        s.set_margin_bottom(2)
        self.grid.attach(s, 0, self.row, 2, 1)
        self.row += 1

    def _kv(self, k, v, widget=None):
        self.grid.attach(label(k, "sec"), 0, self.row, 1, 1)
        if widget is None:
            widget = label(v, wrap=True)
            widget.set_selectable(True)
            widget.set_max_width_chars(36)
        self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1
        return widget

    def _start_count(self, paths):
        cancel = self.cancel

        def upd(total, files, folders, final=False):
            if cancel.is_set():
                return False
            self.size_lbl.set_text(fmt_bytes_long(total))
            if hasattr(self, "cont_lbl"):
                self.cont_lbl.set_text(f"{files:,} Files, {folders:,} Folders")
            return False

        def work():
            t = f = d = 0
            for p in paths:
                if cancel.is_set():
                    return
                if os.path.isdir(p) and not os.path.islink(p):
                    base = (t, f, d)
                    r = dir_summary(p, cancel, lambda a, b, c, base=base: GLib.idle_add(
                        upd, base[0] + a, base[1] + b, base[2] + c))
                    t, f, d = t + r[0], f + r[1], d + r[2] + (1 if len(paths) > 1 else 0)
                else:
                    f += 1
                    try:
                        t += os.lstat(p).st_size
                    except OSError:
                        pass
            GLib.idle_add(upd, t, f, d, True)
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, close):
        e = self.e
        err = None
        if e and self.perm_checks:
            mode = self.orig_mode & ~0o777
            for cb, bit in self.perm_checks:
                if cb.get_active():
                    mode |= bit
            if mode != self.orig_mode:
                try:
                    os.chmod(e.path, mode)
                except OSError as ex:
                    err = ex.strerror
        elif e and hasattr(self, "ro"):
            try:
                m = stat.S_IMODE(os.stat(e.path).st_mode)
                want_ro = self.ro.get_active()
                if want_ro and m & stat.S_IWUSR:
                    os.chmod(e.path, m & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
                elif not want_ro and not m & stat.S_IWUSR:
                    os.chmod(e.path, m | stat.S_IWUSR)
            except OSError as ex:
                err = ex.strerror
        if e and self.name_entry and self.name_entry.get_sensitive():
            new = self.name_entry.get_text().strip()
            if hasattr(self, "hid"):
                if self.hid.get_active() and not new.startswith("."):
                    new = "." + new
                elif not self.hid.get_active() and new.startswith(".") and e.hidden:
                    new = new.lstrip(".") or new
            if new and new != e.name and "/" not in new:
                dst = os.path.join(os.path.dirname(e.path), new)
                if os.path.lexists(dst):
                    err = f"There is already a file named \"{new}\"."
                else:
                    try:
                        os.rename(e.path, dst)
                    except OSError as ex:
                        err = ex.strerror
        parent = self.parent_win
        self.destroy()
        if err:
            info_dialog(parent, "Couldn't apply changes", err)
        if hasattr(parent, "refresh_all"):
            parent.refresh_all()


# ----------------------------------------------------------------------------
# Folder options
# ----------------------------------------------------------------------------
class OptionsDialog(WinDialog):
    def __init__(self, win):
        self.win = win
        st = win.app.settings
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Gtk.Box(spacing=10)
        row.pack_start(label("Open File Explorer to:"), False, False, 0)
        self.start = Gtk.ComboBoxText()
        for k, n in ((LOC_HOME, "Home"), (LOC_PC, "This PC"), (HOME, "Your home folder")):
            self.start.append(k, n)
        self.start.set_active_id(st.get("start", LOC_HOME))
        if self.start.get_active() < 0:
            self.start.set_active(0)
        row.pack_start(self.start, False, False, 0)
        box.pack_start(row, False, False, 4)
        self.checks = {}
        for key, text, inv in (("show_hidden", "Show hidden files and folders", False),
                               ("show_ext", "Show file name extensions", False),
                               ("thumbnails", "Always show icons, never thumbnails", True),
                               ("show_recent", "Show recently used files in Home", False),
                               ("confirm_delete", "Ask before moving files to the Recycle Bin", False)):
            cb = Gtk.CheckButton(label=text)
            cb.set_active(bool(st.get(key)) != inv)
            box.pack_start(cb, False, False, 0)
            self.checks[key] = (cb, inv)
        clr = Gtk.Box(spacing=10)
        clr.pack_start(label("Clear File Explorer history:"), False, False, 0)
        cb = Gtk.Button(label="Clear")
        add_cls(cb, "secondary")
        cb.connect("clicked", self._clear)
        clr.pack_start(cb, False, False, 0)
        box.pack_start(clr, False, False, 8)
        super().__init__(win, "Folder Options", None, primary="OK", secondary="Cancel", width=460, extra=box)

    def _clear(self, b):
        try:
            Gtk.RecentManager.get_default().purge_items()
        except GLib.Error:
            pass
        b.set_label("Cleared")
        b.set_sensitive(False)

    def run(self):
        def done(ok):
            if not ok:
                return
            st = self.win.app.settings
            for key, (cb, inv) in self.checks.items():
                st[key] = cb.get_active() != inv
            st["start"] = self.start.get_active_id() or LOC_HOME
            st.save()
            self.win.app.refresh_all()
        self.ask(done)


# ----------------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------------
class TabButton(Gtk.Button):
    def __init__(self, win, view):
        super().__init__()
        self.win = win
        self.view = view
        add_cls(self, "tab")
        self.set_relief(Gtk.ReliefStyle.NONE)
        box = Gtk.Box(spacing=8)
        self.icon = Icon("folder", 16, place=True)
        box.pack_start(self.icon, False, False, 0)
        self.lbl = label("", ellipsize=True)
        self.lbl.set_width_chars(16)
        self.lbl.set_max_width_chars(20)
        box.pack_start(self.lbl, True, True, 0)
        x = Gtk.Button()
        add_cls(x, "tab-x")
        x.add(Icon("close", 10))
        x.set_relief(Gtk.ReliefStyle.NONE)
        x.connect("clicked", lambda *_: win.close_tab(view))
        box.pack_start(x, False, False, 0)
        self.add(box)
        self.connect("clicked", lambda *_: win.switch_tab(view))
        self.connect("button-press-event", self._press)
        # dragging files over a tab switches to it
        self.drag_dest_set(0, [], 0)
        self.connect("drag-motion", self._motion)

    def _press(self, w, ev):
        if ev.button == 2:
            self.win.close_tab(self.view)
            return True
        if ev.button == 3:
            m = build_menu([
                {"label": "New tab", "icon": "plus", "accel": "Ctrl+T", "cb": lambda: self.win.new_tab()},
                {"label": "Duplicate tab", "icon": "newtab", "cb": lambda: self.win.new_tab(self.view.loc)},
                {"label": "Move tab to new window", "icon": "open", "cb": lambda: self.win.tab_to_window(self.view)},
                None,
                {"label": "Close tab", "icon": "close", "accel": "Ctrl+W", "cb": lambda: self.win.close_tab(self.view)},
                {"label": "Close other tabs", "sensitive": len(self.win.views) > 1,
                 "cb": lambda: self.win.close_others(self.view)},
            ])
            self.win._menu = m
            popup_menu(m, self, ev)
            return True
        return False

    def _motion(self, w, ctx, x, y, t):
        if self.win.cur is not self.view:
            self.win.switch_tab(self.view)
        Gdk.drag_status(ctx, 0, t)
        return True

    def update(self):
        v = self.view
        loc = v.loc
        if v.query:
            self.lbl.set_text(f"Search: {v.query}")
            self.icon.set_icon("folder")
        else:
            self.lbl.set_text(disp_name(loc))
            self.icon.set_icon(loc_kind(loc))
        self.set_tooltip_text(LOC_NAMES.get(loc, loc))

    def set_active(self, on):
        ctx = self.get_style_context()
        (ctx.add_class if on else ctx.remove_class)("active")


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class ExplorerWindow(Gtk.ApplicationWindow):
    def __init__(self, app, loc=None, select=None):
        super().__init__(application=app)
        self.app = app
        st = app.settings
        self.views = []
        self.tabs = {}
        self.cur = None
        self._menu = None
        self.set_title("File Explorer")
        self.set_icon_name("pi-explorer")
        add_cls(self, "fx")
        self.set_default_size(st["width"], st["height"])
        self.set_size_request(640, 420)
        if st.get("maximized"):
            self.maximize()
        self.connect("delete-event", self._on_close)
        self.connect("window-state-event", self._wstate)
        self.connect("key-press-event", self._key)
        self.connect("configure-event", self._configure)

        # ---- title bar with tabs
        hb = Gtk.HeaderBar()
        add_cls(hb, "fx-title")
        hb.set_show_close_button(False)
        strip = Gtk.Box(spacing=0)
        add_cls(strip, "tabstrip")
        strip.set_valign(Gtk.Align.END)
        self.tabbox = Gtk.Box(spacing=0)
        strip.pack_start(self.tabbox, False, False, 0)
        nt = icon_button("plus", lambda w: self.new_tab(), cls="tab-new", tip="Add new tab (Ctrl+T)", size=14)
        nt.set_valign(Gtk.Align.CENTER)
        strip.pack_start(nt, False, False, 0)
        hb.pack_start(strip)
        wc = Gtk.Box(spacing=0)
        wc.set_valign(Gtk.Align.START)
        for name, cb, extra in (("min", lambda *_: self.iconify(), ""),
                                ("max", lambda *_: self._toggle_max(), ""),
                                ("close", lambda *_: self.close(), "close")):
            b = Gtk.Button()
            add_cls(b, "wc")
            if extra:
                add_cls(b, extra)
            ic = Icon(name, 10)
            b.add(ic)
            b.connect("clicked", cb)
            if name == "max":
                self.max_icon = ic
            wc.pack_start(b, False, False, 0)
        hb.pack_end(wc)
        hb.set_custom_title(Gtk.Box())
        self.set_titlebar(hb)
        hb.show_all()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # ---- address row
        nav = Gtk.Box(spacing=2)
        add_cls(nav, "navrow")
        self.b_back = icon_button("back", lambda w: self.cur.back(), tip="Back (Alt+Left Arrow)")
        self.b_fwd = icon_button("forward", lambda w: self.cur.forward(), tip="Forward (Alt+Right Arrow)")
        self.b_up = icon_button("up", lambda w: self.cur.up(), tip="Up to parent folder (Alt+Up Arrow)")
        self.b_ref = icon_button("refresh", lambda w: self.cur.refresh(), tip="Refresh (F5)")
        for b in (self.b_back, self.b_fwd, self.b_up, self.b_ref):
            nav.pack_start(b, False, False, 0)
        self.addr = AddressBar(self)
        self.addr.set_margin_start(6)
        nav.pack_start(self.addr, True, True, 0)
        self.search = Gtk.SearchEntry()
        add_cls(self.search, "fx-search")
        self.search.set_size_request(260, -1)
        self.search.set_margin_start(8)
        self.search.connect("search-changed", self._search_changed)
        self.search.connect("activate", self._search_changed)
        self.search.connect("stop-search", lambda *_: (self.search.set_text(""), self.cur.focus_view()))
        nav.pack_start(self.search, False, False, 0)
        root.pack_start(nav, False, False, 0)

        # ---- command bar
        cmd = Gtk.Box(spacing=2)
        add_cls(cmd, "cmdbar")
        self.b_new = tb_button("new", "New", self._new_menu, chev=True)
        cmd.pack_start(self.b_new, False, False, 0)
        cmd.pack_start(self._vsep(), False, False, 0)
        self.b_cut = tb_button("cut", None, lambda w: self.cut(), tip="Cut (Ctrl+X)")
        self.b_copy = tb_button("copy", None, lambda w: self.copy(), tip="Copy (Ctrl+C)")
        self.b_paste = tb_button("paste", None, lambda w: self.paste(), tip="Paste (Ctrl+V)")
        self.b_rename = tb_button("rename", None, lambda w: self.cur.start_rename(), tip="Rename (F2)")
        self.b_del = tb_button("delete", None, lambda w: self.delete(), tip="Delete (Delete)")
        for b in (self.b_cut, self.b_copy, self.b_paste, self.b_rename, self.b_del):
            cmd.pack_start(b, False, False, 0)
        cmd.pack_start(self._vsep(), False, False, 0)
        self.b_sort = tb_button("sort", "Sort", self._sort_menu, chev=True)
        self.b_view = tb_button("view", "View", self._view_menu, chev=True)
        cmd.pack_start(self.b_sort, False, False, 0)
        cmd.pack_start(self.b_view, False, False, 0)
        cmd.pack_start(self._vsep(), False, False, 0)
        # contextual buttons
        self.b_extract = tb_button("extract", "Extract all", lambda w: self.extract())
        self.b_empty = tb_button("trash", "Empty Recycle Bin", lambda w: self.app.empty_trash(self))
        self.b_restore = tb_button("restoreitem", "Restore the selected items", lambda w: self.restore())
        self.b_restore_all = tb_button("restoreitem", "Restore all items", lambda w: self.restore(all_=True))
        self.b_term = tb_button("terminal", "Terminal", lambda w: self.terminal(), tip="Open in Terminal")
        for b in (self.b_extract, self.b_empty, self.b_restore, self.b_restore_all, self.b_term):
            b.get_child().show_all()
            b.set_no_show_all(True)
            cmd.pack_start(b, False, False, 0)
        self.b_more = tb_button("more", None, self._more_menu, tip="See more")
        cmd.pack_start(self.b_more, False, False, 0)
        self.b_pane = tb_button("pane", "Details", lambda w: self.toggle_pane(), tip="Details pane (Alt+Shift+P)")
        cmd.pack_end(self.b_pane, False, False, 0)
        root.pack_start(cmd, False, False, 0)

        # ---- body
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.navpane = NavPane(self)
        self.paned.pack1(self.navpane, False, False)
        self.paned.set_position(st.get("nav_width", 230))
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        mid = Gtk.Box()
        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        mid.pack_start(self.stack, True, True, 0)
        self.dpane = DetailsPane(self)
        self.dpane.set_no_show_all(True)
        mid.pack_start(self.dpane, False, False, 0)
        right.pack_start(mid, True, True, 0)
        # status bar
        sb = Gtk.Box(spacing=12)
        add_cls(sb, "status")
        self.l_count = label("")
        self.l_sel = label("")
        sb.pack_start(self.l_count, False, False, 0)
        sb.pack_start(self.l_sel, False, False, 0)
        self.spin = Gtk.Spinner()
        sb.pack_start(self.spin, False, False, 0)
        self.sv_icons = icon_button("icons-view", lambda w: self.set_view("large"), cls="sv",
                                    tip="Large icons (Ctrl+Shift+2)", size=14)
        self.sv_details = icon_button("details-view", lambda w: self.set_view("details"), cls="sv",
                                      tip="Details (Ctrl+Shift+6)", size=14)
        sb.pack_end(self.sv_icons, False, False, 0)
        sb.pack_end(self.sv_details, False, False, 0)
        right.pack_start(sb, False, False, 0)
        self.paned.pack2(right, True, False)
        root.pack_start(self.paned, True, True, 0)
        self.add(root)
        root.show_all()

        self.stack.connect("scroll-event", self._scroll)
        self.new_tab(loc or st.get("start", LOC_HOME), select=select)
        if st.get("details_pane"):
            self.dpane.show_all()
            self.dpane.set_no_show_all(False)

    @staticmethod
    def _vsep():
        s = Gtk.Box()
        add_cls(s, "vsep")
        return s

    # ---------------- window state
    def _toggle_max(self):
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def _wstate(self, w, ev):
        maxed = bool(ev.new_window_state & Gdk.WindowState.MAXIMIZED)
        self.max_icon.set_icon("restore" if maxed else "max")
        self.app.settings["maximized"] = maxed
        return False

    def _configure(self, w, ev):
        if not self.is_maximized():
            ww, hh = self.get_size()
            self.app.settings["width"], self.app.settings["height"] = ww, hh
        return False

    def _on_close(self, *a):
        self.app.settings["nav_width"] = self.paned.get_position()
        for v in self.views:
            v._stop_monitor()
            v._cancel_search()
        self.app.settings.save()
        return False

    # ---------------- tabs
    def new_tab(self, loc=None, switch=True, select=None):
        if loc is None:
            loc = self.app.settings.get("start", LOC_HOME)
        v = FolderView(self, loc, select)
        self.views.append(v)
        self.stack.add(v)
        tb = TabButton(self, v)
        self.tabs[v] = tb
        self.tabbox.pack_start(tb, False, False, 0)
        tb.show_all()
        tb.update()
        if switch or self.cur is None:
            self.switch_tab(v)
        return v

    def switch_tab(self, v):
        self.cur = v
        self.stack.set_visible_child(v)
        for vv, tb in self.tabs.items():
            tb.set_active(vv is v)
        self.search.handler_block_by_func(self._search_changed)
        self.search.set_text(v.query)
        self.search.handler_unblock_by_func(self._search_changed)
        self.on_location(v)
        v.focus_view()

    def close_tab(self, v):
        if len(self.views) == 1:
            self.close()
            return
        i = self.views.index(v)
        v._stop_monitor()
        v._cancel_search()
        self.views.remove(v)
        tb = self.tabs.pop(v)
        tb.destroy()
        v.destroy()
        if self.cur is v:
            self.switch_tab(self.views[max(0, i - 1)])

    def close_others(self, keep):
        for v in list(self.views):
            if v is not keep:
                self.close_tab(v)

    def tab_to_window(self, v):
        loc = v.loc
        self.app.new_window(loc)
        self.close_tab(v)

    def cycle_tab(self, d):
        i = self.views.index(self.cur)
        self.switch_tab(self.views[(i + d) % len(self.views)])

    # ---------------- callbacks from views
    def on_location(self, v):
        tb = self.tabs.get(v)
        if tb:
            tb.update()
        if v is not self.cur:
            return
        self.addr.set_location(v.loc, v.query)
        self.set_title(f"{disp_name(v.loc)} - File Explorer")
        where = disp_name(v.loc)
        self.search.set_placeholder_text(f"Search {where}")
        self.search.set_sensitive(v.is_folder())
        if not v.query and self.search.get_text():
            self.search.handler_block_by_func(self._search_changed)
            self.search.set_text("")
            self.search.handler_unblock_by_func(self._search_changed)
        self.b_back.set_sensitive(v.can_back() or bool(v.query))
        self.b_fwd.set_sensitive(v.can_forward())
        self.b_up.set_sensitive(v.parent() is not None)
        self.navpane.sync(v.loc)
        self.on_items(v)

    def on_items(self, v):
        if v is not self.cur:
            return
        if v.mode in PAGE_MODES:
            self.l_count.set_text("")
        elif v.mode == "search" and v.searching:
            self.l_count.set_text(f"{v.count:,} items found so far")
        else:
            self.l_count.set_text(f"{v.count:,} item{'s' if v.count != 1 else ''}")
        if v.searching:
            self.spin.show()
            self.spin.start()
        else:
            self.spin.stop()
            self.spin.hide()
        self.on_selection(v)

    def on_selection(self, v):
        if v is not self.cur:
            return
        paths = v.selected_paths()
        n = len(paths)
        if n:
            total = 0
            only_files = True
            for p in paths[:2000]:
                try:
                    if os.path.isdir(p):
                        only_files = False
                    else:
                        total += os.path.getsize(p)
                except OSError:
                    pass
            txt = f"|    {n:,} item{'s' if n != 1 else ''} selected"
            if only_files:
                txt += f"  {fmt_size(total)}"
            self.l_sel.set_text(txt)
        else:
            self.l_sel.set_text("")
        folder = v.mode in ("folder", "search")
        trash = v.mode == "trash"
        for b in (self.b_cut, self.b_copy):
            b.set_sensitive(n > 0 and (folder or trash and b is self.b_cut))
        self.b_rename.set_sensitive(n == 1 and folder)
        self.b_del.set_sensitive(n > 0 and (folder or trash))
        self.b_paste.set_sensitive(v.mode == "folder" and self.app.has_clip())
        self.b_new.set_sensitive(v.mode == "folder")
        self.b_sort.set_sensitive(v.mode not in PAGE_MODES)
        self.b_view.set_sensitive(v.mode not in PAGE_MODES + ("trash",))
        arch = n == 1 and folder and Entry(paths[0]).kind == "archive"
        self.b_extract.set_visible(arch)
        self.b_empty.set_visible(trash)
        self.b_restore.set_visible(trash and n > 0)
        self.b_restore_all.set_visible(trash and n == 0)
        self.b_term.set_visible(v.mode == "folder")
        self.sv_icons.get_style_context().remove_class("on")
        self.sv_details.get_style_context().remove_class("on")
        if v.mode not in PAGE_MODES:
            (self.sv_details if v.view_mode == "details" or trash else self.sv_icons).get_style_context().add_class("on")
        if self.dpane.get_visible():
            self.dpane.update(v)

    def refresh_all(self):
        self.app.refresh_all()

    # ---------------- search
    def _search_changed(self, *a):
        if not self.cur.is_folder():
            return
        q = self.search.get_text()
        if getattr(self, "_search_to", None):
            GLib.source_remove(self._search_to)
        self._search_to = GLib.timeout_add(350, self._do_search, q)

    def _do_search(self, q):
        self._search_to = None
        self.cur.set_search(q)
        return False

    # ---------------- view
    def set_view(self, mode):
        if self.cur.mode in PAGE_MODES + ("trash",):
            return
        self.cur.set_view(mode)
        self.on_selection(self.cur)

    def _scroll(self, w, ev):
        if not ev.state & Gdk.ModifierType.CONTROL_MASK or self.cur.mode not in ("folder", "search"):
            return False
        order = ["details", "medium", "large", "xl"]
        cur = self.cur.view_mode if self.cur.view_mode in order else "details"
        i = order.index(cur)
        dy = ev.delta_y if ev.direction == Gdk.ScrollDirection.SMOOTH else (
            -1 if ev.direction == Gdk.ScrollDirection.UP else 1)
        if dy < 0 and i < len(order) - 1:
            self.set_view(order[i + 1])
        elif dy > 0 and i > 0:
            self.set_view(order[i - 1])
        return True

    def toggle_pane(self):
        on = not self.dpane.get_visible()
        self.app.settings["details_pane"] = on
        if on:
            self.dpane.set_no_show_all(False)
            self.dpane.show_all()
            self.dpane.update(self.cur)
        else:
            self.dpane.hide()

    # ---------------- menus
    def _new_menu(self, w):
        m = build_menu([
            {"label": "Folder", "place": "folder", "accel": "Ctrl+Shift+N", "cb": self.new_folder},
            None,
            {"label": "Text Document", "place": "text", "cb": lambda: self.new_file("New Text Document.txt")},
            {"label": "Python File", "place": "code", "cb": lambda: self.new_file("new_script.py")},
            {"label": "Shell Script", "place": "script", "cb": lambda: self.new_file("new_script.sh", "#!/bin/bash\n", 0o755)},
            {"label": "Empty File", "place": "file", "cb": lambda: self.new_file("New File")},
        ])
        self._menu = m
        popup_menu(m, w)

    def _view_spec(self):
        v = self.cur
        vm = v.view_mode
        return [
            {"label": "Extra large icons", "checked": vm == "xl", "accel": "Ctrl+Shift+1", "cb": lambda: self.set_view("xl")},
            {"label": "Large icons", "checked": vm == "large", "accel": "Ctrl+Shift+2", "cb": lambda: self.set_view("large")},
            {"label": "Medium icons", "checked": vm == "medium", "accel": "Ctrl+Shift+3", "cb": lambda: self.set_view("medium")},
            {"label": "Details", "checked": vm == "details", "accel": "Ctrl+Shift+6", "cb": lambda: self.set_view("details")},
        ]

    def _view_menu(self, w):
        st = self.app.settings
        spec = self._view_spec() + [
            None,
            {"label": "Details pane", "checked": self.dpane.get_visible(), "icon": "pane", "accel": "Alt+Shift+P",
             "cb": self.toggle_pane},
            {"label": "Show", "icon": "view", "sub": [
                {"label": "File name extensions", "checked": st["show_ext"], "cb": lambda: self.toggle("show_ext")},
                {"label": "Hidden items", "checked": st["show_hidden"], "accel": "Ctrl+H",
                 "cb": lambda: self.toggle("show_hidden")},
            ]},
        ]
        m = build_menu(spec)
        self._menu = m
        popup_menu(m, w)

    def _sort_spec(self):
        st = self.app.settings
        key = st.get("sort_col", "name")
        v = self.cur
        cols = [("name", "Name"), ("date", "Date modified"), ("type", "Type"), ("size", "Size")]
        if v.mode == "trash":
            cols = [("name", "Name"), ("deleted", "Date deleted"), ("type", "Type"), ("size", "Size")]
        elif v.mode == "search":
            cols.append(("folder", "Folder path"))
        spec = [{"label": n, "checked": key == k, "cb": (lambda k=k: v.sort_by(k))} for k, n in cols]
        spec += [None,
                 {"label": "Ascending", "checked": not st.get("sort_desc"), "cb": lambda: v.sort_by(key, False)},
                 {"label": "Descending", "checked": bool(st.get("sort_desc")), "cb": lambda: v.sort_by(key, True)}]
        return spec

    def _sort_menu(self, w):
        m = build_menu(self._sort_spec())
        self._menu = m
        popup_menu(m, w)

    def _more_menu(self, w):
        v = self.cur
        files = v.mode in ("folder", "search", "trash")
        spec = [
            {"label": "Select all", "icon": "selectall", "accel": "Ctrl+A", "sensitive": files, "cb": v.select_all},
            {"label": "Select none", "icon": "close", "sensitive": files, "cb": v.select_none},
            {"label": "Invert selection", "icon": "selectall", "sensitive": files, "cb": v.invert_selection},
            None,
            {"label": "Copy as path", "icon": "link", "accel": "Ctrl+Shift+C", "sensitive": v.is_folder(),
             "cb": self.copy_path},
            {"label": "Open in Terminal", "icon": "terminal", "sensitive": v.mode == "folder", "cb": self.terminal},
            {"label": "Open new window", "icon": "open", "accel": "Ctrl+N", "cb": lambda: self.app.new_window(v.loc)},
            None,
            {"label": "Properties", "icon": "properties", "accel": "Alt+Enter", "sensitive": v.mode != "home",
             "cb": self.properties},
            {"label": "Options", "icon": "properties", "cb": lambda: OptionsDialog(self).run()},
        ]
        m = build_menu(spec)
        self._menu = m
        popup_menu(m, w)

    def item_menu(self, v, w, ev):
        paths = v.selected_paths()
        if not paths:
            return
        if v.mode == "trash":
            spec = [
                {"label": "Restore", "icon": "restoreitem", "cb": self.restore},
                None,
                {"label": "Cut", "icon": "cut", "accel": "Ctrl+X", "cb": self.cut},
                {"label": "Delete permanently", "icon": "delete", "accel": "Delete", "cb": self.delete},
                None,
                {"label": "Properties", "icon": "properties", "accel": "Alt+Enter", "cb": self.properties},
            ]
        else:
            one = len(paths) == 1
            p0 = paths[0]
            isdir = os.path.isdir(p0)
            all_dirs = all(os.path.isdir(p) for p in paths)
            spec = [{"label": "Open", "icon": "open", "accel": "Enter", "cb": v.open_selected}]
            if all_dirs:
                spec.append({"label": "Open in new tab", "icon": "newtab", "cb": lambda: v.open_selected(new_tab=True)})
                spec.append({"label": "Open in new window", "icon": "open",
                             "cb": lambda: [self.app.new_window(p) for p in paths[:5]]})
            if one and not isdir:
                apps, default = apps_for(p0)
                sub = []
                for a in apps[:12]:
                    sub.append({"label": a.get_display_name(), "pixbuf": theme_pixbuf(a.get_icon(), 16),
                                "cb": (lambda a=a: launch_with(a, [p0], self))})
                if sub:
                    sub.append(None)
                sub.append({"label": "Choose another app", "cb": lambda: choose_app(self, p0)})
                spec.append({"label": "Open with", "icon": "open", "sub": sub})
            if v.mode == "search":
                spec.append({"label": "Open file location", "icon": "folder",
                             "cb": lambda: v.navigate(os.path.dirname(p0), select=[p0])})
            spec += [
                None,
                {"label": "Cut", "icon": "cut", "accel": "Ctrl+X", "cb": self.cut},
                {"label": "Copy", "icon": "copy", "accel": "Ctrl+C", "cb": self.copy},
            ]
            if one and isdir and self.app.has_clip():
                spec.append({"label": "Paste into folder", "icon": "paste", "cb": lambda: self.paste(dest=p0)})
            spec += [
                {"label": "Copy as path", "icon": "link", "accel": "Ctrl+Shift+C", "cb": self.copy_path},
                {"label": "Rename", "icon": "rename", "accel": "F2", "sensitive": one, "cb": v.start_rename},
                {"label": "Delete", "icon": "delete", "accel": "Delete", "cb": self.delete},
                None,
                {"label": "Compress to ZIP file", "icon": "zip", "cb": self.compress},
            ]
            if one and Entry(p0).kind == "archive":
                spec.append({"label": "Extract All...", "icon": "extract", "cb": self.extract})
            if one and isdir:
                if p0 in self.app.settings["quick_access"]:
                    spec.append({"label": "Unpin from Quick access", "icon": "pin", "cb": lambda: self.app.unpin(p0)})
                else:
                    spec.append({"label": "Pin to Quick access", "icon": "pin", "cb": lambda: self.app.pin(p0)})
                spec.append({"label": "Open in Terminal", "icon": "terminal", "cb": lambda: open_terminal(p0, self)})
                if not is_gvfs_path(p0):
                    sh = share_for_path(p0) if samba_installed() else None
                    if sh and sh["ours"]:
                        spec.append({"label": "Stop sharing on network", "icon": "share",
                                     "cb": lambda: self.app.unshare(self, sh["name"])})
                    elif not sh:
                        spec.append({"label": "Share on network", "icon": "share",
                                     "cb": lambda: self.app.share_folder(self, p0)})
            spec += [None, {"label": "Properties", "icon": "properties", "accel": "Alt+Enter", "cb": self.properties}]
        m = build_menu(spec)
        self._menu = m
        popup_menu(m, w, ev)

    def bg_menu(self, v, w, ev):
        if v.mode == "trash":
            spec = [{"label": "Sort by", "icon": "sort", "sub": self._sort_spec()},
                    {"label": "Refresh", "icon": "refresh", "accel": "F5", "cb": v.refresh},
                    None,
                    {"label": "Empty Recycle Bin", "icon": "trash", "sensitive": v.count > 0,
                     "cb": lambda: self.app.empty_trash(self)},
                    {"label": "Restore all items", "icon": "restoreitem", "sensitive": v.count > 0,
                     "cb": lambda: self.restore(all_=True)}]
        else:
            folder = v.mode == "folder"
            spec = [
                {"label": "View", "icon": "view", "sub": self._view_spec()},
                {"label": "Sort by", "icon": "sort", "sub": self._sort_spec()},
                {"label": "Refresh", "icon": "refresh", "accel": "F5", "cb": v.refresh},
                None,
                {"label": "Paste", "icon": "paste", "accel": "Ctrl+V", "sensitive": folder and self.app.has_clip(),
                 "cb": self.paste},
                None,
                {"label": "New", "icon": "new", "sensitive": folder, "sub": [
                    {"label": "Folder", "place": "folder", "cb": self.new_folder},
                    None,
                    {"label": "Text Document", "place": "text", "cb": lambda: self.new_file("New Text Document.txt")},
                    {"label": "Python File", "place": "code", "cb": lambda: self.new_file("new_script.py")},
                    {"label": "Shell Script", "place": "script",
                     "cb": lambda: self.new_file("new_script.sh", "#!/bin/bash\n", 0o755)},
                    {"label": "Empty File", "place": "file", "cb": lambda: self.new_file("New File")},
                ]},
                None,
                {"label": "Open in Terminal", "icon": "terminal", "sensitive": folder, "cb": self.terminal},
                {"label": "Properties", "icon": "properties", "sensitive": folder,
                 "cb": lambda: PropertiesDialog(self, [v.loc]).show_all()},
            ]
        m = build_menu(spec)
        self._menu = m
        popup_menu(m, w, ev)

    # ---------------- actions
    def toggle(self, key):
        self.app.settings[key] = not self.app.settings[key]
        self.app.refresh_all()

    def cut(self):
        v = self.cur
        paths = v.selected_paths()
        if paths:
            self.app.set_clip("cut", paths, trash=v.mode == "trash")

    def copy(self):
        paths = self.cur.selected_paths()
        if paths and self.cur.mode != "trash":
            self.app.set_clip("copy", paths)

    def copy_path(self):
        v = self.cur
        paths = v.selected_paths() or ([v.loc] if v.is_folder() else [])
        if paths:
            self.app.copy_text("\n".join(f'"{p}"' if " " in p else p for p in paths))

    def paste(self, dest=None):
        v = self.cur
        dest = dest or (v.loc if v.mode == "folder" else None)
        if not dest:
            return
        op, paths = self.app.get_clip()
        if not paths:
            return
        self.app.transfer(self, "move" if op == "cut" else "copy", paths, dest, select_in=v)
        if op == "cut":
            self.app.clear_clip()

    def delete(self, permanent=False):
        v = self.cur
        paths = v.selected_paths()
        if not paths:
            return
        if v.mode == "trash":
            ents = v.selected_entries()
            n = len(ents)
            what = f'"{ents[0].name}"' if n == 1 else f"these {n} items"
            confirm(self, "Delete File" if n == 1 else "Delete Multiple Items",
                    f"Are you sure you want to permanently delete {what}?", "Yes",
                    lambda ok: ok and self.app.trash_delete(self, ents), no="No")
            return
        if v.mode not in ("folder", "search"):
            return
        self.app.delete(self, paths, permanent)

    def restore(self, all_=False):
        ents = trash_items() if all_ else self.cur.selected_entries()
        errs = []
        for e in ents:
            try:
                trash_restore(e)
            except OSError as ex:
                errs.append((e.name, ex.strerror or str(ex)))
        if errs:
            error_list_dialog(self, "Some items couldn't be restored", errs)
        self.app.refresh_all()

    def new_folder(self):
        v = self.cur
        if v.mode != "folder":
            return
        name = unique_name(v.loc, "New folder", True)
        p = os.path.join(v.loc, name)
        try:
            os.mkdir(p)
        except OSError as e:
            info_dialog(self, "Couldn't create the folder", e.strerror or str(e))
            return
        v.refresh()
        v.select_paths([p])
        GLib.timeout_add(120, lambda: v.start_rename(p) or False)

    def new_file(self, base, content="", mode=None):
        v = self.cur
        if v.mode != "folder":
            return
        name = unique_name(v.loc, base)
        p = os.path.join(v.loc, name)
        try:
            with open(p, "x") as f:
                f.write(content)
            if mode:
                os.chmod(p, mode)
        except OSError as e:
            info_dialog(self, "Couldn't create the file", e.strerror or str(e))
            return
        v.refresh()
        v.select_paths([p])
        GLib.timeout_add(120, lambda: v.start_rename(p) or False)

    def compress(self):
        v = self.cur
        paths = v.selected_paths()
        if not paths:
            return
        dest = os.path.dirname(paths[0])
        if len(paths) == 1:
            out = split_ext(os.path.basename(paths[0]), os.path.isdir(paths[0]))[0] + ".zip"
        else:
            out = os.path.basename(dest.rstrip("/")) + ".zip" if dest != "/" else "Archive.zip"
        job = FileJob("compress", paths, dest, out_name=out)
        self.app.run_job(self, job, f"Compressing {len(paths)} item{'s' if len(paths) != 1 else ''}",
                         disp_name(dest), None, select_in=v, rename_after=True)

    def extract(self):
        v = self.cur
        paths = [p for p in v.selected_paths() if Entry(p).kind == "archive"]
        if not paths:
            return
        dest = os.path.dirname(paths[0])
        job = FileJob("extract", paths, dest)
        self.app.run_job(self, job, f"Extracting {os.path.basename(paths[0])}", os.path.basename(paths[0]),
                         disp_name(dest), select_in=v)

    def terminal(self):
        v = self.cur
        if v.is_folder() and v.mode == "folder":
            open_terminal(v.loc, self)

    def properties(self):
        v = self.cur
        if v.mode == "trash":
            ents = v.selected_entries()
            if ents:
                PropertiesDialog(self, [ents[0].path], trash_entries=ents[:1]).show_all()
            return
        paths = v.selected_paths()
        if not paths:
            if v.mode == "folder":
                paths = [v.loc]
            elif v.mode == "pc":
                paths = ["/"]
            else:
                return
        PropertiesDialog(self, paths).show_all()

    def drop_files(self, paths, dest, widget):
        if not os.path.isdir(dest):
            return
        mods = pointer_mods(widget)
        if mods & Gdk.ModifierType.CONTROL_MASK:
            op = "copy"
        elif mods & Gdk.ModifierType.SHIFT_MASK:
            op = "move"
        else:
            op = "move" if all(same_device(p, dest) for p in paths) else "copy"
        paths = [p for p in paths if os.path.realpath(os.path.dirname(p)) != os.path.realpath(dest)]
        if paths:
            self.app.transfer(self, op, paths, dest, select_in=self.cur)

    # ---------------- keyboard
    def _key(self, w, ev):
        k = ev.keyval
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(ev.state & Gdk.ModifierType.MOD1_MASK)
        kl = Gdk.keyval_to_lower(k)
        v = self.cur
        focus = self.get_focus()
        in_entry = isinstance(focus, Gtk.Entry)
        # window / tab shortcuts work everywhere
        if ctrl and not shift and kl == Gdk.KEY_t:
            self.new_tab()
            return True
        if ctrl and kl == Gdk.KEY_w:
            self.close_tab(v)
            return True
        if ctrl and not shift and kl == Gdk.KEY_n:
            self.app.new_window(v.loc)
            return True
        if ctrl and k in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self.cycle_tab(-1 if shift else 1)
            return True
        if ctrl and k in (Gdk.KEY_Page_Down, Gdk.KEY_Page_Up):
            self.cycle_tab(1 if k == Gdk.KEY_Page_Down else -1)
            return True
        if (ctrl and kl in (Gdk.KEY_l,)) or (alt and kl == Gdk.KEY_d) or k == Gdk.KEY_F4:
            self.addr.edit()
            return True
        if (ctrl and kl in (Gdk.KEY_f, Gdk.KEY_e)) or k == Gdk.KEY_F3:
            if v.is_folder():
                self.search.grab_focus()
            return True
        if k == Gdk.KEY_F5 or (ctrl and kl == Gdk.KEY_r):
            v.refresh()
            return True
        if alt and k == Gdk.KEY_Left:
            v.back()
            return True
        if alt and k == Gdk.KEY_Right:
            v.forward()
            return True
        if alt and k == Gdk.KEY_Up:
            v.up()
            return True
        if alt and shift and kl == Gdk.KEY_p:
            self.toggle_pane()
            return True
        if k == Gdk.KEY_Escape and focus is self.search:
            self.search.set_text("")
            v.focus_view()
            return True
        if in_entry or v._editing():
            return False
        if ctrl and shift and kl == Gdk.KEY_n:
            self.new_folder()
            return True
        if ctrl and shift and kl == Gdk.KEY_c:
            self.copy_path()
            return True
        if ctrl and shift and Gdk.KEY_1 <= k <= Gdk.KEY_6 or (ctrl and shift and k in (
                Gdk.KEY_exclam, Gdk.KEY_at, Gdk.KEY_numbersign, Gdk.KEY_dollar, Gdk.KEY_percent,
                Gdk.KEY_asciicircum)):
            m = {Gdk.KEY_1: "xl", Gdk.KEY_exclam: "xl", Gdk.KEY_2: "large", Gdk.KEY_at: "large",
                 Gdk.KEY_3: "medium", Gdk.KEY_numbersign: "medium", Gdk.KEY_4: "medium", Gdk.KEY_dollar: "medium",
                 Gdk.KEY_5: "details", Gdk.KEY_percent: "details", Gdk.KEY_6: "details",
                 Gdk.KEY_asciicircum: "details"}
            self.set_view(m.get(k, "details"))
            return True
        if ctrl and kl == Gdk.KEY_c:
            self.copy()
            return True
        if ctrl and kl == Gdk.KEY_x:
            self.cut()
            return True
        if ctrl and kl == Gdk.KEY_v:
            self.paste()
            return True
        if ctrl and kl == Gdk.KEY_a:
            v.select_all()
            return True
        if ctrl and kl == Gdk.KEY_h:
            self.toggle("show_hidden")
            return True
        if k == Gdk.KEY_Delete or k == Gdk.KEY_KP_Delete:
            self.delete(permanent=shift)
            return True
        if k == Gdk.KEY_F2:
            v.start_rename()
            return True
        if alt and k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.properties()
            return True
        return False


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------
class ExplorerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = None
        self.clip_op = None
        self.clip_paths = []
        self.clip_text = None
        self.clip_trash = False

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.settings = Settings()
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        self.thumbs = Thumbnailer()
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clipboard.connect("owner-change", self._clip_changed)
        try:
            self.vm = Gio.VolumeMonitor.get()
            for sig in ("mount-added", "mount-removed", "volume-added", "volume-removed"):
                self.vm.connect(sig, lambda *a: GLib.timeout_add(600, self._mounts_changed))
        except Exception:
            self.vm = None
        GLib.timeout_add_seconds(30, self._autosave)

    def _autosave(self):
        self.settings.save()
        return True

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        cwd = cmdline.get_cwd() or HOME
        opened = False
        select_next = False
        for a in args:
            if a in ("--select", "-s"):
                select_next = True
                continue
            if a.startswith("-"):
                continue
            p = uri_to_path(a) if "://" in a else os.path.join(cwd, os.path.expanduser(a))
            if a.startswith("trash:"):
                self.new_window(LOC_TRASH)
                opened = True
                continue
            if not p:
                continue
            p = os.path.abspath(p)
            if os.path.isdir(p) and not select_next:
                self.new_window(p)
            elif os.path.exists(p):
                self.new_window(os.path.dirname(p), select=[p])
            else:
                self.new_window(LOC_HOME)
            opened = True
            select_next = False
        if not opened:
            self.new_window(None)
        return 0

    def new_window(self, loc=None, select=None):
        w = ExplorerWindow(self, loc, select)
        w.present()
        return w

    def windows(self):
        return [w for w in self.get_windows() if isinstance(w, ExplorerWindow)]

    def all_views(self):
        for w in self.windows():
            yield from w.views

    def refresh_all(self):
        for w in self.windows():
            w.navpane.rebuild()
            for v in w.views:
                v.load(select=v.selected_paths())
            w.on_location(w.cur)

    def _mounts_changed(self):
        for w in self.windows():
            w.navpane.rebuild()
            w.navpane.sync(w.cur.loc)
            for v in w.views:
                if v.mode in ("pc", "net"):
                    v.load()
                elif v.is_folder() and not os.path.isdir(v.loc):
                    v.navigate(LOC_PC)
        return False

    def update_trash_icon(self):
        for w in self.windows():
            store = w.navpane.store
            for row in store:
                if row[N_TARGET] == LOC_TRASH:
                    row[N_ICON] = place_pixbuf("recycle-full" if trash_count() else "recycle", 16)

    # ---------------- quick access
    def pin(self, path):
        qa = self.settings["quick_access"]
        if path not in qa:
            qa.append(path)
            self.settings.save()
            self._qa_changed()

    def unpin(self, path):
        qa = self.settings["quick_access"]
        if path in qa:
            qa.remove(path)
            self.settings.save()
            self._qa_changed()

    def rename_pinned(self, old, new):
        qa = self.settings["quick_access"]
        if old in qa:
            qa[qa.index(old)] = new
            self._qa_changed()

    def _qa_changed(self):
        for w in self.windows():
            w.navpane.rebuild()
            w.navpane.sync(w.cur.loc)
            for v in w.views:
                if v.mode == "home":
                    v.load()

    # ---------------- clipboard
    def set_clip(self, op, paths, trash=False):
        self.clip_op = op
        self.clip_paths = list(paths)
        self.clip_trash = trash
        self.clip_text = "\n".join(paths)
        self._setting_clip = True
        self.clipboard.set_text(self.clip_text, -1)
        self._setting_clip = False
        self._mark_cut()
        for w in self.windows():
            w.on_selection(w.cur)

    def clear_clip(self):
        self.clip_op = None
        self.clip_paths = []
        self.clip_text = None
        self._mark_cut()

    def copy_text(self, text):
        self.clip_op = None
        self.clip_paths = []
        self.clip_text = None
        self.clipboard.set_text(text, -1)
        self._mark_cut()

    def _clip_changed(self, cb, ev):
        if getattr(self, "_setting_clip", False):
            return
        GLib.timeout_add(100, self._check_clip)

    def _check_clip(self):
        if self.clip_text is not None:
            t = self.clipboard.wait_for_text()
            if t != self.clip_text:
                self.clear_clip()
        for w in self.windows():
            w.on_selection(w.cur)
        return False

    def is_cut(self, path):
        return self.clip_op == "cut" and path in self.clip_paths

    def _mark_cut(self):
        cut = set(self.clip_paths) if self.clip_op == "cut" else set()
        for v in self.all_views():
            for row in v.model:
                p = row[C_PATH]
                dim = p in cut or os.path.basename(p).startswith(".")
                row[C_FG] = FG_DIM if dim else FG_NORMAL

    def has_clip(self):
        if self.clip_paths:
            return True
        try:
            return (self.clipboard.wait_is_uris_available() or
                    self.clipboard.wait_is_target_available(Gdk.Atom.intern("x-special/gnome-copied-files", False)))
        except Exception:
            return False

    def get_clip(self):
        if self.clip_paths:
            return self.clip_op, [p for p in self.clip_paths if os.path.lexists(p)]
        try:
            sd = self.clipboard.wait_for_contents(Gdk.Atom.intern("x-special/gnome-copied-files", False))
            if sd is not None and sd.get_length() > 0:
                lines = sd.get_data().decode(errors="replace").splitlines()
                op = "cut" if lines and lines[0].strip() == "cut" else "copy"
                paths = [p for p in (uri_to_path(u) for u in lines[1:]) if p and os.path.lexists(p)]
                if paths:
                    return op, paths
            uris = self.clipboard.wait_for_uris()
            if uris:
                paths = [p for p in (uri_to_path(u) for u in uris) if p and os.path.lexists(p)]
                if paths:
                    return "copy", paths
        except Exception:
            pass
        return None, []

    # ---------------- file operations
    def transfer(self, win, op, paths, dest, select_in=None):
        if not os.path.isdir(dest):
            return
        if op == "move" and self.clip_trash:
            # moving out of the Recycle Bin = restore to a chosen place
            self.clip_trash = False
        same = [p for p in paths if os.path.realpath(os.path.dirname(p)) == os.path.realpath(dest)]
        if op == "move" and len(same) == len(paths):
            return
        conflicts = []
        if not (op == "copy" and same):
            for p in paths:
                if os.path.realpath(os.path.dirname(p)) == os.path.realpath(dest):
                    continue
                if os.path.lexists(os.path.join(dest, os.path.basename(p.rstrip("/")))):
                    conflicts.append(p)
        verb = "Copying" if op == "copy" else "Moving"
        n = len(paths)
        src_dir = os.path.dirname(paths[0])
        title = f"{verb} {n} item{'s' if n != 1 else ''}"

        def go(policy):
            if policy is None:
                return
            job = FileJob(op, paths, dest, policy)
            self.run_job(win, job, title, disp_name(src_dir), disp_name(dest), select_in=select_in)
        if conflicts:
            nc = len(conflicts)
            what = (f'a file named "{os.path.basename(conflicts[0])}"' if nc == 1
                    else f"{nc} files with the same names")
            ChoiceDialog(win, "Replace or Skip Files",
                         f"{verb} {n} item{'s' if n != 1 else ''} from <b>{esc(disp_name(src_dir))}</b> to "
                         f"<b>{esc(disp_name(dest))}</b>\n\nThe destination already has {esc(what)}.",
                         [("replace", "Replace the file" + ("s" if nc > 1 else "") + " in the destination",
                           "Folders with the same name are merged"),
                          ("skip", "Skip " + ("this file" if nc == 1 else "these files"), None),
                          ("keep", "Keep both", "The new items get a number added to their names")], go)
        else:
            go("replace")

    def run_job(self, win, job, title, src_desc, dst_desc, select_in=None, rename_after=False):
        pw = ProgressWindow(win, job, title, src_desc, dst_desc)
        state = {"shown": False}

        def show():
            if not job.finished:
                pw.show_all()
                state["shown"] = True
            return False
        GLib.timeout_add(450, show)

        def done(j):
            if j.errors:
                error_list_dialog(win, "Some items couldn't be processed", j.errors)
            if j.not_trashable:
                n = len(j.not_trashable)
                confirm(win, "Delete permanently?",
                        f"{n} item{'s' if n != 1 else ''} can't be moved to the Recycle Bin on this drive. "
                        "Do you want to permanently delete " + ("it" if n == 1 else "them") + "?", "Delete",
                        lambda ok: ok and self.run_job(win, FileJob("delete", j.not_trashable),
                                                       "Deleting", None, None))
            self.update_trash_icon()
            for v in self.all_views():
                if v.mode in ("folder", "trash") and v.loc and (
                        v.mode == "trash" or v.loc in (j.dest,) or any(os.path.dirname(p) == v.loc for p in j.items)):
                    v.refresh()
            if select_in is not None and j.created and select_in.mode == "folder" and \
                    os.path.dirname(j.created[0]) == select_in.loc:
                select_in.select_paths(j.created)
                if rename_after and len(j.created) == 1:
                    GLib.idle_add(lambda: select_in.start_rename(j.created[0]) or False)
            return False
        job.done_cb = done
        job.start()

    def delete(self, win, paths, permanent=False):
        n = len(paths)
        what = f'"{os.path.basename(paths[0])}"' if n == 1 else f"these {n} items"
        if permanent:
            confirm(win, "Delete File" if n == 1 else "Delete Multiple Items",
                    f"Are you sure you want to permanently delete {what}?", "Yes",
                    lambda ok: ok and self.run_job(win, FileJob("delete", paths), f"Deleting {n} items",
                                                   disp_name(os.path.dirname(paths[0])), None), no="No")
            return

        def go(ok=True):
            if ok:
                self.run_job(win, FileJob("trash", paths), f"Deleting {n} item{'s' if n != 1 else ''}",
                             disp_name(os.path.dirname(paths[0])), None)
        if self.settings.get("confirm_delete"):
            confirm(win, "Delete File" if n == 1 else "Delete Multiple Items",
                    f"Are you sure you want to move {what} to the Recycle Bin?", "Yes", go, no="No")
        else:
            go()

    def trash_delete(self, win, ents):
        errs = []
        for e in ents:
            try:
                trash_delete(e)
            except OSError as ex:
                errs.append((e.name, ex.strerror or str(ex)))
        if errs:
            error_list_dialog(win, "Some items couldn't be deleted", errs)
        self.update_trash_icon()
        for v in self.all_views():
            if v.mode == "trash":
                v.refresh()

    def empty_trash(self, win):
        n = trash_count()
        if not n:
            return
        confirm(win, "Delete Multiple Items" if n > 1 else "Delete File",
                f"Are you sure you want to permanently delete {'these ' + str(n) + ' items' if n > 1 else 'this item'}?",
                "Yes", lambda ok: ok and self._empty(), no="No")

    def _empty(self):
        trash_empty()
        self.update_trash_icon()
        for v in self.all_views():
            if v.mode == "trash":
                v.refresh()

    # ---------------- drives
    def mount_volume(self, win, vol, view):
        if vol is None:
            return

        def done(v, res):
            try:
                v.mount_finish(res)
            except GLib.Error as e:
                info_dialog(win, "Couldn't open the drive", e.message)
                return
            m = v.get_mount()
            if m:
                view.navigate(m.get_root().get_path())
        vol.mount(Gio.MountMountFlags.NONE, NetMountOp(win), None, done)

    def eject(self, win, path):
        if not self.vm:
            return
        target = None
        for m in self.vm.get_mounts():
            r = m.get_root().get_path()
            if r == path:
                target = m
                break
        if target is None:
            info_dialog(win, "Eject", "This drive can't be ejected from here.")
            return
        for v in self.all_views():
            if v.is_folder() and is_inside(v.loc, path):
                v.navigate(LOC_PC)

        def done(m, res):
            try:
                if m.can_eject():
                    m.eject_with_operation_finish(res)
                else:
                    m.unmount_with_operation_finish(res)
                if not is_gvfs_path(path):
                    info_dialog(win, "Safe To Remove Hardware", "The drive can now be safely removed.")
            except GLib.Error as e:
                info_dialog(win, "Problem Ejecting Drive", e.message)
        op = NetMountOp(win)
        if target.can_eject():
            target.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)
        else:
            target.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)


# ---------------- network (ExplorerApp methods)
def _open_uri(self, win, view, uri):
    """Open a network location (smb://host/share/...). Mounts it first if needed."""
    u = urllib.parse.urlparse(uri)
    if u.scheme == "smb" and not u.path.strip("/"):
        view.navigate(host_loc(f"smb://{u.hostname}/"))
        return
    gf = Gio.File.new_for_uri(uri)

    def go():
        p = gf.get_path()
        if not p:
            info_dialog(win, "Can't show this folder",
                        "The network folder is connected, but File Explorer needs gvfs-fuse to open it.\n\n"
                        "Install it with:  sudo apt install gvfs-fuse  and then log out and back in.")
            return
        view.navigate(p)

    try:
        gf.find_enclosing_mount(None)
        go()
        return
    except GLib.Error:
        pass

    def done(f, res):
        try:
            f.mount_enclosing_volume_finish(res)
        except GLib.Error as e:
            if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.FAILED_HANDLED):
                    info_dialog(win, "Couldn't open the network folder", e.message)
                return
        go()
    gf.mount_enclosing_volume(Gio.MountMountFlags.NONE, NetMountOp(win), None, done)


def _open_network_path(self, win, view, text):
    t = text.strip()
    if t.startswith("\\\\"):
        t = "smb://" + t.lstrip("\\").replace("\\", "/")
    elif t.startswith("//"):
        t = "smb:" + t
    elif "://" not in t:
        t = "smb://" + t.replace("\\", "/")
    parts = urllib.parse.urlparse(t)
    if not parts.hostname:
        info_dialog(win, "File Explorer", f"'{text}' isn't a network address. Try \\\\server\\share.")
        return
    path = "/".join(urllib.parse.quote(seg) for seg in urllib.parse.unquote(parts.path).split("/"))
    self.open_uri(win, view, f"{parts.scheme}://{parts.netloc}{path}")


def _pick_and_share(self, win):
    dlg = Gtk.FileChooserDialog(title="Pick a folder to share", transient_for=win,
                                action=Gtk.FileChooserAction.SELECT_FOLDER)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Share", Gtk.ResponseType.OK)
    dlg.set_current_folder(HOME)
    r = dlg.run()
    path = dlg.get_filename()
    dlg.destroy()
    if r == Gtk.ResponseType.OK and path:
        self.share_folder(win, path)


def _share_folder(self, win, path):
    if not samba_installed():
        info_dialog(win, "Sharing needs Samba", "Install it with:  sudo apt install samba")
        return
    if not helper_available():
        info_dialog(win, "Sharing isn't set up", "Run the File Explorer installer again to turn on sharing.")
        return
    existing = share_for_path(path)
    if existing:
        info_dialog(win, "Already shared",
                    f"This folder is already shared as \\\\{server_name()}\\{existing['name']}")
        return
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.pack_start(label("Share name"), False, False, 0)
    ent = Gtk.Entry()
    add_cls(ent, "field")
    ent.set_text(share_name_from(path))
    ent.set_activates_default(True)
    box.pack_start(ent, False, False, 0)
    rw = Gtk.CheckButton(label="Let people change, add and delete files")
    rw.set_active(True)
    box.pack_start(rw, False, False, 0)
    note = label("People on your network open it in File Explorer on Windows (or Finder on a Mac) "
                 "and sign in with your Samba username and password.", "ter", wrap=True)
    note.set_max_width_chars(52)
    box.pack_start(note, False, False, 0)
    dlg = WinDialog(win, f"Share \"{os.path.basename(path.rstrip('/')) or path}\"", None, primary="Share", extra=box)

    def done(ok):
        if not ok:
            return
        name = ent.get_text().strip()
        if not re.match(r"^[A-Za-z0-9_-]{1,40}$", name):
            info_dialog(win, "Pick another name", "Use letters, numbers, - or _ only (no spaces), up to 40.")
            return

        def work():
            ok2, msg = run_helper("share-add", name, path, "rw" if rw.get_active() else "ro")
            GLib.idle_add(finished, ok2, msg, name)
        threading.Thread(target=work, daemon=True).start()

    def finished(ok2, msg, name):
        if ok2:
            srv = server_name()
            info_dialog(win, "Folder shared",
                        f"Open it from another computer at:\n\n\\\\{srv}\\{name}\n\n"
                        f"On a Mac use  smb://{srv.lower()}.local/{name}")
        else:
            info_dialog(win, "Couldn't share the folder", msg or "Unknown error")
        self._net_changed()
        return False
    dlg.ask(done)
    ent.grab_focus()


def _unshare(self, win, name):
    def go(ok):
        if not ok:
            return

        def work():
            ok2, msg = run_helper("share-remove", name)
            GLib.idle_add(finished, ok2, msg)
        threading.Thread(target=work, daemon=True).start()

    def finished(ok2, msg):
        if not ok2:
            info_dialog(win, "Couldn't stop sharing", msg or "Unknown error")
        self._net_changed()
        return False
    confirm(win, "Stop sharing", f"Stop sharing \"{name}\" on the network? The folder and its files stay on this Pi.",
            "Stop sharing", go)


def _net_changed(self):
    for v in self.all_views():
        if v.mode == "net":
            v.load()
    for w in self.windows():
        w.on_selection(w.cur)


ExplorerApp.open_uri = _open_uri
ExplorerApp.open_network_path = _open_network_path
ExplorerApp.pick_and_share = _pick_and_share
ExplorerApp.share_folder = _share_folder
ExplorerApp.unshare = _unshare
ExplorerApp._net_changed = _net_changed


def main():
    GLib.set_prgname("pi-explorer")
    GLib.set_application_name("File Explorer")
    app = ExplorerApp()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
