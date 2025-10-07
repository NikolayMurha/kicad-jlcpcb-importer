"""Editing helpers for KiCad footprint files (.kicad_mod/.mod)."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Optional


class FootprintEditor:
    """Editor for footprint files under a search root.

    - Provides rewriting of absolute 3D model paths inside the project to ${KIPRJMOD}-relative
    """

    def __init__(self, project_dir: Path | str):
        self.project_dir = Path(project_dir).resolve()

    @staticmethod
    def _is_abs(p: str) -> bool:
        if p.startswith("/"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", p):
            return True
        if p.startswith("\\\\"):
            return True
        return False

    def relativize_3d_model_paths(self, search_root: Path | str) -> int:
        """Scan `.kicad_mod`/`.mod` under search_root and rewrite absolute 3D model paths
        that point inside the project directory to `${KIPRJMOD}`-relative paths.

        Returns the number of replacements made.
        """
        root = Path(search_root)
        try:
            files = list(root.rglob("*.kicad_mod")) + list(root.rglob("*.mod"))
        except Exception:
            files = []
        if not files:
            return 0

        pattern = re.compile(r"\(model\s+\"([^\"]+)\"")

        total = 0
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue

            changed_any = False
            changes_here = 0

            def repl(m) -> str:
                nonlocal changed_any, changes_here
                original = m.group(1)
                if "${" in original:
                    return m.group(0)
                norm = original.replace("\\", "/")
                if not self._is_abs(norm):
                    return m.group(0)
                try:
                    rel = Path(norm).resolve().relative_to(self.project_dir)
                except Exception:
                    return m.group(0)
                newp = f"${{KIPRJMOD}}/{rel.as_posix()}"
                if newp != original:
                    changed_any = True
                    changes_here += 1
                    return m.group(0).replace(f'"{original}"', f'"{newp}"', 1)
                return m.group(0)

            new_text = pattern.sub(repl, text)
            if changed_any and new_text != text:
                try:
                    f.write_text(new_text, encoding="utf-8")
                    total += changes_here
                except Exception:
                    pass

        return total

