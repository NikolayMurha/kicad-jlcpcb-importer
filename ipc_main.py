"""KiCad IPC API action entrypoint."""

from __future__ import annotations

import sys
import traceback

from src.core.kicad_ipc import PLUGIN_IDENTIFIER
from src.core.single_instance import SingleInstanceCoordinator


def main() -> int:
    """Connect to KiCad through IPC and run the importer as an external process."""

    coordinator = SingleInstanceCoordinator.for_kicad_environment(PLUGIN_IDENTIFIER)
    try:
        if not coordinator.acquire():
            return 0

        import wx

        from src.core.application_presentation import (
            activate_application_window,
            configure_application,
            configure_application_window,
        )
        from src.core.kicad_ipc import KicadIpcProvider
        from src.core.kicad_lifecycle import KicadLifecycleMonitor
        from src.core.plugin_paths import PLUGIN_PATH
        from src.ui.mainwindow import AssignLCSCMainDialog

        provider = KicadIpcProvider.connect()
        provider.prepare_environment()

        _app = wx.App(False)
        app_icon = PLUGIN_PATH / "images" / "jlcpcb_app_256.png"
        configure_application(
            _app,
            app_icon,
        )
        dialog = AssignLCSCMainDialog(kicad_provider=provider)
        configure_application_window(dialog, app_icon)

        def _close_for_kicad_exit() -> None:
            if not dialog or dialog.IsBeingDeleted():
                return
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)
            else:
                dialog.Close(force=True)

        lifecycle = KicadLifecycleMonitor(
            lambda: wx.CallAfter(_close_for_kicad_exit)
        )

        def _activate_dialog() -> None:
            if not dialog or dialog.IsBeingDeleted():
                return
            activate_application_window(dialog)

        coordinator.set_activation_callback(lambda: wx.CallAfter(_activate_dialog))
        try:
            lifecycle.start()
            dialog.Center()
            wx.CallAfter(_activate_dialog)
            dialog.ShowModal()
        finally:
            lifecycle.close()
            dialog.Destroy()
        return 0
    except Exception as exc:
        traceback.print_exc()
        try:
            import wx

            app = wx.GetApp() or wx.App(False)
            wx.MessageBox(
                f"Unable to start JLCPCB Importer through KiCad IPC:\n\n{exc}",
                "JLCPCB Importer",
                wx.OK | wx.ICON_ERROR,
            )
            del app
        except Exception:
            pass
        return 1
    finally:
        coordinator.close()


if __name__ == "__main__":
    sys.exit(main())
