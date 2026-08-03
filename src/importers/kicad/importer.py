"""KiCad format importer using kicad-cli for symbol/footprint conversion."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import fnmatch
import hashlib
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
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
    apply_pin_name_map,
    add_or_replace_symbol,
    ensure_value_visible_in_symbol,
    extract_parent_symbol_chain,
    extract_symbol_block,
    fix_symbol_label_positions,
    normalize_pin_names,
    get_connected_fp_libs,
    get_connected_sym_libs,
    get_footprint_property,
    hide_value_in_footprint,
    list_symbols_kicad_lcsc_ids,
    list_symbols_kicad_meta,
    patch_footprint_property,
    set_footprint_meta_property,
    set_footprint_property,
)
from ...core.events import LogboxAppendEvent
from ...core.helpers import (
    PLUGIN_PATH,
    as_bool,
    sanitize_lib_name,
    strip_lcsc_suffix,
)
from ...core.plugin_paths import get_plugin_cache_path
from ...core.lib_paths import resolve_lib_root, resolve_library_base_name, resolve_target_library_name
from ...core.platform_support import resolve_system_library_root
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
from . import footprint_matcher, symbol_matcher


class KicadImporter:
    """Importer that generates .kicad_sym and .pretty libraries."""

    _PASSIVE_KINDS = footprint_matcher.PASSIVE_KINDS
    _CHIP_SIZE_IMPERIAL_TO_METRIC = footprint_matcher.CHIP_SIZE_IMPERIAL_TO_METRIC
    _CHIP_SIZE_METRIC_TO_IMPERIAL = footprint_matcher.CHIP_SIZE_METRIC_TO_IMPERIAL
    _DEFAULT_SYMBOL_MAP = symbol_matcher.DEFAULT_SYMBOL_MAP
    _DEFAULT_FP_LIB_PRIORITY = footprint_matcher.DEFAULT_FP_LIB_PRIORITY
    _SOURCE_SYMBOL_PROP = "JLCPCB Source Symbol"
    _SOURCE_FOOTPRINT_PROP = "JLCPCB Source Footprint"
    _SYMBOL_ATTR_DROP_KEYS = {
        "3dmodel",
        "3dmodeltitle",
        "3dmodeltransform",
        "supplierfootprint",
        "symbol",
        "symbolid",
        "symboluuid",
        "designator",
        "footprintid",
        "footprintuuid",
    }
    _SYMBOL_PURGE_PROPS = (
        "3D Model",
        "3D Model Title",
        "3D Model Transform",
        "Supplier Footprint",
        "Symbol",
        "Symbol UUID",
        "Footprint UUID",
        "Designator",
        "LCSC Description",
        "Part Description",
    )
    _DB_CANON_KEYS = (
        "LCSC Part",
        "MFR.Part",
        "Package",
        "Manufacturer",
        "Description",
        "First Category",
        "Second Category",
        "Datasheet",
        "Library Type",
    )
    _DB_CANON_ALIASES = {
        "LCSC Part": (
            "LCSC Part",
            "Supplier Part",
            "Supplier Part #",
            "LCSC Part #",
            "LCSC",
            "Component Code",
        ),
        "MFR.Part": (
            "MFR.Part",
            "Manufacturer Part",
            "MPN",
            "Part Number",
            "PartNumber",
            "Part No",
            "Device",
            "Name",
        ),
        "Package": (
            "Package",
            "Supplier Footprint",
            "Footprint",
            "Case",
            "Encapsulation",
            "Housing",
            "Outline",
        ),
        "Manufacturer": (
            "Manufacturer",
            "Mfr",
            "Vendor",
            "Brand",
        ),
        "Description": (
            "Description",
            "LCSC Description",
            "Part Description",
            "LCSC Part Name",
            "ki_description",
        ),
        "First Category": (
            "First Category",
            "Primary Category",
            "Category",
            "Parent Tag",
        ),
        "Second Category": (
            "Second Category",
            "Secondary Category",
            "Subcategory",
            "Child Tag",
        ),
        "Datasheet": (
            "Datasheet",
            "Datasheet URL",
            "Data Sheet",
        ),
        "Library Type": (
            "Library Type",
            "Library",
            "Component Library",
        ),
    }

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
        self._symbol_index_cache_key: str = ""
        self._symbol_index_cache: Dict[str, Tuple[str, Path]] = {}
        self._lcsc_symbol_index_cache: Dict[str, Tuple[str, Path]] = {}
        self._easyeda_pin_map_cache: Dict[str, Dict[str, str]] = {}

    def import_part(self, lcsc_id: str) -> Tuple[bool, Path]:
        return self._import_part_via_elibz(lcsc_id)

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

    def _exclude_project_local_libs(
        self,
        libs: List[Tuple[str, Path]],
        kind: str,
    ) -> List[Tuple[str, Path]]:
        """Exclude project-local libraries from builtin lookup/index scans."""
        if not libs:
            return libs
        try:
            project_root = self.project_path.resolve()
        except Exception:
            project_root = self.project_path
        filtered: List[Tuple[str, Path]] = []
        skipped = 0
        for alias, lib_path in libs:
            try:
                resolved = lib_path.resolve()
            except Exception:
                resolved = lib_path
            try:
                resolved.relative_to(project_root)
                skipped += 1
                continue
            except Exception:
                pass
            filtered.append((alias, resolved))
        if skipped:
            self.log(
                f"KiCad builtin: excluded {skipped} project-local {kind} libraries from lookup candidates.\n"
            )
        return filtered

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
                    lib_format="kicad",
                )
                ensure_project_table_links(self.project_path, lib_root, log=self.log)
            else:
                _, uri_prefix = resolve_lib_root(general, self.project_path)
                LibTablesManager(self.project_path, log=self.log).ensure_project_lib_tables(
                    lib_root,
                    uri_prefix=uri_prefix,
                    lib_format="kicad",
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
            elibz_path.parent / f"{model_title}.step",
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

    def _normalize_footprint_model_paths(self, footprint_dir: Path, models_dir: Path) -> int:
        """Rewrite footprint model refs to the active models directory when possible."""
        if not footprint_dir.exists() or not models_dir.exists():
            return 0
        model_base = self._model_3d_path(models_dir).rstrip("/")
        changed = 0
        for mod_path in sorted(footprint_dir.glob("*.kicad_mod")):
            try:
                content = mod_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            def _replace(match: re.Match) -> str:
                original = match.group(1).strip()
                if original.startswith("kicad-embed://"):
                    return match.group(0)
                name = Path(original.replace("\\", "/")).name
                if not name:
                    return match.group(0)
                target = models_dir / name
                if not target.exists():
                    return match.group(0)
                updated = f"{model_base}/{name}"
                if updated == original:
                    return match.group(0)
                return f'(model "{updated}"'

            patched = re.sub(r'\(model\s+"([^"]+)"', _replace, content)
            if patched != content:
                try:
                    mod_path.write_text(patched, encoding="utf-8")
                    changed += 1
                except Exception:
                    pass
        if changed:
            self.log(f"Normalized 3D model paths in {changed} footprint(s).\n")
        return changed

    def warm_symbol_index_cache(
        self,
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> bool:
        """Warm up KiCad symbol index cache on startup (memory + optional disk cache)."""
        general = self._general_settings()
        fmt = str(general.get("lib_format", "easyeda_pro")).strip().lower()
        if fmt != "kicad":
            return False
        if not self._kicad_builtin_first_enabled(general):
            return False
        if not self._kicad_symbol_matching_enabled(general):
            return False
        sym_libs = self._exclude_project_local_libs(
            get_connected_sym_libs(
                self.project_path,
                include_project_tables=False,
                include_user_tables=False,
            ),
            kind="symbol",
        )
        if not sym_libs:
            self.log("KiCad builtin: no connected symbol libraries for startup index warmup.\n")
            return False
        max_symbol_libs = max(20, self._as_int(general.get("kicad_symbol_index_max_libs"), 300))
        self.log(
            f"KiCad builtin: startup symbol index warmup (libs={len(sym_libs)}, max={max_symbol_libs}).\n"
        )
        self._get_symbol_index(sym_libs, general, max_libs=max_symbol_libs, progress_cb=progress_cb)
        return True

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _normalize_pin_number_key(value: str) -> str:
        token = str(value or "").strip().upper()
        if not token:
            return ""
        token = token.strip("()[]{}")
        token = re.sub(r"\s+", "", token)
        token = token.replace('"', "").replace("'", "")
        return token

    @staticmethod
    def _sanitize_pin_label(value: str) -> str:
        text = str(value or "").replace('"', "'").strip()
        text = re.sub(r"\s+", " ", text)
        if text == "~":
            return ""
        return text

    @classmethod
    def _parse_easyeda_symbol_pin_map_data(cls, data_str: str) -> Dict[str, str]:
        pin_map: Dict[str, str] = {}
        if not data_str:
            return pin_map

        pin_nodes: set[str] = set()
        pin_attrs: Dict[str, Dict[str, str]] = {}

        for line in str(data_str).splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except Exception:
                continue
            if not isinstance(rec, list) or not rec:
                continue
            kind = str(rec[0] or "").upper()
            if kind == "PIN" and len(rec) >= 2:
                pin_id = str(rec[1] or "").strip()
                if pin_id:
                    pin_nodes.add(pin_id)
                continue
            if kind == "ATTR" and len(rec) >= 5:
                parent = str(rec[2] or "").strip()
                key = str(rec[3] or "").strip().upper()
                val = str(rec[4] or "").strip()
                if not parent or parent not in pin_nodes:
                    continue
                if key not in {"NAME", "NUMBER"}:
                    continue
                node = pin_attrs.setdefault(parent, {})
                node[key] = val

        for attrs in pin_attrs.values():
            raw_num = str(attrs.get("NUMBER") or "").strip()
            raw_name = cls._sanitize_pin_label(str(attrs.get("NAME") or ""))
            if not raw_num:
                continue
            num = cls._normalize_pin_number_key(raw_num)
            if not num:
                continue
            pin_map[num] = raw_name
        return pin_map

    def _fetch_easyeda_pin_map(
        self,
        attrs: Dict[str, str],
        device_entry: Optional[Dict] = None,
    ) -> Dict[str, str]:
        symbol_uuid = str(attrs.get("Symbol") or "").strip()
        if not symbol_uuid and device_entry:
            symbol_uuid = str(((device_entry.get("symbol") or {}).get("uuid") or "")).strip()
        symbol_uuid = symbol_uuid.split("|")[0].strip()
        if not symbol_uuid:
            return {}

        cached = self._easyeda_pin_map_cache.get(symbol_uuid)
        if cached is not None:
            return dict(cached)

        pin_map: Dict[str, str] = {}
        try:
            api = LCSC_API()
            comp_info = api.easyeda_get_component(symbol_uuid)
            comp = (comp_info or {}).get("result") or {}
            pin_map = self._parse_easyeda_symbol_pin_map_data(str(comp.get("dataStr") or ""))
        except Exception as exc:
            self.log(f"EasyEDA pin-map fetch failed for symbol {symbol_uuid}: {exc}\n")
            pin_map = {}

        self._easyeda_pin_map_cache[symbol_uuid] = dict(pin_map)
        return pin_map

    @classmethod
    def _pin_name_map_from_meta(cls, meta: Dict) -> Dict[str, str]:
        pin_map_raw = meta.get("easyeda_pin_map")
        if not isinstance(pin_map_raw, dict):
            raw_json = str(meta.get("easyeda_pin_map_json") or "").strip()
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    pin_map_raw = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    pin_map_raw = {}
            else:
                pin_map_raw = {}

        out: Dict[str, str] = {}
        for raw_num, raw_name in (pin_map_raw or {}).items():
            num = cls._normalize_pin_number_key(raw_num)
            if not num:
                continue
            out[num] = cls._sanitize_pin_label(raw_name)
        return out

    def _symbol_index_cache_path(self) -> Path:
        return get_plugin_cache_path() / "kicad_symbol_index_v1.json"

    def _symbol_index_ttl_seconds(self, general: Dict) -> int:
        return max(0, self._as_int(general.get("kicad_symbol_index_ttl_sec"), 86400))

    @staticmethod
    def _uniq(values: Iterable[str]) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    def _read_string_list_map(self, general: Dict, key: str) -> Dict[str, List[str]]:
        raw = general.get(key)
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, List[str]] = {}
        for k, v in raw.items():
            name = str(k or "").strip().lower()
            if not name:
                continue
            if isinstance(v, (list, tuple)):
                items = list(v)
            else:
                items = [v]
            values = self._uniq(str(item or "").strip() for item in items)
            if values:
                out[name] = values
        return out

    def _kicad_builtin_first_enabled(self, general: Dict) -> bool:
        return as_bool(general.get("kicad_builtin_first"), default=True)

    def _kicad_symbol_matching_enabled(self, general: Dict) -> bool:
        return as_bool(general.get("kicad_symbol_matching_enabled"), default=False)

    @staticmethod
    def _component_kind(
        category: str,
        description: str,
        attrs: Dict[str, str],
        meta: Optional[Dict] = None,
    ) -> str:
        return symbol_matcher.component_kind(category, description, attrs, meta)

    @staticmethod
    def _package_variants(value: str) -> List[str]:
        return footprint_matcher.package_variants(value)

    def _extract_package_candidates(self, meta: Dict, attrs: Dict[str, str]) -> List[str]:
        return footprint_matcher.extract_package_candidates(meta, attrs)

    @staticmethod
    def _is_uuid_like(value: str) -> bool:
        return footprint_matcher.is_uuid_like(value)

    @staticmethod
    def _package_family_variants(value: str) -> List[str]:
        return footprint_matcher.package_family_variants(value)

    @staticmethod
    def _is_dimension_like_token(value: str) -> bool:
        return footprint_matcher.is_dimension_like_token(value)

    @staticmethod
    def _is_generic_pkg_token(value: str) -> bool:
        return footprint_matcher.is_generic_pkg_token(value)

    @staticmethod
    def _family_keywords(value: str) -> List[str]:
        return footprint_matcher.family_keywords(value)

    @staticmethod
    def _package_pin_tokens(package_candidates: List[str]) -> List[str]:
        return footprint_matcher.package_pin_tokens(package_candidates)

    @staticmethod
    def _strict_package_keywords(package_candidates: List[str]) -> List[str]:
        return footprint_matcher.strict_package_keywords(package_candidates)

    @staticmethod
    def _has_strict_keyword_overlap(mod_name: str, strict_keywords: List[str]) -> bool:
        return footprint_matcher.has_strict_keyword_overlap(mod_name, strict_keywords)

    @staticmethod
    def _package_dimension_tokens(package_candidates: List[str]) -> set[str]:
        return footprint_matcher.package_dimension_tokens(package_candidates)

    @staticmethod
    def _dimension_match_adjustment(mod_name: str, package_dims: set[str], strict: bool = False) -> int:
        return footprint_matcher.dimension_match_adjustment(mod_name, package_dims, strict=strict)

    @staticmethod
    def _keywords_overlap(a_keys: set[str], b_keys: set[str]) -> bool:
        return footprint_matcher.keywords_overlap(a_keys, b_keys)

    @classmethod
    def _chip_size_codes(cls, value: str) -> Tuple[set[str], set[str]]:
        return footprint_matcher.chip_size_codes(value)

    @classmethod
    def _chip_size_hints(cls, package_candidates: List[str]) -> Tuple[set[str], set[str]]:
        return footprint_matcher.chip_size_hints(package_candidates)

    @classmethod
    def _chip_size_footprint_adjustment(
        cls,
        mod_name: str,
        lib_alias: str,
        component_kind: str,
        hinted_imperial: set[str],
        hinted_metric: set[str],
    ) -> int:
        return footprint_matcher.chip_size_footprint_adjustment(
            mod_name,
            lib_alias,
            component_kind,
            hinted_imperial,
            hinted_metric,
        )

    def _pick_symbol_block(
        self,
        component_kind: str,
        meta: Dict,
        general: Dict,
        sym_libs: List[Tuple[str, Path]],
    ) -> Optional[Tuple[str, str, bool, Path]]:
        def _clip(value: object, limit: int = 96) -> str:
            text = str(value or "").strip()
            if len(text) > limit:
                return text[: limit - 3] + "..."
            return text

        def _collect_pairs(keys: List[str], source: Dict[str, str]) -> List[str]:
            out: List[str] = []
            for key in keys:
                val = str(source.get(key) or "").strip()
                if val:
                    out.append(f"{key}='{_clip(val)}'")
            return out

        attrs = self._canonicalize_easyeda_attrs_for_db(
            self._parse_attributes_map(str(meta.get("attributes_json") or "")),
            meta=meta,
        )
        max_symbol_libs = max(20, self._as_int(general.get("kicad_symbol_index_max_libs"), 300))
        symbol_index = self._get_symbol_index(sym_libs, general, max_libs=max_symbol_libs)
        wildcard_min_score = max(0, self._as_int(general.get("kicad_symbol_wildcard_min_score"), 80))
        wildcard_min_gap = max(0, self._as_int(general.get("kicad_symbol_wildcard_min_gap"), 14))
        ref_hint_raw = str(meta.get("reference_prefix") or attrs.get("Reference") or "").strip()
        value_hint_raw = str(meta.get("value") or attrs.get("Value") or "").strip()
        self.log(
            "KiCad builtin: symbol search params: "
            f"kind={component_kind}, "
            f"ref_hint='{_clip(ref_hint_raw)}', "
            f"value_hint='{_clip(value_hint_raw)}', "
            f"index_size={len(symbol_index)}, "
            f"wildcard_min_score={wildcard_min_score}, "
            f"wildcard_min_gap={wildcard_min_gap}\n"
        )

        meta_pairs = _collect_pairs(["mfr_part", "part_no", "mpn", "symbol_name"], meta)
        attr_pairs = _collect_pairs(
            [
                "Manufacturer Part",
                "MFR.Part",
                "MPN",
                "Part Number",
                "PartNumber",
                "Part No",
                "Device",
                "Name",
                "Symbol",
                "symbol",
                "Schematic Symbol",
                "KiCad Symbol",
            ],
            attrs,
        )
        if meta_pairs:
            self.log("KiCad builtin: symbol meta hints: " + ", ".join(meta_pairs[:10]) + "\n")
        if attr_pairs:
            self.log("KiCad builtin: symbol attr hints: " + ", ".join(attr_pairs[:14]) + "\n")

        # 0) LCSC-ID lookup: if a symbol in a connected library already carries
        #    this exact LCSC Part property, reuse it immediately.
        lcsc_id = str(attrs.get("LCSC Part") or meta.get("lcsc") or "").strip().upper()
        if lcsc_id and lcsc_id.startswith("C") and lcsc_id[1:].isdigit():
            hit = self._lcsc_symbol_index_cache.get(lcsc_id)
            if hit:
                name, lib_path = hit
                blk = extract_symbol_block(lib_path, name)
                if blk:
                    self.log(f"KiCad builtin: found symbol by LCSC ID {lcsc_id} → {name}\n")
                    return name, blk, True, lib_path

        # 1) Exact part-number style lookup (preferred):
        # if KiCad already has a dedicated symbol (e.g. IR4321), use that first.
        exact_refs = self._exact_symbol_candidates(meta, attrs)[:20]
        if exact_refs:
            self.log(f"KiCad builtin: exact symbol candidates: {', '.join(exact_refs[:5])}\n")
        for ref in exact_refs:
            resolved = self._resolve_symbol_ref(
                sym_libs,
                ref,
                symbol_index=symbol_index,
                wildcard_min_score=wildcard_min_score,
                wildcard_min_gap=wildcard_min_gap,
            )
            if resolved:
                name, block, lib_path = resolved
                return name, block, True, lib_path

        # 2) Fallback to class-based mapping.
        custom_map = self._read_string_list_map(general, "kicad_symbol_map")
        refs = self._uniq(
            custom_map.get(component_kind, [])
            + custom_map.get("default", [])
            + self._DEFAULT_SYMBOL_MAP.get(component_kind, [])
            + self._DEFAULT_SYMBOL_MAP.get("default", [])
        )
        for cand in (
            attrs.get("Symbol"),
            attrs.get("symbol"),
            attrs.get("Schematic Symbol"),
            attrs.get("KiCad Symbol"),
        ):
            text = str(cand or "").strip()
            if text:
                refs.insert(0, text)
        refs = self._uniq(refs)
        if refs:
            self.log(f"KiCad builtin: mapped symbol refs to try: {', '.join(refs[:8])}\n")

        for ref in refs:
            resolved = self._resolve_symbol_ref(
                sym_libs,
                ref,
                symbol_index=symbol_index,
                wildcard_min_score=wildcard_min_score,
                wildcard_min_gap=wildcard_min_gap,
            )
            if resolved:
                name, block, lib_path = resolved
                return name, block, False, lib_path
        return None

    @staticmethod
    def _exact_symbol_candidates(meta: Dict, attrs: Dict[str, str]) -> List[str]:
        return symbol_matcher.exact_symbol_candidates(meta, attrs)

    @staticmethod
    def _family_symbol_candidates(part_number: str) -> List[str]:
        return symbol_matcher.family_symbol_candidates(part_number)

    @staticmethod
    def _pick_wildcard_symbol_key(
        pattern: str,
        candidates: Iterable[str],
        min_score: int = 80,
        min_gap: int = 14,
    ) -> Optional[str]:
        return symbol_matcher.pick_wildcard_symbol_key(
            pattern,
            candidates,
            min_score=min_score,
            min_gap=min_gap,
        )

    def _resolve_symbol_ref(
        self,
        sym_libs: List[Tuple[str, Path]],
        symbol_ref: str,
        symbol_index: Optional[Dict[str, Tuple[str, Path]]] = None,
        wildcard_min_score: int = 80,
        wildcard_min_gap: int = 14,
    ) -> Optional[Tuple[str, str, Path]]:
        ref = str(symbol_ref or "").strip()
        if not ref:
            return None

        # "Lib:Symbol"
        if ":" in ref:
            lib_name, symbol_name = ref.split(":", 1)
            for alias, lib_path in sym_libs:
                if alias != lib_name:
                    continue
                block = extract_symbol_block(lib_path, symbol_name)
                if block:
                    return symbol_name, block, lib_path
                for name, _desc, _fp in list_symbols_kicad_meta(lib_path):
                    if name.lower() == symbol_name.lower():
                        blk = extract_symbol_block(lib_path, name)
                        if blk:
                            return name, blk, lib_path
            return None

        # "Symbol" (find in any connected library)
        if symbol_index:
            key = ref.lower()
            has_wildcards = any(ch in key for ch in ("*", "?", "["))
            hit = symbol_index.get(key)
            if hit:
                name, lib_path = hit
                blk = extract_symbol_block(lib_path, name)
                if blk:
                    return name, blk, lib_path
            if has_wildcards:
                chosen_key = self._pick_wildcard_symbol_key(
                    key,
                    symbol_index.keys(),
                    min_score=max(0, int(wildcard_min_score)),
                    min_gap=max(0, int(wildcard_min_gap)),
                )
                if chosen_key:
                    chosen_hit = symbol_index.get(chosen_key)
                    if chosen_hit:
                        idx_name, idx_lib_path = chosen_hit
                        blk = extract_symbol_block(idx_lib_path, idx_name)
                        if blk:
                            return idx_name, blk, idx_lib_path
            return None

        for _alias, lib_path in sym_libs:
            block = extract_symbol_block(lib_path, ref)
            if block:
                return ref, block, lib_path
        return None

    @staticmethod
    def _lib_alias_for_path(libs: List[Tuple[str, Path]], lib_path: Optional[Path]) -> str:
        if lib_path is None:
            return ""
        try:
            want = lib_path.resolve()
        except Exception:
            want = lib_path
        for alias, path in libs:
            try:
                candidate = path.resolve()
            except Exception:
                candidate = path
            if candidate == want:
                return alias
        return ""

    @staticmethod
    def _build_lcsc_index(
        sym_libs: List[Tuple[str, Path]],
    ) -> Dict[str, Tuple[str, Path]]:
        """Scan libraries for symbols that carry an LCSC Part property.
        Returns ``{lcsc_id_upper: (symbol_name, lib_path)}``."""
        lcsc_index: Dict[str, Tuple[str, Path]] = {}
        for _alias, lib_path in sym_libs:
            try:
                for name, lcsc_id in list_symbols_kicad_lcsc_ids(lib_path):
                    k = lcsc_id.upper()
                    if k not in lcsc_index:
                        lcsc_index[k] = (name, lib_path)
            except Exception:
                continue
        return lcsc_index

    def _get_symbol_index(
        self,
        sym_libs: List[Tuple[str, Path]],
        general: Dict,
        max_libs: int = 300,
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> Dict[str, Tuple[str, Path]]:
        def _report(value: int) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(max(0, min(100, int(value))))
            except Exception:
                pass

        _report(0)
        scan_libs = sym_libs[:max_libs]
        if len(sym_libs) > max_libs:
            self.log(
                f"KiCad builtin: symbol index limited to first {max_libs}/{len(sym_libs)} libraries.\n"
            )
        ttl_seconds = self._symbol_index_ttl_seconds(general)
        signature = self._symbol_libs_signature(scan_libs)
        signature_hash = self._symbol_signature_hash(signature)
        if signature_hash == self._symbol_index_cache_key and self._symbol_index_cache:
            self.log("KiCad builtin: using in-memory symbol index cache.\n")
            _report(100)
            return self._symbol_index_cache

        disk_index = self._load_symbol_index_from_disk(
            signature=signature,
            signature_hash=signature_hash,
            max_libs=max_libs,
            ttl_seconds=ttl_seconds,
        )
        if disk_index is not None:
            self._symbol_index_cache_key = signature_hash
            self._symbol_index_cache = disk_index
            self._lcsc_symbol_index_cache = self._build_lcsc_index(scan_libs)
            self.log("KiCad builtin: loaded symbol index from disk cache.\n")
            _report(100)
            return disk_index

        index: Dict[str, Tuple[str, Path]] = {}
        lcsc_index: Dict[str, Tuple[str, Path]] = {}
        start_logged = False
        for idx, (_alias, lib_path) in enumerate(scan_libs, start=1):
            if not start_logged:
                self.log(f"KiCad builtin: building symbol index from {len(scan_libs)} libraries...\n")
                start_logged = True
            try:
                for name, _desc, _fp in list_symbols_kicad_meta(lib_path):
                    k = name.lower()
                    if k not in index:
                        index[k] = (name, lib_path)
                for name, lcsc_id in list_symbols_kicad_lcsc_ids(lib_path):
                    k = lcsc_id.upper()
                    if k not in lcsc_index:
                        lcsc_index[k] = (name, lib_path)
            except Exception:
                continue
            _report(int(idx * 100 / max(1, len(scan_libs))))
            if idx % 50 == 0:
                self.log(f"KiCad builtin: indexed {idx}/{len(scan_libs)} symbol libraries.\n")

        self._save_symbol_index_to_disk(
            signature=signature,
            signature_hash=signature_hash,
            index=index,
            max_libs=max_libs,
            ttl_seconds=ttl_seconds,
        )
        self._symbol_index_cache_key = signature_hash
        self._symbol_index_cache = index
        self._lcsc_symbol_index_cache = lcsc_index
        if lcsc_index:
            self.log(f"KiCad builtin: LCSC symbol index ready ({len(lcsc_index)} entries).\n")
        self.log(f"KiCad builtin: symbol index ready ({len(index)} unique symbols).\n")
        _report(100)
        return index

    def _symbol_libs_signature(self, libs: List[Tuple[str, Path]]) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for alias, lib_path in libs:
            try:
                rp = lib_path.resolve()
            except Exception:
                rp = lib_path
            try:
                st = rp.stat()
                mtime_ns = int(st.st_mtime_ns)
                size = int(st.st_size)
            except Exception:
                mtime_ns = 0
                size = 0
            out.append(
                {
                    "alias": str(alias),
                    "path": str(rp),
                    "mtime_ns": mtime_ns,
                    "size": size,
                }
            )
        return out

    @staticmethod
    def _symbol_signature_hash(signature: List[Dict[str, object]]) -> str:
        raw = json.dumps(signature, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _load_symbol_index_from_disk(
        self,
        signature: List[Dict[str, object]],
        signature_hash: str,
        max_libs: int,
        ttl_seconds: int,
    ) -> Optional[Dict[str, Tuple[str, Path]]]:
        cache_path = self._symbol_index_cache_path()
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            self.log(f"KiCad builtin: symbol index cache unreadable ({exc}), rebuilding.\n")
            return None

        try:
            if int(payload.get("schema_version", 0)) != 1:
                return None
            if int(payload.get("max_libs", 0)) != int(max_libs):
                self.log("KiCad builtin: symbol index cache max_libs changed, rebuilding.\n")
                return None
            if str(payload.get("signature_hash") or "") != signature_hash:
                self.log("KiCad builtin: symbol library signature changed, rebuilding index.\n")
                return None
            if int(payload.get("library_count", 0)) != len(signature):
                self.log("KiCad builtin: symbol library count changed, rebuilding index.\n")
                return None
            generated_at = int(payload.get("generated_at", 0))
            if ttl_seconds > 0 and generated_at > 0:
                age = int(time.time()) - generated_at
                if age > ttl_seconds:
                    self.log(
                        f"KiCad builtin: symbol index cache expired (age={age}s, ttl={ttl_seconds}s), rebuilding.\n"
                    )
                    return None
            raw_index = payload.get("index")
            if not isinstance(raw_index, dict) or not raw_index:
                return None

            restored: Dict[str, Tuple[str, Path]] = {}
            for key, value in raw_index.items():
                if not isinstance(value, dict):
                    continue
                name = str(value.get("name") or "").strip()
                lib_path = str(value.get("lib_path") or "").strip()
                if not name or not lib_path:
                    continue
                restored[str(key)] = (name, Path(lib_path))
            if not restored:
                return None
            return restored
        except Exception:
            return None

    def _save_symbol_index_to_disk(
        self,
        signature: List[Dict[str, object]],
        signature_hash: str,
        index: Dict[str, Tuple[str, Path]],
        max_libs: int,
        ttl_seconds: int,
    ) -> None:
        cache_path = self._symbol_index_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            serial_index: Dict[str, Dict[str, str]] = {}
            for key, (name, lib_path) in index.items():
                try:
                    rp = lib_path.resolve()
                except Exception:
                    rp = lib_path
                serial_index[key] = {"name": name, "lib_path": str(rp)}
            payload = {
                "schema_version": 1,
                "generated_at": int(time.time()),
                "ttl_seconds": int(ttl_seconds),
                "max_libs": int(max_libs),
                "library_count": len(signature),
                "signature_hash": signature_hash,
                "index_size": len(serial_index),
                "index": serial_index,
            }
            tmp_path = cache_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(cache_path)
            self.log(f"KiCad builtin: symbol index cache saved to {cache_path}.\n")
        except Exception as exc:
            self.log(f"KiCad builtin: unable to save symbol index cache ({exc}).\n")

    @staticmethod
    def _is_exact_fp_ref(ref: str) -> bool:
        text = str(ref or "").strip()
        if ":" not in text:
            return False
        return not any(ch in text for ch in ("*", "?", "[", "]", "{", "}"))

    def _ordered_fp_libs(
        self,
        fp_libs: List[Tuple[str, Path]],
        preferred_names: List[str],
    ) -> List[Tuple[str, Path]]:
        preferred = [str(name or "").strip() for name in preferred_names if str(name or "").strip()]
        preferred_set = set(preferred)
        ordered: List[Tuple[str, Path]] = []
        for wanted in preferred:
            for alias, path in fp_libs:
                if alias == wanted and (alias, path) not in ordered:
                    ordered.append((alias, path))
        for alias, path in fp_libs:
            if alias not in preferred_set and (alias, path) not in ordered:
                ordered.append((alias, path))
        return ordered

    @staticmethod
    def _score_fp_name(mod_name: str, package_candidates: List[str]) -> int:
        return footprint_matcher.score_fp_name(mod_name, package_candidates)

    @staticmethod
    def _explicit_fp_ref_bonus(mod_name: str, explicit_refs: List[str]) -> int:
        return footprint_matcher.explicit_fp_ref_bonus(mod_name, explicit_refs)

    @staticmethod
    def _remember_fp_candidate(
        bucket: Dict[str, Tuple[int, str, str]],
        alias: str,
        stem: str,
        score: int,
    ) -> None:
        key = str(stem or "").upper()
        if not key:
            return
        prev = bucket.get(key)
        if prev is None or score > prev[0]:
            bucket[key] = (int(score), str(alias or ""), str(stem or ""))

    @staticmethod
    def _format_fp_candidates(bucket: Dict[str, Tuple[int, str, str]], limit: int = 8) -> str:
        if not bucket:
            return "none"
        ranked = sorted(bucket.values(), key=lambda item: (-item[0], item[2].lower(), item[1].lower()))
        return ", ".join(f"{alias}:{stem}({score})" for score, alias, stem in ranked[: max(1, int(limit))])

    def _expand_fp_templates(self, entries: List[str], package_candidates: List[str]) -> List[str]:
        out: List[str] = []
        for entry in entries:
            text = str(entry or "").strip()
            if not text:
                continue
            if "${package}" in text and package_candidates:
                for pkg in package_candidates:
                    out.append(text.replace("${package}", pkg))
            else:
                out.append(text)
        return self._uniq(out)

    def _pick_footprint(
        self,
        component_kind: str,
        package_candidates: List[str],
        explicit_refs: List[str],
        general: Dict,
        fp_libs: List[Tuple[str, Path]],
    ) -> Optional[Tuple[str, Path]]:
        custom_map = self._read_string_list_map(general, "kicad_footprint_map")
        custom_entries = self._expand_fp_templates(
            custom_map.get(component_kind, []) + custom_map.get("default", []),
            package_candidates,
        )

        preferred_libs = []
        if component_kind == "ic":
            # ICs should consider all KiCad Package_* footprint libraries.
            for alias, _ in fp_libs:
                if str(alias or "").upper().startswith("PACKAGE_"):
                    preferred_libs.append(alias)
        for entry in custom_entries:
            if ":" not in entry and any(alias == entry for alias, _ in fp_libs):
                preferred_libs.append(entry)
        preferred_libs.extend(self._DEFAULT_FP_LIB_PRIORITY.get(component_kind, []))
        # Add dynamic package-type preferred libraries (by alias keyword overlap).
        pkg_keys: set[str] = set()
        for pkg in package_candidates:
            for key in self._family_keywords(pkg):
                pkg_keys.add(key.lower())
        if pkg_keys:
            for alias, _ in fp_libs:
                alias_keys = {k.lower() for k in self._family_keywords(alias)}
                if self._keywords_overlap(alias_keys, pkg_keys):
                    preferred_libs.append(alias)
        ordered_libs = self._ordered_fp_libs(fp_libs, self._uniq(preferred_libs))

        refs_to_try = self._uniq(explicit_refs + custom_entries)
        if refs_to_try:
            self.log(f"KiCad builtin: footprint refs to try: {', '.join(refs_to_try[:5])}\n")

        # 1) Exact references and names
        for ref in refs_to_try:
            ref = str(ref or "").strip()
            if not ref:
                continue
            if ":" in ref:
                lib_name, fp_name = ref.split(":", 1)
                for alias, pretty in ordered_libs:
                    if alias != lib_name:
                        continue
                    if any(ch in fp_name for ch in ("*", "?", "[")):
                        for mod in pretty.glob("*.kicad_mod"):
                            if fnmatch.fnmatch(mod.stem.lower(), fp_name.lower()):
                                return alias, mod
                        continue
                    mod = resolve_footprint_mod_path(pretty, fp_name)
                    if mod is not None:
                        return alias, mod
            else:
                for alias, pretty in ordered_libs:
                    if any(ch in ref for ch in ("*", "?", "[")):
                        for mod in pretty.glob("*.kicad_mod"):
                            if fnmatch.fnmatch(mod.stem.lower(), ref.lower()):
                                return alias, mod
                        continue
                    mod = resolve_footprint_mod_path(pretty, ref)
                    if mod is not None:
                        return alias, mod

        # 2) Fuzzy match by package/case in preferred libraries.
        # Passives/discretes: any score > 0 is acceptable — package name is a reliable signal.
        # ICs/unknown: only accept a strong match (score >= 90, i.e. the family keyword
        # overlap or near-exact name match). Dimension-only matches (score <= 45) are
        # too ambiguous — e.g. "7.5x7.5mm" matching EP7.5x7.5mm inside a QFN name.
        # "default" kind: skip fuzzy entirely — no reliable package signal.
        if not package_candidates:
            return None
        hinted_imperial, hinted_metric = self._chip_size_hints(package_candidates)
        pkg_pin_tokens = self._package_pin_tokens(package_candidates)
        strict_pkg_keywords = self._strict_package_keywords(package_candidates)
        package_dims = self._package_dimension_tokens(package_candidates)
        if package_dims:
            self.log(
                "KiCad builtin: package size hints: "
                f"{', '.join(sorted(package_dims))}.\n"
            )
        require_keyword_overlap = as_bool(
            general.get("kicad_footprint_require_keyword_overlap"),
            default=True,
        )
        strict_pkg_pin_min_gap = max(0, self._as_int(general.get("kicad_footprint_strict_pkg_pin_min_gap"), 14))
        passive_fuzzy_min_score = max(0, self._as_int(general.get("kicad_footprint_fuzzy_min_score_passive"), 70))
        ic_fuzzy_min_score = max(0, self._as_int(general.get("kicad_footprint_fuzzy_min_score_ic"), 95))
        passive_fuzzy_min_gap = max(0, self._as_int(general.get("kicad_footprint_fuzzy_min_gap_passive"), 16))
        nonpassive_fuzzy_min_gap = max(
            0,
            self._as_int(general.get("kicad_footprint_fuzzy_min_gap_nonpassive"), 12),
        )
        passive_size_strict_mode = as_bool(
            general.get("kicad_footprint_passive_size_strict_mode"),
            default=True,
        )
        max_libs = max(1, self._as_int(general.get("kicad_footprint_fuzzy_max_libs"), 12))
        max_files_per_lib = max(100, self._as_int(general.get("kicad_footprint_fuzzy_max_files_per_lib"), 2500))
        ic_package_aliases = {
            alias for alias, _ in ordered_libs if str(alias or "").upper().startswith("PACKAGE_")
        }
        if preferred_libs:
            pref = set(preferred_libs)
            candidate_libs = [(a, p) for a, p in ordered_libs if a in pref]
            if not candidate_libs:
                candidate_libs = ordered_libs
        else:
            candidate_libs = ordered_libs

        if component_kind == "ic" and ic_package_aliases:
            package_libs = [(a, p) for a, p in candidate_libs if a in ic_package_aliases]
            other_libs = [(a, p) for a, p in candidate_libs if a not in ic_package_aliases]
            extra_budget = max(0, max_libs - len(package_libs))
            fuzzy_libs = package_libs + other_libs[:extra_budget]
            if len(package_libs) > max_libs:
                self.log(
                    "KiCad builtin: IC mode includes all Package_* libraries "
                    f"({len(package_libs)} libs), exceeding fuzzy max libs={max_libs}.\n"
                )
        else:
            fuzzy_libs = candidate_libs[:max_libs]

        # Strict package+pin pass for non-passive/IC-like packages:
        # examples: QFN-44, SOIC-8, SOT-23-5.
        if pkg_pin_tokens:
            strict_pkgpin_candidates: Dict[str, Tuple[int, str, str]] = {}
            strict_best_score = -1
            strict_second_best_score = -1
            strict_best_stem = ""
            strict_second_best_stem = ""
            strict_best: Optional[Tuple[str, Path]] = None
            for alias, pretty in fuzzy_libs:
                try:
                    mods = pretty.glob("*.kicad_mod")
                except Exception:
                    continue
                scanned = 0
                for mod in mods:
                    scanned += 1
                    if scanned > max_files_per_lib:
                        break
                    name_compact = re.sub(r"[^A-Z0-9]", "", mod.stem.upper())
                    if not any(tok in name_compact for tok in pkg_pin_tokens):
                        continue
                    if (
                        require_keyword_overlap
                        and strict_pkg_keywords
                        and not self._has_strict_keyword_overlap(mod.stem, strict_pkg_keywords)
                    ):
                        continue
                    strict_score = self._score_fp_name(mod.stem, package_candidates) + 1200
                    strict_score += self._explicit_fp_ref_bonus(mod.stem, refs_to_try)
                    strict_score += self._dimension_match_adjustment(mod.stem, package_dims, strict=True)
                    self._remember_fp_candidate(strict_pkgpin_candidates, alias, mod.stem, strict_score)
                    stem_key = mod.stem.upper()
                    if strict_best is None:
                        strict_best_score = strict_score
                        strict_best_stem = stem_key
                        strict_best = (alias, mod)
                        continue
                    if stem_key == strict_best_stem:
                        # Same footprint name appears in multiple libraries; keep the first
                        # one (preferred ordering), but do not treat as ambiguity.
                        continue
                    if strict_score > strict_best_score:
                        strict_second_best_score = strict_best_score
                        strict_second_best_stem = strict_best_stem
                        strict_best_score = strict_score
                        strict_best_stem = stem_key
                        strict_best = (alias, mod)
                    elif strict_score > strict_second_best_score:
                        strict_second_best_score = strict_score
                        strict_second_best_stem = stem_key
            self.log(
                "KiCad builtin: strict package+pin candidates (top): "
                f"{self._format_fp_candidates(strict_pkgpin_candidates)}.\n"
            )
            if strict_best is not None:
                if strict_second_best_score >= 0 and (strict_best_score - strict_second_best_score) < strict_pkg_pin_min_gap:
                    self.log(
                        "KiCad builtin: strict package+pin candidates are ambiguous, skipping "
                        f"(best='{strict_best_stem}', second='{strict_second_best_stem}').\n"
                    )
                else:
                    return strict_best

        _fuzzy_min_score = (
            passive_fuzzy_min_score
            if component_kind in self._PASSIVE_KINDS
            else (ic_fuzzy_min_score if component_kind == "ic" else None)
        )
        if _fuzzy_min_score is None:
            return None

        # Deterministic chip-size pass for passives:
        # if we have explicit chip-size hints (e.g. 0402/1005), select only footprints
        # carrying the same size token(s) and ignore THT/axial families.
        if component_kind in self._PASSIVE_KINDS and (hinted_imperial or hinted_metric):
            strict_size_candidates: Dict[str, Tuple[int, str, str]] = {}
            strict_best_score = -1
            strict_best: Optional[Tuple[str, Path]] = None
            for alias, pretty in fuzzy_libs:
                alias_up = alias.upper()
                try:
                    mods = pretty.glob("*.kicad_mod")
                except Exception:
                    continue
                scanned = 0
                for mod in mods:
                    scanned += 1
                    if scanned > max_files_per_lib:
                        break
                    name_up = mod.stem.upper()
                    compact = re.sub(r"[^A-Z0-9]", "", name_up)
                    has_imp = any(code in compact for code in hinted_imperial)
                    has_met = any(code in compact for code in hinted_metric)
                    if not (has_imp or has_met):
                        continue
                    # Do NOT apply keyword overlap filter here: chip-size tokens
                    # (1206, 0402 …) are purely numeric so strict_pkg_keywords may
                    # only contain EasyEDA-specific family tokens ("CAP", "CAPSMD" …)
                    # that have no match in standard KiCad names like C_1206_3216Metric.
                    strict_score = self._score_fp_name(mod.stem, package_candidates)
                    strict_score += self._explicit_fp_ref_bonus(mod.stem, refs_to_try)
                    if has_imp:
                        strict_score += 600
                    if has_met:
                        strict_score += 350
                    if has_imp and has_met:
                        strict_score += 300
                    if "SMD" in alias_up:
                        strict_score += 40
                    strict_score += self._dimension_match_adjustment(mod.stem, package_dims, strict=False)
                    self._remember_fp_candidate(strict_size_candidates, alias, mod.stem, strict_score)
                    if strict_score > strict_best_score:
                        strict_best_score = strict_score
                        strict_best = (alias, mod)
            self.log(
                "KiCad builtin: strict chip-size candidates (top): "
                f"{self._format_fp_candidates(strict_size_candidates)}.\n"
            )
            if strict_best is not None:
                return strict_best
            if passive_size_strict_mode:
                self.log("KiCad builtin: no strict chip-size footprint match, skipping fuzzy fallback.\n")
                return None

        self.log(
            "KiCad builtin: fuzzy footprint scan "
            f"in {len(fuzzy_libs)} libs (max {max_files_per_lib} files/lib).\n"
        )

        fuzzy_candidates: Dict[str, Tuple[int, str, str]] = {}
        best_score = 0
        second_best_score = 0
        best_stem = ""
        second_best_stem = ""
        best: Optional[Tuple[str, Path]] = None
        for alias, pretty in fuzzy_libs:
            try:
                mods = pretty.glob("*.kicad_mod")
            except Exception:
                continue
            scanned = 0
            for mod in mods:
                scanned += 1
                if scanned > max_files_per_lib:
                    break
                if (
                    require_keyword_overlap
                    and strict_pkg_keywords
                    and not self._has_strict_keyword_overlap(mod.stem, strict_pkg_keywords)
                ):
                    continue
                score = self._score_fp_name(mod.stem, package_candidates)
                score += self._explicit_fp_ref_bonus(mod.stem, refs_to_try)
                score += self._chip_size_footprint_adjustment(
                    mod_name=mod.stem,
                    lib_alias=alias,
                    component_kind=component_kind,
                    hinted_imperial=hinted_imperial,
                    hinted_metric=hinted_metric,
                )
                score += self._dimension_match_adjustment(mod.stem, package_dims, strict=False)
                self._remember_fp_candidate(fuzzy_candidates, alias, mod.stem, score)
                stem_key = mod.stem.upper()
                if best is None:
                    best_score = score
                    best_stem = stem_key
                    best = (alias, mod)
                    continue
                if stem_key == best_stem:
                    continue
                if score > best_score:
                    second_best_score = best_score
                    second_best_stem = best_stem
                    best_score = score
                    best_stem = stem_key
                    best = (alias, mod)
                elif score > second_best_score:
                    second_best_score = score
                    second_best_stem = stem_key
        self.log(
            "KiCad builtin: fuzzy candidates (top): "
            f"{self._format_fp_candidates(fuzzy_candidates)}.\n"
        )
        if best is None or best_score < _fuzzy_min_score:
            return None
        min_gap = passive_fuzzy_min_gap if component_kind in self._PASSIVE_KINDS else nonpassive_fuzzy_min_gap
        if second_best_score > 0 and (best_score - second_best_score) < min_gap:
            self.log(
                "KiCad builtin: fuzzy footprint candidates are too close "
                f"(best={best_score} '{best_stem}', second={second_best_score} '{second_best_stem}'), rejecting.\n"
            )
            return None
        return best

    @staticmethod
    def _target_symbol_name(meta: Dict, lcsc_id: str, fallback: str) -> str:
        def _clean(value: str) -> str:
            text = str(value or "").replace('"', "'")
            text = re.sub(r'[:/\\\r\n\t]+', "_", text).strip()
            return text

        for key in ("mfr_part", "part_no", "mpn", "symbol_name"):
            candidate = str(meta.get(key) or "").strip()
            if candidate:
                return _clean(candidate)
        if lcsc_id:
            return _clean(lcsc_id)
        return _clean(fallback)

    def _import_part_via_builtin_kicad(
        self,
        lcsc_id: str,
        meta: Dict,
        general: Dict,
        symbol_lib_path: Path,
        footprint_dir: Path,
        lib_name: str,
    ) -> Tuple[Optional[str], Optional[Path], Optional[str], Optional[str]]:
        description = str(meta.get("description") or "").strip()
        attrs = self._parse_attributes_map(str(meta.get("attributes_json") or ""))
        component_kind = self._component_kind("", description, attrs, meta=meta)
        package_candidates = self._extract_package_candidates(meta, attrs)
        self.log(
            f"KiCad builtin: kind={component_kind}, "
            f"package_hints={len(package_candidates)}\n"
        )

        sym_libs = self._exclude_project_local_libs(
            get_connected_sym_libs(
                self.project_path,
                include_project_tables=False,
                include_user_tables=False,
            ),
            kind="symbol",
        )
        fp_libs = self._exclude_project_local_libs(
            get_connected_fp_libs(
                self.project_path,
                include_project_tables=False,
                include_user_tables=False,
            ),
            kind="footprint",
        )
        self.log(
            f"KiCad builtin: connected symbol libs={len(sym_libs)}, footprint libs={len(fp_libs)}\n"
        )
        if not fp_libs:
            return None, None, None, None

        symbol_matching_enabled = self._kicad_symbol_matching_enabled(general)
        if not symbol_matching_enabled:
            self.log("KiCad builtin: symbol matching disabled by settings.\n")
        symbol_hit = (
            self._pick_symbol_block(component_kind, meta, general, sym_libs)
            if symbol_matching_enabled and sym_libs
            else None
        )
        explicit_fp_refs: List[str] = []

        for key in ("Footprint", "footprint", "Package", "package", "Case", "case"):
            value = str(attrs.get(key) or meta.get(key) or "").strip()
            if not value:
                continue
            for token in re.split(r"[,\n;/]+", value):
                token = token.strip()
                if not token:
                    continue
                if self._is_uuid_like(token):
                    continue
                if self._is_generic_pkg_token(token):
                    continue
                explicit_fp_refs.append(token)

        source_symbol_name = ""
        source_symbol_lib_path: Optional[Path] = None
        symbol_block = ""
        is_exact_symbol = False
        if symbol_hit is None:
            if symbol_matching_enabled and not sym_libs:
                self.log("KiCad builtin: no connected symbol libraries, skipping symbol match.\n")
            elif symbol_matching_enabled:
                self.log("KiCad builtin: no symbol match.\n")
        else:
            source_symbol_name, symbol_block, is_exact_symbol, source_symbol_lib_path = symbol_hit
            self.log(
                f"KiCad builtin: symbol match '{source_symbol_name}'"
                + (" (exact)\n" if is_exact_symbol else " (mapped)\n")
            )
            source_fp_ref = get_footprint_property(symbol_block) or ""
            if self._is_exact_fp_ref(source_fp_ref):
                explicit_fp_refs.insert(0, source_fp_ref)

        footprint_hit = self._pick_footprint(
            component_kind=component_kind,
            package_candidates=package_candidates,
            explicit_refs=explicit_fp_refs,
            general=general,
            fp_libs=fp_libs,
        )
        if footprint_hit is None:
            self.log("KiCad builtin: no footprint match.\n")
            return None, None, None, None

        src_alias, source_mod = footprint_hit
        source_fp_ref = f"KiCad:{src_alias}:{source_mod.stem}"
        self.log(f"KiCad builtin: footprint match '{src_alias}:{source_mod.stem}'.\n")
        footprint_dir.mkdir(parents=True, exist_ok=True)
        dest_mod = footprint_dir / source_mod.name
        try:
            same = source_mod.resolve() == dest_mod.resolve()
        except Exception:
            same = False
        if not same:
            if FootprintEditor.copy_preserving_models_if_missing(source_mod, dest_mod):
                self.log(f"KiCad builtin: preserved existing 3D model assignment for '{dest_mod.name}'.\n")
        if symbol_hit is None:
            self._apply_footprint_metadata(dest_mod, source_fp_ref)
            return None, dest_mod, None, source_fp_ref

        source_symbol_ref = ""
        if source_symbol_lib_path is not None:
            parent_chain = extract_parent_symbol_chain(source_symbol_lib_path, source_symbol_name)
            for parent_name, parent_block in parent_chain:
                add_or_replace_symbol(symbol_lib_path, parent_name, parent_block)
            source_alias = self._lib_alias_for_path(sym_libs, source_symbol_lib_path)
            if source_alias:
                source_symbol_ref = f"KiCad:{source_alias}:{source_symbol_name}"
        if not source_symbol_ref:
            source_symbol_ref = f"KiCad:{source_symbol_name}"

        symbol_name = self._target_symbol_name(meta, lcsc_id, source_symbol_name)
        symbol_block = rename_symbol_block(symbol_block, source_symbol_name, symbol_name)
        symbol_block = fix_symbol_label_positions(symbol_block)
        symbol_block = normalize_pin_names(symbol_block)
        easyeda_pin_map = self._pin_name_map_from_meta(meta)
        if easyeda_pin_map:
            patched_block, renamed_cnt, matched_cnt, total_pins = apply_pin_name_map(
                symbol_block,
                easyeda_pin_map,
            )
            min_required = 2 if total_pins <= 4 else max(3, int(total_pins * 0.5))
            if total_pins > 0 and matched_cnt >= min_required:
                symbol_block = patched_block
                self.log(
                    "KiCad builtin: pin labels patched from EasyEDA map "
                    f"({renamed_cnt} renamed, {matched_cnt}/{total_pins} matched by pin number).\n"
                )
            elif total_pins > 0:
                self.log(
                    "KiCad builtin: EasyEDA pin map coverage is too low, skip pin-label patch "
                    f"({matched_cnt}/{total_pins}).\n"
                )
        symbol_block = ensure_value_visible_in_symbol(symbol_block)
        symbol_block = set_footprint_property(symbol_block, f"{lib_name}:{dest_mod.stem}")
        add_or_replace_symbol(symbol_lib_path, symbol_name, symbol_block)

        try:
            content = dest_mod.read_text(encoding="utf-8", errors="replace")
            content = hide_value_in_footprint(content)
            dest_mod.write_text(content, encoding="utf-8")
        except Exception:
            pass
        self._apply_footprint_metadata(dest_mod, source_fp_ref)
        if is_exact_symbol:
            self.log(
                f"KiCad exact symbol match used: '{source_symbol_name}' with footprint '{source_mod.stem}'.\n"
            )
        return symbol_name, dest_mod, source_symbol_ref, source_fp_ref

    def _import_part_via_elibz(self, lcsc_id: str) -> Tuple[bool, Path]:
        general = self._general_settings()
        symbols_root, footprints_root, models_root, lib_name, lib_root = self._compute_outputs("")
        symbol_lib_path = symbols_root / f"{lib_name}.kicad_sym"
        footprint_dir = footprints_root / f"{lib_name}.pretty"
        models_dir = (
            models_root / f"{lib_name}.3dshapes"
            if self.is_system_scope
            else (models_root / "3dmodels")
        )
        symbol_name = lcsc_id
        try:
            self.log(f"Running KiCad import for {lcsc_id}\n")
            symbol_lib_path.parent.mkdir(parents=True, exist_ok=True)
            footprint_dir.mkdir(parents=True, exist_ok=True)
            models_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_symbol_lib(symbol_lib_path)

            elibz_importer = EasyedaProImporter(
                project_path=self.project_path,
                python_exe=self.python_exe,
                parent_window=self.parent_window,
                scope=self.scope,
            )
            # Fetch EasyEDA data first — used both for KiCad builtin meta enrichment
            # and as the source for EasyEDA conversion fallback.
            with tempfile.TemporaryDirectory(prefix="kicad_elibz_src_") as elibz_tmp:
                elibz_tmp_path = Path(elibz_tmp)
                temp_elibz = elibz_tmp_path / f"{sanitize_lib_name(lcsc_id)}.elibz"
                ok, elibz_path = elibz_importer.import_part(
                    lcsc_id=lcsc_id,
                    output_elibz_path=temp_elibz,
                    update_tables=False,
                    log_saved_path=False,
                    skip_models=True,
                )
                if not ok:
                    return False, lib_root

                payload = load_elibz_payload(elibz_path)
                device_entry = pick_device(payload, lcsc_id=lcsc_id)
                if device_entry is None:
                    raise ValueError("No matching device in ELIBZ")

                attrs = device_entry.get("attributes", {}) or {}
                model_title = str(attrs.get("3D Model Title") or "").strip()

                # Build enriched meta entirely from EasyEDA data.
                # Build a rich description that includes EasyEDA category tags so that
                # _component_kind can detect buzzers, connectors, etc. correctly even when
                # the human-readable LCSC Part Name is in Chinese or uses generic wording.
                tags_info = device_entry.get("tags") or {}
                tag_parts = []
                first_category = ""
                second_category = ""
                for tag_key in ("parent_tag", "child_tag"):
                    tag_node = tags_info.get(tag_key) or {}
                    tag_name = str(tag_node.get("name") or "").strip()
                    if tag_name:
                        tag_parts.append(tag_name)
                    if tag_key == "parent_tag":
                        first_category = tag_name
                    elif tag_key == "child_tag":
                        second_category = tag_name
                tags_text = " ".join(tag_parts)
                library_type = ""
                basic_flag = device_entry.get("is_basic")
                if basic_flag is None:
                    basic_flag = device_entry.get("isBasic")
                if isinstance(basic_flag, bool):
                    library_type = "Basic" if basic_flag else "Extended"
                else:
                    library_type = str(attrs.get("Library Type") or "").strip()
                description_text = " ".join(filter(None, [
                    str(device_entry.get("description") or "").strip()
                    or str(attrs.get("Description") or "").strip()
                    or str(attrs.get("LCSC Part Name") or "").strip(),
                    tags_text,
                ])).strip()
                enriched_meta = {
                    "lcsc_part": str(lcsc_id or "").strip().upper(),
                    "mfr_part": str(attrs.get("Manufacturer Part") or attrs.get("MFR.Part") or device_display_name(device_entry) or "").strip(),
                    "package": str(attrs.get("Supplier Footprint") or attrs.get("Package") or "").strip(),
                    "footprint_display": str((device_entry.get("footprint", {}) or {}).get("display_title") or "").strip(),
                    "footprint_title": str((device_entry.get("footprint", {}) or {}).get("title") or "").strip(),
                    "model_title": model_title,
                    "description": description_text,
                    "manufacturer": str(attrs.get("Manufacturer") or "").strip(),
                    "first_category": first_category,
                    "second_category": second_category,
                    "library_type": library_type,
                    "datasheet": str(device_entry.get("datasheet") or "").strip(),
                }
                attrs = self._canonicalize_easyeda_attrs_for_db(
                    attrs,
                    meta=enriched_meta,
                    lcsc_id=lcsc_id,
                )
                easyeda_pin_map = self._fetch_easyeda_pin_map(attrs, device_entry=device_entry)
                if easyeda_pin_map:
                    enriched_meta["easyeda_pin_map"] = easyeda_pin_map
                    enriched_meta["easyeda_pin_map_json"] = json.dumps(easyeda_pin_map, ensure_ascii=False)
                    self.log(
                        f"EasyEDA pin-map parsed: {len(easyeda_pin_map)} pins "
                        f"(symbol={attrs.get('Symbol') or ((device_entry.get('symbol') or {}).get('uuid') or '')}).\n"
                    )
                if not enriched_meta["mfr_part"]:
                    enriched_meta["mfr_part"] = str(attrs.get("MFR.Part") or "").strip()
                if not enriched_meta["package"]:
                    enriched_meta["package"] = str(attrs.get("Package") or "").strip()
                if not enriched_meta["manufacturer"]:
                    enriched_meta["manufacturer"] = str(attrs.get("Manufacturer") or "").strip()
                if not enriched_meta["description"]:
                    enriched_meta["description"] = str(attrs.get("Description") or "").strip()
                enriched_meta["attributes_json"] = json.dumps(attrs, ensure_ascii=False)
                symbol_name = enriched_meta["mfr_part"] or lcsc_id

                builtin_fp_path: Optional[Path] = None
                builtin_symbol_source_ref: Optional[str] = None
                builtin_fp_source_ref: Optional[str] = None
                builtin_matching_enabled = (
                    self._kicad_builtin_first_enabled(general)
                    and self._kicad_symbol_matching_enabled(general)
                )
                if builtin_matching_enabled:
                    (
                        symbol_name_builtin,
                        builtin_fp_path,
                        builtin_symbol_source_ref,
                        builtin_fp_source_ref,
                    ) = self._import_part_via_builtin_kicad(
                        lcsc_id=lcsc_id,
                        meta=enriched_meta,
                        general=general,
                        symbol_lib_path=symbol_lib_path,
                        footprint_dir=footprint_dir,
                        lib_name=lib_name,
                    )
                    if symbol_name_builtin:
                        if builtin_symbol_source_ref:
                            enriched_meta["source_symbol_ref"] = builtin_symbol_source_ref
                        if builtin_fp_source_ref:
                            enriched_meta["source_footprint_ref"] = builtin_fp_source_ref
                        self._apply_symbol_metadata(
                            symbol_lib_path=symbol_lib_path,
                            symbol_name=symbol_name_builtin,
                            meta=enriched_meta,
                            fallback_value=symbol_name_builtin,
                            lcsc_id=lcsc_id,
                        )
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
                        self.log(
                            f"KiCad builtin mapping matched: symbol '{symbol_name_builtin}', "
                            f"footprint from standard library.\n"
                        )
                        self.log(f"Generated KiCad libraries under: {lib_root}\n")
                        return True, lib_root
                    if builtin_fp_path is not None:
                        self.log(
                            f"KiCad builtin: symbol not matched, but footprint '{builtin_fp_path.stem}' was matched.\n"
                        )

                    self.log("KiCad builtin mapping not found, fallback to EasyEDA conversion.\n")
                elif self._kicad_builtin_first_enabled(general):
                    self.log(
                        "KiCad builtin matching disabled by settings; using EasyEDA conversion only.\n"
                    )

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
                    parent_chain = extract_parent_symbol_chain(converted_sym, source_symbol_name)

                    symbol_name = device_display_name(device_entry) or strip_lcsc_suffix(source_symbol_name)
                    symbol_name = symbol_name or source_symbol_name
                    enriched_meta["source_symbol_ref"] = f"EasyEDA:{source_symbol_name}"
                    symbol_block = rename_symbol_block(symbol_block, source_symbol_name, symbol_name)
                    symbol_block = fix_symbol_label_positions(symbol_block)
                    symbol_block = normalize_pin_names(symbol_block)
                    symbol_block = ensure_value_visible_in_symbol(symbol_block)
                    if builtin_fp_path is not None:
                        symbol_block = set_footprint_property(symbol_block, f"{lib_name}:{builtin_fp_path.stem}")
                    else:
                        symbol_block = patch_footprint_property(symbol_block, lib_name)
                    for parent_name, parent_block in parent_chain:
                        parent_block = normalize_pin_names(parent_block)
                        add_or_replace_symbol(symbol_lib_path, parent_name, parent_block)
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
                    if builtin_fp_path is not None and builtin_fp_path.exists():
                        footprint_path = builtin_fp_path
                        self.log(
                            f"KiCad fallback: reusing builtin-matched footprint '{builtin_fp_path.stem}'.\n"
                        )
                    else:
                        for fp_name in fp_name_candidates:
                            src_mod = resolve_footprint_mod_path(converted_pretty, fp_name)
                            if src_mod is None:
                                continue
                            footprint_path = footprint_dir / src_mod.name
                            if FootprintEditor.copy_preserving_models_if_missing(src_mod, footprint_path):
                                self.log(f"KiCad fallback: preserved existing 3D model assignment for '{footprint_path.name}'.\n")
                            break
                        if footprint_path is None:
                            mods = list(converted_pretty.glob("*.kicad_mod"))
                            if mods:
                                footprint_path = footprint_dir / mods[0].name
                                if FootprintEditor.copy_preserving_models_if_missing(mods[0], footprint_path):
                                    self.log(f"KiCad fallback: preserved existing 3D model assignment for '{footprint_path.name}'.\n")
                            else:
                                self.log("Warning: no footprint found in converted ELIBZ output.\n")

                    if builtin_fp_path is not None and builtin_fp_path.exists():
                        enriched_meta["source_footprint_ref"] = (
                            builtin_fp_source_ref or f"KiCad:{builtin_fp_path.stem}"
                        )
                    elif footprint_path is not None:
                        enriched_meta["source_footprint_ref"] = f"EasyEDA:{footprint_path.stem}"

                    # kicad-cli writes ${KIPRJMOD}/EASYEDA_MODELS/<model>.step;
                    # rewrite to our configured models directory.
                    # Also hide the Value property (KiCad convention: Value is hidden on PCB).
                    if footprint_path and footprint_path.exists():
                        model_3d_path = self._model_3d_path(models_dir)
                        try:
                            content = footprint_path.read_text(encoding="utf-8", errors="replace")
                            content = re.sub(
                                r'\$\{KIPRJMOD\}/EASYEDA_MODELS/',
                                model_3d_path.rstrip("/") + "/",
                                content,
                            )
                            content = hide_value_in_footprint(content)
                            footprint_path.write_text(content, encoding="utf-8")
                        except Exception:
                            pass
                    self._apply_footprint_metadata(
                        footprint_path,
                        str(enriched_meta.get("source_footprint_ref") or ""),
                    )

                if not self.is_system_scope:
                    step_copied = self._copy_elibz_step_model(model_title, elibz_path, models_dir)
                    if not step_copied:
                        # ComponentLoader sometimes skips a model download; fall back to a
                        # direct LCSC API download using the model UUID from device.json.
                        self._download_3d_model_fallback(attrs, model_title, models_dir)
                    self._normalize_footprint_model_paths(footprint_dir, models_dir)

            self._apply_symbol_metadata(
                symbol_lib_path=symbol_lib_path,
                symbol_name=symbol_name,
                meta=enriched_meta,
                fallback_value=symbol_name,
                lcsc_id=lcsc_id,
            )

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
            base_path = resolve_system_library_root(PLUGIN_PATH)

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
            lib_artifacts_root = lib_root / lib_name
            lib_artifacts_root.mkdir(parents=True, exist_ok=True)
            symbols_root = footprints_root = models_root = lib_artifacts_root

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

    @staticmethod
    def _normalize_attr_key(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower())

    @classmethod
    def _first_attr_value(cls, attrs: Dict[str, str], alias_names: Iterable[str]) -> str:
        if not attrs:
            return ""
        by_norm = {
            cls._normalize_attr_key(k): str(v or "").strip()
            for k, v in attrs.items()
            if str(k or "").strip()
        }
        for alias in alias_names:
            norm = cls._normalize_attr_key(alias)
            if not norm:
                continue
            val = str(by_norm.get(norm) or "").strip()
            if val:
                return val
        return ""

    @classmethod
    def _canonicalize_easyeda_attrs_for_db(
        cls,
        attrs: Dict[str, str],
        meta: Optional[Dict] = None,
        lcsc_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Normalize EasyEDA attributes to canonical keys used in parts DB.
        """
        out: Dict[str, str] = {}
        for k, v in (attrs or {}).items():
            key = str(k or "").strip()
            val = str(v or "").strip()
            if key and val:
                out[key] = val

        meta = meta or {}

        def _set_if_value(name: str, value: str) -> None:
            text = str(value or "").strip()
            if text:
                out[name] = text

        for canon in cls._DB_CANON_KEYS:
            value = cls._first_attr_value(out, cls._DB_CANON_ALIASES.get(canon, (canon,)))
            if value:
                out[canon] = value

        lcsc_val = str(lcsc_id or meta.get("lcsc_part") or out.get("LCSC Part") or "").strip().upper()
        if re.match(r"^C\d+$", lcsc_val):
            out["LCSC Part"] = lcsc_val
            out.setdefault("Supplier Part", lcsc_val)

        _set_if_value("MFR.Part", str(meta.get("mfr_part") or out.get("MFR.Part") or ""))
        _set_if_value("Package", str(meta.get("package") or out.get("Package") or ""))
        _set_if_value("Manufacturer", str(meta.get("manufacturer") or out.get("Manufacturer") or ""))
        _set_if_value("Description", str(meta.get("description") or out.get("Description") or ""))
        _set_if_value("First Category", str(meta.get("first_category") or out.get("First Category") or ""))
        _set_if_value("Second Category", str(meta.get("second_category") or out.get("Second Category") or ""))
        _set_if_value("Datasheet", str(meta.get("datasheet") or out.get("Datasheet") or ""))
        _set_if_value("Library Type", str(meta.get("library_type") or out.get("Library Type") or ""))

        if out.get("MFR.Part") and not out.get("Manufacturer Part"):
            out["Manufacturer Part"] = out["MFR.Part"]

        return out

    @classmethod
    def _sanitize_symbol_attrs(cls, attrs: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        seen_norm: set[str] = set()
        for key, value in (attrs or {}).items():
            k = str(key or "").strip()
            v = str(value or "").strip()
            if not k or not v:
                continue
            norm = cls._normalize_attr_key(k)
            if not norm:
                continue
            if norm in cls._SYMBOL_ATTR_DROP_KEYS:
                continue
            if footprint_matcher.is_uuid_like(v) and any(
                tok in norm for tok in ("symbol", "footprint", "model")
            ):
                continue
            if norm in {"kidescription", "lcscdescription", "partdescription"}:
                continue
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            out[k] = v
        return out

    def _apply_symbol_metadata(
        self,
        symbol_lib_path: Path,
        symbol_name: str,
        meta: Dict,
        fallback_value: Optional[str] = None,
        lcsc_id: Optional[str] = None,
    ) -> None:
        try:
            attrs = self._sanitize_symbol_attrs(
                self._parse_attributes_map(str(meta.get("attributes_json") or ""))
            )
            attrs = self._canonicalize_easyeda_attrs_for_db(
                attrs,
                meta=meta,
                lcsc_id=lcsc_id,
            )
            mfr_part = str(meta.get("mfr_part") or "").strip()
            manufacturer = str(meta.get("manufacturer") or "").strip()
            description = str(meta.get("description") or "").strip()
            source_symbol_ref = str(meta.get("source_symbol_ref") or "").strip()
            # Derive component kind from EasyEDA data for correct Value assignment
            component_kind = self._component_kind("", description, attrs)
            if lcsc_id:
                attrs["LCSC Part"] = lcsc_id
            if manufacturer:
                attrs.setdefault("Manufacturer", manufacturer)
            if description:
                attrs["ki_description"] = description

            editor = SymbolEditor(
                sym_path=symbol_lib_path,
                symbol_id=symbol_name,
                parent_window=self.parent_window,
            )
            changed = bool(editor.remove_properties(list(self._SYMBOL_PURGE_PROPS)))
            changed = bool(
                editor.update_value_from_attributes(
                    category=component_kind,
                    attrs=attrs,
                    mfr_part=mfr_part,
                    fallback_value=fallback_value,
                )
            ) or changed
            source_props: Dict[str, str] = {}
            if source_symbol_ref:
                source_props[self._SOURCE_SYMBOL_PROP] = source_symbol_ref
            if source_props:
                changed = bool(
                    editor.apply_properties(
                        source_props,
                        update_empty_only=False,
                        hidden=True,
                        exclude_equal_to_value=False,
                    )
                ) or changed
            if lcsc_id:
                id_value = str(lcsc_id).strip().upper()
                if re.match(r"^C\d+$", id_value):
                    changed = bool(
                        editor.apply_properties(
                            {
                                "LCSC Part": id_value,
                                "Supplier Part": id_value,
                            },
                            update_empty_only=False,
                            hidden=True,
                            exclude_equal_to_value=False,
                        )
                    ) or changed
            if changed:
                editor.save(strip_ids=True)
        except Exception as exc:
            import traceback
            self.log(f"Symbol metadata update failed: {exc}\n{traceback.format_exc()}\n")

    def _apply_footprint_metadata(
        self,
        footprint_path: Optional[Path],
        source_footprint_ref: Optional[str],
    ) -> None:
        path = Path(footprint_path) if footprint_path else None
        source_ref = str(source_footprint_ref or "").strip()
        if not path or not path.exists() or not source_ref:
            return
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            updated = set_footprint_meta_property(
                content,
                self._SOURCE_FOOTPRINT_PROP,
                source_ref,
                layer="Cmts.User",
            )
            if updated != content:
                path.write_text(updated, encoding="utf-8")
        except Exception as exc:
            self.log(f"Footprint metadata update failed ({path.name}): {exc}\n")

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
            "  (generator kicad_symbol_editor)\n"
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
