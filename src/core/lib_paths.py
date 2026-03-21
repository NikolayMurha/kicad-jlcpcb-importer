"""Shared helpers for resolving project library paths and names."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .shared_lib import read_shared_meta


DEFAULT_LIB_PATH = "${KIPRJMOD}/library"
DEFAULT_LIB_DIR_NAME = "library"
DEFAULT_LIB_NAME = "JLCPCB"


def resolve_lib_root(general: dict, project_path: Path) -> tuple[Path, str]:
    """Return (filesystem_path, uri_prefix) for the configured library root."""
    scope = str(general.get("library_scope", "project")).strip().lower()

    if scope == "project":
        dir_name = str(general.get("lib_path") or "").strip()
        if dir_name.startswith("${KIPRJMOD}/"):
            dir_name = dir_name[len("${KIPRJMOD}/"):]
        dir_name = dir_name.strip().strip("/\\")
        if not dir_name:
            dir_name = DEFAULT_LIB_DIR_NAME
        if "/" in dir_name or "\\" in dir_name:
            dir_name = Path(dir_name).name or DEFAULT_LIB_DIR_NAME
        lib_dir = (project_path / dir_name).resolve()
        return lib_dir, f"${{KIPRJMOD}}/{Path(dir_name).as_posix()}"

    if scope == "shared":
        raw = str(general.get("lib_path") or "").strip() or "${KIPRJMOD}/../library"
        if "${" not in raw and "/" not in raw and "\\" not in raw:
            raw = f"${{KIPRJMOD}}/../{raw}"
        fs_str = raw.replace("${KIPRJMOD}", str(project_path))
        p = Path(fs_str)
        if not p.is_absolute():
            p = (project_path / p).resolve()
        else:
            p = p.resolve()
        return p, raw.rstrip("/")

    raw = str(general.get("lib_path") or "").strip() or DEFAULT_LIB_PATH
    uri_prefix = raw.rstrip("/")
    fs_str = raw.replace("${KIPRJMOD}", str(project_path))
    p = Path(fs_str)
    if not p.is_absolute():
        p = (project_path / p).resolve()
    else:
        p = p.resolve()
    return p, uri_prefix


def _to_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def resolve_group_by_category(general: dict, default: bool = True) -> bool:
    """Return whether library naming should include category suffixes."""
    if "group_by_category" in general:
        return _to_bool(general.get("group_by_category"), default)

    # Legacy migration: single_library=False means grouping by category.
    if "single_library" in general:
        single_library = _to_bool(general.get("single_library"), True)
        return not single_library

    return default


def _project_name(project_path: Path | str | None, fallback: str) -> str:
    if project_path is None:
        return fallback
    try:
        name = Path(project_path).resolve().name
        return name.strip() or fallback
    except Exception:
        return fallback


def resolve_library_base_name(
    general: dict,
    default: str = DEFAULT_LIB_NAME,
    project_path: Path | str | None = None,
) -> str:
    """Resolve configured library name/prefix from new and legacy keys."""
    raw = str(general.get("lib_prefix") or "").strip()
    legacy_single_name = str(general.get("single_library_name") or "").strip()
    chosen = raw or legacy_single_name or default

    scope = str(general.get("library_scope", "project")).strip().lower()
    grouped = resolve_group_by_category(general, default=True)
    untouched = not raw or chosen == default

    # For project-local single library mode, default to project name when the
    # user did not set a custom name explicitly.
    if scope == "project" and not grouped and untouched:
        return _project_name(project_path, fallback=default).rstrip("_") or default

    if scope == "shared" and project_path is not None:
        try:
            shared_root, _ = resolve_lib_root(general, Path(project_path))
            meta = read_shared_meta(shared_root)
            meta_name = str(meta.get("library_name") or "").strip()
            if meta_name:
                return meta_name.rstrip("_") or default
        except Exception:
            pass

    return chosen.rstrip("_") or default


def resolve_target_library_name(
    general: dict,
    category: str,
    sanitize: Callable[[str], str],
    default_name: str = DEFAULT_LIB_NAME,
    project_path: Path | str | None = None,
) -> str:
    """Resolve target library name depending on grouping mode."""
    base = resolve_library_base_name(
        general,
        default=default_name,
        project_path=project_path,
    )
    if not resolve_group_by_category(general, default=True):
        return sanitize(base)

    cat = sanitize(category or "Misc")
    return sanitize(f"{base}_{cat}")
