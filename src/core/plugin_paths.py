"""Static assets and persistent storage paths for the IPC plugin."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


def _resolve_plugin_path() -> Path:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "plugin.json").is_file() and (parent / "VERSION").is_file():
            return parent
    return here.parent


PLUGIN_PATH = _resolve_plugin_path()


def get_plugin_settings_path(
    *,
    environ: Optional[Mapping[str, str]] = None,
    plugin_path: Optional[Path] = None,
) -> Path:
    """Return the persistent directory assigned by KiCad's IPC API."""

    env = os.environ if environ is None else environ
    configured = str(env.get("JLCPCB_PLUGIN_SETTINGS_PATH", "")).strip()
    return Path(configured).expanduser() if configured else (plugin_path or PLUGIN_PATH)


def get_plugin_data_path(
    *,
    environ: Optional[Mapping[str, str]] = None,
    plugin_path: Optional[Path] = None,
) -> Path:
    """Return database storage while preserving an existing legacy cache."""

    root = plugin_path or PLUGIN_PATH
    legacy = root / "jlcpcb"
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
    except OSError:
        pass
    return get_plugin_settings_path(environ=environ, plugin_path=root) / "jlcpcb"


def get_plugin_cache_path(
    *,
    environ: Optional[Mapping[str, str]] = None,
    plugin_path: Optional[Path] = None,
) -> Path:
    """Return the IPC-managed cache directory."""

    return get_plugin_settings_path(
        environ=environ,
        plugin_path=plugin_path,
    ) / "cache"
