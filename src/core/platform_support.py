"""Linux and macOS runtime path discovery for KiCad integrations."""

from __future__ import annotations

import os
import platform
from pathlib import Path
import re
import shutil
import sys
from typing import Callable, Mapping, Optional


SUPPORTED_SYSTEMS = frozenset({"Darwin", "Linux"})


def is_supported_system(system_name: Optional[str] = None) -> bool:
    """Return whether the runtime OS is an officially supported target."""

    return (system_name or platform.system()) in SUPPORTED_SYSTEMS


def detected_kicad_major(version_text: Optional[str] = None) -> int:
    """Return the active KiCad major version, defaulting to the supported minimum."""

    if version_text is None:
        try:
            import pcbnew  # type: ignore

            version_text = str(pcbnew.Version())
        except Exception:
            version_text = ""
    match = re.search(r"(\d+)", str(version_text or ""))
    return int(match.group(1)) if match else 9


def resolve_system_library_root(
    plugin_path: Path | str,
    *,
    system_name: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path | str] = None,
    version_text: Optional[str] = None,
) -> Path:
    """Resolve KiCad's per-user ``3rdparty`` root on Linux and macOS."""

    env = os.environ if environ is None else environ
    major = detected_kicad_major(version_text)
    candidates = []
    for value in (major, 10, 9):
        if value not in candidates:
            candidates.append(value)
    for version in candidates:
        configured = str(env.get(f"KICAD{version}_3RD_PARTY", "")).strip()
        if configured:
            return Path(configured).expanduser()

    system = system_name or platform.system()
    user_home = Path.home() if home is None else Path(home)
    version_dir = f"{major}.0"
    if system == "Darwin":
        return user_home / "Documents" / "KiCad" / version_dir / "3rdparty"
    if system == "Linux":
        return user_home / ".local" / "share" / "kicad" / version_dir / "3rdparty"

    # Preserve best-effort behavior on unclaimed platforms.
    return Path(plugin_path) / "libraries"


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_kicad_cli(
    *,
    system_name: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    executable: Optional[str] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    is_executable: Optional[Callable[[Path], bool]] = None,
) -> str:
    """Locate ``kicad-cli`` in native, Flatpak, AppImage, or app-bundle installs."""

    env = os.environ if environ is None else environ
    check = _is_executable_file if is_executable is None else is_executable
    explicit = str(env.get("KICAD_CLI", "")).strip()
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if check(explicit_path):
            return str(explicit_path)

    which_fn = shutil.which if which is None else which
    discovered = which_fn("kicad-cli")
    if discovered:
        return discovered

    system = system_name or platform.system()
    running_executable = executable or sys.executable
    candidates = []
    if running_executable:
        candidates.append(Path(running_executable).parent / "kicad-cli")

    if system == "Darwin":
        candidates.extend(
            [
                Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
                Path("/Applications/KiCad 10.0/KiCad.app/Contents/MacOS/kicad-cli"),
                Path("/Applications/KiCad 9.0/KiCad.app/Contents/MacOS/kicad-cli"),
            ]
        )
    elif system == "Linux":
        candidates.extend(
            [
                Path("/app/bin/kicad-cli"),
                Path("/usr/local/bin/kicad-cli"),
                Path("/usr/bin/kicad-cli"),
            ]
        )
        app_dir = str(env.get("APPDIR", "")).strip()
        if app_dir:
            candidates.insert(0, Path(app_dir) / "usr" / "bin" / "kicad-cli")

    for candidate in candidates:
        if check(candidate):
            return str(candidate)

    raise RuntimeError(
        "kicad-cli was not found. Install KiCad 9+ or set the KICAD_CLI "
        "environment variable to its executable path."
    )
