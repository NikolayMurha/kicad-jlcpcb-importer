"""Contains helper function used all over the plugin."""

import os
import re

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

from .plugin_paths import PLUGIN_PATH


def as_bool(value, default: bool = False) -> bool:
    """Normalize JSON/UI boolean values consistently across the plugin."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return default


def getWxWidgetsVersion():
    """Get wx widgets version."""
    v = re.search(r"wxWidgets\s([\d\.]+)", wx.version())
    v = int(v.group(1).replace(".", ""))
    return v


def getVersion():
    """READ Version from file."""
    if not os.path.isfile(os.path.join(PLUGIN_PATH, "VERSION")):
        return "unknown"
    with open(os.path.join(PLUGIN_PATH, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def GetOS():
    """Get String with OS type."""
    return wx.PlatformInformation.Get().GetOperatingSystemIdName()


def GetScaleFactor(window):
    """Workaround if wxWidgets Version does not support GetDPIScaleFactor, for Mac OS always return 1.0."""
    if "Apple Mac OS" in GetOS():
        return 1.0
    if hasattr(window, "GetDPIScaleFactor"):
        return window.GetDPIScaleFactor()
    return 1.0


def HighResWxSize(window, size):
    """Workaround if wxWidgets Version does not support FromDIP."""
    if hasattr(window, "FromDIP"):
        return window.FromDIP(size)
    return size


def loadBitmapScaled(filename, scale=1.0, static=False):
    """Load a scaled bitmap, handle differences between Kicad versions."""
    if filename:
        path = os.path.join(PLUGIN_PATH, "icons", filename)
        bmp = wx.Bitmap(path)
        w, h = bmp.GetSize()
        img = bmp.ConvertToImage()
        if hasattr(wx.SystemSettings, "GetAppearance") and hasattr(
            wx.SystemSettings.GetAppearance, "IsUsingDarkBackground"
        ):
            if wx.SystemSettings.GetAppearance().IsUsingDarkBackground():
                img.Replace(0, 0, 0, 255, 255, 255)
            bmp = wx.Bitmap(img.Scale(int(w * scale), int(h * scale)))
    else:
        bmp = wx.Bitmap()
    if getWxWidgetsVersion() > 315 and not static:
        return wx.BitmapBundle(bmp)
    return bmp


def loadIconScaled(filename, scale=1.0):
    """Load a scaled icon, handle differences between Kicad versions."""
    bmp = loadBitmapScaled(filename, scale=scale, static=False)
    if getWxWidgetsVersion() > 315:
        return bmp
    return wx.Icon(bmp)


def apply_button_label_tooltips(window, overwrite: bool = True):
    """Apply button tooltips from button labels for all buttons in a window tree."""
    if window is None:
        return
    stack = [window]
    while stack:
        current = stack.pop()
        try:
            children = list(current.GetChildren())
        except Exception:
            children = []
        stack.extend(children)

        if not isinstance(current, wx.Button):
            continue
        try:
            label = str(current.GetLabel() or "").strip()
        except Exception:
            label = ""
        if not label:
            continue
        if not overwrite:
            try:
                existing = str(current.GetToolTipText() or "").strip()
            except Exception:
                existing = ""
            if existing:
                continue
        try:
            current.SetToolTip(label)
        except Exception:
            continue


def natural_sort_collation(a, b):
    """Natural sort collation for use in sqlite."""
    if a == b:
        return 0

    def convert(text):
        return int(text) if text.isdigit() else text.lower()

    def alphanum_key(key):
        return [convert(c) for c in re.split("([0-9]+)", key)]

    natorder = sorted([a, b], key=alphanum_key)
    return -1 if natorder.index(a) == 0 else 1


def sanitize_lib_name(name: str) -> str:
    """Convert a string to a safe library/folder name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned or "Misc"


def strip_lcsc_suffix(name: str) -> str:
    """Drop trailing LCSC suffix from part name, e.g. ``AO3400A_C123456`` -> ``AO3400A``."""
    raw = str(name or "").strip()
    if not raw:
        return raw
    stripped = re.sub(r"_C\d+$", "", raw)
    return stripped or raw


def dict_factory(cursor, row) -> dict:
    """Row factory that returns a dict."""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d
