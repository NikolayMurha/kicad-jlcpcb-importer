"""Contains the settings dialog for the LCSC plugin."""

import logging
import os

import wx  # pylint: disable=import-error

from ..core.events import UpdateSetting
from ..core.helpers import HighResWxSize, loadBitmapScaled

_DEFAULT_LIB_PATH = "${KIPRJMOD}/library"


class SettingsDialog(wx.Dialog):
    """Settings dialog for storage scope and generation options."""

    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB importer plugin settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(520, 280)),
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
            choices=["Project", "System"],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
            name="general_library_scope",
        )
        self.library_scope_box.SetToolTip(wx.ToolTip(
            "Project — libraries are stored inside the project folder.\n"
            "System — libraries are stored in a shared plugin folder."
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

        # Library name prefix
        prefix_row = wx.BoxSizer(wx.HORIZONTAL)
        prefix_row.Add(wx.StaticText(self, label="Library name prefix:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
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

        # Library path (project scope only)
        # Uses ${KIPRJMOD} so paths survive git checkout on any machine.
        # Examples:
        #   ${KIPRJMOD}/library       — default, folder inside the project
        #   ${KIPRJMOD}/../library    — shared folder one level up (monorepo)
        lib_path_row = wx.BoxSizer(wx.HORIZONTAL)
        self._lib_path_label = wx.StaticText(self, label="Library path:")
        lib_path_row.Add(self._lib_path_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_path_ctrl = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            style=wx.TE_READONLY,
            name="general_lib_path",
        )
        self.lib_path_ctrl.SetToolTip(wx.ToolTip(
            "Path to the library folder. Use ${KIPRJMOD} so the path survives git checkout.\n"
            "Examples:\n"
            "  ${KIPRJMOD}/library         — folder inside this project (default)\n"
            "  ${KIPRJMOD}/../library      — shared folder one level up (monorepo)"
        ))
        self.lib_path_ctrl.Bind(wx.EVT_TEXT, self.update_settings)
        self._lib_path_browse_btn = wx.Button(self, wx.ID_ANY, "Browse\u2026", style=wx.BU_EXACTFIT)
        self._lib_path_browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_lib_path)
        lib_path_row.Add(self.lib_path_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        lib_path_row.Add(self._lib_path_browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        gen_box.Add(lib_path_row, 0, wx.ALL | wx.EXPAND, 5)

        layout.Add(gen_box, 0, wx.ALL | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def load_settings(self):
        general = self.parent.settings.get("general", {})
        self.update_library_scope(general.get("library_scope", "project"))
        self.update_lib_prefix(general.get("lib_prefix", "JLCPCB"))
        self.update_lib_format(general.get("lib_format", "easyeda_pro"))
        self.update_lib_path(self._resolve_lib_path_setting(general))

    def _resolve_lib_path_setting(self, general: dict) -> str:
        return str(general.get("lib_path") or "").strip() or _DEFAULT_LIB_PATH

    def update_settings(self, event):
        """Update and persist a setting that was changed."""
        obj = event.GetEventObject()
        section, name = obj.GetName().split("_", 1)
        if hasattr(obj, "GetValue"):
            value = obj.GetValue()
        elif hasattr(obj, "GetSelection"):
            sel = obj.GetSelection()
            if name == "library_scope":
                value = "project" if sel == 0 else "system"
            elif name == "lib_format":
                value = "easyeda_pro" if sel == 0 else "kicad"
            else:
                value = sel
        else:
            value = None
        getattr(self, f"update_{name}")(value)
        wx.PostEvent(
            self.parent,
            UpdateSetting(section=section, setting=name, value=value),
        )

    def quit_dialog(self, *_):
        self.Destroy()
        self.EndModal(0)

    def update_library_scope(self, scope):
        if isinstance(scope, str):
            idx = 0 if scope.lower() == "project" else 1
        else:
            idx = int(scope) if scope in (0, 1) else 0
        try:
            self.library_scope_box.SetSelection(idx)
        except Exception:
            pass
        # Library path is only meaningful for project scope
        is_project = (idx == 0)
        for ctrl in (self.lib_path_ctrl, self._lib_path_browse_btn, self._lib_path_label):
            try:
                ctrl.Enable(is_project)
            except Exception:
                pass

    def update_lib_prefix(self, value: str):
        try:
            self.lib_prefix_ctrl.ChangeValue(str(value) if value is not None else "")
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

        # Resolve current value to a real path for the dialog start directory
        current = self.lib_path_ctrl.GetValue().strip() or _DEFAULT_LIB_PATH
        resolved_str = current.replace("${KIPRJMOD}", str(project_path))
        resolved = Path(resolved_str)
        start_dir = str(resolved) if resolved.exists() else str(project_path)

        dlg = wx.DirDialog(
            self,
            message="Choose library folder",
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


