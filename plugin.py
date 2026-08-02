"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin  # pylint: disable=import-error

# Import lazily in Run to avoid registration failures if deps are missing


class JLCPCBPlugin(ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self):
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        self.name = "JLCPCB Importer"
        self.category = "LCSC Library"
        self.description = "Assign LCSC numbers, search library, update database"
        self.show_toolbar_button = True
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "images", "jlcpcb_32x32.png")
        self._pcbnew_frame = None
        self._dialog = None

    def _on_dialog_destroyed(self, event):
        """Drop the retained wx wrapper after the plugin window is destroyed."""

        try:
            if event.GetEventObject() is self._dialog:
                self._dialog = None
        finally:
            event.Skip()

    def Run(self):
        """Overwrite Run."""
        import wx  # pylint: disable=import-error,import-outside-toplevel

        from .src.ui.mainwindow import AssignLCSCMainDialog  # local import to avoid import-time errors

        current = self._dialog
        if current is not None:
            try:
                if current.IsShown():
                    current.Raise()
                    current.SetFocus()
                    return
            except RuntimeError:
                self._dialog = None

        dialog = AssignLCSCMainDialog()
        self._dialog = dialog
        dialog.Bind(wx.EVT_WINDOW_DESTROY, self._on_dialog_destroyed)
        dialog.Center()
        dialog.Show()
