"""Editing helpers for KiCad footprint files (.kicad_mod/.mod)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Optional, Callable


class FootprintEditor:
    """Editor for footprint files under a search root.
    - Provides rewriting of absolute 3D model paths to ${KIPRJMOD}-relative
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
    def _model_block_spans(text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        idx = 0
        while True:
            start = text.find("(model ", idx)
            if start == -1:
                break
            depth = 0
            end = -1
            i = start
            while i < len(text):
                ch = text[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                i += 1
            if end == -1:
                break
            spans.append((start, end))
            idx = end
        return spans

    @classmethod
    def _model_blocks(cls, text: str) -> list[str]:
        return [text[s:e] for s, e in cls._model_block_spans(text)]

    @classmethod
    def _has_model_blocks(cls, text: str) -> bool:
        return bool(cls._model_block_spans(text))

    @classmethod
    def _insert_model_blocks(cls, text: str, model_blocks: list[str]) -> str:
        if not model_blocks:
            return text
        insert_at = text.rfind(")")
        if insert_at == -1:
            return text
        indent = "\t"
        for line in text.splitlines():
            if line.lstrip().startswith("(property "):
                indent = line[: len(line) - len(line.lstrip())]
                break
        block_text = "".join(
            block if block.endswith("\n") else f"{block}\n"
            for block in model_blocks
        )
        block_text = "".join(
            f"{indent}{line.lstrip()}" if line.strip() else line
            for line in block_text.splitlines(True)
        )
        prefix = "" if text[:insert_at].endswith("\n") else "\n"
        return text[:insert_at] + prefix + block_text + text[insert_at:]

    @classmethod
    def copy_preserving_models_if_missing(cls, src: Path | str, dest: Path | str) -> bool:
        """Copy footprint, restoring old model blocks only when the new footprint has none."""
        src_path = Path(src)
        dest_path = Path(dest)
        old_models: list[str] = []
        if dest_path.exists():
            try:
                old_models = cls._model_blocks(dest_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                old_models = []

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)

        if not old_models:
            return False
        try:
            new_text = dest_path.read_text(encoding="utf-8", errors="replace")
            if cls._has_model_blocks(new_text):
                return False
            patched = cls._insert_model_blocks(new_text, old_models)
            if patched != new_text:
                dest_path.write_text(patched, encoding="utf-8")
                return True
        except Exception:
            return False
        return False

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
        to `${KIPRJMOD}`-relative paths.

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
                    rel = Path(os.path.relpath(Path(norm).resolve(), self.project_dir))
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

    @staticmethod
    def set_3d_model_offset(
        footprint_path: Path | str,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
    ) -> bool:
        """Set (offset (xyz X Y Z)) in a single .kicad_mod file.

        Some importers write raw canvas coordinates as the model offset,
        displacing the 3D model far from the footprint. Pass the real
        translation from the EasyEDA Pro device API (converted to mm), or leave
        defaults to zero out the offset when no transform data is available.

        Returns True if the file was modified.
        """
        fp = Path(footprint_path)
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            return False

        pattern = re.compile(
            r"\(offset\s+\(xyz\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s*\)\)"
        )
        new_text = pattern.sub(f"(offset (xyz {x:.6f} {y:.6f} {z:.6f}))", text)
        if new_text == text:
            return False
        try:
            fp.write_text(new_text, encoding="utf-8")
            return True
        except Exception:
            return False

    def rewrite_system_3d_model_paths(self, footprints_base: Path | str, models3d_base: Path | str) -> int:
        """In system-wide layout, fix absolute model paths that wrongly point under 'footprints'.

        System-scope imports sometimes write model paths like:
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
