"""KiCad format importer using kicad-cli for symbol/footprint conversion."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple
import wx  # type: ignore
from ...core.elibz_native import (
    convert_elibz_with_kicad_cli,
    device_display_name,
    load_elibz_payload,
    pick_device,
    pick_symbol_name_from_converted,
    rename_symbol_block,
    resolve_footprint_mod_path,
)
from ...core.sym_lib_reader import (
    add_or_replace_symbol,
    extract_symbol_block,
    get_footprint_property,
    patch_footprint_property,
)
from ...core.events import LogboxAppendEvent
from ...core.helpers import PLUGIN_PATH, sanitize_lib_name, strip_lcsc_suffix
from ...core.lib_paths import resolve_lib_root, resolve_library_base_name, resolve_target_library_name
from ...core.lib_tables import LibTablesManager
from ...core.shared_lib import (
    ensure_project_legacy_models_link,
    ensure_project_table_links,
    ensure_shared_meta,
)
from ...ui.footprint_editor import FootprintEditor
from ...ui.symbol_editor import SymbolEditor
from ..easyedapro.importer import EasyedaProImporter
from ...core.lcsc_api import LCSC_API
from ..step_utils import fixup_step_model


class KicadImporter:
    """Importer that generates .kicad_sym and .pretty libraries."""

    def __init__(
        self,
        project_path: Path | str,
        python_exe: str,
        parent_window: Optional[wx.Window] = None,
        scope: str = "project",
        lib_dir: Optional[Path | str] = None,
        backend: str = "direct",  # kept for API compatibility, ignored
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
        category = sanitize_lib_name(category or "Misc")
        return self._import_part_via_elibz(lcsc_id, category, meta)

    @property
    def is_system_scope(self) -> bool:
        return self.scope == "system"

    @property
    def is_shared_scope(self) -> bool:
        return self.scope == "shared"

    def _general_settings(self) -> Dict:
        try:
            return (getattr(self.parent_window, "settings", {}) or {}).get("general", {}) or {}
        except Exception:
            return {}

    def _postprocess_non_system_import(
        self,
        footprint_dir: Path,
        lib_root: Path,
        models_root: Path,
        general: Dict,
    ) -> None:
        try:
            FootprintEditor(self.project_path, log=self.log).relativize_3d_model_paths(footprint_dir)
        except Exception:
            pass
        try:
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

    def _copy_elibz_step_model(self, model_title: str, elibz_path: Path, models_dir: Path) -> bool:
        if not model_title:
            return False
        candidates = [
            elibz_path.parent / "3dmodels" / f"{model_title}.step",
            elibz_path.parent / "EASYEDA_MODELS" / f"{model_title}.step",
            self.project_path / "3dmodels" / f"{model_title}.step",
            self.project_path / "EASYEDA_MODELS" / f"{model_title}.step",
        ]
        src_step = next((p for p in candidates if p.exists()), None)
        if src_step is None:
            return False
        models_dir.mkdir(parents=True, exist_ok=True)
        dest_step = models_dir / src_step.name
        if not dest_step.exists():
            dest_step.write_bytes(src_step.read_bytes())
        return True

    def _import_part_via_elibz(
        self,
        lcsc_id: str,
        category: str,
        meta: Optional[Dict] = None,
    ) -> Tuple[bool, Path]:
        general = self._general_settings()
        symbols_root, footprints_root, models_root, lib_name, lib_root = self._compute_outputs(category)
        symbol_lib_path = symbols_root / f"{lib_name}.kicad_sym"
        footprint_dir = footprints_root / f"{lib_name}.pretty"
        models_dir = (
            models_root / f"{lib_name}.3dshapes"
            if self.is_system_scope
            else (models_root / "3dmodels")
        )
        try:
            self.log(f"Running KiCad import via ELIBZ backend for {lcsc_id}\n")
            symbol_lib_path.parent.mkdir(parents=True, exist_ok=True)
            footprint_dir.mkdir(parents=True, exist_ok=True)
            models_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_symbol_lib(symbol_lib_path)

            elibz_importer = EasyedaProImporter(
                project_path=self.project_path,
                python_exe=self.python_exe,
                parent_window=self.parent_window,
                scope=self.scope,
                lib_dir=self.lib_dir,
            )
            ok, elibz_path = elibz_importer.import_part(
                lcsc_id=lcsc_id,
                category=category,
                meta=meta or {},
            )
            if not ok:
                return False, lib_root

            payload = load_elibz_payload(elibz_path)
            device_entry = pick_device(payload, lcsc_id=lcsc_id)
            if device_entry is None:
                raise ValueError("No matching device in ELIBZ")

            attrs = device_entry.get("attributes", {}) or {}
            model_title = str(attrs.get("3D Model Title") or "").strip()

            # kicad-cli sym/fp upgrade both support .elibz natively (KiCad 9+).
            # This gives better quality than the v4 API pipeline and already
            # produces correct (0 0 0) model offsets.
            with tempfile.TemporaryDirectory(prefix="kicad_elibz_") as tmp:
                tmp_dir = Path(tmp)
                converted_sym = tmp_dir / "converted.kicad_sym"
                converted_pretty = tmp_dir / "converted.pretty"
                convert_elibz_with_kicad_cli(elibz_path, converted_sym, converted_pretty)

                source_symbol_name = pick_symbol_name_from_converted(converted_sym, device_entry)
                if not source_symbol_name:
                    raise ValueError("Unable to locate symbol in converted ELIBZ")
                symbol_block = extract_symbol_block(converted_sym, source_symbol_name)
                if not symbol_block:
                    raise ValueError(f"Unable to extract symbol block '{source_symbol_name}'")

                symbol_name = device_display_name(device_entry) or strip_lcsc_suffix(source_symbol_name)
                symbol_name = symbol_name or source_symbol_name
                symbol_block = rename_symbol_block(symbol_block, source_symbol_name, symbol_name)
                symbol_block = patch_footprint_property(symbol_block, lib_name)
                add_or_replace_symbol(symbol_lib_path, symbol_name, symbol_block)

                # Select footprint: prefer the one linked in the symbol, then
                # fall back to device attribute candidates, then first available.
                fp_name_candidates: list[str] = []
                fp_ref = get_footprint_property(symbol_block) or ""
                if ":" in fp_ref:
                    fp_name_candidates.append(fp_ref.split(":", 1)[1])
                for candidate in (
                    attrs.get("Footprint"),
                    (device_entry.get("footprint", {}) or {}).get("display_title"),
                    (device_entry.get("footprint", {}) or {}).get("title"),
                ):
                    text = str(candidate or "").strip()
                    if text and text not in fp_name_candidates:
                        fp_name_candidates.append(text)

                footprint_path: Optional[Path] = None
                for fp_name in fp_name_candidates:
                    src_mod = resolve_footprint_mod_path(converted_pretty, fp_name)
                    if src_mod is None:
                        continue
                    footprint_path = footprint_dir / src_mod.name
                    shutil.copy2(src_mod, footprint_path)
                    break
                if footprint_path is None:
                    mods = list(converted_pretty.glob("*.kicad_mod"))
                    if mods:
                        footprint_path = footprint_dir / mods[0].name
                        shutil.copy2(mods[0], footprint_path)
                    else:
                        self.log("Warning: no footprint found in converted ELIBZ output.\n")

                # kicad-cli writes ${KIPRJMOD}/EASYEDA_MODELS/<model>.step;
                # rewrite to our configured models directory.
                if footprint_path and footprint_path.exists():
                    model_3d_path = self._model_3d_path(models_dir)
                    try:
                        content = footprint_path.read_text(encoding="utf-8", errors="replace")
                        content = re.sub(
                            r'\$\{KIPRJMOD\}/EASYEDA_MODELS/',
                            model_3d_path.rstrip("/") + "/",
                            content,
                        )
                        footprint_path.write_text(content, encoding="utf-8")
                    except Exception:
                        pass

            self._apply_symbol_metadata(
                symbol_lib_path=symbol_lib_path,
                symbol_name=symbol_name,
                category=category,
                meta=meta or {},
            )

            if not self.is_system_scope:
                step_copied = self._copy_elibz_step_model(model_title, elibz_path, models_dir)
                if not step_copied:
                    # ComponentLoader sometimes skips a model download; fall back to a
                    # direct LCSC API download using the model UUID from device.json.
                    self._download_3d_model_fallback(attrs, model_title, models_dir)

            if self.is_system_scope:
                editor = FootprintEditor(self.project_path, log=self.log)
                footprints_base = footprints_root / lib_name
                models_base = models_root / lib_name
                editor.rewrite_system_3d_model_paths(footprints_base, models_base)
            else:
                self._postprocess_non_system_import(
                    footprint_dir=footprint_dir,
                    lib_root=lib_root,
                    models_root=models_root,
                    general=general,
                )

            self.log(f"Generated KiCad libraries under: {lib_root}\n")
            return True, lib_root
        except Exception as exc:
            self.log(f"KiCad via ELIBZ import failed: {exc}\n")
            return False, lib_root

    def _download_3d_model_fallback(self, attrs: Dict, model_title: str, models_dir: Path) -> None:
        """Download 3D STEP model directly via LCSC API when ComponentLoader skips it."""
        model_attr = str(attrs.get("3D Model") or "").strip()
        if not model_attr or not model_title:
            return
        try:
            api = LCSC_API()
            direct_uuid = api.get_3d_model_direct_uuid(model_attr)
            if not direct_uuid:
                self.log("3D model fallback: could not resolve model UUID\n")
                return
            step_path = models_dir / f"{model_title}.step"
            if step_path.exists():
                return
            models_dir.mkdir(parents=True, exist_ok=True)
            api.download_easyeda_step_model(direct_uuid, step_path)
            self.log(f"3D model downloaded via fallback: {step_path.name}\n")
            try:
                fixup_step_model(step_path)
            except Exception as exc:
                self.log(f"3D model fixup skipped: {exc}\n")
        except Exception as exc:
            self.log(f"3D model fallback download skipped: {exc}\n")

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
            third_party = (
                os.environ.get("KICAD10_3RD_PARTY")
                or os.environ.get("KICAD9_3RD_PARTY")
            )
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

    @staticmethod
    def _parse_attributes_map(raw_json: str) -> Dict[str, str]:
        text = str(raw_json or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except Exception:
            return {}

        out: Dict[str, str] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                key = str(k or "").strip()
                val = str(v or "").strip()
                if key and val:
                    out[key] = val
            return out

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    key = (
                        str(item.get("attribute_name") or "").strip()
                        or str(item.get("name") or "").strip()
                        or str(item.get("key") or "").strip()
                    )
                    val = (
                        str(item.get("attribute_value") or "").strip()
                        or str(item.get("value") or "").strip()
                        or str(item.get("val") or "").strip()
                    )
                    if key and val:
                        out[key] = val
        return out

    def _apply_symbol_metadata(
        self,
        symbol_lib_path: Path,
        symbol_name: str,
        category: str,
        meta: Dict,
    ) -> None:
        try:
            attrs = self._parse_attributes_map(str(meta.get("attributes_json") or ""))
            mfr_part = str(meta.get("mfr_part") or "").strip()
            manufacturer = str(meta.get("manufacturer") or "").strip()
            description = str(meta.get("description") or "").strip()
            if manufacturer:
                attrs.setdefault("Manufacturer", manufacturer)
            if description:
                # Keep user-facing LCSC text and also populate KiCad-native description
                # when it is missing.
                attrs.setdefault("LCSC Description", description)
                attrs.setdefault("ki_description", description)
                attrs.setdefault("Part Description", description)

            editor = SymbolEditor(
                sym_path=symbol_lib_path,
                symbol_id=symbol_name,
                parent_window=self.parent_window,
            )
            changed = editor.update_value_from_attributes(
                category=category,
                attrs=attrs,
                mfr_part=mfr_part,
            )
            if changed:
                editor.save(strip_ids=True)
        except Exception as exc:
            self.log(f"Symbol metadata update skipped: {exc}\n")

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

    def log(self, msg: str) -> None:
        try:
            if self.parent_window is not None:
                wx.PostEvent(self.parent_window, LogboxAppendEvent(msg=msg))
        except Exception as e:
            try:
                print(f"UI log dispatch failed: {e}. Message: {msg}")
            except Exception:
                ...
