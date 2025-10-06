"""Install plugin dependencies into the local lib/ folder.

Usage:
  python3 scripts/install_deps.py

This installs packages from requirements.txt into kicad_lcsc_plugin/lib,
which is added to sys.path by __init__.py at runtime inside KiCad.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    target = repo_root / "lib"
    req = repo_root / "requirements.txt"

    target.mkdir(parents=True, exist_ok=True)
    if not req.exists():
        print("requirements.txt not found:", req)
        return 1

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "-r",
        str(req),
        "--target",
        str(target),
    ]
    print("Running:", " ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        print("pip install failed:", e)
        return e.returncode or 1
    print("Dependencies installed into:", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

