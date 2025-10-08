"""Init file for plugin."""

import os
import sys
import site

lib_path = os.path.join(os.path.dirname(__file__), "lib")
# Ensure plugin's vendored deps are discoverable early
try:
    site.addsitedir(lib_path)
except Exception:
    pass
if lib_path not in sys.path:
    # Prepend to prioritize over conflicting global packages
    sys.path.insert(0, lib_path)

# No special handling for local KiCadFiles; using regex-based editor

from .plugin import JLCPCBPlugin  # noqa: I001, E402

if __name__ != "__main__":
    JLCPCBPlugin().register()
