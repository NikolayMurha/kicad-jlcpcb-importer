"""Project library tables manager for KiCad (sym-lib-table / fp-lib-table)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Tuple


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
            return "(sym_lib_table\r\n  (version 7))\r\n"
        return "(fp_lib_table\r\n  (version 7))\r\n"

    def _ensure_entry(self, tbl_path: Path, tbl_kind: str, name: str, uri: str) -> bool:
        entry = (
            f"  (lib (name \"{name}\")(type \"KiCad\")(uri \"{uri}\")(options \"\")(descr \"\"))\n"
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

    # --------------- public API ---------------
    def ensure_project_lib_tables(
        self, out_dir: Path, use_project_relative: bool = True
    ) -> Tuple[List[Path], List[Path], Path]:
        """Discover generated libraries under out_dir and ensure project lib tables reference them.

        Returns (sym_files, pretty_dirs, lib_base)
        """
        project_dir = self.project_dir
        sym_tbl = project_dir / "sym-lib-table"
        fp_tbl = project_dir / "fp-lib-table"

        lib_dir = Path(out_dir)
        sym_files = sorted(lib_dir.rglob("*.kicad_sym"))
        pretty_dirs = sorted(p for p in lib_dir.rglob("*.pretty") if p.is_dir())
        lib_base = lib_dir

        self._log(
            f"Updating library tables in: {project_dir}\n"
            f"Found symbols: {len(sym_files)}; footprint libs: {len(pretty_dirs)}\n"
        )

        sym_content = self._read(sym_tbl)
        fp_content = self._read(fp_tbl)

        changes = 0

        # Add symbol libraries
        for sym in sym_files:
            if use_project_relative:
                try:
                    rel = sym.resolve().relative_to(project_dir.resolve()).as_posix()
                except Exception:
                    rel = sym.resolve().as_posix()
                uri = f"${{KIPRJMOD}}/{rel}"
            else:
                uri = sym.resolve().as_posix()
            name = self._unique_name(sym.stem, sym_content)
            if self._ensure_entry(sym_tbl, "sym", name, uri):
                self._log(f"Added symbol lib: {name} -> {uri}\n")
                sym_content = self._read(sym_tbl)
                changes += 1

        # Add footprint libraries
        for pd in pretty_dirs:
            if use_project_relative:
                try:
                    rel = pd.resolve().relative_to(project_dir.resolve()).as_posix()
                except Exception:
                    rel = pd.resolve().as_posix()
                uri = f"${{KIPRJMOD}}/{rel}"
            else:
                uri = pd.resolve().as_posix()
            name = self._unique_name(pd.stem, fp_content)
            if self._ensure_entry(fp_tbl, "fp", name, uri):
                self._log(f"Added footprint lib: {name} -> {uri}\n")
                fp_content = self._read(fp_tbl)
                changes += 1

        if changes == 0:
            if not sym_files and not pretty_dirs:
                self._log(
                    "No new libraries found to add. "
                    "Ensure easyeda2kicad generated *.kicad_sym and *.pretty in the specified directory.\n"
                )
            else:
                self._log(
                    "Libraries are already present in sym-lib-table / fp-lib-table. No changes needed.\n"
                )

        return sym_files, pretty_dirs, lib_base
