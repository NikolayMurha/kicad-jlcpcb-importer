"""Cross-platform runtime path discovery for KiCad integrations."""

from __future__ import annotations

import os
import platform
from pathlib import Path
import re
import shutil
import sys
from typing import Callable, Mapping, Optional


SUPPORTED_SYSTEMS = frozenset({"Darwin", "Linux", "Windows"})


def is_supported_system(system_name: Optional[str] = None) -> bool:
    """Return whether the runtime OS is an officially supported target."""

    return (system_name or platform.system()) in SUPPORTED_SYSTEMS


def detected_kicad_major(version_text: Optional[str] = None) -> int:
    """Return the active KiCad major version, defaulting to the supported minimum."""

    if version_text is None:
        version_text = os.environ.get("KICAD_VERSION", "")
    match = re.search(r"(\d+)", str(version_text or ""))
    return int(match.group(1)) if match else 10


def resolve_system_library_root(
    plugin_path: Path | str,
    *,
    system_name: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path | str] = None,
    version_text: Optional[str] = None,
) -> Path:
    """Resolve KiCad's per-user ``3rdparty`` root on supported platforms."""

    env = os.environ if environ is None else environ
    major = detected_kicad_major(version_text)
    configured = str(env.get(f"KICAD{major}_3RD_PARTY", "")).strip()
    if configured:
        return Path(configured).expanduser()

    system = system_name or platform.system()
    user_home = Path.home() if home is None else Path(home)
    version_dir = f"{major}.0"
    if system == "Darwin":
        return user_home / "Documents" / "KiCad" / version_dir / "3rdparty"
    if system == "Linux":
        return user_home / ".local" / "share" / "kicad" / version_dir / "3rdparty"
    if system == "Windows":
        appdata = str(env.get("APPDATA", "")).strip()
        if appdata:
            return Path(appdata) / "kicad" / version_dir / "3rdparty"
        profile = str(env.get("USERPROFILE", "")).strip()
        profile_home = Path(profile) if profile else user_home
        return profile_home / "AppData" / "Roaming" / "kicad" / version_dir / "3rdparty"

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
    if not discovered and (system_name or platform.system()) == "Windows":
        discovered = which_fn("kicad-cli.exe")
    if discovered:
        return discovered

    system = system_name or platform.system()
    running_executable = executable or sys.executable
    candidates = []
    if running_executable:
        executable_name = "kicad-cli.exe" if system == "Windows" else "kicad-cli"
        candidates.append(Path(running_executable).parent / executable_name)

    if system == "Darwin":
        major = detected_kicad_major(str(env.get("KICAD_VERSION", "")))
        candidates.extend(
            [
                Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
                Path(f"/Applications/KiCad {major}.0/KiCad.app/Contents/MacOS/kicad-cli"),
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
    elif system == "Windows":
        major = detected_kicad_major(str(env.get("KICAD_VERSION", "")))
        program_roots = []
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            value = str(env.get(variable, "")).strip()
            if value and value not in program_roots:
                program_roots.append(value)
        for root in program_roots:
            candidates.append(
                Path(root) / "KiCad" / f"{major}.0" / "bin" / "kicad-cli.exe"
            )

    for candidate in candidates:
        if check(candidate):
            return str(candidate)

    raise RuntimeError(
        "kicad-cli was not found. Install KiCad 10+ or set the KICAD_CLI "
        "environment variable to its executable path."
    )
