"""Assign LCSC main dialog wrapping the part selector as primary UI."""

import os
import json
import sys
import logging
import subprocess
import shutil
import threading
from pathlib import Path
import wx
from typing import Optional, Dict

from .partselector import PartSelectorDialog
from .settings import SettingsDialog
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
)
from .library import Library
from .easyeda_importer import EasyedaImporter

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
            self.project_path, self.board_name, self.schematic_name = self._detect_project_context()
        except Exception:
            self.project_path = os.getcwd()
            self.board_name = "board.kicad_pcb"
            self.schematic_name = "board.kicad_sch"

        # Settings and library context
        self.settings = {}
        self._load_settings()
        self.library = Library(self)
        # Dependencies state
        self._deps_ready = False

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

        # Settings button
        self.settings_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Settings",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(120, -1)),
            0,
        )
        self.settings_btn.SetBitmap(
            loadBitmapScaled("mdi-cog-outline.png", self.scale_factor)
        )
        self.settings_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.settings_btn, 0, wx.ALL, 5)

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
        self.settings_btn.Bind(wx.EVT_BUTTON, self._open_settings)

        # Initialize logging to forward to the bottom console
        self._init_logger()
        # Log resolved project context for clarity
        try:
            self.log(
                f"Project: {self.project_path}\n"
                f"Board: {self.board_name}\n"
                f"Schematic: {self.schematic_name}\n"
            )
        except Exception:
            pass

        # On first window launch, verify deps and offer installation if missing
        self._check_and_offer_install_deps()
        # Ensure UI matches current deps state
        self._update_select_enabled()

    def _resolve_python_exe(self) -> str:
        try:
            exe = sys.executable or ""
            name = os.path.basename(exe).lower()
            if exe and ("python" in name):
                return exe
            major, minor = sys.version_info.major, sys.version_info.minor
            candidates = [
                Path(sys.exec_prefix) / "bin" / f"python{major}.{minor}",
                Path(sys.exec_prefix) / "bin" / "python3",
                Path(sys.exec_prefix) / "bin" / "python",
            ]
            if sys.platform.startswith("win"):
                candidates.extend([Path(sys.exec_prefix) / "python.exe"])
            import shutil as _sh
            for c in [*candidates, _sh.which("python3"), _sh.which("python")]:
                if not c:
                    continue
                p = str(c)
                if os.path.exists(p) and os.access(p, os.X_OK):
                    return p
        except Exception:
            pass
        return sys.executable or "python3"

    def _run_and_stream(self, cmd, env=None) -> int:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            if proc.stdout is not None:
                for line in proc.stdout:
                    self.log(line)
            return proc.wait()
        except Exception as e:
            self.log(f"Execution error: {e}\n")
            return 1

    # Override: do not assign in this simplified window — show placeholder
    def select_part(self, *_):  # noqa: N802 (KiCad naming)
        if not getattr(self, "_deps_ready", False):
            # Re-offer installation when user attempts to select
            self._check_and_offer_install_deps(force_prompt=True)
            return
        try:
            if getattr(self, "part_list", None) is None:
                wx.PostEvent(
                    self,
                    MessageEvent(title="Error", text="Component list is unavailable.", style="error"),
                )
                return
            if self.part_list.GetSelectedItemsCount() <= 0:
                wx.PostEvent(
                    self,
                    MessageEvent(title="No selection", text="Select an item in the list.", style="warning"),
                )
                return
            item = self.part_list.GetSelection()
            lcsc_id = str(self.part_list_model.get_lcsc(item)).strip()
            # Also capture metadata for symbol augmentation
            try:
                category = str(self.part_list_model.get_category(item)).strip()
            except Exception:
                category = ""
            try:
                mfr_part = str(self.part_list_model.get_mfr_number(item)).strip()
            except Exception:
                mfr_part = ""
            try:
                manufacturer = str(self.part_list_model.get_manufacturer(item)).strip()
            except Exception:
                manufacturer = ""
            try:
                descr = str(self.part_list_model.get_description(item)).strip()
            except Exception:
                descr = ""
            try:
                attributes_json = str(self.part_list_model.get_attributes(item)).strip()
            except Exception:
                attributes_json = ""
            if not lcsc_id:
                wx.PostEvent(
                    self,
                    MessageEvent(title="No LCSC", text="No LCSC ID in the selected row.", style="warning"),
                )
                return
        except Exception:
            wx.PostEvent(
                self,
                MessageEvent(title="Error", text="Failed to obtain LCSC ID.", style="error"),
            )
            return

        meta = {
            "mfr_part": mfr_part,
            "manufacturer": manufacturer,
            "description": descr,
            "attributes_json": attributes_json,
        }
        # Print a message to the UI console
        try:
            wx.PostEvent(self, LogboxAppendEvent(msg=f"Meta prepared for import: {meta}\n"))
        except Exception:
            pass
        self._import_part_via_easyeda(lcsc_id, category, meta)

    def _import_part_via_easyeda(self, lcsc_id: str, category: str = "", meta: Optional[Dict] = None):
        base = Path(__file__).resolve().parent
        lib_dir = base / "lib"
        scope = self._ensure_library_scope_selected()
        if not scope:
            wx.PostEvent(
                self, LogboxAppendEvent(msg="Import canceled: no library location selected.\n")
            )
            return

        btn = getattr(self, "select_part_button", None)
        if btn is not None:
            btn.Enable(False)

        importer = EasyedaImporter(
            project_path=self.project_path,
            python_exe=self._resolve_python_exe(),
            parent_window=self,
            scope=str(scope),
            lib_dir=lib_dir,
        )

        def _worker():
            wx.BeginBusyCursor()
            try:
                ok, lib_base = importer.import_part(
                    lcsc_id=lcsc_id,
                    category=category,
                    meta=meta or {},
                )
            except Exception as e:
                wx.PostEvent(self, LogboxAppendEvent(msg=f"{e}\n"))
            finally:
                wx.EndBusyCursor()
            
            if btn is not None:
                wx.CallAfter(btn.Enable, True)
            
            if ok:
                wx.PostEvent(
                    self, LogboxAppendEvent(msg=f"Import completed. Files at: {lib_base}\n")
                )
                wx.CallAfter(
                    wx.MessageBox,
                    f"Imported {lcsc_id} into project.\nFolder: {lib_base}",
                    "Done",
                    wx.ICON_INFORMATION,
                )
            else:
                wx.PostEvent(self, LogboxAppendEvent(msg="Import finished with an error.\n"))
                wx.CallAfter(
                    wx.MessageBox,
                    "Failed to import component. Check the log above.",
                    "Error",
                    wx.ICON_ERROR,
                )
        threading.Thread(target=_worker, daemon=True).start()

    def _ensure_library_scope_selected(self) -> Optional[str]:
        """Return existing library scope or ask the user to choose.

        Returns "project" or "system". Returns None if the user cancels.
        """
        try:
            scope = None
            if isinstance(self.settings, dict):
                scope = (self.settings.get("general", {}) or {}).get("library_scope")
            
            if scope in ("project", "system"):
                return scope

            # Ask user
            choice = self._ask_library_scope_dialog()
            if choice in ("project", "system"):
                if "general" not in self.settings:
                    self.settings["general"] = {}
                self.settings["general"]["library_scope"] = choice
                self._save_settings()
                return choice
            return None
        except Exception:
            return None

    def _ask_library_scope_dialog(self) -> Optional[str]:
        dlg = wx.Dialog(
            self,
            title="Where to store libraries?",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=HighResWxSize(self, wx.Size(420, 180)),
        )
        try:
            vbox = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(
                dlg,
                label=(
                    "Where to store and which library to use for saving?\n"
                    "Choose location for symbols and footprints."
                ),
            )
            vbox.Add(text, 0, wx.ALL | wx.EXPAND, 10)

            hbox = wx.BoxSizer(wx.HORIZONTAL)
            btn_project = wx.Button(dlg, wx.ID_ANY, "Project level")
            btn_system = wx.Button(dlg, wx.ID_ANY, "System level")
            hbox.Add(btn_project, 1, wx.ALL | wx.EXPAND, 5)
            hbox.Add(btn_system, 1, wx.ALL | wx.EXPAND, 5)
            vbox.Add(hbox, 0, wx.ALL | wx.EXPAND, 5)

            dlg.SetSizer(vbox)
            dlg.Layout()

            result: dict[str, str | None] = {"choice": None}

            def _choose_project(_evt):
                result["choice"] = "project"
                dlg.EndModal(wx.ID_OK)

            def _choose_system(_evt):
                result["choice"] = "system"
                dlg.EndModal(wx.ID_OK)

            btn_project.Bind(wx.EVT_BUTTON, _choose_project)
            btn_system.Bind(wx.EVT_BUTTON, _choose_system)

            dlg.CentreOnParent()
            dlg.ShowModal()
            return result["choice"]
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass

    # Footprint 3D model rewriting moved to FootprintEditor
    # Nickname prefix resolver moved to EasyedaImporter

    @staticmethod
    def _safe_remove(path: Path) -> int:
        # Remove 
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
            return 1
        if path.exists() and path.is_file():
            path.unlink()
            return 1
        return 0

    def _detect_project_context(self):
        """Detect project root and names robustly."""
        # 1) Use KIPRJMOD when available
        kiprjmod = os.environ.get("KIPRJMOD")
        board = self.pcbnew.GetBoard()
        project_dir = kiprjmod
        board_name = os.path.split(board.GetFileName())[1] if board else "board.kicad_pcb"
        schematic_name = f"{board_name.split('.')[0]}.kicad_sch"
        return project_dir, board_name, schematic_name

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

    # Unified logging: always post an event, thread-safe
    def log(self, msg: str) -> None:
        try:
            wx.PostEvent(self, LogboxAppendEvent(msg=msg))
        except Exception:
            pass

    def _show_message(self, e):
        styles = {"info": wx.ICON_INFORMATION, "warning": wx.ICON_WARNING, "error": wx.ICON_ERROR}
        wx.MessageBox(e.text, e.title, style=styles.get(e.style, wx.ICON_INFORMATION))

    def _clear_log(self, *_):
        if hasattr(self, "console") and self.console:
            self.console.Clear()

    def _open_settings(self, *_):
        dlg = None
        try:
            dlg = SettingsDialog(self)
            dlg.ShowModal()
        finally:
            try:
                if dlg is not None:
                    dlg.Destroy()
            except Exception:
                pass

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

        # Keep stderr quiet to avoid external popups; only emit severe errors
        if sys.stderr is not None:
            self._stderr_handler = logging.StreamHandler(sys.stderr)
            self._stderr_handler.setLevel(logging.ERROR)
            self._stderr_handler.setFormatter(formatter)
            root.addHandler(self._stderr_handler)

        self._ui_log_handler = LogBoxHandler(self)
        self._ui_log_handler.setLevel(logging.DEBUG)
        self._ui_log_handler.setFormatter(formatter)
        root.addHandler(self._ui_log_handler)

    # Dependency check and interactive installer
    def _check_and_offer_install_deps(self, force_prompt: bool = False):
        try:
            # Check runtime deps: pip (easyeda2kicad) only
            __import__("easyeda2kicad")
            self._deps_ready = True
            self._update_select_enabled()
            return
        except Exception as e:
            self.log(f"Error: {e}\n")
            pass

        msg = (
            "Required dependency (easyeda2kicad) not found.\n"
            "Install pip dependencies into the local 'lib/' folder?"
        )
        if force_prompt or not self._deps_ready:
            dlg = wx.MessageDialog(
                self,
                message=msg,
                caption="Install dependencies",
                style=wx.YES_NO | wx.ICON_QUESTION,
            )
            res = dlg.ShowModal()
            dlg.Destroy()
            if res == wx.ID_YES:
                # While installing, keep selection disabled
                self._deps_ready = False
                self._update_select_enabled()
                self._install_requirements()
            else:
                # User declined: lock selection until installed
                self._deps_ready = False
                self._update_select_enabled()

    def _install_requirements(self):
        base = Path(__file__).resolve().parent
        req = base / "requirements.txt"
        lib_dir = base / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)

        if not req.exists():
            self.log("requirements.txt not found.\n")
            wx.MessageBox("requirements.txt not found", "Error", style=wx.ICON_ERROR)
            return

        # Resolve a real Python interpreter (KiCad on macOS sets sys.executable to the app binary)
        py_exe = self._resolve_python_exe()
        cmd = [
            py_exe,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "-r",
            str(req),
            "--target",
            str(lib_dir),
        ]
        self.log(f"Interpreter: {py_exe}\n")
        self.log(f"Command: {' '.join(cmd)}\n")
        wx.BeginBusyCursor()

        def _worker():
            try:
                self.log(f"Running: {' '.join(cmd)}\n")
                ret = self._run_and_stream(cmd)
            except Exception as e:  # process spawn/read failure
                self.log(f"Installation error: {e}\n")
                wx.CallAfter(
                    wx.MessageBox,
                    "Failed to launch dependency installation.",
                    "Error",
                    wx.ICON_ERROR,
                )
                ret = 1
            finally:
                if wx.IsBusy():
                    wx.CallAfter(wx.EndBusyCursor)

            if ret != 0:
                # Try to bootstrap pip if it was missing
                try:
                    self.log("Attempting to install pip via ensurepip...\n")
                    ensure_cmd = [cmd[0], "-m", "ensurepip", "--upgrade"]
                    ret2 = self._run_and_stream(ensure_cmd)
                    if ret2 == 0:
                        self.log("pip installed. Retrying dependency installation...\n")
                        ret = self._run_and_stream(cmd)
                except Exception:
                    pass
                if ret != 0:
                    self.log("pip exited with an error.\n")
                    wx.CallAfter(
                        wx.MessageBox,
                        "Failed to install dependencies. Check network/pip access.",
                        "Error",
                        wx.ICON_ERROR,
                    )
                    return

            # Successful install path: make lib visible and validate import
            try:
                import site
                import importlib
                site.addsitedir(str(lib_dir))
                if str(lib_dir) not in sys.path:
                    sys.path.insert(0, str(lib_dir))
                importlib.invalidate_caches()
                __import__("easyeda2kicad")
                self._deps_ready = True
                self.log("Dependencies installed successfully.\n")
                wx.CallAfter(self._update_select_enabled)
                wx.CallAfter(
                    wx.MessageBox,
                    "Dependencies installed successfully.",
                    "Done",
                    wx.ICON_INFORMATION,
                )
            except Exception as e:
                self.log(f"Installation finished, but import failed: {e}.\n")
                self._deps_ready = False
                wx.CallAfter(self._update_select_enabled)
                wx.CallAfter(
                    wx.MessageBox,
                    "Installation finished, but import failed. Try restarting KiCad.",
                    "Warning",
                    wx.ICON_WARNING,
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _update_select_enabled(self):
        try:
            btn = getattr(self, "select_part_button", None)
            if btn is not None:
                btn.Enable(bool(self._deps_ready))
                if self._deps_ready:
                    btn.SetToolTip("")
                else:
                    btn.SetToolTip("Dependencies not installed. Install to enable.")
        except Exception:
            pass

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

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Convert category name to a safe folder name."""
        try:
            import re as _re
            cleaned = _re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
            return cleaned or "Misc"
        except Exception:
            return "Misc"

class LogBoxHandler(logging.StreamHandler):
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

    # --- helpers for system-wide 3rdparty integration ---
# legacy helpers removed; file edits are handled by SymbolEditor
