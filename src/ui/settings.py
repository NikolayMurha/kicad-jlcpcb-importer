"""Contains the settings dialog for the LCSC plugin."""

import logging
import os
from pathlib import Path

import wx  # pylint: disable=import-error

from ..core.events import UpdateSetting
from ..core.helpers import HighResWxSize, loadBitmapScaled
from ..core.lib_tables import LibTablesManager
from ..core.lib_paths import (
    DEFAULT_LIB_PATH,
    DEFAULT_LIB_DIR_NAME,
    resolve_group_by_category,
    resolve_lib_root,
    resolve_library_base_name,
)
from ..core.sym_lib_reader import get_user_settings_path


class SettingsDialog(wx.Dialog):
    """Settings dialog for storage scope and generation options."""

    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB importer plugin settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(560, 380)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent

        # Hotkeys
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)
        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))

        layout = wx.BoxSizer(wx.VERTICAL)

        # Storage scope (Project vs System)
        self.library_scope_box = wx.RadioBox(
            self,
            id=wx.ID_ANY,
            label="Where to store symbols and models?",
            choices=["Project", "System", "Shared Library"],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
            name="general_library_scope",
        )
        self.library_scope_box.SetToolTip(wx.ToolTip(
            "Project — libraries are stored inside the project folder.\n"
            "System — libraries are stored in a shared plugin folder.\n"
            "Shared Library — libraries and tables are stored in one shared folder."
        ))
        self.library_scope_box.Bind(wx.EVT_RADIOBOX, self.update_settings)

        storage_scope_sizer = wx.BoxSizer(wx.HORIZONTAL)
        storage_scope_sizer.Add(
            wx.StaticBitmap(
                self,
                wx.ID_ANY,
                loadBitmapScaled("database-outline.png", self.parent.scale_factor, static=True),
                wx.DefaultPosition,
                wx.DefaultSize,
                0,
            ),
            10,
            wx.ALL | wx.EXPAND,
            5,
        )
        storage_scope_sizer.Add(self.library_scope_box, 100, wx.ALL | wx.EXPAND, 5)
        layout.Add(storage_scope_sizer, 0, wx.ALL | wx.EXPAND, 5)

        # Generation options box
        gen_box = wx.StaticBoxSizer(wx.VERTICAL, self, label="Generated libraries")

        # Library format
        format_row = wx.BoxSizer(wx.HORIZONTAL)
        format_row.Add(wx.StaticText(self, label="Library format:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_format_ctrl = wx.Choice(
            self,
            wx.ID_ANY,
            choices=["EasyEDA Pro", "KiCad"],
            name="general_lib_format",
        )
        self.lib_format_ctrl.SetToolTip(wx.ToolTip("Select output library format."))
        self.lib_format_ctrl.Bind(wx.EVT_CHOICE, self.update_settings)
        format_row.Add(self.lib_format_ctrl, 1, wx.EXPAND)
        gen_box.Add(format_row, 0, wx.ALL | wx.EXPAND, 5)

        # Grouping mode
        group_row = wx.BoxSizer(wx.HORIZONTAL)
        self.group_by_category_ctrl = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Group components by category",
            name="general_group_by_category",
        )
        self.group_by_category_ctrl.SetToolTip(wx.ToolTip("Group components by category."))
        self.group_by_category_ctrl.Bind(wx.EVT_CHECKBOX, self.update_settings)
        group_row.Add(self.group_by_category_ctrl, 1, wx.EXPAND)
        gen_box.Add(group_row, 0, wx.ALL | wx.EXPAND, 5)

        # Library name prefix
        prefix_row = wx.BoxSizer(wx.HORIZONTAL)
        self._lib_name_label = wx.StaticText(self, label="Library name prefix:")
        prefix_row.Add(self._lib_name_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_prefix_ctrl = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.parent.window, wx.Size(200, -1)),
            name="general_lib_prefix",
        )
        self.lib_prefix_ctrl.SetToolTip(wx.ToolTip("Prefix prepended to generated library names (e.g. JLCPCB)."))
        self.lib_prefix_ctrl.Bind(wx.EVT_TEXT, self.update_settings)
        prefix_row.Add(self.lib_prefix_ctrl, 1, wx.EXPAND)
        gen_box.Add(prefix_row, 0, wx.ALL | wx.EXPAND, 5)

        # Library location:
        # - Project scope: directory name under ${KIPRJMOD}
        # - Shared scope: path to a shared library folder
        lib_path_row = wx.BoxSizer(wx.HORIZONTAL)
        self._lib_path_label = wx.StaticText(self, label="Library path:")
        lib_path_row.Add(self._lib_path_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_path_ctrl = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            name="general_lib_path",
        )
        self.lib_path_ctrl.SetToolTip(wx.ToolTip("Library location setting depends on selected storage scope."))
        self.lib_path_ctrl.Bind(wx.EVT_TEXT, self.update_settings)
        self._lib_path_browse_btn = wx.Button(self, wx.ID_ANY, "Browse\u2026", style=wx.BU_EXACTFIT)
        self._lib_path_browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_lib_path)
        lib_path_row.Add(self.lib_path_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        lib_path_row.Add(self._lib_path_browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        gen_box.Add(lib_path_row, 0, wx.ALL | wx.EXPAND, 5)

        maintenance_row = wx.BoxSizer(wx.HORIZONTAL)
        self._cleanup_tables_btn = wx.Button(self, wx.ID_ANY, "Clear invalid paths in tables")
        self._cleanup_tables_btn.SetToolTip(wx.ToolTip(
            "Remove missing-library entries from sym-lib-table and fp-lib-table."
        ))
        self._cleanup_tables_btn.Bind(wx.EVT_BUTTON, self._on_cleanup_invalid_table_paths)
        maintenance_row.Add(self._cleanup_tables_btn, 0, wx.TOP, 2)
        gen_box.Add(maintenance_row, 0, wx.ALL | wx.EXPAND, 5)

        layout.Add(gen_box, 0, wx.ALL | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def load_settings(self):
        general = self.parent.settings.get("general", {})
        self.update_library_scope(general.get("library_scope", "project"))
        self.update_group_by_category(resolve_group_by_category(general, default=False))
        self.update_lib_prefix(
            resolve_library_base_name(general, project_path=Path(self.parent.project_path))
        )
        self.update_lib_format(general.get("lib_format", "easyeda_pro"))
        self.update_lib_path(self._resolve_lib_path_setting(general))

    def _resolve_lib_path_setting(self, general: dict) -> str:
        scope = str(general.get("library_scope", "project")).strip().lower()
        raw = str(general.get("lib_path") or "").strip()
        if scope == "project":
            if raw.startswith("${KIPRJMOD}/"):
                raw = raw[len("${KIPRJMOD}/"):]
            raw = raw.strip().strip("/\\")
            if not raw:
                return DEFAULT_LIB_DIR_NAME
            # Project mode expects a directory name under KIPRJMOD.
            if "/" in raw or "\\" in raw:
                p = Path(raw)
                return p.name or DEFAULT_LIB_DIR_NAME
            return raw
        if scope == "shared":
            if not raw:
                return "${KIPRJMOD}/../library"
            if "${" not in raw and "/" not in raw and "\\" not in raw:
                return f"${{KIPRJMOD}}/../{raw}"
            return raw
        return raw or DEFAULT_LIB_PATH

    def update_settings(self, event):
        """Update and persist a setting that was changed."""
        obj = event.GetEventObject()
        raw_name = obj.GetName()
        if "_" not in raw_name:
            return
        section, name = raw_name.split("_", 1)
        if hasattr(obj, "GetValue"):
            value = obj.GetValue()
        elif hasattr(obj, "GetSelection"):
            sel = obj.GetSelection()
            if name == "library_scope":
                value = "project" if sel == 0 else ("system" if sel == 1 else "shared")
            elif name == "lib_format":
                value = "easyeda_pro" if sel == 0 else "kicad"
            else:
                value = sel
        else:
            value = None
        getattr(self, f"update_{name}")(value)
        if name == "library_scope":
            scope_key = str(value or "project").strip().lower()
            normalized_lib_path = self._normalize_lib_path_for_scope(
                scope_key,
                self.lib_path_ctrl.GetValue(),
            )
            if self.lib_path_ctrl.GetValue() != normalized_lib_path:
                self.lib_path_ctrl.ChangeValue(normalized_lib_path)
            wx.PostEvent(
                self.parent,
                UpdateSetting(section="general", setting="lib_path", value=normalized_lib_path),
            )
        if name == "library_scope" and str(value).lower() == "shared":
            self._show_shared_scope_notice()
        wx.PostEvent(
            self.parent,
            UpdateSetting(section=section, setting=name, value=value),
        )

    def quit_dialog(self, *_):
        self.Destroy()
        self.EndModal(0)

    def update_library_scope(self, scope):
        if isinstance(scope, str):
            key = scope.lower()
            if key == "project":
                idx = 0
            elif key == "system":
                idx = 1
            else:
                idx = 2
        else:
            idx = int(scope) if scope in (0, 1, 2) else 0
        try:
            self.library_scope_box.SetSelection(idx)
        except Exception:
            pass
        scope_key = "project" if idx == 0 else ("system" if idx == 1 else "shared")

        try:
            self._lib_path_label.Enable(scope_key != "system")
            self.lib_path_ctrl.Enable(scope_key != "system")
        except Exception:
            pass

        if scope_key == "project":
            self._lib_path_label.SetLabel("Library dir name:")
            self.lib_path_ctrl.SetHint("library")
            self.lib_path_ctrl.SetToolTip(wx.ToolTip(
                "Directory name under ${KIPRJMOD} where plugin libraries are stored."
            ))
            self._lib_path_browse_btn.Enable(False)
        elif scope_key == "shared":
            self._lib_path_label.SetLabel("Shared library path:")
            self.lib_path_ctrl.SetHint("${KIPRJMOD}/../library")
            self.lib_path_ctrl.SetToolTip(wx.ToolTip(
                "Shared library folder. Tables are stored there and linked into each project."
            ))
            self._lib_path_browse_btn.Enable(True)
        else:
            self._lib_path_label.SetLabel("Library path:")
            self._lib_path_browse_btn.Enable(False)

        try:
            normalized = self._normalize_lib_path_for_scope(scope_key, self.lib_path_ctrl.GetValue())
            if self.lib_path_ctrl.GetValue() != normalized:
                self.lib_path_ctrl.ChangeValue(normalized)
        except Exception:
            pass

        self.Layout()

    def _normalize_lib_path_for_scope(self, scope_key: str, raw_value: str) -> str:
        scope = str(scope_key or "").strip().lower()
        raw = str(raw_value or "").strip()
        if scope == "project":
            if raw.startswith("${KIPRJMOD}/"):
                raw = raw[len("${KIPRJMOD}/"):]
            raw = raw.strip().strip("/\\")
            if not raw:
                return DEFAULT_LIB_DIR_NAME
            if "/" in raw or "\\" in raw:
                p = Path(raw)
                return p.name or DEFAULT_LIB_DIR_NAME
            return raw
        if scope == "shared":
            if not raw:
                return "${KIPRJMOD}/../library"
            if "${" not in raw and "/" not in raw and "\\" not in raw:
                return f"${{KIPRJMOD}}/../{raw}"
            return raw
        return raw or DEFAULT_LIB_PATH

    def update_lib_prefix(self, value: str):
        try:
            self.lib_prefix_ctrl.ChangeValue(str(value) if value is not None else "")
        except Exception:
            pass

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        return default

    def update_group_by_category(self, value):
        try:
            enabled = self._as_bool(value, default=True)
            self.group_by_category_ctrl.SetValue(enabled)
            self._lib_name_label.SetLabel(
                "Library name prefix:" if enabled else "Library name:"
            )
            tip = (
                "Prefix prepended to generated library names (e.g. JLCPCB)."
                if enabled
                else "Fixed name for generated library files (e.g. JLCPCB)."
            )
            self.lib_prefix_ctrl.SetToolTip(wx.ToolTip(tip))
            self.Layout()
        except Exception:
            pass

    def update_lib_format(self, value: str):
        if isinstance(value, str):
            key = value.strip().lower()
            idx = 1 if key == "kicad" else 0
        else:
            idx = int(value) if value in (0, 1) else 0
        try:
            self.lib_format_ctrl.SetSelection(idx)
        except Exception:
            pass
    def update_lib_path(self, value: str):
        try:
            self.lib_path_ctrl.ChangeValue(str(value) if value is not None else "")
        except Exception:
            pass

    def _on_browse_lib_path(self, _evt):
        """Open a directory picker; store result as ${KIPRJMOD}/... path."""
        from pathlib import Path

        project_path = Path(self.parent.project_path)

        try:
            scope = self.library_scope_box.GetSelection()
        except Exception:
            scope = 0
        if scope != 2:
            return

        # Resolve current value to a real path for the dialog start directory
        current = self.lib_path_ctrl.GetValue().strip() or "${KIPRJMOD}/../library"
        resolved_str = current.replace("${KIPRJMOD}", str(project_path))
        resolved = Path(resolved_str)
        start_dir = str(resolved) if resolved.exists() else str(project_path)

        dlg = wx.DirDialog(
            self,
            message="Choose shared library folder",
            defaultPath=start_dir,
            style=wx.DD_DEFAULT_STYLE,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                chosen = Path(dlg.GetPath())
                # Express as ${KIPRJMOD}/... using os.path.relpath (handles ..)
                rel = os.path.relpath(chosen, project_path)
                result = "${KIPRJMOD}/" + Path(rel).as_posix()
                # SetValue fires EVT_TEXT → update_settings → saves automatically
                self.lib_path_ctrl.SetValue(result)
        finally:
            dlg.Destroy()

    def _show_shared_scope_notice(self):
        wx.MessageBox(
            "Shared Library mode is a project-level workflow for one repository that contains multiple KiCad subprojects. "
            "It lets those subprojects reuse one vendored component library stored inside the repository "
            "(symbols, footprints, and 3D models), so everything travels with git. "
            "Unlike System libraries, this mode does not rely on machine-wide KiCad setup and stays consistent across different computers. "
            "Because of KiCad path limitations, projects that share this library should be kept at the same relative directory level "
            "to preserve stable path expansion.\n\n"
            "Shared Library mode conventions:\n"
            "- sym-lib-table / fp-lib-table are stored inside the shared library directory.\n"
            "- Each project receives links to these tables.\n"
            "- Shared metadata (including library name) is stored in jlcpcb_shared.json.\n\n"
            "Windows warning:\n"
            "Creating symlinks may require Developer Mode or Administrator privileges.\n"
            "If symlink creation fails, plugin will copy table files instead.",
            "Shared Library Mode",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _active_tables_root(self) -> Path:
        general = (self.parent.settings.get("general", {}) or {})
        scope = str(general.get("library_scope", "project")).strip().lower()
        if scope == "shared":
            lib_root, _uri_prefix = resolve_lib_root(general, Path(self.parent.project_path))
            return lib_root
        return Path(self.parent.project_path)

    def _on_cleanup_invalid_table_paths(self, _evt):
        try:
            roots = [self._active_tables_root()]
            user_root = get_user_settings_path()
            if user_root is not None and user_root not in roots:
                roots.append(user_root)

            existing_roots = [
                r for r in roots if (r / "sym-lib-table").exists() or (r / "fp-lib-table").exists()
            ]
            if not existing_roots:
                wx.MessageBox(
                    "No library tables found in active project/shared scope or KiCad user settings.",
                    "Cleanup Tables",
                    wx.OK | wx.ICON_INFORMATION,
                )
                return

            details = []
            total_sym = 0
            total_fp = 0
            for root in existing_roots:
                mgr = LibTablesManager(root, log=lambda _m: None)
                removed_sym, removed_fp = mgr.prune_invalid_table_paths(
                    project_path=Path(self.parent.project_path)
                )
                total_sym += removed_sym
                total_fp += removed_fp
                if removed_sym or removed_fp:
                    details.append(f"{root}\n  sym: {removed_sym}, fp: {removed_fp}")

            total = total_sym + total_fp
            if total == 0:
                wx.MessageBox(
                    "No invalid paths found in sym-lib-table / fp-lib-table.",
                    "Cleanup Tables",
                    wx.OK | wx.ICON_INFORMATION,
                )
                return
            details_text = "\n\n".join(details) if details else "(details unavailable)"
            wx.MessageBox(
                "Removed invalid entries:\n"
                f"- sym-lib-table: {total_sym}\n"
                f"- fp-lib-table: {total_fp}\n\n"
                f"Roots:\n{details_text}",
                "Cleanup Tables",
                wx.OK | wx.ICON_INFORMATION,
            )
        except Exception as exc:
            wx.MessageBox(
                f"Failed to clean library tables:\n{exc}",
                "Cleanup Tables",
                wx.OK | wx.ICON_ERROR,
            )
