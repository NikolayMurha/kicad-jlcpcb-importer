"""Assign LCSC main dialog wrapping the part selector as primary UI."""

import os
import re
import json
import sys
import logging
import threading
from pathlib import Path
import wx
import wx.dataview as dv
from typing import Optional, Protocol

from .partselector import PartSelectorDialog
from .settings import SettingsDialog
from ..core.helpers import (
    HighResWxSize,
    loadBitmapScaled,
    GetScaleFactor,
    PLUGIN_PATH,
    sanitize_lib_name,
    as_bool,
)
from ..core.kicad_ipc import ProjectContext
from ..core.events import (
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
    EVT_SYMBOL_INDEX_BUILD_STARTED_EVENT,
    EVT_SYMBOL_INDEX_BUILD_PROGRESS_EVENT,
    EVT_SYMBOL_INDEX_BUILD_COMPLETED_EVENT,
    LogboxAppendEvent,
    MessageEvent,
    SymbolIndexBuildStartedEvent,
    SymbolIndexBuildProgressEvent,
    SymbolIndexBuildCompletedEvent,
)
from ..core.library import Library, LibraryState
from ..importers.importer import EasyedaImporter
from ..importers.kicad.importer import KicadImporter


class KicadProvider(Protocol):
    """Runtime boundary shared by IPC and standalone modes."""

    def get_project_context(self) -> ProjectContext:
        """Return the active project's paths and file names."""


class ToolsDialog(wx.Dialog):
    """Tools dialog with BOM import, re-import all, and clean library actions."""

    def __init__(self, parent: "AssignLCSCMainDialog"):
        super().__init__(
            parent,
            wx.ID_ANY,
            "Tools",
            wx.DefaultPosition,
            HighResWxSize(parent.window, wx.Size(320, -1)),
            wx.DEFAULT_DIALOG_STYLE,
        )
        self._parent = parent
        scale = parent.scale_factor

        root = wx.BoxSizer(wx.VERTICAL)

        def _make_btn(label: str, icon: str, tooltip: str) -> wx.Button:
            btn = wx.Button(
                self,
                wx.ID_ANY,
                label,
                wx.DefaultPosition,
                HighResWxSize(parent.window, wx.Size(-1, 36)),
                0,
            )
            btn.SetBitmap(loadBitmapScaled(icon, scale))
            btn.SetBitmapMargins((4, 0))
            btn.SetToolTip(tooltip)
            btn.SetMinSize(HighResWxSize(parent.window, wx.Size(-1, 36)))
            return btn

        self.bom_btn = _make_btn(
            "Import from BOM",
            "file-import-outline.png",
            "Import component list from a BOM CSV/XLSX file",
        )
        self.reimport_btn = _make_btn(
            "Re-import all",
            "mdi-arrow-collapse-down.png",
            "Re-import all previously imported parts with current settings",
        )
        self.clean_btn = _make_btn(
            "Clean Library",
            "mdi-trash-can-outline.png",
            "Remove stale lib-table entries, orphan footprints and orphan 3D models",
        )

        scope = str(
            (parent.settings.get("general", {}) or {}).get("library_scope", "project")
        ).strip().lower()
        is_local = scope in ("project", "shared")
        self.library_btn = _make_btn(
            "Import from other libraries",
            "table-arrow-down.png",
            "Import from EasyEDA or other libraries" if is_local
            else "Available for Project/Shared scopes only",
        )
        self.library_btn.Enable(is_local)

        for btn in (self.bom_btn, self.reimport_btn, self.library_btn, self.clean_btn):
            root.Add(btn, 0, wx.EXPAND | wx.ALL, 6)

        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        close_btn = wx.Button(self, wx.ID_CANCEL, "Close")
        root.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(root)
        root.Fit(self)
        self.CentreOnParent()

        self.bom_btn.Bind(wx.EVT_BUTTON, self._on_bom)
        self.reimport_btn.Bind(wx.EVT_BUTTON, self._on_reimport)
        self.library_btn.Bind(wx.EVT_BUTTON, self._on_library)
        self.clean_btn.Bind(wx.EVT_BUTTON, self._on_clean)

    def _on_bom(self, _evt=None):
        self.EndModal(wx.ID_CANCEL)
        wx.CallAfter(self._parent._on_import_from_bom)

    def _on_reimport(self, _evt=None):
        self.EndModal(wx.ID_CANCEL)
        wx.CallAfter(self._parent._on_reimport_all)

    def _on_library(self, _evt=None):
        self.EndModal(wx.ID_CANCEL)
        wx.CallAfter(self._parent._open_library_manager)

    def _on_clean(self, _evt=None):
        self.EndModal(wx.ID_CANCEL)
        wx.CallAfter(self._parent._on_clean_library)


