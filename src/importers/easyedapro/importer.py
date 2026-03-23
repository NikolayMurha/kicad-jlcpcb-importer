"""EasyEDA to KiCad import orchestrator for LCSC parts using ComponentLoader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Tuple
import wx  # type: ignore
from ...core.events import LogboxAppendEvent

from ...core.helpers import PLUGIN_PATH, sanitize_lib_name
from ...core.lib_paths import resolve_lib_root, resolve_library_base_name, resolve_target_library_name
from ...core.lib_tables import LibTablesManager
from ...core.shared_lib import (
    ensure_project_legacy_models_link,
    ensure_project_table_links,
    ensure_shared_meta,
)
from .component_loader import ComponentLoader, MODELS_DIR


class EasyedaProImporter:
    """Importer that reuses ComponentLoader for EasyEDA Pro (.elibz) output."""

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
        # support both names for clarity
        self.parent_window = parent_window
        self.scope = str(scope).lower()
        self.lib_dir = Path(lib_dir) if lib_dir is not None else (Path(PLUGIN_PATH) / "lib")

    def _compute_outputs(self, category: str) -> Tuple[Path, Path, Path, str, Path]:

        # Resolve generation settings from parent (with defaults)
        try:
            settings = getattr(self.parent_window, "settings", {}) or {}
            general = settings.get("general", {}) or {}
        except Exception:
            general = {}
        target_output_name = resolve_target_library_name(
            general,
            category,
            sanitize=sanitize_lib_name,
            project_path=self.project_path,
        )

        if self.is_system_scope:
            third_party = (
                os.environ.get("KICAD10_3RD_PARTY")
                or os.environ.get("KICAD9_3RD_PARTY")
            )
            if third_party and isinstance(third_party, str) and third_party.strip():
                base_path = Path(third_party)
            else:
                base_path = Path(PLUGIN_PATH) / "libraries"

            plugin_folder = Path(PLUGIN_PATH).resolve().name
            symbols_path = base_path / "symbols" / plugin_folder
            footprints_path = base_path / "footprints" / plugin_folder
            models_3d_path = base_path / "3dmodels" / plugin_folder

            for folder in ("symbols", "footprints", "3dmodels"):
                (base_path / folder / plugin_folder).mkdir(parents=True, exist_ok=True)
            lib_root = base_path / "symbols" / plugin_folder

        else:
            lib_root, _ = resolve_lib_root(general, self.project_path)
            lib_root.mkdir(parents=True, exist_ok=True)
            symbols_path = footprints_path = lib_root
            # KiCad's built-in EasyEDA Pro library reader generates model paths as
            # ${KIPRJMOD}/EASYEDA_MODELS/<name>.step, so we store models there.
            models_3d_path = self.project_path / "EASYEDA_MODELS"

        return symbols_path, footprints_path, models_3d_path, target_output_name, lib_root

    def _component_loader_progress(self, current: int, total: int) -> None:
        """Bridge progress updates into the UI gauge when available."""
        try:
            if self.parent_window is not None:
                gauge = getattr(self.parent_window, "gauge", None)
                if gauge is not None:
                    wx.CallAfter(gauge.SetRange, max(1, int(total)))
                    wx.CallAfter(gauge.SetValue, int(current))
        except Exception:
            # Keep progress best-effort; ignore UI errors
            pass

    def import_part(
        self,
        lcsc_id: str,
        category: str,
        meta: Optional[Dict] = None,
    ) -> Tuple[bool, Path]:
        category = sanitize_lib_name(category or "Misc")
        symbols_path, _footprints_path, models_3d_path, target_name, lib_root = self._compute_outputs(category)
        target_path = symbols_path
        elibz_path = target_path / f"{target_name}.elibz"

        try:
            self.log(f"Launching ComponentLoader for {lcsc_id} into {target_path}\n")
            loader = ComponentLoader(
                kiprjmod=str(self.project_path),
                target_path=str(target_path),
                target_name=target_name,
                progress=self._component_loader_progress,
                models_dir=str(models_3d_path),
            )
            loader.downloadAll([lcsc_id])
            if not self.is_system_scope:
                try:
                    general = (getattr(self.parent_window, "settings", {}) or {}).get("general", {}) or {}
                    if self.is_shared_scope:
                        default_name = resolve_library_base_name(
                            general,
                            project_path=self.project_path,
                        )
                        _shared_root, shared_uri_prefix = resolve_lib_root(general, self.project_path)
                        ensure_shared_meta(lib_root, default_name, log=self.log)
                        LibTablesManager(lib_root, log=self.log).ensure_project_lib_tables(
                            lib_root,
                            use_project_relative=False,
                            uri_prefix=shared_uri_prefix,
                        )
                        ensure_project_table_links(self.project_path, lib_root, log=self.log)
                    else:
                        _, uri_prefix = resolve_lib_root(general, self.project_path)
                        LibTablesManager(self.project_path, log=self.log).ensure_project_lib_tables(
                            lib_root, uri_prefix=uri_prefix
                        )
                    ensure_project_legacy_models_link(
                        self.project_path,
                        models_3d_path,
                        log=self.log,
                    )
                except Exception as exc:
                    self.log(f"Library table update failed: {exc}\n")
            self.log(f"ComponentLoader saved library: {elibz_path}\n")
            return True, elibz_path
        except Exception as exc:
            self.log(f"ComponentLoader import failed: {exc}\n")
            return False, elibz_path

    @property
    def is_system_scope(self) -> bool:
        return self.scope == "system"

    @property
    def is_shared_scope(self) -> bool:
        return self.scope == "shared"

    def log(self, msg: str) -> None:
        try:
            if self.parent_window is not None:
                wx.PostEvent(self.parent_window, LogboxAppendEvent(msg=msg))
        except Exception as e:
            try:
                print(f"UI log dispatch failed: {e}. Message: {msg}")
            except Exception:
                # Last resort: swallow
                ...
