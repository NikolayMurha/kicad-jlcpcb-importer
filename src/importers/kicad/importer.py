"""KiCad format importer using easyeda2kicad."""

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
from ...ui.footprint_editor import FootprintEditor
from ..step_utils import fixup_step_model

from ...core.lcsc_api import LCSC_API
from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
from easyeda2kicad.easyeda.easyeda_importer import (
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from easyeda2kicad.helpers import (
    add_component_in_symbol_lib_file,
    id_already_in_symbol_lib,
    update_component_in_symbol_lib_file,
)
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
from easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad
from easyeda2kicad.kicad.parameters_kicad_symbol import KicadVersion


class KicadImporter:
    """Importer that generates .kicad_sym and .pretty libraries."""

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
        self.kicad_version = KicadVersion.v6

    def import_part(
        self,
        lcsc_id: str,
        category: str,
        meta: Optional[Dict] = None,
    ) -> Tuple[bool, Path]:
        category = sanitize_lib_name(category or "Misc")
        symbols_root, footprints_root, models_root, lib_name, lib_root = self._compute_outputs(category)
        symbol_lib_path = symbols_root / f"{lib_name}.kicad_sym"
        footprint_dir = footprints_root / f"{lib_name}.pretty"
        models_dir = (
            models_root / f"{lib_name}.3dshapes"
            if self.is_system_scope
            else (models_root / "3dmodels")
        )

        try:
            self.log(f"Running easyeda2kicad for {lcsc_id} into {lib_root}\n")

            symbol_lib_path.parent.mkdir(parents=True, exist_ok=True)
            footprint_dir.mkdir(parents=True, exist_ok=True)
            models_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_symbol_lib(symbol_lib_path)

            cad_data = EasyedaApi().get_cad_data_of_component(lcsc_id=lcsc_id)
            if not cad_data:
                self.log(f"Failed to fetch EasyEDA data for {lcsc_id}\n")
                return False, lib_root

            symbol_importer = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data)
            easyeda_symbol = symbol_importer.get_symbol()
            kicad_symbol = ExporterSymbolKicad(
                symbol=easyeda_symbol, kicad_version=self.kicad_version
            ).export(footprint_lib_name=lib_name)

            if id_already_in_symbol_lib(
                lib_path=str(symbol_lib_path),
                component_name=easyeda_symbol.info.name,
                kicad_version=self.kicad_version,
            ):
                update_component_in_symbol_lib_file(
                    lib_path=str(symbol_lib_path),
                    component_name=easyeda_symbol.info.name,
                    component_content=kicad_symbol,
                    kicad_version=self.kicad_version,
                )
            else:
                add_component_in_symbol_lib_file(
                    lib_path=str(symbol_lib_path),
                    component_content=kicad_symbol,
                    kicad_version=self.kicad_version,
                )

            footprint_importer = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data)
            easyeda_footprint = footprint_importer.get_footprint()
            footprint_filename = f"{easyeda_footprint.info.name}.kicad_mod"
            footprint_full_path = footprint_dir / footprint_filename

            model_3d_path = self._model_3d_path(models_dir)
            ExporterFootprintKicad(footprint=easyeda_footprint).export(
                footprint_full_path=str(footprint_full_path),
                model_3d_path=model_3d_path,
            )
            tx, ty, tz = self._fetch_model_offset(lcsc_id)
            FootprintEditor.set_3d_model_offset(footprint_full_path, tx, ty, tz)

            try:
                model = Easyeda3dModelImporter(
                    easyeda_cp_cad_data=cad_data, download_raw_3d_model=True
                ).output
                exporter = Exporter3dModelKicad(model_3d=model)
                if self.is_system_scope:
                    exporter.export(lib_path=str(models_root / lib_name))
                else:
                    if exporter.output:
                        (models_dir / f"{exporter.output.name}.wrl").write_text(
                            exporter.output.raw_wrl, encoding="utf-8"
                        )
                    if exporter.output_step and exporter.output:
                        step_path = models_dir / f"{exporter.output.name}.step"
                        step_path.write_bytes(exporter.output_step)
                        try:
                            fixup_step_model(step_path)
                        except Exception as exc:
                            self.log(f"3D model fixup skipped: {exc}\n")
                    elif exporter.output_step and model:
                        step_path = models_dir / f"{model.name}.step"
                        step_path.write_bytes(exporter.output_step)
                        try:
                            fixup_step_model(step_path)
                        except Exception as exc:
                            self.log(f"3D model fixup skipped: {exc}\n")
                        try:
                            content = footprint_full_path.read_text(encoding="utf-8", errors="replace")
                            content = content.replace(".wrl\"", ".step\"")
                            footprint_full_path.write_text(content, encoding="utf-8")
                        except Exception:
                            pass
            except Exception as exc:
                self.log(f"3D model download skipped: {exc}\n")

            if self.is_system_scope:
                editor = FootprintEditor(self.project_path, log=self.log)
                footprints_base = footprints_root / lib_name
                models_base = models_root / lib_name
                editor.rewrite_system_3d_model_paths(footprints_base, models_base)
            else:
                try:
                    FootprintEditor(self.project_path, log=self.log).relativize_3d_model_paths(
                        footprint_dir
                    )
                except Exception:
                    pass
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
                        models_root / "3dmodels",
                        log=self.log,
                    )
                except Exception as exc:
                    self.log(f"Library table update failed: {exc}\n")

            self.log(f"Generated KiCad libraries under: {lib_root}\n")
            return True, lib_root
        except Exception as exc:
            self.log(f"KiCad format import failed: {exc}\n")
            return False, lib_root

    @property
    def is_system_scope(self) -> bool:
        return self.scope == "system"

    @property
    def is_shared_scope(self) -> bool:
        return self.scope == "shared"

    def _compute_outputs(self, category: str) -> Tuple[Path, Path, Path, str, Path]:
        try:
            settings = getattr(self.parent_window, "settings", {}) or {}
            general = settings.get("general", {}) or {}
        except Exception:
            general = {}

        lib_name = resolve_target_library_name(
            general,
            category,
            sanitize=sanitize_lib_name,
            project_path=self.project_path,
        )

        if self.is_system_scope:
            third_party = os.environ.get("KICAD9_3RD_PARTY")
            if third_party and isinstance(third_party, str) and third_party.strip():
                base_path = Path(third_party)
            else:
                base_path = Path(PLUGIN_PATH) / "libraries"

            plugin_folder = Path(PLUGIN_PATH).resolve().name
            symbols_root = base_path / "symbols" / plugin_folder
            footprints_root = base_path / "footprints" / plugin_folder
            models_root = base_path / "3dmodels" / plugin_folder
            for folder in (symbols_root, footprints_root, models_root):
                folder.mkdir(parents=True, exist_ok=True)
            lib_root = symbols_root
        else:
            lib_root, _uri_prefix = resolve_lib_root(general, self.project_path)
            lib_root.mkdir(parents=True, exist_ok=True)
            symbols_root = footprints_root = models_root = lib_root

        return symbols_root, footprints_root, models_root, lib_name, lib_root

    def _model_3d_path(self, models_dir: Path) -> str:
        if self.is_system_scope:
            return models_dir.resolve().as_posix()
        try:
            rel = os.path.relpath(models_dir.resolve(), self.project_path.resolve())
            return "${KIPRJMOD}/" + Path(rel).as_posix()
        except Exception:
            return "${KIPRJMOD}/3dmodels"

    def _ensure_symbol_lib(self, path: Path) -> None:
        if path.exists():
            return
        header = (
            "(kicad_symbol_lib\n"
            "  (version 20211014)\n"
            "  (generator https://github.com/uPesy/easyeda2kicad.py)\n"
            ")\n"
        )
        path.write_text(header, encoding="utf-8")

    def _fetch_model_offset(self, lcsc_id: str) -> tuple[float, float, float]:
        """Fetch 3D model translation from EasyEDA Pro device API.

        Returns (x, y, z) in mm — indices [6,7,8] of the '3D Model Transform'
        attribute (comma-separated mils). Falls back to (0, 0, 0) on any error.
        """
        try:
            api = LCSC_API()
            result = api.easyeda_search_by_codes([lcsc_id])
            if not result.get("success") or not result.get("result"):
                return (0.0, 0.0, 0.0)
            uuid = result["result"][0]["uuid"]
            device = api.easyeda_get_device(uuid)
            transform_str = (
                device.get("result", {})
                .get("attributes", {})
                .get("3D Model Transform", "")
            )
            if not transform_str:
                return (0.0, 0.0, 0.0)
            parts = [float(v.strip()) for v in transform_str.split(",") if v.strip()]
            if len(parts) < 9:
                return (0.0, 0.0, 0.0)
            # mils → mm
            return (parts[6] * 0.0254, parts[7] * 0.0254, parts[8] * 0.0254)
        except Exception as exc:
            self.log(f"3D model offset fetch skipped: {exc}\n")
            return (0.0, 0.0, 0.0)

    def log(self, msg: str) -> None:
        try:
            if self.parent_window is not None:
                wx.PostEvent(self.parent_window, LogboxAppendEvent(msg=msg))
        except Exception as e:
            try:
                print(f"UI log dispatch failed: {e}. Message: {msg}")
            except Exception:
                ...
