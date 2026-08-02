"""Project library tables manager for KiCad (sym-lib-table / fp-lib-table)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .sym_lib_reader import resolve_uri


class LibTablesManager:
    """Encapsulates updates to project sym-lib-table and fp-lib-table."""

    def __init__(self, project_dir: Path, log: Callable[[str], None] | None = None):
        self.project_dir = Path(project_dir)
        self._log = log or (lambda _m: None)

    # --------------- helpers ---------------
    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _init_table(kind: str) -> str:
        if kind == "sym":
            return "(sym_lib_table\n  (version 7))\n"
        return "(fp_lib_table\n  (version 7))\n"

    def _ensure_entry(
        self,
        tbl_path: Path,
        tbl_kind: str,
        name: str,
        uri: str,
        lib_type: str = "KiCad",
    ) -> bool:
        entry = (
            f"  (lib (name \"{name}\")(type \"{lib_type}\")(uri \"{uri}\")(options \"\")(descr \"\"))\n"
        )
        content = self._read(tbl_path)
        if not content:
            content = self._init_table(tbl_kind)
        # Skip if already present (by uri or name)
        if f"(uri \"{uri}\")" in content or f"(name \"{name}\")" in content:
            return False
        stripped = content.rstrip()
        idx = stripped.rfind(")")
        if idx == -1:
            stripped = self._init_table(tbl_kind).rstrip()
            idx = stripped.rfind(")")
        new_content = stripped[:idx] + entry + stripped[idx:] + "\n"
        self._write(tbl_path, new_content)
        return True

    @staticmethod
    def _unique_name(base: str, existing: str) -> str:
        if existing and f"(name \"{base}\")" in existing:
            i = 1
            while f"(name \"{base}_{i}\")" in existing:
                i += 1
            return f"{base}_{i}"
        return base

    @staticmethod
    def _iter_lib_blocks(content: str) -> List[Tuple[int, int, str]]:
        blocks: List[Tuple[int, int, str]] = []
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
                            blocks.append((start, j + 1, content[start : j + 1]))
                            i = j
                            break
                    j += 1
            i += 1
        return blocks

    @staticmethod
    def _parse_lib_fields(block: str) -> Optional[Tuple[str, str, str]]:
        nm = re.search(r'\(name\s+"([^"]+)"\)', block)
        tp = re.search(r'\(type\s+"([^"]+)"\)', block)
        ur = re.search(r'\(uri\s+"([^"]+)"\)', block)
        if nm and tp and ur:
            return nm.group(1), tp.group(1), ur.group(1)
        return None

    @staticmethod
    def _has_uri_scheme(value: str) -> bool:
        return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", str(value or "").strip()))

    @staticmethod
    def _contains_unresolved_var(uri: str) -> bool:
        return "${" in uri or "$ENV{" in uri or "$(" in uri

    def _uri_points_to_existing_path(
        self,
        uri: str,
        table_dir: Path,
        project_path: Optional[Path],
    ) -> bool:
        try:
            resolved = resolve_uri(uri, project_path=project_path, table_dir=table_dir)
        except Exception:
            resolved = uri

        if self._has_uri_scheme(resolved):
            return True
        if self._contains_unresolved_var(resolved):
            return True

        p = Path(resolved)
        if not p.is_absolute():
            p = (table_dir / p).resolve()
        return p.exists()

    def _prune_invalid_entries_from_table(
        self,
        table_path: Path,
        table_kind: str,
        project_path: Optional[Path],
    ) -> int:
        content = self._read(table_path)
        if not content:
            return 0

        blocks = self._iter_lib_blocks(content)
        if not blocks:
            return 0

        kept_parts: List[str] = []
        cursor = 0
        removed = 0
        for start, end, block in blocks:
            keep = True
            fields = self._parse_lib_fields(block)
            if fields is not None:
                name, _lib_type, uri = fields
                if not self._uri_points_to_existing_path(
                    uri,
                    table_dir=table_path.parent,
                    project_path=project_path,
                ):
                    keep = False
                    removed += 1
                    self._log(f"Removed invalid {table_kind} entry: {name} -> {uri}\n")

            if keep:
                kept_parts.append(content[cursor:end])
            else:
                kept_parts.append(content[cursor:start])
            cursor = end

        kept_parts.append(content[cursor:])
        if removed > 0:
            self._write(table_path, "".join(kept_parts))
        return removed

    def _uri_points_under(
        self,
        uri: str,
        root: Path,
        table_dir: Path,
        project_path: Optional[Path],
    ) -> bool:
        try:
            resolved = resolve_uri(uri, project_path=project_path, table_dir=table_dir)
        except Exception:
            resolved = uri
        if self._has_uri_scheme(resolved) or self._contains_unresolved_var(resolved):
            return False
        try:
            p = Path(resolved)
            if not p.is_absolute():
                p = (table_dir / p).resolve()
            else:
                p = p.resolve()
            p.relative_to(root.resolve())
            return True
        except Exception:
            return False

    def _resolved_uri_path(
        self,
        uri: str,
        table_dir: Path,
        project_path: Optional[Path],
    ) -> Optional[Path]:
        try:
            resolved = resolve_uri(uri, project_path=project_path, table_dir=table_dir)
        except Exception:
            resolved = uri
        if self._has_uri_scheme(resolved) or self._contains_unresolved_var(resolved):
            return None
        try:
            p = Path(resolved)
            if not p.is_absolute():
                p = (table_dir / p).resolve()
            else:
                p = p.resolve()
            return p
        except Exception:
            return None

    def _remove_entries_from_table(
        self,
        table_path: Path,
        table_kind: str,
        predicate: Callable[[str, str, str], bool],
    ) -> int:
        content = self._read(table_path)
        if not content:
            return 0
        blocks = self._iter_lib_blocks(content)
        if not blocks:
            return 0

        kept_parts: List[str] = []
        cursor = 0
        removed = 0
        for start, end, block in blocks:
            remove = False
            fields = self._parse_lib_fields(block)
            if fields is not None:
                name, lib_type, uri = fields
                remove = predicate(name, lib_type, uri)
                if remove:
                    removed += 1
                    self._log(f"Removed stale {table_kind} entry: {name} -> {uri}\n")

            kept_parts.append(content[cursor:start if remove else end])
            cursor = end

        kept_parts.append(content[cursor:])
        if removed > 0:
            self._write(table_path, "".join(kept_parts))
        return removed

    # --------------- public API ---------------
    def ensure_project_lib_tables(
        self,
        out_dir: Path,
        use_project_relative: bool = True,
        uri_prefix: str | None = None,
        use_lib_relative_uri: bool = False,
        lib_format: str | None = None,
    ) -> Tuple[List[Path], List[Path], Path]:
        """Discover generated libraries under out_dir and ensure project lib tables reference them.

        When *uri_prefix* is given (e.g. ``${KIPRJMOD}/../library``) it is used
        as the base for all URIs, preserving the KiCad variable so paths survive
        a git checkout on any machine.  *use_project_relative* is ignored in
        that case.

        Returns (sym_files, pretty_dirs, lib_base)
        """
        project_dir = self.project_dir
        sym_tbl = project_dir / "sym-lib-table"
        fp_tbl = project_dir / "fp-lib-table"

        lib_dir = Path(out_dir)
        fmt = str(lib_format or "all").strip().lower()
        include_kicad = fmt not in ("easyeda_pro", "easyeda", "elibz")
        include_elibz = fmt not in ("kicad", "kicad_native")

        sym_files = sorted(lib_dir.rglob("*.kicad_sym")) if include_kicad else []
        pretty_dirs = sorted(p for p in lib_dir.rglob("*.pretty") if p.is_dir()) if include_kicad else []
        elibz_files = sorted(lib_dir.rglob("*.elibz")) if include_elibz else []
        lib_base = lib_dir

        nested_sym_names = {p.stem for p in sym_files if p.parent.parent == lib_dir and p.parent.name == p.stem}
        nested_pretty_names = {p.stem for p in pretty_dirs if p.parent == lib_dir / p.stem}
        nested_elibz_names = {p.stem for p in elibz_files if p.parent.parent == lib_dir and p.parent.name == p.stem}
        superseded_direct_paths = {
            *(p.resolve() for p in sym_files if p.parent == lib_dir and p.stem in nested_sym_names),
            *(p.resolve() for p in pretty_dirs if p.parent == lib_dir and p.stem in nested_pretty_names),
            *(p.resolve() for p in elibz_files if p.parent == lib_dir and p.stem in nested_elibz_names),
        }
        if superseded_direct_paths:
            sym_files = [p for p in sym_files if p.resolve() not in superseded_direct_paths]
            pretty_dirs = [p for p in pretty_dirs if p.resolve() not in superseded_direct_paths]
            elibz_files = [p for p in elibz_files if p.resolve() not in superseded_direct_paths]

        self._log(
            f"Updating library tables in: {project_dir}\n"
            f"Found symbols: {len(sym_files)}; footprint libs: {len(pretty_dirs)}; elibz: {len(elibz_files)}\n"
        )

        def _is_managed_uri(uri: str) -> bool:
            return self._uri_points_under(
                uri,
                root=lib_dir,
                table_dir=project_dir,
                project_path=project_dir,
            )

        def _is_superseded_direct_uri(uri: str) -> bool:
            resolved = self._resolved_uri_path(uri, table_dir=project_dir, project_path=project_dir)
            return resolved in superseded_direct_paths if resolved is not None else False

        if superseded_direct_paths:
            self._remove_entries_from_table(
                sym_tbl,
                "sym-lib-table",
                lambda _name, _lib_type, uri: _is_superseded_direct_uri(uri),
            )
            self._remove_entries_from_table(
                fp_tbl,
                "fp-lib-table",
                lambda _name, _lib_type, uri: _is_superseded_direct_uri(uri),
            )

        if not include_elibz:
            self._remove_entries_from_table(
                sym_tbl,
                "sym-lib-table",
                lambda _name, lib_type, uri: _is_managed_uri(uri)
                and (uri.lower().endswith(".elibz") or lib_type in ("EasyEDA (JLCEDA) Pro", "EasyEDA_Pro", "EasyEDA / JLCEDA Pro")),
            )
            self._remove_entries_from_table(
                fp_tbl,
                "fp-lib-table",
                lambda _name, lib_type, uri: _is_managed_uri(uri)
                and (uri.lower().endswith(".elibz") or lib_type in ("EasyEDA (JLCEDA) Pro", "EasyEDA_Pro", "EasyEDA / JLCEDA Pro")),
            )
        if not include_kicad:
            self._remove_entries_from_table(
                sym_tbl,
                "sym-lib-table",
                lambda _name, lib_type, uri: _is_managed_uri(uri)
                and (uri.lower().endswith(".kicad_sym") or lib_type in ("KiCad", "KiCad_Sym")),
            )
            self._remove_entries_from_table(
                fp_tbl,
                "fp-lib-table",
                lambda _name, lib_type, uri: _is_managed_uri(uri)
                and (uri.lower().endswith(".pretty") or lib_type in ("KiCad", "KiCad_Fp")),
            )

        sym_content = self._read(sym_tbl)
        fp_content = self._read(fp_tbl)

        changes = 0

        def _uri(path: Path) -> str:
            if use_lib_relative_uri:
                rel = path.resolve().relative_to(lib_dir.resolve()).as_posix()
                return rel
            if uri_prefix is not None:
                rel = path.resolve().relative_to(lib_dir.resolve()).as_posix()
                return f"{uri_prefix.rstrip('/')}/{rel}"
            if use_project_relative:
                try:
                    rel = path.resolve().relative_to(project_dir.resolve()).as_posix()
                    return f"${{KIPRJMOD}}/{rel}"
                except Exception:
                    pass
            return path.resolve().as_posix()

        # Add symbol libraries
        for sym in sym_files:
            uri = _uri(sym)
            name = self._unique_name(sym.stem, sym_content)
            if self._ensure_entry(sym_tbl, "sym", name, uri):
                self._log(f"Added symbol lib: {name} -> {uri}\n")
                sym_content = self._read(sym_tbl)
                changes += 1

        # Add footprint libraries
        for pd in pretty_dirs:
            uri = _uri(pd)
            name = self._unique_name(pd.stem, fp_content)
            if self._ensure_entry(fp_tbl, "fp", name, uri):
                self._log(f"Added footprint lib: {name} -> {uri}\n")
                fp_content = self._read(fp_tbl)
                changes += 1

        for elibz in elibz_files:
            uri = _uri(elibz)
            elibz_name = f"{elibz.stem} (elibz)"

            sym_name = self._unique_name(elibz_name, sym_content)
            if self._ensure_entry(sym_tbl, "sym", sym_name, uri, lib_type="EasyEDA (JLCEDA) Pro"):
                self._log(f"Added elibz sym lib: {sym_name} -> {uri}\n")
                sym_content = self._read(sym_tbl)
                changes += 1

            fp_name = self._unique_name(elibz_name, fp_content)
            if self._ensure_entry(fp_tbl, "fp", fp_name, uri, lib_type="EasyEDA / JLCEDA Pro"):
                self._log(f"Added elibz fp lib: {fp_name} -> {uri}\n")
                fp_content = self._read(fp_tbl)
                changes += 1

        if changes == 0:
            if not sym_files and not pretty_dirs and not elibz_files:
                self._log(
                    "No new libraries found to add. "
                    "Ensure generated *.kicad_sym, *.pretty folders, or *.elibz files exist in the specified directory.\n"
                )
            else:
                self._log(
                    "Libraries are already present in sym-lib-table / fp-lib-table. No changes needed.\n"
                )

        return sym_files, pretty_dirs, lib_base

    def prune_invalid_table_paths(
        self,
        project_path: Optional[Path] = None,
    ) -> Tuple[int, int]:
        """Remove table entries that point to missing filesystem paths.

        Returns ``(removed_sym_entries, removed_fp_entries)``.
        """
        sym_tbl = self.project_dir / "sym-lib-table"
        fp_tbl = self.project_dir / "fp-lib-table"
        proj = Path(project_path) if project_path is not None else self.project_dir

        removed_sym = self._prune_invalid_entries_from_table(sym_tbl, "sym-lib-table", proj)
        removed_fp = self._prune_invalid_entries_from_table(fp_tbl, "fp-lib-table", proj)
        return removed_sym, removed_fp
