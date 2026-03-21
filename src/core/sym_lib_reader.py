"""Utilities for reading KiCad library tables and symbol libraries."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# KiCad path variable resolution
# ---------------------------------------------------------------------------


def _kicad_dirs(kind: str) -> List[str]:
    """Return candidate paths for built-in KiCad symbol or footprint libraries."""
    paths: List[str] = []
    for ver in ("9", "8", "7", "6"):
        env = os.environ.get(f"KICAD{ver}_{kind}_DIR")
        if env:
            paths.append(env)
    if kind == "SYMBOL":
        paths += [
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
            "/usr/share/kicad/symbols",
            "/usr/local/share/kicad/symbols",
            r"C:\Program Files\KiCad\9.0\share\kicad\symbols",
            r"C:\Program Files\KiCad\8.0\share\kicad\symbols",
        ]
    else:
        paths += [
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
            "/usr/share/kicad/footprints",
            "/usr/local/share/kicad/footprints",
            r"C:\Program Files\KiCad\9.0\share\kicad\footprints",
            r"C:\Program Files\KiCad\8.0\share\kicad\footprints",
        ]
    return [p for p in paths if os.path.isdir(p)]


def resolve_uri(uri: str, project_path: Optional[Path] = None, table_dir: Optional[Path] = None) -> str:
    """Substitute KiCad path variables in a library URI."""
    result = uri
    if project_path is not None:
        result = result.replace("${KIPRJMOD}", str(project_path))
    sym_dirs = _kicad_dirs("SYMBOL")
    fp_dirs = _kicad_dirs("FOOTPRINT")
    for ver in ("9", "8", "7", "6"):
        if sym_dirs:
            result = result.replace(f"${{KICAD{ver}_SYMBOL_DIR}}", sym_dirs[0])
        if fp_dirs:
            result = result.replace(f"${{KICAD{ver}_FOOTPRINT_DIR}}", fp_dirs[0])
    p = Path(result)
    if not p.is_absolute() and table_dir is not None:
        p = (Path(table_dir) / p).resolve()
        return p.as_posix()
    return result


def get_user_settings_path() -> Optional[Path]:
    """Return KiCad user settings directory, or None if unavailable."""
    try:
        import pcbnew  # type: ignore

        p = pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
        if p:
            return Path(p)
    except Exception:
        pass

    home = Path.home()
    for ver in ("9.0", "8.0", "7.0", "6.0"):
        for base in [
            home / ".config" / "kicad" / ver,
            home / "Library" / "Preferences" / "kicad" / ver,
            Path(os.environ.get("APPDATA", "X")) / "kicad" / ver,
        ]:
            if base.exists():
                return base
    return None


# ---------------------------------------------------------------------------
# Library table parsing
# ---------------------------------------------------------------------------


def parse_lib_table(path: Path) -> List[Tuple[str, str, str]]:
    """Parse a KiCad sym-lib-table or fp-lib-table into (name, type, uri)."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    entries: List[Tuple[str, str, str]] = []
    i = 0
    length = len(content)
    while i < length:
        if content[i] == "(" and content[i + 1 : i + 5] == "lib ":
            start = i
            depth = 0
            j = i
            while j < length:
                if content[j] == "(":
                    depth += 1
                elif content[j] == ")":
                    depth -= 1
                    if depth == 0:
                        block = content[start : j + 1]
                        nm = re.search(r'\(name\s+"([^"]+)"\)', block)
                        tp = re.search(r'\(type\s+"([^"]+)"\)', block)
                        ur = re.search(r'\(uri\s+"([^"]+)"\)', block)
                        if nm and tp and ur:
                            entries.append((nm.group(1), tp.group(1), ur.group(1)))
                        i = j
                        break
                j += 1
        i += 1
    return entries


