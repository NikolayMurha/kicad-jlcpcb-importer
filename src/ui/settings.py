"""Contains the settings dialog for the LCSC plugin."""

import logging

import wx  # pylint: disable=import-error

from ..core.events import UpdateSetting
from ..core.helpers import HighResWxSize, loadBitmapScaled


class SettingsDialog(wx.Dialog):
    """Settings dialog for storage scope and generation options."""

    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB importer plugin settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(520, 260)),
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

        # Layout (storage scope + generation options)
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
        self.library_scope_box.SetToolTip(
            wx.ToolTip(
                "Choose whether generated libraries are stored inside the current project (project) or in a shared plugin folder (system)."
            )
        )
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

        # Library format selection
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

        # Library prefix text field (used as base name prefix, e.g. "JLCPCB")
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

        # Project directory name where libraries are placed when scope=project
        projdir_row = wx.BoxSizer(wx.HORIZONTAL)
        projdir_row.Add(wx.StaticText(self, label="Project library folder:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.project_lib_dir_ctrl = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.parent.window, wx.Size(200, -1)),
            name="general_project_lib_dir",
        )
        self.project_lib_dir_ctrl.SetToolTip(wx.ToolTip("Folder name under the project to store generated libs (default: library)."))
        self.project_lib_dir_ctrl.Bind(wx.EVT_TEXT, self.update_settings)
        projdir_row.Add(self.project_lib_dir_ctrl, 1, wx.EXPAND)
        gen_box.Add(projdir_row, 0, wx.ALL | wx.EXPAND, 5)

        layout.Add(gen_box, 0, wx.ALL | wx.EXPAND, 5)
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def load_settings(self):
        # Default to project scope if not set
        self.update_library_scope(
            self.parent.settings.get("general", {}).get("library_scope", "project")
        )
        self.update_lib_prefix(
            self.parent.settings.get("general", {}).get("lib_prefix", "JLCPCB")
        )
        self.update_project_lib_dir(
            self.parent.settings.get("general", {}).get("project_lib_dir", "library")
        )
        self.update_lib_format(
            self.parent.settings.get("general", {}).get("lib_format", "easyeda_pro")
        )

    def update_settings(self, event):
        """Update and persist a setting that was changed."""
        obj = event.GetEventObject()
        section, name = obj.GetName().split("_", 1)
        # Support controls that use GetValue (CheckBox) and GetSelection (RadioBox/Choice)
        if hasattr(obj, "GetValue"):
            value = obj.GetValue()
        elif hasattr(obj, "GetSelection"):
            sel = obj.GetSelection()
            # Map radio to string for library_scope
            if name == "library_scope":
                value = "project" if sel == 0 else "system"
            elif name == "lib_format":
                value = "easyeda_pro" if sel == 0 else "kicad"
            else:
                value = sel
        else:
            value = None
        # Reflect new state in UI
        getattr(self, f"update_{name}")(value)

        wx.PostEvent(
            self.parent,
            UpdateSetting(
                section=section,
                setting=name,
                value=value,
            ),
        )

    def quit_dialog(self, *_):
        """Close this dialog."""
        self.Destroy()
        self.EndModal(0)

    # ----- updater for the only option -----
    def update_library_scope(self, scope):
        # Accept "project"/"system" or int index
        if isinstance(scope, str):
            idx = 0 if scope.lower() == "project" else 1
        else:
            idx = int(scope) if scope in (0, 1) else 0
        try:
            self.library_scope_box.SetSelection(idx)
        except Exception:
            pass

    def update_lib_prefix(self, value: str):
        try:
            self.lib_prefix_ctrl.ChangeValue(str(value) if value is not None else "")
        except Exception:
            pass

    def update_project_lib_dir(self, value: str):
        try:
            self.project_lib_dir_ctrl.ChangeValue(str(value) if value is not None else "")
        except Exception:
            pass

    def update_lib_format(self, value: str):
        if isinstance(value, str):
            key = value.strip().lower()
            if key in ("easyeda_pro", "easyeda", "easyeda pro"):
                idx = 0
            elif key == "kicad":
                idx = 1
            else:
                idx = 0
        else:
            idx = int(value) if value in (0, 1) else 0
        try:
            self.lib_format_ctrl.SetSelection(idx)
        except Exception:
            pass
