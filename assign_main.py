"""Assign LCSC main dialog wrapping the part selector as primary UI."""

import os
import json
import sys
import logging
import wx
from typing import Optional

from .partselector import PartSelectorDialog
from .helpers import HighResWxSize, loadBitmapScaled, GetScaleFactor, PLUGIN_PATH
from .events import (
    EVT_LOGBOX_APPEND_EVENT,
    EVT_MESSAGE_EVENT,
    EVT_DOWNLOAD_STARTED_EVENT,
    EVT_DOWNLOAD_PROGRESS_EVENT,
    EVT_DOWNLOAD_COMPLETED_EVENT,
    EVT_UNZIP_COMBINING_STARTED_EVENT,
    EVT_UNZIP_COMBINING_PROGRESS_EVENT,
    EVT_UNZIP_EXTRACTING_STARTED_EVENT,
    EVT_UNZIP_EXTRACTING_PROGRESS_EVENT,
    EVT_UNZIP_EXTRACTING_COMPLETED_EVENT,
    LogboxAppendEvent,
    MessageEvent,
    UpdateSetting,
)
from .library import Library

import pcbnew as kicad_pcbnew  # pylint: disable=import-error


class KicadProvider:
    """KiCad provider for board access."""

    def get_pcbnew(self):  # pragma: no cover - depends on KiCad runtime
        return kicad_pcbnew


