"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin  # pylint: disable=import-error

# Import lazily in Run to avoid registration failures if deps are missing


class JLCPCBPlugin(ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self):
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        self.name = "Assign LCSC Number"
        self.category = "LCSC Library"
        self.description = "Assign LCSC numbers, search library, update database"
        self.show_toolbar_button = True
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self._pcbnew_frame = None

    def Run(self):
        """Overwrite Run."""
        from .mainwindow import AssignLCSCMainDialog  # local import to avoid import-time errors
        dialog = AssignLCSCMainDialog()
        dialog.Center()
        dialog.Show()
