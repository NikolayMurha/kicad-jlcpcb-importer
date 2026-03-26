"""Assign LCSC main dialog wrapping the part selector as primary UI."""

import os
import json
import sys
import logging
import subprocess
import threading
import shlex
from pathlib import Path
import wx
import wx.dataview as dv
from typing import Optional, Dict

from .partselector import PartSelectorDialog
from .settings import SettingsDialog
from ..core.helpers import HighResWxSize, loadBitmapScaled, GetScaleFactor, PLUGIN_PATH
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
    LogboxAppendEvent,
    MessageEvent,
)
from ..core.library import Library, LibraryState
from ..importers.importer import EasyedaImporter

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
        self._deps_prompted = False
        self._deps_installing = False

        # Build the PartSelectorDialog with self as the logical parent context
        super().__init__(self, parts={})

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

        # BOM import button
        self.bom_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Import from BOM",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(150, -1)),
            0,
        )
        self.bom_btn.SetBitmap(
            loadBitmapScaled("mdi-file-document-outline.png", self.scale_factor)
        )
        self.bom_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.bom_btn, 0, wx.ALL, 5)

        # Library Manager button (project scope only)
        self.library_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Import from other libraries",
            wx.DefaultPosition,
            wx.DefaultSize,
            0,
        )
        self.library_btn.SetBitmap(
            loadBitmapScaled("mdi-file-document-outline.png", self.scale_factor)
        )
        self.library_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.library_btn, 0, wx.ALL, 5)
        topbar.AddStretchSpacer(1)
        self.mode_status_text = wx.StaticText(self, wx.ID_ANY, "")
        self.mode_status_text.SetToolTip("Current library storage mode.")
        topbar.Add(self.mode_status_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)

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
        # Settings updates from PartSelectorDialog
        from ..core.events import EVT_UPDATE_SETTING  # local import to avoid cycle
        self.Bind(EVT_UPDATE_SETTING, self._on_update_setting)
        self.settings_btn.Bind(wx.EVT_BUTTON, self._open_settings)
        self.library_btn.Bind(wx.EVT_BUTTON, self._open_library_manager)
        self.bom_btn.Bind(wx.EVT_BUTTON, self._on_import_from_bom)
        self._update_library_btn_state()
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

        # On first window launch, verify deps and offer installation if missing
        self._check_and_offer_install_deps()
        # Ensure UI matches current deps state
        self._update_select_enabled()

        # Trigger initial DB download if missing and disable search UI until ready
        try:
            if getattr(self.library, "state", None) == LibraryState.UPDATE_NEEDED:
                self.log("Parts database not found. Starting initial download...\n")
                try:
                    self.set_db_ready(False)
                except Exception:
                    pass
                self.library.update()
        except Exception:
            pass

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

    @staticmethod
    def _format_cmd(cmd) -> str:
        if sys.platform.startswith("win"):
            return subprocess.list2cmdline(cmd)
        return " ".join(shlex.quote(str(part)) for part in cmd)

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
                rows.append(
                    (
                        lcsc_id,
                        category,
                        {
                            "mfr_part": mfr_part,
                            "manufacturer": manufacturer,
                            "description": descr,
                            "attributes_json": attributes_json,
                        },
                    )
                )
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
            lcsc_id, category, meta = rows[0]
            self._import_part_via_easyeda(lcsc_id, category, meta)
            return

        self._import_parts_via_easyeda(rows)

    def _import_parts_via_easyeda(self, rows):
        base = Path(PLUGIN_PATH)
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
            ok_count = 0
            total = len(rows)
            try:
                for idx, (lcsc_id, category, meta) in enumerate(rows, start=1):
                    wx.PostEvent(
                        self,
                        LogboxAppendEvent(msg=f"Importing {lcsc_id} ({idx}/{total})...\n"),
                    )
                    try:
                        ok, lib_base = importer.import_part(
                            lcsc_id=lcsc_id,
                            category=category,
                            meta=meta or {},
                        )
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
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(
                        msg=f"Batch import finished: {ok_count}/{total} successful.\n"
                    ),
                )
                failed_count = total - ok_count
                wx.CallAfter(self.show_import_status, ok_count, failed_count)
            finally:
                wx.EndBusyCursor()
                if btn is not None:
                    wx.CallAfter(btn.Enable, True)

        threading.Thread(target=_worker, daemon=True).start()

    def _import_part_via_easyeda(self, lcsc_id: str, category: str = "", meta: Optional[Dict] = None):
        base = Path(PLUGIN_PATH)
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
                ok, lib_base = False, self.project_path
            finally:
                wx.EndBusyCursor()
            
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
            else:
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(msg=f"*********  IMPORT FAILED: {lcsc_id}  *********\n"),
                )
        threading.Thread(target=_worker, daemon=True).start()

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
        # 1) Use KIPRJMOD when available
        kiprjmod = os.environ.get("KIPRJMOD")
        board = self.pcbnew.GetBoard()
        if kiprjmod:
            project_dir = kiprjmod
        else:
            try:
                project_dir = os.getcwd()
            except Exception:
                project_dir = str(PLUGIN_PATH)
        board_name = os.path.split(board.GetFileName())[1] if board else "board.kicad_pcb"
        schematic_name = f"{board_name.split('.')[0]}.kicad_sch"
        return project_dir, board_name, schematic_name

    # Settings persistence
    # Per-project settings are stored in jlcpcb_importer.json next to the
    # .kicad_pro file.  Each subproject (PCB_1/, PCB_2/, …) carries its own
    # file so settings travel with the project and are tracked by git.
    # KiCad overwrites .kicad_pro on save, so we never touch it.

    @property
    def _project_settings_path(self) -> str:
        """Return path to jlcpcb_importer.json in the current project directory."""
        return os.path.join(self.project_path, "jlcpcb_importer.json")

    def _load_settings(self):
        # 1) Per-project jlcpcb_importer.json next to .kicad_pro
        try:
            with open(self._project_settings_path, encoding="utf-8") as f:
                self.settings = json.load(f)
                return
        except Exception:
            pass
        # 2) Plugin-level jlcpcb_importer.json (shipped default)
        try:
            with open(os.path.join(PLUGIN_PATH, "jlcpcb_importer.json"), encoding="utf-8") as f:
                self.settings = json.load(f)
                return
        except Exception:
            pass
        self.settings = {}

    def _save_settings(self):
        try:
            with open(self._project_settings_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    # Called when PartSelectorDialog posts UpdateSetting
    def _on_update_setting(self, e):
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value
        self._save_settings()
        if e.setting in ("library_scope", "lib_path", "lib_prefix"):
            self._sync_shared_meta(changed_setting=e.setting)
        if e.setting == "library_scope":
            self._update_library_btn_state()

    def _sync_shared_meta(self, changed_setting: str = ""):
        try:
            general = (self.settings.get("general", {}) or {})
            if str(general.get("library_scope", "project")).strip().lower() != "shared":
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

    def _update_library_btn_state(self):
        """Enable Library button for project-scoped library workflows."""
        try:
            scope = (self.settings.get("general", {}) or {}).get("library_scope", "project")
            scope_key = str(scope).strip().lower()
            is_local = scope_key in ("project", "shared")
            self.library_btn.Enable(is_local)
            tip = "" if is_local else "Available for Project/Shared scopes only."
            self.library_btn.SetToolTip(tip)
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

    # Dependency check and interactive installer
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
            import openpyxl  # noqa: F401
        except Exception:
            missing.append("openpyxl")

        if missing:
            self._deps_ready = False
            self._update_select_enabled()
            lib_dir = Path(PLUGIN_PATH) / "lib"
            cmd = [
                self._resolve_python_exe(),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--target",
                str(lib_dir),
                *missing,
            ]
            cmd_text = self._format_cmd(cmd)
            msg = (
                "Missing dependencies for importer:\n"
                f"- {', '.join(missing)}\n\n"
                "Install them now? This will run:\n"
                f"{cmd_text}\n"
            )
            prompt = force_prompt or not self._deps_prompted
            if prompt:
                self._deps_prompted = True
                choice = wx.MessageBox(msg, "Dependencies missing", style=wx.YES_NO | wx.ICON_WARNING)
                if choice == wx.YES:
                    self._install_requirements_async(missing)
            else:
                try:
                    self.log(msg + "\n")
                except Exception:
                    pass
            return

        self._deps_ready = True
        self._update_select_enabled()

    def _install_requirements_async(self, packages):
        if self._deps_installing:
            return
        self._deps_installing = True

        def _worker():
            ok = self._install_requirements(packages)

            def _after():
                self._deps_installing = False
                if ok:
                    try:
                        self.log("Dependencies installed.\n")
                    except Exception:
                        pass
                    self._check_and_offer_install_deps(force_prompt=False)
                else:
                    wx.MessageBox(
                        "Dependency installation failed. Check the log for details.",
                        "Dependencies",
                        wx.ICON_ERROR,
                    )

            wx.CallAfter(_after)

        threading.Thread(target=_worker, daemon=True).start()

    def _install_requirements(self, packages):
        if not packages:
            return True
        lib_dir = Path(PLUGIN_PATH) / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        python_exe = self._resolve_python_exe()
        env = os.environ.copy()
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        env.setdefault("PYTHONNOUSERSITE", "1")
        cmd = [
            python_exe,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--target",
            str(lib_dir),
            *packages,
        ]
        try:
            self.log(f"Running: {self._format_cmd(cmd)}\n")
        except Exception:
            pass
        rc = self._run_and_stream(cmd, env=env)
        if rc != 0:
            try:
                self.log("pip failed, attempting ensurepip...\n")
            except Exception:
                pass
            ensure_cmd = [python_exe, "-m", "ensurepip", "--upgrade"]
            self._run_and_stream(ensure_cmd, env=env)
            rc = self._run_and_stream(cmd, env=env)
        return rc == 0

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
