"""EasyEDA to KiCad import orchestrator for LCSC parts using ComponentLoader."""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Dict, Optional, Tuple
import wx  # type: ignore
from ...core.events import LogboxAppendEvent

from ...core.helpers import PLUGIN_PATH
from ...core.lib_tables import LibTablesManager
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

    def resolve_nickname_prefix(self) -> str:
        """Resolve KiCad 3rd-party library nickname prefix."""
        # 1) KiCad settings (best effort)
        try:
            try:
                import pcbnew as _kicad_pcbnew  # type: ignore
            except Exception as e:
                self.log(f"resolve_nickname_prefix: pcbnew import failed: {e}\n")
                _kicad_pcbnew = None
            base = None
            if _kicad_pcbnew is not None:
                try:
                    base = _kicad_pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
                except Exception as e:
                    self.log(f"resolve_nickname_prefix: cannot get user settings path: {e}\n")
                    base = None
            if base:
                settings_path = Path(base) / "kicad.json"
                if settings_path.exists():
                    try:
                        data = json.loads(settings_path.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            pcm = data.get("pcm", {}) or {}
                            prefix = pcm.get("lib_prefix")
                            if isinstance(prefix, str) and prefix.strip():
                                return prefix.strip()
                    except Exception as e:
                        self.log(f"resolve_nickname_prefix: failed to parse kicad.json: {e}\n")
        except Exception as e:
            self.log(f"resolve_nickname_prefix: unexpected error: {e}\n")

        return "PCM_"

    @staticmethod
    def _safe_remove(path: Path) -> int:
        try:
            if path.exists() and path.is_dir():
                import shutil
                shutil.rmtree(path)
                return 1
            if path.exists() and path.is_file():
                path.unlink()
                return 1
        except Exception as e:
            try:
                print(f"safe_remove failed for {path}: {e}")
            except Exception:
                ...
        return 0

    def _resolve_lib_root(self, general: dict) -> tuple[Path, str]:
        """Return (filesystem_path, uri_prefix) for the library root.

        The setting is stored as a ``${KIPRJMOD}``-relative path so it
        survives git checkout on any machine:

            ${KIPRJMOD}/library       → <project>/library/
            ${KIPRJMOD}/../library    → <repo>/library/    (monorepo shared)

        Returns:
            fs_path   — absolute filesystem path (${KIPRJMOD} substituted)
            uri_prefix — the raw setting value, kept as-is for lib table URIs
        """
        raw = str(general.get("lib_path") or "").strip() or "${KIPRJMOD}/library"

        uri_prefix = raw.rstrip("/")

        fs_str = raw.replace("${KIPRJMOD}", str(self.project_path))
        p = Path(fs_str)
        if not p.is_absolute():
            p = (self.project_path / p).resolve()
        else:
            p = p.resolve()
        return p, uri_prefix

    def _compute_outputs(self, category: str) -> Tuple[Path, Path, Path, str, Path]:

        # Resolve generation settings from parent (with defaults)
        try:
            settings = getattr(self.parent_window, "settings", {}) or {}
            general = settings.get("general", {}) or {}
        except Exception:
            general = {}
        lib_prefix = str(general.get("lib_prefix", "JLCPCB")).strip()
        lib_prefix = lib_prefix.rstrip("_") or "JLCPCB"

        single_name = str(general.get("single_library_name", "")).strip()
        use_single = bool(general.get("single_library", True))
        if use_single:
            if not single_name:
                single_name = lib_prefix
            target_output_name = self._sanitize(single_name)
        else:
            target_output_name = f"{lib_prefix}_{category}"

        if self.is_system_scope:
            third_party = os.environ.get("KICAD9_3RD_PARTY")
            if third_party and isinstance(third_party, str) and third_party.strip():
                base_path = Path(third_party)
            else:
                base_path = Path(PLUGIN_PATH) / "libraries"

            plugin_folder = Path(PLUGIN_PATH).resolve().name
            symbols_path = base_path / "symbols" / plugin_folder / target_output_name
            footprints_path = base_path / "footprints" / plugin_folder / target_output_name
            models_3d_path = base_path / "3dmodels" / plugin_folder / target_output_name

            for folder in ("symbols", "footprints", "3dmodels"):
                (base_path / folder / plugin_folder).mkdir(parents=True, exist_ok=True)
            lib_root = base_path / "symbols" / plugin_folder

        else:
            lib_root, uri_prefix = self._resolve_lib_root(general)
            lib_root.mkdir(parents=True, exist_ok=True)
            if use_single:
                symbols_path = footprints_path = lib_root
            else:
                symbols_path = footprints_path = lib_root / target_output_name
            models_3d_path = lib_root / MODELS_DIR

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
        category = self._sanitize(category or "Misc")
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
                    _, uri_prefix = self._resolve_lib_root(
                        (getattr(self.parent_window, "settings", {}) or {}).get("general", {}) or {}
                    )
                    LibTablesManager(self.project_path, log=self.log).ensure_project_lib_tables(
                        lib_root, uri_prefix=uri_prefix
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
    
    @staticmethod
    def _sanitize(name: str) -> str:
        try:
            import re
            cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
            return cleaned or "Misc"
        except Exception:
            return "Misc"
        
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