def get_connected_sym_libs(project_path: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """Return connected KiCad symbol libraries as (name, resolved_path)."""
    table_paths: List[Path] = []
    if project_path is not None:
        t = project_path / "sym-lib-table"
        if t.exists():
            table_paths.append(t)
    user = get_user_settings_path()
    if user:
        t = user / "sym-lib-table"
        if t.exists():
            table_paths.append(t)

    seen: set[str] = set()
    result: List[Tuple[str, Path]] = []
    for tbl in table_paths:
        for name, lib_type, uri in parse_lib_table(tbl):
            if lib_type not in ("KiCad", "KiCad_Sym"):
                continue
            if name in seen:
                continue
            p = Path(resolve_uri(uri, project_path, table_dir=tbl.parent))
            if p.exists():
                seen.add(name)
                result.append((name, p))

    # Fallback to built-ins so panel still works with no project entries.
    for sym_dir in _kicad_dirs("SYMBOL"):
        for sym_file in sorted(Path(sym_dir).glob("*.kicad_sym")):
            name = sym_file.stem
            if name not in seen:
                seen.add(name)
                result.append((name, sym_file))

    return result


def get_connected_fp_libs(project_path: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """Return connected KiCad footprint libraries as (name, resolved_path)."""
    table_paths: List[Path] = []
    if project_path is not None:
        t = project_path / "fp-lib-table"
        if t.exists():
            table_paths.append(t)
    user = get_user_settings_path()
    if user:
        t = user / "fp-lib-table"
        if t.exists():
            table_paths.append(t)

    seen: set[str] = set()
    result: List[Tuple[str, Path]] = []
    for tbl in table_paths:
        for name, lib_type, uri in parse_lib_table(tbl):
            if lib_type not in ("KiCad", "KiCad_Fp"):
                continue
            if name in seen:
                continue
            p = Path(resolve_uri(uri, project_path, table_dir=tbl.parent))
            if p.exists():
                seen.add(name)
                result.append((name, p))
    return result


def get_connected_elibz_libs(project_path: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """Return connected EasyEDA Pro symbol libraries as (name, resolved_path)."""
    table_paths: List[Path] = []
    if project_path is not None:
        t = project_path / "sym-lib-table"
        if t.exists():
            table_paths.append(t)
    user = get_user_settings_path()
    if user:
        t = user / "sym-lib-table"
        if t.exists():
            table_paths.append(t)

    seen: set[str] = set()
    result: List[Tuple[str, Path]] = []
    for tbl in table_paths:
        for name, lib_type, uri in parse_lib_table(tbl):
            if lib_type not in ("EasyEDA (JLCEDA) Pro", "EasyEDA_Pro", "EasyEDA / JLCEDA Pro"):
                continue
            if name in seen:
                continue
            p = Path(resolve_uri(uri, project_path, table_dir=tbl.parent))
            if p.exists():
                seen.add(name)
                result.append((name, p))
    return result


# ---------------------------------------------------------------------------
# .kicad_sym reading/writing helpers
# ---------------------------------------------------------------------------


def list_symbols_kicad(lib_path: Path) -> List[Tuple[str, str]]:
    """Return (name, description) list for top-level symbols in a .kicad_sym file."""
    try:
        content = lib_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return _list_symbols_from_content(content)


def list_symbols_kicad_meta(lib_path: Path) -> List[Tuple[str, str, str]]:
    """Return (name, description, footprint_ref) for top-level symbols in a .kicad_sym file."""
    try:
        content = lib_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return _list_symbols_meta_from_content(content)


def _list_symbols_from_content(content: str) -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []
    for name, desc, _fp_ref in _list_symbols_meta_from_content(content):
        results.append((name, desc))
    return results


def _list_symbols_meta_from_content(content: str) -> List[Tuple[str, str, str]]:
    results: List[Tuple[str, str, str]] = []
    depth = 0
    i = 0
    length = len(content)
    while i < length:
        c = content[i]
        if c == "(":
            depth += 1
            if depth == 2:
                m = re.match(r'\(symbol\s+"([^"]+)"', content[i:])
                if m:
                    name = m.group(1)
                    start = i
                    bd = 0
                    j = i
                    while j < length:
                        if content[j] == "(":
                            bd += 1
                        elif content[j] == ")":
                            bd -= 1
                            if bd == 0:
                                block = content[start : j + 1]
                                dm = re.search(r'\(property\s+"Description"\s+"([^"]*)"', block)
                                fm = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', block)
                                results.append((name, dm.group(1) if dm else "", fm.group(1) if fm else ""))
                                i = j - 1
                                break
                        j += 1
        elif c == ")":
            depth -= 1
        i += 1
    return results


def extract_symbol_block(lib_path: Path, symbol_name: str) -> Optional[str]:
    """Extract complete (symbol ...) block from a .kicad_sym file by symbol name."""
    try:
        content = lib_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return _extract_from_content(content, symbol_name)


def _extract_from_content(content: str, symbol_name: str) -> Optional[str]:
    target = f'(symbol "{symbol_name}"'
    depth = 0
    i = 0
    length = len(content)
    while i < length:
        c = content[i]
        if c == "(":
            depth += 1
            if depth == 2 and content[i:].startswith(target):
                start = i
                bd = 0
                j = i
                while j < length:
                    if content[j] == "(":
                        bd += 1
                    elif content[j] == ")":
                        bd -= 1
                        if bd == 0:
                            return content[start : j + 1]
                    j += 1
        elif c == ")":
            depth -= 1
        i += 1
    return None


def get_footprint_property(symbol_content: str) -> Optional[str]:
    """Return Footprint property value from symbol block."""
    m = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', symbol_content)
    return m.group(1) if m and m.group(1) else None


def patch_footprint_property(symbol_content: str, new_lib_name: str) -> str:
    """Rewrite Footprint property to point to another footprint library nickname."""

    def _replace(m: re.Match) -> str:
        old_ref = m.group(1)
        new_ref = f"{new_lib_name}:{old_ref.split(':', 1)[1]}" if ":" in old_ref else old_ref
        return m.group(0).replace(f'"{old_ref}"', f'"{new_ref}"', 1)

    return re.sub(r'\(property\s+"Footprint"\s+"([^"]*)"', _replace, symbol_content)


_KICAD_SYM_HEADER = (
    "(kicad_symbol_lib\n"
    "  (version 20211014)\n"
    "  (generator kicad_symbol_editor)\n"
    ")\n"
)


def ensure_kicad_sym_lib(path: Path) -> None:
    """Create an empty .kicad_sym file if missing."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_KICAD_SYM_HEADER, encoding="utf-8")


def add_or_replace_symbol(lib_path: Path, symbol_name: str, symbol_block: str) -> None:
    """Add or replace symbol block in destination .kicad_sym file."""
    ensure_kicad_sym_lib(lib_path)
    content = lib_path.read_text(encoding="utf-8")
    existing = _extract_from_content(content, symbol_name)
    if existing:
        content = content.replace(existing, symbol_block, 1)
    else:
        idx = content.rfind(")")
        indent = "  "
        if idx >= 0:
            content = content[:idx] + f"\n{indent}{symbol_block}\n" + content[idx:]
        else:
            content += f"\n{indent}{symbol_block}\n"
    lib_path.write_text(content, encoding="utf-8")


def copy_footprint_to_pretty(
    fp_ref: str,
    dest_pretty: Path,
    fp_libs: List[Tuple[str, Path]],
) -> bool:
    """Copy footprint referenced as LibName:FootprintName into destination .pretty."""
    if ":" not in fp_ref:
        return False
    lib_name, fp_name = fp_ref.split(":", 1)
    for name, lib_path in fp_libs:
        if name != lib_name:
            continue
        src = lib_path / f"{fp_name}.kicad_mod"
        if not src.exists():
            continue
        dest_pretty.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_pretty / f"{fp_name}.kicad_mod")
        return True
    return False


# ---------------------------------------------------------------------------
# EasyEDA Pro helpers
# ---------------------------------------------------------------------------


def list_symbols_elibz(lib_path: Path) -> List[Tuple[str, str]]:
    """Return (display_name, description) list from .elibz device.json."""
    rows = list_symbols_elibz_meta(lib_path)
    return [(name, desc) for name, desc, _footprint, _has_3d in rows]


def list_symbols_elibz_meta(lib_path: Path) -> List[Tuple[str, str, str, bool]]:
    """Return (display_name, description, footprint_name, has_3d_model) from .elibz device.json."""
    import json
    import zipfile

    try:
        with zipfile.ZipFile(lib_path, "r") as zf:
            data = json.loads(zf.read("device.json").decode("utf-8"))
        result: List[Tuple[str, str, str, bool]] = []
        for _uuid, dev in data.get("devices", {}).items():
            name = (
                dev.get("display_title")
                or dev.get("title")
                or dev.get("name")
                or dev.get("product_code")
                or _uuid
            ).strip()
            desc = (dev.get("description") or "").strip()
            attrs = dev.get("attributes", {}) or {}
            footprint = str(attrs.get("Footprint") or "").strip()
            has_3d = bool(
                str(attrs.get("3D Model Title") or "").strip()
                or str(attrs.get("3D Model") or "").strip()
                or str(attrs.get("3DModel") or "").strip()
            )
            result.append((name, desc, footprint, has_3d))
        return sorted(result, key=lambda t: t[0].lower())
    except Exception:
        return []
