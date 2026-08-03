"""Native application presentation helpers for the external IPC process."""

from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path
import sys
from typing import Any


APPLICATION_NAME = "JLCPCB Importer"
WINDOWS_APP_USER_MODEL_ID = "NikolayMurha.KiCad.JLCPCBImporter"


def configure_application(wx_app: Any, icon_path: Path) -> bool:
    """Set process-level name and icon without modifying KiCad or Python."""

    try:
        wx_app.SetAppName(APPLICATION_NAME)
        wx_app.SetAppDisplayName(APPLICATION_NAME)
    except Exception:
        pass

    if sys.platform == "darwin":
        return _configure_macos_dock_icon(icon_path)
    if sys.platform == "win32":
        return _configure_windows_identity()
    return icon_path.is_file()


def configure_application_window(window: Any, icon_path: Path) -> bool:
    """Set the top-level window icon used by Windows and Linux task switchers."""

    if not icon_path.is_file():
        return False

    try:
        import wx

        icon = wx.Icon(str(icon_path), wx.BITMAP_TYPE_PNG)
        if not icon.IsOk():
            return False
        window.SetIcon(icon)
        return True
    except Exception:
        return False


def activate_application_window(window: Any) -> bool:
    """Bring the plugin window to the foreground after an explicit user action."""

    native_activation = False
    if sys.platform == "darwin":
        native_activation = _activate_macos_application()
    elif sys.platform == "win32":
        native_activation = _activate_windows_window(window)

    try:
        if window.IsIconized():
            window.Iconize(False)
        window.Raise()
        window.SetFocus()
        if not native_activation:
            try:
                import wx

                window.RequestUserAttention(wx.USER_ATTENTION_INFO)
            except Exception:
                pass
        return True
    except Exception:
        return native_activation


def _configure_windows_identity() -> bool:
    """Keep the plugin in its own Windows taskbar group."""

    try:
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            WINDOWS_APP_USER_MODEL_ID
        )
        return result == 0
    except Exception:
        return False


def _activate_windows_window(window: Any) -> bool:
    """Restore and foreground the plugin window on Windows."""

    try:
        handle = int(window.GetHandle())
        ctypes.windll.user32.ShowWindow(handle, 9)  # SW_RESTORE
        return bool(ctypes.windll.user32.SetForegroundWindow(handle))
    except Exception:
        return False


def _activate_macos_application() -> bool:
    """Activate Python.app so Raise() can place the wx window over pcbnew."""

    try:
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            return False
        objc = ctypes.cdll.LoadLibrary(objc_path)
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        send_id = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        send_void_bool = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_bool,
        )(("objc_msgSend", objc))
        application_class = objc.objc_getClass(b"NSApplication")
        if not application_class:
            return False
        selector = objc.sel_registerName
        application = send_id(application_class, selector(b"sharedApplication"))
        if not application:
            return False
        send_void_bool(
            application,
            selector(b"activateIgnoringOtherApps:"),
            True,
        )
        return True
    except Exception:
        return False


def _configure_macos_dock_icon(icon_path: Path) -> bool:
    """Set the Dock icon for the current process through the Objective-C runtime."""

    if not icon_path.is_file():
        return False

    try:
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            return False
        objc = ctypes.cdll.LoadLibrary(objc_path)

        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p

        send_id = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        send_id_cstr = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
        )(("objc_msgSend", objc))
        send_id_id = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        send_void_id = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))

        selector = objc.sel_registerName
        ns_application = objc.objc_getClass(b"NSApplication")
        ns_image = objc.objc_getClass(b"NSImage")
        ns_string = objc.objc_getClass(b"NSString")
        if not ns_application or not ns_image or not ns_string:
            return False

        application = send_id(ns_application, selector(b"sharedApplication"))
        path_string = send_id_cstr(
            ns_string,
            selector(b"stringWithUTF8String:"),
            str(icon_path).encode("utf-8"),
        )
        image = send_id_id(
            send_id(ns_image, selector(b"alloc")),
            selector(b"initWithContentsOfFile:"),
            path_string,
        )
        if not application or not image:
            return False

        send_void_id(application, selector(b"setApplicationIconImage:"), image)
        return True
    except Exception:
        return False
