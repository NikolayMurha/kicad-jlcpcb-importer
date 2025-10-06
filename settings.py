"""Contains the settings dialog for the LCSC plugin."""

import logging

import wx  # pylint: disable=import-error

from .events import UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled


class SettingsDialog(wx.Dialog):
    """Minimal settings dialog: only storage scope option."""

    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="LCSC plugin settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(520, 220)),
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

        # Layout (only storage scope option visible)
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
        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    def load_settings(self):
        # Default to project scope if not set
        self.update_library_scope(
            self.parent.settings.get("general", {}).get("library_scope", "project")
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
