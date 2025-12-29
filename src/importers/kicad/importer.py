"""KiCad format importer (placeholder)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple
import wx  # type: ignore
from ...core.events import LogboxAppendEvent


class KicadImporter:
    """Stub implementation for KiCad format import."""

    def __init__(
        self,
        project_path: Path | str,
        python_exe: str,
        parent_window: Optional[wx.Window] = None,
        scope: str = "project",
        lib_dir: Optional[Path | str] = None,
    ) -> None:
        self.project_path = Path(project_path)
        self.python_exe = python_exe
        self.parent_window = parent_window
        self.scope = str(scope).lower()
        self.lib_dir = Path(lib_dir) if lib_dir is not None else None

    def import_part(
        self,
        lcsc_id: str,
        category: str,
        meta: Optional[Dict] = None,
    ) -> Tuple[bool, Path]:
        self.log("KiCad format import is not implemented yet.\n")
        return False, self.project_path

    @property
    def is_system_scope(self) -> bool:
        return self.scope == "system"

    def log(self, msg: str) -> None:
        try:
            if self.parent_window is not None:
                wx.PostEvent(self.parent_window, LogboxAppendEvent(msg=msg))
        except Exception as e:
            try:
                print(f"UI log dispatch failed: {e}. Message: {msg}")
            except Exception:
                ...
