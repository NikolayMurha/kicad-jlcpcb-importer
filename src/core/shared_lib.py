"""Helpers for shared-library mode metadata and project table links."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, Optional


SHARED_META_FILE = "jlcpcb_shared.json"
LEGACY_MODELS_DIR = "EASYEDA_MODELS"


def _log(log: Optional[Callable[[str], None]], msg: str) -> None:
    try:
        if log:
            log(msg)
    except Exception:
        pass


def shared_meta_path(shared_root: Path) -> Path:
    return shared_root / SHARED_META_FILE


def read_shared_meta(shared_root: Path) -> dict:
    path = shared_meta_path(shared_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ensure_shared_meta(
    shared_root: Path,
    default_library_name: str,
    log: Optional[Callable[[str], None]] = None,
    override_library_name: str | None = None,
) -> dict:
    shared_root.mkdir(parents=True, exist_ok=True)
    current = read_shared_meta(shared_root)
    if not isinstance(current, dict):
        current = {}

    forced = str(override_library_name or "").strip()
    if forced:
        name = forced
    else:
        name = str(current.get("library_name") or "").strip() or default_library_name
    data = {
        "version": 1,
        "library_name": name,
        "tables_mode": "shared_symlink",
    }
    path = shared_meta_path(shared_root)
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        _log(log, f"Shared metadata write failed: {exc}\n")
    return data


def ensure_project_table_links(project_dir: Path, shared_root: Path, log: Optional[Callable[[str], None]] = None) -> None:
    """Create project symlinks to shared `sym-lib-table` / `fp-lib-table`.

    Falls back to file copy if symlink creation is unavailable because of
    filesystem, sandbox, or permission restrictions.
    """
    project_dir = Path(project_dir).resolve()
    shared_root = Path(shared_root).resolve()

    for name, header in (
        ("sym-lib-table", "(sym_lib_table\n  (version 7))\n"),
        ("fp-lib-table", "(fp_lib_table\n  (version 7))\n"),
    ):
        src = shared_root / name
        if not src.exists():
            src.write_text(header, encoding="utf-8")

        dst = project_dir / name
        if dst.exists():
            try:
                if dst.is_symlink() and dst.resolve() == src.resolve():
                    continue
            except Exception:
                pass
            # Migrate pre-existing project table into shared mode.
            try:
                src_text = src.read_text(encoding="utf-8", errors="replace")
            except Exception:
                src_text = ""
            try:
                dst_text = dst.read_text(encoding="utf-8", errors="replace")
            except Exception:
                dst_text = ""
            if (not src_text.strip()) or (src_text.strip() == header.strip()):
                try:
                    shutil.copy2(dst, src)
                    _log(log, f"Migrated table content to shared root: {src}\n")
                except Exception as exc:
                    _log(log, f"Failed to migrate table content to shared root: {exc}\n")

            backup = project_dir / f"{name}.bak"
            try:
                if backup.exists():
                    backup.unlink()
            except Exception:
                pass
            try:
                shutil.move(str(dst), str(backup))
                _log(log, f"Backed up project table before linking: {backup}\n")
            except Exception as exc:
                _log(log, f"Unable to backup existing project table {dst}: {exc}\n")
                continue

        rel_target = os.path.relpath(src, project_dir)
        try:
            dst.symlink_to(rel_target)
            _log(log, f"Linked project table: {dst} -> {rel_target}\n")
            continue
        except Exception as exc:
            _log(
                log,
                "Symlink creation failed; copied table file instead. "
                "Check filesystem and sandbox permissions if live linking is required.\n",
            )
            _log(log, f"Details: {exc}\n")
            shutil.copy2(src, dst)


def ensure_project_legacy_models_link(
    project_dir: Path,
    models_dir: Path,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Create compatibility symlink ``EASYEDA_MODELS -> 3dmodels`` in project dir."""
    project_dir = Path(project_dir).resolve()
    models_dir = Path(models_dir).resolve()
    models_dir.mkdir(parents=True, exist_ok=True)

    legacy = project_dir / LEGACY_MODELS_DIR
    if legacy.resolve() == models_dir:
        return

    if legacy.exists() or legacy.is_symlink():
        try:
            if legacy.is_symlink() and legacy.resolve() == models_dir:
                return
        except Exception:
            pass

        if legacy.is_symlink():
            try:
                legacy.unlink()
            except Exception as exc:
                _log(log, f"Failed to replace legacy models symlink {legacy}: {exc}\n")
                return
        else:
            backup = project_dir / f"{LEGACY_MODELS_DIR}.bak"
            idx = 1
            while backup.exists():
                backup = project_dir / f"{LEGACY_MODELS_DIR}.bak{idx}"
                idx += 1
            try:
                shutil.move(str(legacy), str(backup))
                _log(log, f"Backed up existing legacy models folder: {backup}\n")
            except Exception as exc:
                _log(log, f"Cannot create legacy models symlink; existing path is in use: {legacy} ({exc})\n")
                return

    rel_target = os.path.relpath(models_dir, project_dir)
    try:
        legacy.symlink_to(rel_target)
        _log(log, f"Linked legacy models path: {legacy} -> {rel_target}\n")
    except Exception as exc:
        _log(
            log,
            "Legacy models symlink creation failed. "
            "Check filesystem and sandbox permissions.\n",
        )
        _log(log, f"Details: {exc}\n")