class AssignLCSCMainDialog(PartSelectorDialog):
    """Main plugin window for catalog search and library import."""

    def __init__(self, kicad_provider: Optional[KicadProvider] = None):
        # Minimal context expected by PartSelectorDialog
        if kicad_provider is None:
            raise RuntimeError("A KiCad IPC or standalone provider is required")
        self.kicad_provider = kicad_provider
        self.window = self  # fallback until wx top-level is available
        self.scale_factor = 1.0
        self._library_rename_timer = None
        self._pending_library_rename = None
        self._active_import_jobs = 0

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
        self._deps_prompted = False
        self._symbol_index_warmup_running = False

        # Build the PartSelectorDialog with self as the logical parent context
        super().__init__(self, parts={})
        self.Bind(wx.EVT_CLOSE, self._on_close_request)

        # Now that wx is initialized, update window and scale factor
        self.window = wx.GetTopLevelParent(self) or self
        self.scale_factor = GetScaleFactor(self.window)
        self._sync_shared_meta()

        # Insert a topbar with Update button
        topbar = wx.BoxSizer(wx.HORIZONTAL)
        
        self.update_db_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Update database",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(-1, 32)),
            0,
        )
        self.update_db_btn.SetBitmap(
            loadBitmapScaled("mdi-database-import-outline.png", self.scale_factor)
        )
        self.update_db_btn.SetBitmapMargins((2, 0))
        self.update_db_btn.SetToolTip("Update component database")

        # Settings button
        self.settings_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Settings",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(32, 32)),
            0,
        )
        self.settings_btn.SetBitmap(
            loadBitmapScaled("mdi-cog-outline.png", self.scale_factor)
        )
        self.settings_btn.SetBitmapMargins((2, 0))
        self.settings_btn.SetToolTip("Open settings")

        # Tools button (BOM import, re-import, clean library)
        self.tools_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Tools",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(32, 32)),
            0,
        )
        self.tools_btn.SetBitmap(
            loadBitmapScaled("mdi-tools.png", self.scale_factor)
        )
        self.tools_btn.SetBitmapMargins((2, 0))
        self.tools_btn.SetToolTip("Import, re-import and library maintenance tools")

        # Library Manager button (project scope only)
        self._topbar_button_labels = {
            "update_db_btn": "Update database",
            "settings_btn": "Settings",
            "tools_btn": "Tools",
        }
        self.mode_status_text = wx.StaticText(self, wx.ID_ANY, "")
        self.mode_status_text.SetToolTip("Current library storage mode.")

        self._apply_hide_button_labels_setting()
        topbar.Add(self.update_db_btn, 0, wx.ALL, 5)
        topbar.Add(self.settings_btn, 0, wx.ALL, 5)
        topbar.Add(self.tools_btn, 0, wx.ALL, 5)
        topbar.AddStretchSpacer(1)
        topbar.Add(self.mode_status_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        # apply_button_label_tooltips(self, overwrite=True)
        layout = self.GetSizer()
        if layout:
            layout.Insert(0, topbar, 0, wx.EXPAND | wx.ALL, 5)

        # Add bottom console with clear button
        console_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.console = wx.TextCtrl(
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.console.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 140)))
        
        self.clear_log_button = wx.Button(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(36, 36)),
            0,
        )
        self.clear_log_button.SetBitmap(
            loadBitmapScaled("mdi-trash-can-outline.png", self.scale_factor)
        )
        self.clear_log_button.SetBitmapMargins((2, 0))
        self.clear_log_button.Bind(wx.EVT_BUTTON, self._clear_log)
        
        console_sizer.Add(self.console, 1, wx.EXPAND | wx.RIGHT, 5)
        # Right zone with button aligned to the far right
        btn_zone = wx.BoxSizer(wx.HORIZONTAL)
        btn_zone.AddStretchSpacer(1)  # push the button to the right edge
        btn_zone.Add(self.clear_log_button, 0, wx.ALIGN_TOP | wx.RIGHT, 8)
        console_sizer.Add(btn_zone, 0, wx.EXPAND, 0)
        
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
            layout.Add(console_sizer, 0, wx.ALL | wx.EXPAND, 5)
            layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)
            # Console hidden by default; user opens it via log toggle button
            self.console.Hide()
            self.clear_log_button.Hide()
            self.Layout()

        

        # Wire events and actions
        self.update_db_btn.Bind(wx.EVT_BUTTON, lambda _evt: self.update_library())
        # Handle messages and progress locally
        self.Bind(EVT_LOGBOX_APPEND_EVENT, self._append_log)
        self.Bind(EVT_MESSAGE_EVENT, self._show_message)
        self.Bind(EVT_DOWNLOAD_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_DOWNLOAD_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_DOWNLOAD_COMPLETED_EVENT, self._on_progress_reset)
        self.Bind(EVT_DOWNLOAD_COMPLETED_EVENT, self._on_db_download_completed)
        self.Bind(EVT_UNZIP_COMBINING_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_COMBINING_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_UNZIP_EXTRACTING_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_EXTRACTING_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_UNZIP_EXTRACTING_COMPLETED_EVENT, self._on_progress_reset)
        self.Bind(EVT_SYMBOL_INDEX_BUILD_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_SYMBOL_INDEX_BUILD_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_SYMBOL_INDEX_BUILD_COMPLETED_EVENT, self._on_symbol_index_build_completed)
        # Settings updates from PartSelectorDialog
        from ..core.events import EVT_UPDATE_SETTING  # local import to avoid cycle
        self.Bind(EVT_UPDATE_SETTING, self._on_update_setting)
        self.settings_btn.Bind(wx.EVT_BUTTON, self._open_settings)
        self.tools_btn.Bind(wx.EVT_BUTTON, self._open_tools_dialog)
        # Double-click / Enter on list row triggers import instead of part details
        self.part_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.select_part)

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

        # KiCad installs requirements before launching an IPC action. Verify the
        # environment here so a broken installation has an actionable message.
        self._check_and_offer_install_deps()
        # Ensure UI matches current deps state
        self._update_select_enabled()

        self._start_symbol_index_warmup()

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

    def _start_symbol_index_warmup(self, force: bool = False) -> None:
        general = (self.settings.get("general", {}) or {})
        lib_format = str(general.get("lib_format", "kicad")).strip().lower()
        if lib_format != "kicad":
            return
        if not as_bool(general.get("kicad_builtin_first"), default=True):
            return
        if not as_bool(general.get("kicad_symbol_matching_enabled"), default=False):
            return
        if self._symbol_index_warmup_running:
            if force:
                self.log("KiCad builtin: symbol index warmup is already running.\n")
            return

        scope = str(general.get("library_scope", "project")).strip().lower()
        if scope not in ("project", "system", "shared"):
            scope = "project"
        self._symbol_index_warmup_running = True

        def _worker():
            started = False

            def _report(value: int) -> None:
                nonlocal started
                if not started:
                    started = True
                    wx.PostEvent(self, SymbolIndexBuildStartedEvent())
                wx.PostEvent(self, SymbolIndexBuildProgressEvent(value=max(0, min(100, int(value)))))

            try:
                importer = KicadImporter(
                    project_path=self.project_path,
                    python_exe=self._resolve_python_exe(),
                    parent_window=self,
                    scope=scope,
                )
                warmed = importer.warm_symbol_index_cache(progress_cb=_report)
                if warmed:
                    self.log("KiCad builtin: startup symbol index warmup completed.\n")
                else:
                    self.log("KiCad builtin: startup symbol index warmup skipped.\n")
            except Exception as exc:
                self.log(f"KiCad builtin: startup symbol index warmup failed: {exc}\n")
            finally:
                self._symbol_index_warmup_running = False
                if started:
                    wx.PostEvent(self, SymbolIndexBuildCompletedEvent())

        threading.Thread(target=_worker, daemon=True).start()

    # Override: do not assign in this simplified window — show placeholder
    def select_part(self, *_):  # noqa: N802 (KiCad naming)
        if not getattr(self, "_deps_ready", False):
            # Re-offer installation when user attempts to select
            self._check_and_offer_install_deps(force_prompt=True)
            return
        rows = []
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

            selected_items = []
            try:
                arr = dv.DataViewItemArray()
                count = self.part_list.GetSelections(arr)
                try:
                    n = int(count)
                except Exception:
                    n = 0
                if n > 0:
                    selected_items = [arr[i] for i in range(n)]
                else:
                    selected_items = [arr[i] for i in range(len(arr))]
            except Exception:
                try:
                    raw = self.part_list.GetSelections()
                    if isinstance(raw, (list, tuple)):
                        selected_items = list(raw)
                    else:
                        selected_items = list(raw) if raw else []
                except Exception:
                    selected_items = []

            if not selected_items:
                item = self.part_list.GetSelection()
                is_ok = False
                try:
                    is_ok = bool(item.IsOk())
                except Exception:
                    is_ok = False
                if is_ok:
                    selected_items = [item]

            for item in selected_items:
                try:
                    lcsc_id = str(self.part_list_model.get_lcsc(item)).strip()
                except Exception:
                    continue
                if not lcsc_id:
                    continue
                rows.append(lcsc_id)
            if not rows:
                wx.PostEvent(
                    self,
                    MessageEvent(title="No LCSC", text="No valid LCSC ID in selected rows.", style="warning"),
                )
                return
        except Exception as exc:
            try:
                self.log(f"Selection parse failed: {exc}\n")
            except Exception:
                pass
            wx.PostEvent(
                self,
                MessageEvent(title="Error", text="Failed to obtain LCSC ID.", style="error"),
            )
            return

        if len(rows) == 1:
            self._import_part_via_easyeda(rows[0])
            return

        self._import_parts_via_easyeda(rows)

    def _make_importer(self, scope: str):
        """Return the appropriate importer based on lib_format setting."""
        general = (self.settings.get("general", {}) or {})
        lib_format = str(general.get("lib_format", "kicad")).strip().lower()
        if lib_format == "kicad":
            return KicadImporter(
                project_path=self.project_path,
                python_exe=self._resolve_python_exe(),
                parent_window=self,
                scope=str(scope),
            )
        return EasyedaImporter(
            project_path=self.project_path,
            python_exe=self._resolve_python_exe(),
            parent_window=self,
            scope=str(scope),
        )

    def _import_parts_via_easyeda(self, rows):
        # Import pipeline is lcsc_id-only; importer always fetches component data.
        normalized_rows = []
        seen: set[str] = set()
        for item in rows or []:
            lcsc_id = str(item or "").strip()
            if not re.match(r"^C\d+$", lcsc_id):
                continue
            if lcsc_id in seen:
                continue
            seen.add(lcsc_id)
            normalized_rows.append(lcsc_id)

        if not normalized_rows:
            wx.PostEvent(
                self,
                MessageEvent(title="No LCSC", text="No valid LCSC ID in import list.", style="warning"),
            )
            return

        scope = self._ensure_library_scope_selected()
        if not scope:
            wx.PostEvent(
                self, LogboxAppendEvent(msg="Import canceled: no library location selected.\n")
            )
            return

        btn = getattr(self, "select_part_button", None)
        if btn is not None:
            btn.Enable(False)

        importer = self._make_importer(scope)
        self._batch_import_running = True
        wx.BeginBusyCursor()

        def _worker():
            ok_count = 0
            total = len(normalized_rows)
            try:
                wx.CallAfter(self.gauge.SetRange, 100)
                wx.CallAfter(self.gauge.SetValue, 0)
            except Exception:
                pass
            wx.PostEvent(
                self,
                LogboxAppendEvent(msg="Import mode: always fetch by LCSC ID.\n"),
            )
            try:
                for idx, lcsc_id in enumerate(normalized_rows, start=1):
                    wx.PostEvent(
                        self,
                        LogboxAppendEvent(msg=f"Importing {lcsc_id} ({idx}/{total})...\n"),
                    )
                    try:
                        ok, lib_base = importer.import_part(lcsc_id=lcsc_id)
                    except Exception as e:
                        wx.PostEvent(self, LogboxAppendEvent(msg=f"{e}\n"))
                        ok, lib_base = False, self.project_path

                    if ok:
                        ok_count += 1
                        display_path = Path(lib_base)
                        location_label = "File" if display_path.suffix.lower() == ".elibz" else "Folder"
                        wx.PostEvent(
                            self,
                            LogboxAppendEvent(
                                msg=f"*********  IMPORT SUCCESS: {lcsc_id}  *********\n{location_label}: {display_path}\n"
                            ),
                        )
                    else:
                        wx.PostEvent(
                            self,
                            LogboxAppendEvent(msg=f"*********  IMPORT FAILED: {lcsc_id}  *********\n"),
                        )
                    try:
                        wx.CallAfter(self.gauge.SetValue, int(idx / total * 100))
                    except Exception:
                        pass
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(
                        msg=f"Batch import finished: {ok_count}/{total} successful.\n"
                    ),
                )
                failed_count = total - ok_count
                wx.CallAfter(self.show_import_status, ok_count, failed_count)
            finally:
                self._batch_import_running = False
                wx.CallAfter(wx.EndBusyCursor)
                try:
                    wx.CallAfter(self.gauge.SetValue, 0)
                except Exception:
                    pass
                if btn is not None:
                    wx.CallAfter(btn.Enable, True)

        self._start_import_worker(_worker)

    def _start_import_worker(self, target) -> None:
        """Retain the window until an import worker has stopped posting wx events."""

        self._active_import_jobs += 1

        def _runner():
            try:
                target()
            finally:
                wx.CallAfter(self._finish_import_job)

        try:
            threading.Thread(target=_runner, daemon=True).start()
        except Exception:
            self._active_import_jobs = max(0, self._active_import_jobs - 1)
            raise

    def _finish_import_job(self) -> None:
        self._active_import_jobs = max(0, self._active_import_jobs - 1)

    def _active_background_work(self) -> str:
        if self._active_import_jobs:
            return "component import"
        if getattr(self.library, "state", None) == LibraryState.DOWNLOAD_RUNNING:
            return "database update"
        if self._symbol_index_warmup_running:
            return "symbol index build"
        return ""

    @staticmethod
    def _show_close_blocked(work: str) -> None:
        wx.MessageBox(
            f"A {work} is still running. Wait for it to finish before closing the plugin.",
            "JLCPCB Importer",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _on_close_request(self, event) -> None:
        """Do not destroy wx controls while a worker still references them."""

        work = self._active_background_work()
        if work:
            if event.CanVeto():
                event.Veto()
            self._show_close_blocked(work)
            return
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()

    def _import_part_via_easyeda(self, lcsc_id: str):
        scope = self._ensure_library_scope_selected()
        if not scope:
            wx.PostEvent(
                self, LogboxAppendEvent(msg="Import canceled: no library location selected.\n")
            )
            return

        btn = getattr(self, "select_part_button", None)
        if btn is not None:
            btn.Enable(False)

        importer = self._make_importer(scope)
        wx.BeginBusyCursor()

        def _worker():
            try:
                ok, lib_base = importer.import_part(lcsc_id=lcsc_id)
            except Exception as e:
                wx.PostEvent(self, LogboxAppendEvent(msg=f"{e}\n"))
                ok, lib_base = False, self.project_path
            finally:
                wx.CallAfter(wx.EndBusyCursor)
            
            if btn is not None:
                wx.CallAfter(btn.Enable, True)
            
            if ok:
                display_path = Path(lib_base)
                location_label = "File" if display_path.suffix.lower() == ".elibz" else "Folder"
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(
                        msg=f"*********  IMPORT SUCCESS: {lcsc_id}  *********\n{location_label}: {display_path}\n"
                    ),
                )
                wx.CallAfter(self.show_import_status, 1, 0)
            else:
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(msg=f"*********  IMPORT FAILED: {lcsc_id}  *********\n"),
                )
                wx.CallAfter(self.show_import_status, 0, 1)
        self._start_import_worker(_worker)

    def _ensure_library_scope_selected(self) -> Optional[str]:
        """Return existing library scope or ask the user to choose.

        Returns "project", "system", or "shared". Returns None if the user cancels.
        """
        try:
            scope = None
            if isinstance(self.settings, dict):
                scope = (self.settings.get("general", {}) or {}).get("library_scope")
            
            if scope in ("project", "system", "shared"):
                return scope

            # Ask user
            choice = self._ask_library_scope_dialog()
            if choice in ("project", "system", "shared"):
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
            size=HighResWxSize(self, wx.Size(560, 200)),
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
            btn_shared = wx.Button(dlg, wx.ID_ANY, "Shared library")
            hbox.Add(btn_project, 1, wx.ALL | wx.EXPAND, 5)
            hbox.Add(btn_system, 1, wx.ALL | wx.EXPAND, 5)
            hbox.Add(btn_shared, 1, wx.ALL | wx.EXPAND, 5)
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

            def _choose_shared(_evt):
                result["choice"] = "shared"
                dlg.EndModal(wx.ID_OK)

            btn_project.Bind(wx.EVT_BUTTON, _choose_project)
            btn_system.Bind(wx.EVT_BUTTON, _choose_system)
            btn_shared.Bind(wx.EVT_BUTTON, _choose_shared)
            # apply_button_label_tooltips(dlg, overwrite=True)

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

    def _detect_project_context(self):
        """Detect project root and names robustly."""
        context = self.kicad_provider.get_project_context()
        return str(context.project_path), context.board_name, context.schematic_name

    # Settings persistence
    # Project-level settings live under the current KiCad project directory.

    @property
    def _project_settings_path(self) -> str:
        """Return path to project-level settings file."""
        return os.path.join(self.project_path, "jlcpcb_importer.json")

    @staticmethod
    def _merge_settings(base: dict, override: dict) -> dict:
        """Return settings with nested dict values from override applied to base."""
        result = dict(base or {})
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = AssignLCSCMainDialog._merge_settings(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _read_json_file(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_settings(self):
        # 1) Plugin defaults
        defaults = self._read_json_file(os.path.join(PLUGIN_PATH, "settings.default.json"))
        # 2) Project-level persisted settings
        project_settings = self._read_json_file(self._project_settings_path)
        self.settings = self._merge_settings(defaults, project_settings)

    def _save_settings(self):
        try:
            settings_path = Path(self._project_settings_path)
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
            try:
                self.log(f"Settings saved: {settings_path}\n")
            except Exception:
                pass
        except Exception:
            try:
                self.log("Settings save failed.\n")
            except Exception:
                pass

    # Called when PartSelectorDialog posts UpdateSetting
    def _on_update_setting(self, e):
        old_lib_prefix = ""
        if e.section == "general" and e.setting == "lib_prefix":
            old_lib_prefix = str(getattr(e, "previous_value", "") or "").strip()
            if not old_lib_prefix:
                try:
                    from ..core.lib_paths import resolve_library_base_name
                    old_lib_prefix = resolve_library_base_name(
                        self.settings.get("general", {}) or {},
                        project_path=Path(self.project_path),
                    )
                except Exception:
                    old_lib_prefix = str((self.settings.get("general", {}) or {}).get("lib_prefix") or "").strip()
        try:
            if e.section == "general" and e.setting in ("lib_prefix", "lib_path", "lib_format", "library_scope"):
                self.log(
                    f"UpdateSetting: {e.section}.{e.setting}={getattr(e, 'value', None)!r}"
                    + (f" old_lib_prefix={old_lib_prefix!r}" if e.setting == "lib_prefix" else "")
                    + "\n"
                )
        except Exception:
            pass
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value
        self._save_settings()
        if e.setting in ("library_scope", "lib_path", "lib_prefix", "lib_format"):
            self._sync_shared_meta(changed_setting=e.setting)
        if e.section == "general" and e.setting == "lib_prefix":
            self._schedule_library_rename_prompt(old_lib_prefix, str(e.value or "").strip())
        if e.setting == "library_scope":
            self._update_mode_status()
        if e.section == "general" and e.setting in (
            "lib_format",
            "kicad_builtin_first",
            "kicad_symbol_matching_enabled",
            "kicad_symbol_index_ttl_sec",
            "kicad_symbol_index_max_libs",
        ):
            self._start_symbol_index_warmup(force=True)
        if e.section == "general" and e.setting == "hide_button_labels":
            self._apply_hide_button_labels_setting()
        if e.section == "general" and e.setting == "debug_log":
            self._apply_logging_level_settings()

    def _sync_shared_meta(self, changed_setting: str = ""):
        try:
            general = (self.settings.get("general", {}) or {})
            if str(general.get("library_scope", "project")).strip().lower() != "shared":
                self.log(f"Shared metadata sync skipped: scope={general.get('library_scope', 'project')!r}\n")
                return
            from ..core.lib_paths import resolve_lib_root, resolve_library_base_name
            from ..core.lib_tables import LibTablesManager
            from ..core.shared_lib import (
                ensure_project_legacy_models_link,
                ensure_project_table_links,
                ensure_shared_meta,
            )

            shared_root, shared_uri_prefix = resolve_lib_root(general, Path(self.project_path))
            default_name = resolve_library_base_name(general, project_path=Path(self.project_path))
            override_name = str(general.get("lib_prefix") or "").strip() if changed_setting == "lib_prefix" else None
            self.log(
                "Shared metadata sync: "
                f"changed={changed_setting!r} root={shared_root} uri_prefix={shared_uri_prefix!r} "
                f"default_name={default_name!r} override_name={override_name!r}\n"
            )
            ensure_shared_meta(
                shared_root,
                default_name,
                log=self.log,
                override_library_name=override_name,
            )
            # Keep shared tables in sync and ensure project links exist immediately
            # when user switches to Shared mode (not only after import).
            LibTablesManager(shared_root, log=self.log).ensure_project_lib_tables(
                shared_root,
                use_project_relative=False,
                uri_prefix=shared_uri_prefix,
                lib_format=str(general.get("lib_format", "kicad")).strip().lower(),
            )
            ensure_project_table_links(
                Path(self.project_path),
                shared_root,
                log=self.log,
            )
            ensure_project_legacy_models_link(
                Path(self.project_path),
                shared_root / "3dmodels",
                log=self.log,
            )
        except Exception as exc:
            self.log(f"Shared metadata sync failed: {exc}\n")

    def _schedule_library_rename_prompt(self, old_name: str, new_name: str) -> None:
        raw_old_name = old_name
        raw_new_name = new_name
        old_name = sanitize_lib_name(str(old_name or "").strip())
        new_name = sanitize_lib_name(str(new_name or "").strip())
        self.log(
            f"Library rename schedule: raw_old={raw_old_name!r} raw_new={raw_new_name!r} "
            f"old={old_name!r} new={new_name!r}\n"
        )
        if not old_name or not new_name or old_name == new_name:
            self.log("Library rename schedule skipped: empty or unchanged name.\n")
            return
        general = dict((self.settings.get("general", {}) or {}))
        if str(general.get("library_scope", "project")).strip().lower() == "system":
            self.log("Library rename schedule skipped: system scope.\n")
            return
        self._pending_library_rename = {
            "old_name": old_name,
            "new_name": new_name,
            "general": general,
        }
        try:
            if self._library_rename_timer is not None:
                self._library_rename_timer.Stop()
        except Exception:
            pass
        self._library_rename_timer = wx.CallLater(900, self._maybe_prompt_library_rename)
        self.log("Library rename prompt scheduled.\n")

    def _library_rename_pairs(self, lib_root: Path, old_name: str, new_name: str) -> list[tuple[Path, Path, str, str]]:
        grouped = False
        try:
            from ..core.lib_paths import resolve_group_by_category
            grouped = resolve_group_by_category(self.settings.get("general", {}) or {}, default=False)
        except Exception:
            grouped = False

        pairs: list[tuple[Path, Path, str, str]] = []
        if grouped:
            prefix = f"{old_name}_"
            try:
                for item in sorted(lib_root.iterdir()):
                    if not item.name.startswith(prefix):
                        continue
                    target_name = f"{new_name}_{item.name[len(prefix):]}"
                    pairs.append((item, lib_root / target_name, item.name, target_name))
            except Exception:
                pass
        else:
            pairs.append((lib_root / old_name, lib_root / new_name, old_name, new_name))
            for suffix in (".elibz", ".kicad_sym", ".pretty"):
                pairs.append((lib_root / f"{old_name}{suffix}", lib_root / f"{new_name}{suffix}", old_name, new_name))

        return [(src, dst, old, new) for src, dst, old, new in pairs if src.exists() and not dst.exists()]

    def _find_previous_library_names(self, lib_root: Path, new_name: str) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        def _add(name: str) -> None:
            clean = sanitize_lib_name(str(name or "").strip())
            if not clean or clean == new_name or clean in seen:
                return
            seen.add(clean)
            names.append(clean)

        try:
            from ..core.sym_lib_reader import parse_lib_table, resolve_uri
            table_paths = [lib_root / "sym-lib-table", lib_root / "fp-lib-table"]
            project_table_paths = [
                Path(self.project_path) / "sym-lib-table",
                Path(self.project_path) / "fp-lib-table",
            ]
            for table_path in table_paths + project_table_paths:
                if not table_path.exists():
                    continue
                for _entry_name, _lib_type, uri in parse_lib_table(table_path):
                    resolved = Path(resolve_uri(uri, Path(self.project_path), table_dir=table_path.parent))
                    try:
                        resolved.relative_to(lib_root.resolve())
                    except Exception:
                        continue
                    stem = resolved.stem
                    if stem and not stem.startswith(new_name):
                        _add(stem)
                    parent_name = resolved.parent.name
                    if parent_name and parent_name != lib_root.name and not parent_name.startswith(new_name):
                        _add(parent_name)
        except Exception:
            pass

        try:
            for item in sorted(lib_root.iterdir()):
                if item.name.startswith(new_name):
                    continue
                if item.suffix in (".elibz", ".kicad_sym", ".pretty"):
                    _add(item.stem)
                elif item.is_dir():
                    if (item / f"{item.name}.elibz").exists() or (item / f"{item.name}.kicad_sym").exists() or (item / f"{item.name}.pretty").exists():
                        _add(item.name)
        except Exception:
            pass

        return names

    @staticmethod
    def _rewrite_symbol_library_footprint_refs(sym_path: Path, old_name: str, new_name: str) -> None:
        if not sym_path.exists() or not sym_path.is_file():
            return
        try:
            text = sym_path.read_text(encoding="utf-8", errors="replace")
            patched = text.replace(f'"{old_name}:', f'"{new_name}:')
            if patched != text:
                sym_path.write_text(patched, encoding="utf-8")
        except Exception:
            pass

    def _rename_library_artifact(self, src: Path, dst: Path, old_name: str, new_name: str) -> bool:
        try:
            src.rename(dst)
        except Exception as exc:
            self.log(f"Library rename failed: {src} -> {dst}: {exc}\n")
            return False

        if dst.is_dir():
            for suffix in (".elibz", ".kicad_sym", ".pretty"):
                old_child = dst / f"{old_name}{suffix}"
                new_child = dst / f"{new_name}{suffix}"
                if old_child.exists() and not new_child.exists():
                    try:
                        old_child.rename(new_child)
                    except Exception as exc:
                        self.log(f"Library child rename failed: {old_child} -> {new_child}: {exc}\n")
                if suffix == ".kicad_sym":
                    self._rewrite_symbol_library_footprint_refs(new_child, old_name, new_name)
        elif dst.suffix == ".kicad_sym":
            self._rewrite_symbol_library_footprint_refs(dst, old_name, new_name)

        self.log(f"Renamed library artifact: {src.name} -> {dst.name}\n")
        return True

    def _refresh_library_tables_after_rename(self, lib_root: Path) -> None:
        try:
            from ..core.lib_paths import resolve_lib_root
            from ..core.lib_tables import LibTablesManager

            general = self.settings.get("general", {}) or {}
            scope = str(general.get("library_scope", "project")).strip().lower()
            fmt = str(general.get("lib_format", "kicad")).strip().lower()
            if scope == "shared":
                _root, uri_prefix = resolve_lib_root(general, Path(self.project_path))
                manager = LibTablesManager(lib_root, log=self.log)
                manager.prune_invalid_table_paths(project_path=Path(self.project_path))
                manager.ensure_project_lib_tables(
                    lib_root,
                    use_project_relative=False,
                    uri_prefix=uri_prefix,
                    lib_format=fmt,
                )
            else:
                _root, uri_prefix = resolve_lib_root(general, Path(self.project_path))
                manager = LibTablesManager(Path(self.project_path), log=self.log)
                manager.prune_invalid_table_paths(project_path=Path(self.project_path))
                manager.ensure_project_lib_tables(lib_root, uri_prefix=uri_prefix, lib_format=fmt)
        except Exception as exc:
            self.log(f"Library table refresh after rename failed: {exc}\n")

    def _maybe_prompt_library_rename(self) -> None:
        pending = self._pending_library_rename
        self._pending_library_rename = None
        if not pending:
            return
        old_name = str(pending.get("old_name") or "")
        new_name = str(pending.get("new_name") or "")
        if not old_name or not new_name or old_name == new_name:
            return
        try:
            from ..core.lib_paths import resolve_lib_root
            lib_root, _uri_prefix = resolve_lib_root(self.settings.get("general", {}) or {}, Path(self.project_path))
        except Exception as exc:
            self.log(f"Library rename skipped: cannot resolve library root: {exc}\n")
            return
        self.log(
            f"Library rename prompt check: old={old_name!r} new={new_name!r} "
            f"root={lib_root} exists={lib_root.exists()}\n"
        )

        pairs = self._library_rename_pairs(lib_root, old_name, new_name)
        self.log(f"Library rename candidates for '{old_name}' -> '{new_name}': {len(pairs)}\n")
        if not pairs:
            alternatives = self._find_previous_library_names(lib_root, new_name)
            self.log(f"Library rename fallback names: {alternatives}\n")
            for candidate in alternatives:
                pairs = self._library_rename_pairs(lib_root, candidate, new_name)
                if pairs:
                    old_name = candidate
                    self.log(f"Library rename fallback selected previous name: {old_name!r}\n")
                    break
        if not pairs:
            self.log(f"Library rename: no existing artifacts found for previous name '{old_name}'.\n")
            return

        sample = "\n".join(f"- {src.name} -> {dst.name}" for src, dst, _old, _new in pairs[:8])
        more = "" if len(pairs) <= 8 else f"\n- ...and {len(pairs) - 8} more"
        dlg = wx.MessageDialog(
            self,
            f"Library name changed from '{old_name}' to '{new_name}'.\n\n"
            f"Rename existing library artifact(s)?\n\n{sample}{more}",
            "Rename library",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        try:
            result = dlg.ShowModal()
        finally:
            dlg.Destroy()
        if result != wx.ID_YES:
            return

        renamed = 0
        for src, dst, old_item_name, new_item_name in pairs:
            if self._rename_library_artifact(src, dst, old_item_name, new_item_name):
                renamed += 1
        if renamed:
            self._sync_shared_meta(changed_setting="lib_prefix")
            self._refresh_library_tables_after_rename(lib_root)

    def _update_mode_status(self):
        """Update the mode status label in the toolbar."""
        try:
            scope = (self.settings.get("general", {}) or {}).get("library_scope", "project")
            scope_key = str(scope).strip().lower()
            full_label = (
                "Project"
                if scope_key == "project"
                else ("System" if scope_key == "system" else "Shared Library")
            )
            short_label = (
                "Project"
                if scope_key == "project"
                else ("System" if scope_key == "system" else "Shared")
            )
            self.mode_status_text.SetLabel(f"Mode: {short_label}")
            self.mode_status_text.SetToolTip(f"Current library storage mode: {full_label}")
        except Exception:
            pass

    def _apply_hide_button_labels_setting(self):
        general = (self.settings.get("general", {}) or {})
        hide = as_bool(general.get("hide_button_labels"), default=False)

        # Search button (PartSelectorDialog)
        # self.apply_hide_button_labels_setting(hide)

        # Main top toolbar buttons — base size is always 36×36 (set in constructor).
        # When labels are shown the button expands naturally; when hidden it stays 36×36.
        for attr_name, label in self._topbar_button_labels.items():
            btn = getattr(self, attr_name, None)
            if btn is None:
                continue
            
            if hide:
                btn.SetWindowStyleFlag(0)
                btn.InvalidateBestSize()
                btn.SetMinSize(HighResWxSize(self.window, wx.Size(36, 36)))
                btn.SetSize(HighResWxSize(self.window, wx.Size(36, 36)))
                btn.Refresh()
            else:
                btn.SetWindowStyleFlag(1)
                btn.InvalidateBestSize()
                btn.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 36)))
                btn.SetSize(HighResWxSize(self.window, wx.Size(-1, 36)))
                btn.Refresh()
                
        self.Layout()

        

    @staticmethod
    def _normalize_package_from_footprint(fp_stem: str) -> str:
        """Extract EIC size code or package name from a footprint stem.

        Examples:
            "C0402"                        → "0402"
            "C_0402_1005Metric_..."        → "0402"
            "SOT-23-3_L2.9-W1.3-..."      → "SOT-23-3"
            "LGA-14_L5.0-W3.0-..."        → "LGA-14"
        """
        if not fp_stem:
            return ""
        # Single letter prefix + optional underscore + 4-digit EIC code: C0402, R_0603
        m = re.match(r"^[A-Za-z]_?(\d{4})\b", fp_stem)
        if m:
            return m.group(1)
        # Already a bare 4-digit size code
        if re.match(r"^\d{4}$", fp_stem):
            return fp_stem
        # Package name up to first underscore: SOT-23-3_L2.9... → SOT-23-3
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9\-]+?)_", fp_stem)
        if m:
            return m.group(1)
        return fp_stem

    def _collect_reimport_rows(self, include_schematic: bool = False) -> list[str]:
        """Collect unique LCSC IDs from previously imported libraries."""
        import zipfile
        import json as _json
        from ..core.sym_lib_reader import get_connected_elibz_libs

        ids: list[str] = []
        seen: set[str] = set()
        project_path = Path(self.project_path)
        self.log(f"Re-import: scanning project path: {project_path}\n")

        def _add_lcsc(candidate: str) -> bool:
            lcsc_id = str(candidate or "").strip().upper()
            if not re.match(r"^C\d+$", lcsc_id):
                return False
            if lcsc_id in seen:
                return False
            seen.add(lcsc_id)
            ids.append(lcsc_id)
            return True

        # 1. EasyEDA Pro .elibz libraries — LCSC ID stored as product_code
        elibz_paths: dict = {}
        try:
            for alias, lib_path in get_connected_elibz_libs(project_path, include_user_tables=False):
                elibz_paths[str(lib_path)] = alias
        except Exception:
            pass
        try:
            for elibz_file in project_path.glob("*.elibz"):
                key = str(elibz_file)
                if key not in elibz_paths:
                    elibz_paths[key] = elibz_file.stem
        except Exception:
            pass

        self.log(f"Re-import: found {len(elibz_paths)} .elibz lib(s)\n")
        for lib_path_str, alias in elibz_paths.items():
            self.log(f"Re-import:   elibz: {lib_path_str}\n")
            try:
                with zipfile.ZipFile(lib_path_str, "r") as zf:
                    data = _json.loads(zf.read("device.json").decode("utf-8"))
                count_before = len(ids)
                for _uuid, dev in data.get("devices", {}).items():
                    _add_lcsc(str(dev.get("product_code") or "").strip())
                self.log(f"Re-import:   → {len(ids) - count_before} part(s) found\n")
            except Exception as exc:
                self.log(f"Re-import:   ERROR reading elibz: {exc}\n")

        # 2. KiCad .kicad_sym — scan only explicit project sym-lib-table entries.
        try:
            from ..core.sym_lib_reader import parse_lib_table, resolve_uri
            table_files: list = []
            proj_tbl = project_path / "sym-lib-table"
            if proj_tbl.exists():
                table_files.append(proj_tbl)
                self.log(f"Re-import: project sym-lib-table: {proj_tbl}\n")
            else:
                self.log(f"Re-import: no project sym-lib-table at {proj_tbl}\n")

            kicad_seen: set = set()
            for tbl in table_files:
                entries = list(parse_lib_table(tbl))
                self.log(f"Re-import: {tbl.name} has {len(entries)} entries\n")
                for name, lib_type, uri in entries:
                    if lib_type not in ("KiCad", "KiCad_Sym") or name in kicad_seen:
                        continue
                    lib_path = Path(resolve_uri(uri, project_path, table_dir=tbl.parent))
                    self.log(f"Re-import:   kicad_sym [{name}]: {lib_path} exists={lib_path.exists()}\n")
                    if not lib_path.exists():
                        continue
                    kicad_seen.add(name)
                    try:
                        count_before = len(ids)
                        content = lib_path.read_text(encoding="utf-8", errors="replace")
                        # Common property names seen in generated symbols:
                        #   - "LCSC Part"
                        #   - "Supplier Part"
                        # plus LCSC links in Datasheet/URL fields.
                        patterns = [
                            r'\(property\s+"[^"]*LCSC[^"]*"\s+"(C\d+)"',
                            r'\(property\s+"[^"]*Supplier[^"]*Part[^"]*"\s+"(C\d+)"',
                            r'lcsc\.com/[^"\s]*_(C\d+)\.html',
                            r'lcsc\.com/[^"\s]*/(C\d+)\.html',
                        ]
                        for pattern in patterns:
                            for m in re.finditer(pattern, content, flags=re.IGNORECASE):
                                _add_lcsc(m.group(1))

                        added = len(ids) - count_before
                        if added:
                            self.log(f"Re-import:     → {added} symbol-linked LCSC ID(s) found\n")
                    except Exception as exc:
                        self.log(f"Re-import:   ERROR reading kicad_sym: {exc}\n")
        except Exception as exc:
            self.log(f"Re-import: kicad_sym scan error: {exc}\n")

        # 3. KiCad schematics — optional, because plain C123-like tokens can also
        # be component references. Keep this limited to supplier/LCSC fields.
        if include_schematic:
            schematic_patterns = [
                r'\(property\s+"[^"]*LCSC[^"]*"\s+"(C\d+)"',
                r'\(property\s+"[^"]*Supplier[^"]*Part[^"]*"\s+"(C\d+)"',
                r'lcsc\.com/[^"\s]*_(C\d+)\.html',
                r'lcsc\.com/[^"\s]*/(C\d+)\.html',
            ]
            try:
                schematic_files = sorted(project_path.rglob("*.kicad_sch"))
                self.log(f"Re-import: found {len(schematic_files)} schematic file(s)\n")
                for sch_path in schematic_files:
                    try:
                        count_before = len(ids)
                        content = sch_path.read_text(encoding="utf-8", errors="replace")
                        for pattern in schematic_patterns:
                            for m in re.finditer(pattern, content, flags=re.IGNORECASE):
                                _add_lcsc(m.group(1))
                        added = len(ids) - count_before
                        if added:
                            self.log(f"Re-import:   schematic {sch_path.name}: {added} LCSC ID(s) found\n")
                    except Exception as exc:
                        self.log(f"Re-import:   ERROR reading schematic {sch_path}: {exc}\n")
            except Exception as exc:
                self.log(f"Re-import: schematic scan error: {exc}\n")

        self.log(f"Re-import: total {len(ids)} part(s) collected\n")

        return ids

    def _ask_reimport_options_dialog(self) -> Optional[dict]:
        dlg = wx.Dialog(
            self,
            title="Re-import all",
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        try:
            vbox = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(
                dlg,
                label=(
                    "Re-import all previously imported parts using current settings.\n"
                    "Existing symbols and footprints will be overwritten."
                ),
            )
            vbox.Add(text, 0, wx.ALL | wx.EXPAND, 10)

            include_schematic = wx.CheckBox(
                dlg,
                wx.ID_ANY,
                "Also search LCSC IDs in schematic files",
            )
            include_schematic.SetValue(False)
            vbox.Add(include_schematic, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

            buttons = dlg.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
            if buttons is not None:
                vbox.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)

            dlg.SetSizer(vbox)
            vbox.Fit(dlg)
            dlg.CentreOnParent()
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return {"include_schematic": bool(include_schematic.GetValue())}
        finally:
            dlg.Destroy()

    def _on_reimport_all(self, _evt=None):
        options = self._ask_reimport_options_dialog()
        if options is None:
            return

        lcsc_ids = self._collect_reimport_rows(
            include_schematic=bool(options.get("include_schematic")),
        )
        if not lcsc_ids:
            wx.PostEvent(
                self,
                MessageEvent(
                    title="Nothing to re-import",
                    text="No previously imported parts found in connected libraries.",
                    style="warning",
                ),
            )
            return

        dlg = wx.MessageDialog(
            self,
            f"Re-import {len(lcsc_ids)} part(s) using current settings?\n\n"
            "This will overwrite existing symbols and footprints.",
            "Re-import all",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        result = dlg.ShowModal()
        dlg.Destroy()
        if result != wx.ID_YES:
            return

        self._import_parts_via_easyeda(lcsc_ids)

    def _open_tools_dialog(self, _evt=None):
        """Open the Tools dialog with BOM import, re-import, and library cleanup."""
        dlg = None
        try:
            dlg = ToolsDialog(self)
            dlg.ShowModal()
        finally:
            try:
                if dlg is not None:
                    dlg.Destroy()
            except Exception:
                pass

    def _on_clean_library(self, _evt=None):
        """Scan for stale entries / orphans, confirm, then delete."""

        def _scan() -> dict:
            """Collect stats without deleting anything. Returns result dict."""
            def _safe_resolve(path: Path) -> Path:
                try:
                    return path.resolve()
                except Exception:
                    return path

            def _is_within_any(path: Path, roots: list[Path]) -> bool:
                rp = _safe_resolve(path)
                for root in roots:
                    rr = _safe_resolve(root)
                    try:
                        if rp == rr or rr in rp.parents:
                            return True
                    except Exception:
                        continue
                return False

            result = {
                "stale_sym_libs": [],   # (name, path) missing sym-lib-table entries
                "stale_fp_libs": [],    # (name, path) missing fp-lib-table entries
                "orphan_fps": [],       # Path objects — .kicad_mod not referenced by any symbol
                "orphan_models": [],    # Path objects — 3D models not referenced by any footprint
                "managed_fp_roots": [],
                "managed_model_roots": [],
                "scan_fp_dirs": [],
                "errors": [],
            }
            project_path = Path(self.project_path)
            general = (self.settings.get("general", {}) or {})
            scope = str(general.get("library_scope", "project")).strip().lower()
            project_fp_dirs: list[Path] = []

            # Resolve cleanup roots once and keep scan strictly inside them.
            # This prevents deleting footprints from unrelated/global libraries.
            try:
                from ..core.lib_paths import resolve_lib_root
                from ..core.platform_support import resolve_system_library_root

                if scope == "system":
                    base_path = resolve_system_library_root(PLUGIN_PATH)
                    plugin_folder = Path(PLUGIN_PATH).resolve().name
                    result["managed_fp_roots"] = [
                        str(_safe_resolve(base_path / "footprints" / plugin_folder))
                    ]
                    result["managed_model_roots"] = [
                        str(_safe_resolve(base_path / "3dmodels" / plugin_folder))
                    ]
                else:
                    lib_root, _ = resolve_lib_root(general, project_path)
                    root = _safe_resolve(lib_root)
                    result["managed_fp_roots"] = [str(root)]
                    result["managed_model_roots"] = [str(root)]
            except Exception as exc:
                result["errors"].append(f"cleanup root resolve: {exc}")

            # ── Step 1: stale lib-table entries ──
            try:
                from ..core.sym_lib_reader import resolve_uri
                from ..core.lib_paths import resolve_lib_root

                table_roots: set[Path] = {project_path}
                if scope in ("project", "shared"):
                    try:
                        lib_root, _ = resolve_lib_root(general, project_path)
                        table_roots.add(_safe_resolve(lib_root))
                    except Exception:
                        pass

                def _collect_stale(tbl_path):
                    stale = []
                    try:
                        content = tbl_path.read_text(encoding="utf-8", errors="replace")
                        for m in re.finditer(
                            r'\(lib\s+\(name\s+"([^"]+)"\)[^)]*\(uri\s+"([^"]+)"\)', content
                        ):
                            name, uri = m.group(1), m.group(2)
                            try:
                                p = Path(resolve_uri(uri, project_path, table_dir=tbl_path.parent))
                                if not p.exists():
                                    stale.append((name, str(p)))
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return stale

                seen_sym: set = set()
                for tbl in {r / "sym-lib-table" for r in table_roots}:
                    if tbl.exists():
                        for entry in _collect_stale(tbl):
                            if entry[0] not in seen_sym:
                                seen_sym.add(entry[0])
                                result["stale_sym_libs"].append(entry)

                seen_fp: set = set()
                for tbl in {r / "fp-lib-table" for r in table_roots}:
                    if tbl.exists():
                        for entry in _collect_stale(tbl):
                            if entry[0] not in seen_fp:
                                seen_fp.add(entry[0])
                                result["stale_fp_libs"].append(entry)

            except Exception as exc:
                result["errors"].append(f"lib-table scan: {exc}")

            # A shared/system library may be referenced by projects that are not
            # discoverable from the current KiCad process. Without a complete
            # cross-project reference index, deleting its files cannot be proven
            # safe. Limit destructive orphan cleanup to project-owned libraries.
            if scope != "project":
                result["errors"].append(
                    f"orphan file cleanup skipped for {scope} scope; only stale "
                    "current-project lib-table entries are eligible"
                )
                return result

            # ── Step 2: orphan footprints ──
            try:
                from ..core.sym_lib_reader import get_connected_sym_libs, parse_lib_table, resolve_uri
                # Collect footprint names referenced by project/builtin symbols only.
                sym_fps: set = set()
                for _alias, sym_path in get_connected_sym_libs(project_path, include_user_tables=False):
                    try:
                        content = sym_path.read_text(encoding="utf-8", errors="replace")
                        for m in re.finditer(r'\(property\s+"Footprint"\s+"([^"]+)"', content):
                            fp_ref = m.group(1).strip()
                            if ":" in fp_ref:
                                sym_fps.add(fp_ref.split(":", 1)[1])
                    except Exception:
                        pass

                managed_fp_roots = [Path(p) for p in (result.get("managed_fp_roots") or [])]
                fp_dirs_set: set[Path] = set()
                skipped_external = 0

                # Read project/shared fp tables and keep only libs under managed roots.
                table_candidates = [project_path / "fp-lib-table"]
                if scope == "shared":
                    for root in managed_fp_roots:
                        table_candidates.append(root / "fp-lib-table")

                for fp_tbl in table_candidates:
                    if not fp_tbl.exists():
                        continue
                    for _name, lib_type, uri in parse_lib_table(fp_tbl):
                        if lib_type not in ("KiCad", "KiCad_Fp"):
                            continue
                        p = Path(resolve_uri(uri, project_path, table_dir=fp_tbl.parent))
                        if not p.is_dir():
                            continue
                        rp = _safe_resolve(p)
                        if not _is_within_any(rp, managed_fp_roots):
                            skipped_external += 1
                            continue
                        fp_dirs_set.add(rp)

                # Also include all local managed *.pretty directories.
                for root in managed_fp_roots:
                    rr = _safe_resolve(root)
                    if not rr.is_dir():
                        continue
                    for pretty in rr.glob("*.pretty"):
                        if pretty.is_dir():
                            fp_dirs_set.add(_safe_resolve(pretty))

                project_fp_dirs = sorted(fp_dirs_set)
                result["scan_fp_dirs"] = [p.as_posix() for p in project_fp_dirs]
                if skipped_external:
                    result["errors"].append(
                        f"footprint scan: skipped {skipped_external} external fp-lib-table entr{('y' if skipped_external == 1 else 'ies')}"
                    )

                # Also protect footprints that are placed on PCBs or schematics
                # even if the symbol library definition no longer references them
                # (e.g. after reimport changed the symbol's footprint property).
                try:
                    lib_aliases: set[str] = set()
                    for fp_dir in project_fp_dirs:
                        stem = fp_dir.name
                        lib_aliases.add(stem[:-len(".pretty")] if stem.endswith(".pretty") else stem)
                    pcb_scan_roots: list[Path] = []
                    if scope == "shared":
                        for root in managed_fp_roots:
                            parent = _safe_resolve(root).parent
                            if parent.is_dir():
                                pcb_scan_roots.append(parent)
                    else:
                        pcb_scan_roots.append(project_path)
                    for scan_root in pcb_scan_roots:
                        for pcb_file in scan_root.rglob("*.kicad_pcb"):
                            try:
                                content = pcb_file.read_text(encoding="utf-8", errors="replace")
                                for m in re.finditer(r'\(footprint\s+"([^"]+)"', content):
                                    fp_ref = m.group(1).strip()
                                    if ":" in fp_ref:
                                        lib_alias, fp_name = fp_ref.split(":", 1)
                                        if lib_alias in lib_aliases:
                                            sym_fps.add(fp_name)
                            except Exception:
                                pass
                        for sch_file in scan_root.rglob("*.kicad_sch"):
                            try:
                                content = sch_file.read_text(encoding="utf-8", errors="replace")
                                for m in re.finditer(r'\(property\s+"Footprint"\s+"([^"]+)"', content):
                                    fp_ref = m.group(1).strip()
                                    if ":" in fp_ref:
                                        lib_alias, fp_name = fp_ref.split(":", 1)
                                        if lib_alias in lib_aliases:
                                            sym_fps.add(fp_name)
                            except Exception:
                                pass
                except Exception:
                    pass

                for fp_dir in project_fp_dirs:
                    for mod in fp_dir.glob("*.kicad_mod"):
                        if mod.stem not in sym_fps:
                            result["orphan_fps"].append(mod)
            except Exception as exc:
                result["errors"].append(f"footprint scan: {exc}")

            # ── Step 3: orphan 3D models ──
            try:
                model_extensions = {".step", ".stp", ".wrl", ".stl"}
                managed_model_roots = [Path(p) for p in (result.get("managed_model_roots") or [])]
                orphan_fp_paths = {str(_safe_resolve(p)) for p in result.get("orphan_fps", [])}
                # Build set of 3D model filenames referenced by footprints in project fp libs
                referenced_models: set = set()
                for fp_dir in project_fp_dirs:
                    for mod in fp_dir.glob("*.kicad_mod"):
                        if str(_safe_resolve(mod)) in orphan_fp_paths:
                            # This footprint is planned for deletion in the same cleanup run.
                            # Do not keep its 3D models alive.
                            continue
                        try:
                            content = mod.read_text(encoding="utf-8", errors="replace")
                            for m in re.finditer(r'\(model\s+"([^"]+)"', content):
                                referenced_models.add(Path(m.group(1).strip()).name.lower())
                        except Exception:
                            pass
                # Also scan PCB files for models referenced by placed footprints.
                # PCB footprint instances embed model paths directly and may reference
                # models that have no corresponding .kicad_mod template (e.g. after
                # a reimport changed the footprint name but the PCB was not updated yet).
                try:
                    pcb_model_roots: list[Path] = []
                    if scope == "shared":
                        for root in managed_model_roots:
                            parent = _safe_resolve(root).parent
                            if parent.is_dir():
                                pcb_model_roots.append(parent)
                    else:
                        pcb_model_roots.append(project_path)
                    for scan_root in pcb_model_roots:
                        for pcb_file in scan_root.rglob("*.kicad_pcb"):
                            try:
                                content = pcb_file.read_text(encoding="utf-8", errors="replace")
                                for m in re.finditer(r'\(model\s+"([^"]+)"', content):
                                    referenced_models.add(Path(m.group(1).strip()).name.lower())
                            except Exception:
                                pass
                except Exception:
                    pass
                # Search for 3D model files only in managed model directories.
                model_search_dirs: set[Path] = set()
                for fp_dir in project_fp_dirs:
                    parent = fp_dir.parent
                    for candidate in (
                        parent / "3dmodels",
                        parent / "EASYEDA_MODELS",
                        parent / f"{fp_dir.stem}.3dshapes",
                    ):
                        if candidate.is_dir() and _is_within_any(candidate, managed_model_roots):
                            model_search_dirs.add(_safe_resolve(candidate))
                for root in managed_model_roots:
                    rr = _safe_resolve(root)
                    if rr.is_dir():
                        model_search_dirs.add(rr)
                for search_dir in model_search_dirs:
                    for model_file in search_dir.rglob("*"):
                        if model_file.suffix.lower() in model_extensions:
                            if model_file.name.lower() not in referenced_models:
                                result["orphan_models"].append(model_file)
            except Exception as exc:
                result["errors"].append(f"3D model scan: {exc}")

            # Deduplicate by resolved path so counts match preview and deletion list.
            try:
                uniq_fp: dict[str, Path] = {}
                for p in result["orphan_fps"]:
                    rp = _safe_resolve(Path(p))
                    uniq_fp[str(rp)] = rp
                result["orphan_fps"] = list(uniq_fp.values())
            except Exception:
                pass
            try:
                uniq_models: dict[str, Path] = {}
                for p in result["orphan_models"]:
                    rp = _safe_resolve(Path(p))
                    uniq_models[str(rp)] = rp
                result["orphan_models"] = list(uniq_models.values())
            except Exception:
                pass

            return result

        def _do_clean(stats: dict):
            """Actually delete everything found during scan."""
            project_path = Path(self.project_path)
            managed_fp_roots = [Path(p) for p in (stats.get("managed_fp_roots") or [])]
            managed_model_roots = [Path(p) for p in (stats.get("managed_model_roots") or [])]
            removed_sym_entries = 0
            removed_fp_entries = 0
            deleted_fp = 0
            deleted_3d = 0
            skipped_fp = 0
            skipped_3d = 0
            clean_errors = 0

            def _safe_resolve(path: Path) -> Path:
                try:
                    return path.resolve()
                except Exception:
                    return path

            def _is_within_any(path: Path, roots: list[Path]) -> bool:
                rp = _safe_resolve(path)
                for root in roots:
                    rr = _safe_resolve(root)
                    try:
                        if rp == rr or rr in rp.parents:
                            return True
                    except Exception:
                        continue
                return False

            # Prune lib-table entries (project + shared lib_root)
            if stats["stale_sym_libs"] or stats["stale_fp_libs"]:
                try:
                    from ..core.lib_tables import LibTablesManager
                    from ..core.lib_paths import resolve_lib_root
                    general = (self.settings.get("general", {}) or {})
                    scope = str(general.get("library_scope", "project")).strip().lower()
                    table_roots: set[Path] = {project_path}
                    if scope in ("project", "shared"):
                        try:
                            lib_root, _ = resolve_lib_root(general, project_path)
                            table_roots.add(lib_root)
                        except Exception:
                            pass
                    total_sym, total_fp = 0, 0
                    for root in table_roots:
                        mgr = LibTablesManager(root, log=self.log)
                        s, f = mgr.prune_invalid_table_paths(project_path=project_path)
                        total_sym += s
                        total_fp += f
                    removed_sym_entries = total_sym
                    removed_fp_entries = total_fp
                    self.log(f"Clean Library: removed {total_sym} stale symbol lib(s), {total_fp} stale footprint lib(s)\n")
                except Exception as exc:
                    clean_errors += 1
                    self.log(f"Clean Library: lib-table prune error: {exc}\n")

            # Delete orphan footprints
            for mod in stats["orphan_fps"]:
                if not _is_within_any(mod, managed_fp_roots):
                    skipped_fp += 1
                    self.log(
                        f"Clean Library: skipped footprint outside managed roots: {_safe_resolve(mod)}\n"
                    )
                    continue
                try:
                    mod.unlink()
                    deleted_fp += 1
                    self.log(f"Clean Library: deleted footprint {mod.name}\n")
                except Exception as exc:
                    clean_errors += 1
                    self.log(f"Clean Library: could not delete {mod.name}: {exc}\n")

            # Delete orphan 3D models
            for model_file in stats["orphan_models"]:
                if not _is_within_any(model_file, managed_model_roots):
                    skipped_3d += 1
                    self.log(
                        f"Clean Library: skipped 3D model outside managed roots: {_safe_resolve(model_file)}\n"
                    )
                    continue
                try:
                    model_file.unlink()
                    deleted_3d += 1
                    self.log(f"Clean Library: deleted 3D model {model_file.name}\n")
                except Exception as exc:
                    clean_errors += 1
                    self.log(f"Clean Library: could not delete {model_file.name}: {exc}\n")

            self.log("Clean Library: done.\n")
            summary = (
                "Clean Library completed.\n\n"
                f"Removed stale entries: {removed_sym_entries} symbol, {removed_fp_entries} footprint\n"
                f"Deleted files: {deleted_fp} footprint, {deleted_3d} 3D model"
            )
            if skipped_fp or skipped_3d:
                summary += f"\nSkipped (outside managed roots): {skipped_fp} footprint, {skipped_3d} 3D model"
            if clean_errors:
                summary += f"\nErrors: {clean_errors} (see console log)"
            wx.CallAfter(
                wx.MessageBox,
                summary,
                "Clean Library",
                (wx.OK | (wx.ICON_WARNING if clean_errors else wx.ICON_INFORMATION)),
                self,
            )

        def _worker():
            self.log("Clean Library: scanning...\n")
            stats = _scan()

            def _log_paths(title: str, items):
                if not items:
                    return
                self.log(f"Clean Library: {title} ({len(items)}):\n")
                for p in sorted({_p for _p in items}, key=lambda x: str(x)):
                    self.log(f"  - {p}\n")

            for err in stats["errors"]:
                self.log(f"Clean Library: warning: {err}\n")

            n_sym = len(stats["stale_sym_libs"])
            n_fp_lib = len(stats["stale_fp_libs"])
            n_fp = len(stats["orphan_fps"])
            n_3d = len(stats["orphan_models"])
            total = n_sym + n_fp_lib + n_fp + n_3d

            fp_roots = ", ".join(stats.get("managed_fp_roots") or []) or "—"
            model_roots = ", ".join(stats.get("managed_model_roots") or []) or "—"
            self.log(f"Clean Library: managed footprint root(s): {fp_roots}\n")
            self.log(f"Clean Library: managed model root(s): {model_roots}\n")
            self.log(
                f"Clean Library: scanning {len(stats.get('scan_fp_dirs') or [])} managed footprint librar"
                f"{'y' if len(stats.get('scan_fp_dirs') or []) == 1 else 'ies'}\n"
            )
            self.log(
                f"Clean Library: found {n_sym} stale symbol lib(s), "
                f"{n_fp_lib} stale footprint lib(s), "
                f"{n_fp} orphan footprint(s), "
                f"{n_3d} orphan 3D model(s)\n"
            )

            if n_sym:
                self.log(f"Clean Library: stale sym-lib-table entries to remove ({n_sym}):\n")
                for name, path in stats["stale_sym_libs"]:
                    self.log(f"  - {name}: {path}\n")
            if n_fp_lib:
                self.log(f"Clean Library: stale fp-lib-table entries to remove ({n_fp_lib}):\n")
                for name, path in stats["stale_fp_libs"]:
                    self.log(f"  - {name}: {path}\n")
            _log_paths("orphan footprint files to delete", stats["orphan_fps"])
            _log_paths("orphan 3D model files to delete", stats["orphan_models"])

            if total == 0:
                notice = "Nothing to clean — everything looks good."
                if stats["errors"]:
                    notice = (
                        "No eligible stale entries were found.\n\n"
                        + "\n".join(stats["errors"])
                    )
                wx.CallAfter(
                    wx.MessageBox,
                    notice,
                    "Clean Library",
                    wx.OK | (wx.ICON_WARNING if stats["errors"] else wx.ICON_INFORMATION),
                    self,
                )
                return

            lines = []
            if n_sym:
                lines.append(f"  • {n_sym} stale symbol library entry(ies) in sym-lib-table")
            if n_fp_lib:
                lines.append(f"  • {n_fp_lib} stale footprint library entry(ies) in fp-lib-table")
            if n_fp:
                lines.append(f"  • {n_fp} orphan footprint file(s) (.kicad_mod)")
            if n_3d:
                lines.append(f"  • {n_3d} orphan 3D model file(s) (.step/.wrl/.stl)")
            msg = "The following will be permanently deleted:\n\n" + "\n".join(lines) + "\n\nProceed?"

            def _confirm():
                dlg = wx.MessageDialog(
                    self, msg, "Clean Library", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING
                )
                result = dlg.ShowModal()
                dlg.Destroy()
                if result == wx.ID_YES:
                    threading.Thread(target=_do_clean, args=(stats,), daemon=True).start()

            wx.CallAfter(_confirm)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_library_manager(self, *_):
        from .library_panel import LibraryManagerDialog

        dlg = None
        try:
            dlg = LibraryManagerDialog(self)
            dlg.ShowModal()
        finally:
            try:
                if dlg is not None:
                    dlg.Destroy()
            except Exception:
                pass

    # Expose update for button
    def update_library(self):
        try:
            if not getattr(self, "_deps_ready", False):
                self._check_and_offer_install_deps(force_prompt=True)
                return
            if getattr(self.library, "state", None) == LibraryState.DOWNLOAD_RUNNING:
                return
            if hasattr(self, "update_db_btn") and self.update_db_btn:
                self.update_db_btn.Enable(False)
            try:
                self.set_db_ready(False)
            except Exception:
                pass
            self.library.update()
        except Exception:
            try:
                if hasattr(self, "update_db_btn") and self.update_db_btn:
                    self.update_db_btn.Enable(True)
            except Exception:
                pass
            raise

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

    def _on_import_from_bom(self, *_):
        """Open a BOM file, extract LCSC IDs and populate the parts list."""
        from ..core.bom_parser import parse_bom_lcsc_ids

        with wx.FileDialog(
            self,
            "Open BOM file",
            wildcard="BOM files (*.csv;*.xlsx;*.tsv)|*.csv;*.xlsx;*.tsv|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()

        try:
            ids = parse_bom_lcsc_ids(path)
        except Exception as exc:
            wx.MessageBox(
                f"Failed to parse BOM file:\n{exc}",
                "BOM import error",
                wx.OK | wx.ICON_ERROR,
            )
            return

        if not ids:
            wx.MessageBox(
                "No LCSC part numbers found in the selected file.",
                "BOM import",
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        self.log(f"BOM: found {len(ids)} LCSC ID(s): {', '.join(ids)}\n")
        self.populate_from_lcsc_ids(ids)

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
        if getattr(self, "_batch_import_running", False):
            return
        if hasattr(self, "gauge") and self.gauge:
            self.gauge.SetRange(100)
            self.gauge.SetValue(0)

    def _on_progress_update(self, e):
        if getattr(self, "_batch_import_running", False):
            return
        if hasattr(self, "gauge") and self.gauge:
            try:
                val = int(e.value)
            except Exception:
                val = 0
            self.gauge.SetValue(max(0, min(100, val)))

    def _on_symbol_index_build_completed(self, *_):
        if getattr(self, "_batch_import_running", False):
            return
        if hasattr(self, "gauge") and self.gauge:
            try:
                self.gauge.SetValue(0)
            except Exception:
                pass

    def _on_db_download_completed(self, *_):
        """Enable search and refresh categories when DB is ready."""
        try:
            if getattr(self.library, "state", None) == LibraryState.INITIALIZED:
                try:
                    self.refresh_categories()
                except Exception:
                    pass
                try:
                    self.set_db_ready(True)
                except Exception:
                    pass
        except Exception:
            pass

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
        self._apply_logging_level_settings()

    def _apply_logging_level_settings(self) -> None:
        try:
            general = (self.settings.get("general", {}) or {})
        except Exception:
            general = {}
        debug_enabled = as_bool(general.get("debug_log"), default=False)
        level = logging.DEBUG if debug_enabled else logging.INFO

        root = logging.getLogger()
        root.setLevel(level)
        try:
            if hasattr(self, "_ui_log_handler") and self._ui_log_handler is not None:
                self._ui_log_handler.setLevel(level)
        except Exception:
            pass

        noisy = ("urllib3", "requests")
        for name in noisy:
            try:
                logging.getLogger(name).setLevel(logging.DEBUG if debug_enabled else logging.WARNING)
            except Exception:
                pass

    # Dependency check. KiCad's IPC plugin manager owns installation and updates.
    def _check_and_offer_install_deps(self, force_prompt: bool = False):
        missing = []
        try:
            import requests  # noqa: F401
        except Exception:
            missing.append("requests")
        try:
            from Crypto.Cipher import AES  # noqa: F401
        except Exception:
            try:
                from Cryptodome.Cipher import AES  # noqa: F401
            except Exception:
                missing.append("pycryptodome")
        try:
            import OCP  # noqa: F401
        except Exception:
            missing.append("cadquery-ocp")
        if missing:
            self._deps_ready = False
            self._update_select_enabled()
            msg = (
                "The KiCad-managed IPC environment is missing dependencies:\n"
                f"- {', '.join(missing)}\n\n"
                "Reinstall or update the plugin from KiCad's Plugin and Content Manager."
            )
            prompt = force_prompt or not self._deps_prompted
            if prompt:
                self._deps_prompted = True
                wx.MessageBox(msg, "Dependencies missing", style=wx.OK | wx.ICON_ERROR)
            else:
                try:
                    self.log(msg + "\n")
                except Exception:
                    pass
            return

        self._deps_ready = True
        self._update_select_enabled()
        self._maybe_start_initial_db_download()

    def _maybe_start_initial_db_download(self) -> None:
        """Start the first database download only after requests is available."""

        if not getattr(self, "_deps_ready", False):
            return
        if getattr(self.library, "state", None) != LibraryState.UPDATE_NEEDED:
            return
        self.log("Parts database not found. Starting initial download...\n")
        try:
            self.set_db_ready(False)
        except Exception:
            pass
        self.library.update()

    def _update_select_enabled(self):
        try:
            btn = getattr(self, "select_part_button", None)
            if btn is not None:
                btn.Enable(bool(self._deps_ready))
                if self._deps_ready:
                    btn.SetToolTip("")
                else:
                    btn.SetToolTip("Dependencies not installed.")
        except Exception:
            pass

    def Destroy(self):  # noqa: N802 - wx override
        work = self._active_background_work()
        if work:
            self._show_close_blocked(work)
            return False
        try:
            if self._library_rename_timer is not None:
                self._library_rename_timer.Stop()
        except Exception:
            pass
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
