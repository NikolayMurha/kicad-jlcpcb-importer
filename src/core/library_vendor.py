"""Business logic for vendoring symbols/footprints into local KiCad libraries."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .elibz_native import (
    convert_elibz_with_kicad_cli,
    find_device_by_name,
    load_elibz_payload,
    pick_symbol_name_from_converted,
    rename_symbol_block,
    resolve_footprint_mod_path as resolve_elibz_footprint_mod_path,
)
from .helpers import sanitize_lib_name, strip_lcsc_suffix
from .lib_paths import (
    resolve_lib_root,
    resolve_library_base_name,
    resolve_target_library_name,
)
from .lib_tables import LibTablesManager
from .shared_lib import (
    ensure_project_legacy_models_link,
    ensure_project_table_links,
    ensure_shared_meta,
)
from .sym_lib_reader import (
    add_or_replace_symbol,
    copy_footprint_to_pretty,
    extract_parent_symbol_chain,
    extract_symbol_block,
    get_footprint_property,
    patch_footprint_property,
)
from ..ui.footprint_editor import FootprintEditor

_BUILTIN_3D_PREFIXES = ("${KICAD", "$ENV{KICAD", "$(KICAD")


class LibraryVendorService:
    """Use-case service for importing symbols and footprints from source libraries."""

    def __init__(
        self,
        project_path: Path | str,
        general_settings: Optional[Dict] = None,
        fp_libs: Optional[Sequence[Tuple[str, Path]]] = None,
    ) -> None:
        self.project_path = Path(project_path)
        self.general = general_settings or {}
        self._fp_libs: List[Tuple[str, Path]] = list(fp_libs or [])

    def import_symbol(self, symbol_name: str, src_path: Path, category_hint: str) -> Tuple[bool, str]:
        dest = self._get_dest_lib_path(category_hint)
        try:
            if src_path.suffix.lower() == ".elibz":
                return self._import_from_elibz(symbol_name, src_path, dest, category_hint)
            return self._import_from_kicad_sym(symbol_name, src_path, dest, category_hint)
        except Exception as exc:
            return False, f"Import failed: {exc}"

    def import_footprint(
        self,
        fp_name: str,
        src_pretty: Path,
        src_alias: str,
        category_hint: str,
    ) -> Tuple[bool, str]:
        dest_pretty = self._get_dest_pretty_path(category_hint)
        dest_sym = self._get_dest_lib_path(category_hint)
        src_mod = src_pretty / f"{fp_name}.kicad_mod"
        if not src_mod.exists():
            return False, f"Footprint '{fp_name}' not found in source"

        try:
            dest_pretty.mkdir(parents=True, exist_ok=True)
            FootprintEditor.copy_preserving_models_if_missing(src_mod, dest_pretty / src_mod.name)
            model_copied = self._copy_kicad_3d_model(
                f"{src_alias}:{fp_name}",
                dest_pretty,
                [(src_alias, src_pretty)],
                source_root=src_pretty.parent,
            )
            self._update_lib_tables(dest_sym)
            parts = [f"footprint '{fp_name}' vendored"]
            if model_copied:
                parts.append("3D model copied")
            return True, " — ".join(parts)
        except Exception as exc:
            return False, f"Footprint import failed: {exc}"

    def _dest_lib_name(self, category_hint: str) -> str:
        return resolve_target_library_name(
            self.general,
            category_hint,
            sanitize=sanitize_lib_name,
            project_path=self.project_path,
        )

    def _get_dest_lib_path(self, category_hint: str) -> Path:
        lib_dir, _uri_prefix = resolve_lib_root(self.general, self.project_path)
        lib_name = self._dest_lib_name(category_hint)
        return lib_dir / lib_name / f"{lib_name}.kicad_sym"

    def _get_dest_pretty_path(self, category_hint: str) -> Path:
        dest = self._get_dest_lib_path(category_hint)
        return dest.parent / f"{dest.stem}.pretty"

    @staticmethod
    def _get_models_dir_for(dest: Path) -> Path:
        return Path(dest).parent / "3dmodels"

    def _uri_prefix(self) -> str:
        _lib_dir, uri_prefix = resolve_lib_root(self.general, self.project_path)
        return uri_prefix

    def _import_from_elibz(
        self,
        symbol_name: str,
        src_path: Path,
        dest: Path,
        category_hint: str,
    ) -> Tuple[bool, str]:
        try:
            payload = load_elibz_payload(src_path)
            device_entry = find_device_by_name(payload, symbol_name)
            if device_entry is None:
                return False, f"Device '{symbol_name}' not found in source"
            attrs = device_entry.get("attributes", {}) or {}
        except Exception as exc:
            return False, f"Unable to read source .elibz: {exc}"

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest_pretty = self._get_dest_pretty_path(category_hint)
            dest_pretty.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="elibz_convert_") as td:
                tmp_dir = Path(td)
                converted_sym = tmp_dir / "converted.kicad_sym"
                converted_pretty = tmp_dir / "converted.pretty"
                convert_elibz_with_kicad_cli(src_path, converted_sym, converted_pretty)

                clean_symbol_name = strip_lcsc_suffix(symbol_name)
                source_symbol_name = pick_symbol_name_from_converted(
                    converted_sym,
                    device_entry,
                    requested_name=clean_symbol_name,
                )
                if not source_symbol_name:
                    return False, "Unable to locate symbol in converted ELIBZ output"

                block = extract_symbol_block(converted_sym, source_symbol_name)
                if block is None:
                    return False, f"Symbol '{source_symbol_name}' not found in converted ELIBZ output"
                parent_chain = extract_parent_symbol_chain(converted_sym, source_symbol_name)

                if source_symbol_name != clean_symbol_name:
                    block = rename_symbol_block(block, source_symbol_name, clean_symbol_name)

                block = patch_footprint_property(block, dest.stem)
                for parent_name, parent_block in parent_chain:
                    add_or_replace_symbol(dest, parent_name, parent_block)
                add_or_replace_symbol(dest, clean_symbol_name, block)

                fp_candidates: List[str] = []
                fp_ref = get_footprint_property(block) or ""
                if ":" in fp_ref:
                    fp_candidates.append(fp_ref.split(":", 1)[1])
                for cand in (
                    attrs.get("Footprint"),
                    (device_entry.get("footprint", {}) or {}).get("display_title"),
                    (device_entry.get("footprint", {}) or {}).get("title"),
                ):
                    text = str(cand or "").strip()
                    if text and text not in fp_candidates:
                        fp_candidates.append(text)

                footprint_path = None
                for fp_name in fp_candidates:
                    src_mod = resolve_elibz_footprint_mod_path(converted_pretty, fp_name)
                    if src_mod is None:
                        continue
                    footprint_path = dest_pretty / src_mod.name
                    FootprintEditor.copy_preserving_models_if_missing(src_mod, footprint_path)
                    break

            model_copied = self._copy_elibz_step_model(symbol_name, src_path, dest)
            if model_copied:
                try:
                    if footprint_path is not None:
                        content = footprint_path.read_text(encoding="utf-8", errors="replace")
                        content = re.sub(r"\.wrl\"", '.step"', content)
                        footprint_path.write_text(content, encoding="utf-8")
                except Exception:
                    pass

            self._update_lib_tables(dest)
            parts = [f"'{symbol_name}' vendored", "symbol+footprint converted (native kicad-cli)"]
            if model_copied:
                parts.append("3D model copied")
            return True, " — ".join(parts)
        except Exception as exc:
            return False, f"ELIBZ → KiCad conversion failed: {exc}"

    def _copy_elibz_step_model(self, symbol_name: str, src_path: Path, dest: Path) -> bool:
        try:
            payload = load_elibz_payload(src_path)
            dev = find_device_by_name(payload, symbol_name)
            model_title = str(((dev or {}).get("attributes") or {}).get("3D Model Title") or "").strip()
        except Exception:
            return False

        if not model_title:
            return False

        candidates = [
            src_path.parent / "3dmodels" / f"{model_title}.step",
            src_path.parent / "EASYEDA_MODELS" / f"{model_title}.step",
            self.project_path / "3dmodels" / f"{model_title}.step",
            self.project_path / "EASYEDA_MODELS" / f"{model_title}.step",
        ]
        src_step = next((p for p in candidates if p.exists()), None)
        if src_step is None:
            return False

        dest_models = self._get_models_dir_for(dest)
        dest_models.mkdir(parents=True, exist_ok=True)
        dest_step = dest_models / src_step.name
        if not dest_step.exists():
            shutil.copy2(src_step, dest_step)
        return True

    def _import_from_kicad_sym(
        self,
        symbol_name: str,
        src_path: Path,
        dest: Path,
        category_hint: str,
    ) -> Tuple[bool, str]:
        block = extract_symbol_block(src_path, symbol_name)
        if block is None:
            return False, f"Symbol '{symbol_name}' not found in source"
        parent_chain = extract_parent_symbol_chain(src_path, symbol_name)

        fp_ref = get_footprint_property(block)
        fp_copied = False
        model_copied = False
        src_fp_libs = self._source_fp_libs(src_path)

        if fp_ref and ":" in fp_ref:
            dest_pretty = self._get_dest_pretty_path(category_hint)
            fp_copied = copy_footprint_to_pretty(fp_ref, dest_pretty, src_fp_libs)
            if not fp_copied and src_path.suffix.lower() == ".kicad_sym":
                fp_name = fp_ref.split(":", 1)[1]
                fp_copied = self._copy_fp_from_sibling_pretty(src_path, fp_name, dest_pretty)
            if fp_copied:
                block = patch_footprint_property(block, dest.stem)
                model_copied = self._copy_kicad_3d_model(
                    fp_ref,
                    dest_pretty,
                    src_fp_libs,
                    source_root=src_path.parent,
                )

        for parent_name, parent_block in parent_chain:
            add_or_replace_symbol(dest, parent_name, parent_block)
        add_or_replace_symbol(dest, symbol_name, block)
        self._update_lib_tables(dest)

        parts = [f"'{symbol_name}' vendored"]
        if fp_copied:
            parts.append("footprint copied")
        elif fp_ref and ":" in fp_ref:
            parts.append(f"footprint '{fp_ref}' not found")
        if model_copied:
            parts.append("3D model copied")
        return True, " — ".join(parts)

    def _copy_kicad_3d_model(
        self,
        fp_ref: str,
        dest_pretty: Path,
        fp_libs: List[Tuple[str, Path]],
        source_root: Optional[Path] = None,
    ) -> bool:
        if ":" not in fp_ref:
            return False
        lib_name, fp_name = fp_ref.split(":", 1)

        kicad_mod = dest_pretty / f"{fp_name}.kicad_mod"
        if not kicad_mod.exists():
            return False

        content = kicad_mod.read_text(encoding="utf-8", errors="replace")
        model_paths = re.findall(r'\(model\s+"([^"]+)"', content)
        if not model_paths:
            return False

        copied = False
        new_content = content
        dest_models_dir = dest_pretty.parent / "3dmodels"
        models_base = self._model_uri_base(dest_models_dir).rstrip("/")

        for model_path in model_paths:
            if any(model_path.startswith(p) for p in _BUILTIN_3D_PREFIXES):
                continue

            src_model = self._resolve_model_path(
                model_path,
                lib_name,
                fp_libs=fp_libs,
                source_root=source_root,
            )
            if src_model is None or not src_model.exists():
                legacy_ref = self._normalize_legacy_model_ref(model_path, models_base)
                if legacy_ref and legacy_ref != model_path:
                    new_content = new_content.replace(f'"{model_path}"', f'"{legacy_ref}"', 1)
                    copied = True
                continue

            dest_models_dir.mkdir(parents=True, exist_ok=True)
            dest_model = dest_models_dir / src_model.name
            if not dest_model.exists():
                shutil.copy2(src_model, dest_model)

            new_ref = f"{models_base}/{dest_model.name}"
            new_content = new_content.replace(f'"{model_path}"', f'"{new_ref}"', 1)
            copied = True

        if new_content != content:
            kicad_mod.write_text(new_content, encoding="utf-8")
        return copied

    def _resolve_model_path(
        self,
        model_path: str,
        lib_name: str,
        fp_libs: List[Tuple[str, Path]],
        source_root: Optional[Path] = None,
    ) -> Optional[Path]:
        src_pretty: Optional[Path] = None
        for name, p in fp_libs:
            if name == lib_name:
                src_pretty = p
                break

        original = model_path
        resolved = model_path
        if "${KIPRJMOD}" in resolved:
            if src_pretty is not None:
                resolved = resolved.replace("${KIPRJMOD}", str(src_pretty.parent))
            elif source_root is not None:
                resolved = resolved.replace("${KIPRJMOD}", str(source_root))
            else:
                resolved = resolved.replace("${KIPRJMOD}", str(self.project_path))

        p = Path(resolved)
        if p.is_absolute() and p.exists():
            return p

        if src_pretty is not None:
            candidate = src_pretty.parent / resolved
            if candidate.exists():
                return candidate
            candidate = src_pretty / resolved
            if candidate.exists():
                return candidate

        if source_root is not None:
            candidate = source_root / resolved
            if candidate.exists():
                return candidate

        if "${KIPRJMOD}" in original:
            alt = original.replace("${KIPRJMOD}", str(self.project_path))
            p2 = Path(alt)
            if p2.is_absolute() and p2.exists():
                return p2

        if "EASYEDA_MODELS" in original:
            alt2 = original.replace("EASYEDA_MODELS", "3dmodels")
            if "${KIPRJMOD}" in alt2:
                alt2 = alt2.replace("${KIPRJMOD}", str(self.project_path))
            p3 = Path(alt2)
            if p3.is_absolute() and p3.exists():
                return p3

        return None

    @staticmethod
    def _normalize_legacy_model_ref(model_path: str, models_base: str) -> Optional[str]:
        norm = model_path.replace("\\", "/")
        marker = "/EASYEDA_MODELS/"
        if marker not in norm:
            return None
        tail = norm.split(marker, 1)[1].lstrip("/")
        if not tail:
            return None
        return f"{models_base}/{tail}"

    def _source_fp_libs(self, src_sym_path: Path) -> List[Tuple[str, Path]]:
        libs = list(self._fp_libs)
        if src_sym_path.suffix.lower() == ".kicad_sym":
            sibling = src_sym_path.with_suffix(".pretty")
            if sibling.is_dir():
                alias = src_sym_path.stem
                if not any(name == alias and p.resolve() == sibling.resolve() for name, p in libs):
                    libs.append((alias, sibling))
        return libs

    @staticmethod
    def _copy_fp_from_sibling_pretty(src_sym_path: Path, fp_name: str, dest_pretty: Path) -> bool:
        try:
            src = src_sym_path.with_suffix(".pretty") / f"{fp_name}.kicad_mod"
            if not src.exists():
                return False
            dest_pretty.mkdir(parents=True, exist_ok=True)
            FootprintEditor.copy_preserving_models_if_missing(src, dest_pretty / f"{fp_name}.kicad_mod")
            return True
        except Exception:
            return False

    def _model_uri_base(self, models_dir: Path) -> str:
        models_dir = Path(models_dir).resolve()
        try:
            rel = os.path.relpath(models_dir, self.project_path.resolve())
            return "${KIPRJMOD}/" + Path(rel).as_posix()
        except Exception:
            return "${KIPRJMOD}/3dmodels"

    def _update_lib_tables(self, dest: Path) -> None:
        try:
            scope = str(self.general.get("library_scope", "project")).strip().lower()
            if scope == "shared":
                shared_root, shared_uri_prefix = resolve_lib_root(
                    self.general,
                    self.project_path,
                )
                default_name = resolve_library_base_name(
                    self.general,
                    project_path=self.project_path,
                )
                ensure_shared_meta(shared_root, default_name, log=lambda _: None)
                LibTablesManager(shared_root, log=lambda _: None).ensure_project_lib_tables(
                    shared_root,
                    use_project_relative=False,
                    uri_prefix=shared_uri_prefix,
                    lib_format="kicad",
                )
                ensure_project_table_links(
                    self.project_path,
                    shared_root,
                    log=lambda _: None,
                )
            else:
                lib_root, uri_prefix = resolve_lib_root(self.general, self.project_path)
                LibTablesManager(self.project_path, log=lambda _: None).ensure_project_lib_tables(
                    lib_root,
                    uri_prefix=uri_prefix,
                    lib_format="kicad",
                )
            ensure_project_legacy_models_link(
                self.project_path,
                self._get_models_dir_for(dest),
                log=lambda _: None,
            )
        except Exception:
            pass
