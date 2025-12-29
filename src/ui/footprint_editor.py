"""Editing helpers for KiCad footprint files (.kicad_mod/.mod)."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Optional, Callable


class FootprintEditor:
    """Editor for footprint files under a search root.
    - Provides rewriting of absolute 3D model paths inside the project to ${KIPRJMOD}-relative
    """

    def __init__(self, project_dir: Path | str, log: Optional[Callable[[str], None]] = None):
        self.project_dir = Path(project_dir).resolve()
        self._logger: Optional[Callable[[str], None]] = log

    def _log(self, msg: str) -> None:
        try:
            if self._logger:
                self._logger(msg)
            else:
                print(f"[FootprintEditor] {msg}")
        except Exception:
            pass

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
                    self._log(f"Updated {f}: {changes_here} model path(s) → ${'{'}KIPRJMOD{'}'}")
                except Exception:
                    pass

        return total

    def rewrite_system_3d_model_paths(self, footprints_base: Path | str, models3d_base: Path | str) -> int:
        """In system-wide layout, fix absolute model paths that wrongly point under 'footprints'.

        easyeda2kicad sometimes writes model paths like:
          /.../footprints/<plugin>/<Name>.3dshapes/<model>
        while models are actually under:
          /.../3dmodels/<plugin>/<Name>.3dshapes/<model>

        This scans all .kicad_mod files under `<footprints_base>.pretty` and replaces the
        wrong base prefix with the correct 3d base prefix. Returns number of replacements.
        """
        fb = Path(footprints_base)
        mb = Path(models3d_base)
        fp_root = fb.with_suffix(".pretty")
        if not fp_root.exists():
            return 0

        wrong_prefix = fb.with_suffix(".3dshapes").resolve().as_posix()
        correct_prefix = mb.with_suffix(".3dshapes").resolve().as_posix()
        pattern = re.compile(r"\(model\s+\"([^\"]+)\"")
        total = 0
        self._log(
            f"Rewriting system 3D paths: wrong_prefix={wrong_prefix} → correct_prefix={correct_prefix}"
        )

        for mod in fp_root.rglob("*.kicad_mod"):
            try:
                text = mod.read_text(encoding="utf-8")
            except Exception:
                continue

            changed_any = False
            def repl(m):
                nonlocal changed_any
                path = m.group(1)
                norm = path.replace("\\", "/")
                self._log(f"`{path}` → `{norm}`")
                if norm.startswith(wrong_prefix):
                    newp = correct_prefix + norm[len(wrong_prefix):]
                    if newp != path:
                        changed_any = True
                        return m.group(0).replace(f'"{path}"', f'"{newp}"', 1)
                return m.group(0)

            new_text = pattern.sub(repl, text)
            if changed_any and new_text != text:
                try:
                    mod.write_text(new_text, encoding="utf-8")
                    total += 1
                except Exception:
                    pass

        return total