class AssignLCSCMainDialog(PartSelectorDialog):
    """Main plugin window that focuses on assigning LCSC numbers without legacy mainwindow."""

    def __init__(self, kicad_provider: Optional[KicadProvider] = None):
        # Minimal context expected by PartSelectorDialog
        self.pcbnew = (kicad_provider or KicadProvider()).get_pcbnew()
        self.window = self  # fallback until wx top-level is available
        self.scale_factor = 1.0

        # Project context
        try:
            board = self.pcbnew.GetBoard()
            self.project_path = os.path.split(board.GetFileName())[0]
            self.board_name = os.path.split(board.GetFileName())[1]
            self.schematic_name = f"{self.board_name.split('.')[0]}.kicad_sch"
        except Exception:
            self.project_path = os.getcwd()
            self.board_name = "board.kicad_pcb"
            self.schematic_name = "board.kicad_sch"

        # Settings and library context
        self.settings = {}
        self._load_settings()
        self.library = Library(self)

        # Build the PartSelectorDialog with self as the logical parent context
        super().__init__(self, parts={})

        # Now that wx is initialized, update window and scale factor
        self.window = wx.GetTopLevelParent(self) or self
        self.scale_factor = GetScaleFactor(self.window)

        # Insert a topbar with Update button
        topbar = wx.BoxSizer(wx.HORIZONTAL)
        self.update_db_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Update database",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(160, -1)),
            0,
        )
        self.update_db_btn.SetBitmap(
            loadBitmapScaled("mdi-database-import-outline.png", self.scale_factor)
        )
        self.update_db_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.update_db_btn, 0, wx.ALL, 5)

        # Clear log button
        self.clear_log_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Clear log",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(120, -1)),
            0,
        )
        self.clear_log_btn.SetBitmap(
            loadBitmapScaled("mdi-trash-can-outline.png", self.scale_factor)
        )
        self.clear_log_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.clear_log_btn, 0, wx.ALL, 5)

        layout = self.GetSizer()
        if layout:
            layout.Insert(0, topbar, 0, wx.EXPAND | wx.ALL, 5)

        # Add bottom console
        self.console = wx.TextCtrl(
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.console.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 140)))
        # Progress gauge under the console
        self.gauge = wx.Gauge(
            self,
            wx.ID_ANY,
            100,
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(100, -1)),
            wx.GA_HORIZONTAL,
        )
        self.gauge.SetValue(0)
        self.gauge.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 5)))
        if layout:
            layout.Add(self.console, 0, wx.ALL | wx.EXPAND, 5)
            layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)
            self.Layout()

        # Wire events and actions
        self.update_db_btn.Bind(wx.EVT_BUTTON, lambda _evt: self.update_library())
        # Handle messages and progress locally
        self.Bind(EVT_LOGBOX_APPEND_EVENT, self._append_log)
        self.Bind(EVT_MESSAGE_EVENT, self._show_message)
        self.Bind(EVT_DOWNLOAD_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_DOWNLOAD_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_DOWNLOAD_COMPLETED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_COMBINING_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_COMBINING_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_UNZIP_EXTRACTING_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_EXTRACTING_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_UNZIP_EXTRACTING_COMPLETED_EVENT, self._on_progress_reset)
        # Settings updates from PartSelectorDialog
        from .events import EVT_UPDATE_SETTING  # local import to avoid cycle
        self.Bind(EVT_UPDATE_SETTING, self._on_update_setting)
        self.clear_log_btn.Bind(wx.EVT_BUTTON, self._clear_log)

        # Initialize logging to forward to the bottom console
        self._init_logger()

    # Override: do not assign in this simplified window — show placeholder
    def select_part(self, *_):  # noqa: N802 (KiCad naming)
        wx.PostEvent(
            self,
            MessageEvent(title="Not implemented", text="Select part is a placeholder here.", style="info"),
        )

    # Settings persistence
    def _load_settings(self):
        try:
            with open(os.path.join(PLUGIN_PATH, "settings.json"), encoding="utf-8") as j:
                self.settings = json.load(j)
        except Exception:
            self.settings = {}

    def _save_settings(self):
        try:
            with open(os.path.join(PLUGIN_PATH, "settings.json"), "w", encoding="utf-8") as j:
                json.dump(self.settings, j)
        except Exception:
            pass

    # Called when PartSelectorDialog posts UpdateSetting
    def _on_update_setting(self, e):
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value
        self._save_settings()

    # Expose update for button
    def update_library(self):
        self.library.update()

    # Local handlers
    def _append_log(self, e):
        if hasattr(self, "console") and self.console:
            # AppendText works reliably with read-only TextCtrl
            self.console.AppendText(e.msg)

    def _show_message(self, e):
        styles = {"info": wx.ICON_INFORMATION, "warning": wx.ICON_WARNING, "error": wx.ICON_ERROR}
        wx.MessageBox(e.text, e.title, style=styles.get(e.style, wx.ICON_INFORMATION))

    def _clear_log(self, *_):
        if hasattr(self, "console") and self.console:
            self.console.Clear()

    # Progress handlers
    def _on_progress_reset(self, *_):
        if hasattr(self, "gauge") and self.gauge:
            self.gauge.SetRange(100)
            self.gauge.SetValue(0)

    def _on_progress_update(self, e):
        if hasattr(self, "gauge") and self.gauge:
            try:
                val = int(e.value)
            except Exception:
                val = 0
            self.gauge.SetValue(max(0, min(100, val)))

    # Logging setup similar to legacy mainwindow to capture logs in UI
    def _init_logger(self):
        root = logging.getLogger()
        # Avoid stale/duplicate handlers
        try:
            root.handlers.clear()
        except Exception:
            # Fallback for older Python where handlers is read-only
            for h in list(root.handlers):
                root.removeHandler(h)
        root.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(funcName)s -  %(message)s",
            datefmt="%Y.%m.%d %H:%M:%S",
        )

        if sys.stderr is not None:
            self._stderr_handler = logging.StreamHandler(sys.stderr)
            self._stderr_handler.setLevel(logging.DEBUG)
            self._stderr_handler.setFormatter(formatter)
            root.addHandler(self._stderr_handler)

        self._ui_log_handler = _LogBoxHandler(self)
        self._ui_log_handler.setLevel(logging.DEBUG)
        self._ui_log_handler.setFormatter(formatter)
        root.addHandler(self._ui_log_handler)

    def Destroy(self):  # noqa: N802 - wx override
        # Clean up logging handlers to avoid duplicates on reopen
        try:
            root = logging.getLogger()
            if hasattr(self, "_stderr_handler"):
                root.removeHandler(self._stderr_handler)
            if hasattr(self, "_ui_log_handler"):
                root.removeHandler(self._ui_log_handler)
        except Exception:
            pass
        return super().Destroy()


class _LogBoxHandler(logging.StreamHandler):
    """Forward Python logging records to the wx UI via events."""

    def __init__(self, event_destination):
        super().__init__()
        self._event_destination = event_destination

    def emit(self, record):
        try:
            msg = self.format(record)
            wx.PostEvent(self._event_destination, LogboxAppendEvent(msg=f"{msg}\n"))
        except Exception:
            # Never raise from logging
            pass
