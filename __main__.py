"""Entry point for running the plugin in standalone mode."""

from __future__ import annotations

from pathlib import Path
import sys
import wx

if __package__:
    from .src.core import standalone_impl
    from .src.ui.mainwindow import AssignLCSCMainDialog
else:
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    from src.core import standalone_impl
    from src.ui.mainwindow import AssignLCSCMainDialog

if __name__ == "__main__":
    print("starting jlcpcbtools standalone mode...")  # noqa: T201

    # See README.md for how to use this

    app = wx.App(None)

    dialog = AssignLCSCMainDialog(kicad_provider=standalone_impl.KicadStub())
    dialog.Center()
    dialog.Show()

    app.MainLoop()
