"""Import orchestrator that selects the configured library format."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple
import wx  # type: ignore

from .easyedapro.importer import EasyedaProImporter
from .kicad.importer import KicadImporter


class EasyedaImporter:
    """Importer that delegates to EasyEDA Pro or KiCad format implementations."""

    def __init__(
        self,
        project_path: Path | str,
        python_exe: str,
        parent_window: Optional[wx.Window] = None,
        scope: str = "project",
    ) -> None:
        self.project_path = Path(project_path)
        self.python_exe = python_exe
        self.parent_window = parent_window
        self.scope = str(scope).lower()
        self._impl = self._select_impl()

    def _select_impl(self):
        fmt = "kicad"
        try:
            settings = getattr(self.parent_window, "settings", {}) or {}
            general = settings.get("general", {}) or {}
            fmt = str(general.get("lib_format", "kicad")).strip().lower()
        except Exception:
            fmt = "kicad"

        if fmt == "kicad":
            return KicadImporter(
                project_path=self.project_path,
                python_exe=self.python_exe,
                parent_window=self.parent_window,
                scope=self.scope,
            )
        return EasyedaProImporter(
            project_path=self.project_path,
            python_exe=self.python_exe,
            parent_window=self.parent_window,
            scope=self.scope,
        )

    def import_part(self, lcsc_id: str) -> Tuple[bool, Path]:
        return self._impl.import_part(lcsc_id=lcsc_id)

    def __getattr__(self, name: str):
        return getattr(self._impl, name)
