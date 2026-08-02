"""Cross-platform tests for shared library filesystem links."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from src.core.shared_lib import (
    ensure_project_legacy_models_link,
    ensure_project_table_links,
)


class SharedLibraryLinkTests(unittest.TestCase):
    def test_project_tables_remain_linked_to_shared_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            project = root / "project"
            shared = root / "shared"
            project.mkdir()
            shared.mkdir()

            ensure_project_table_links(project, shared)

            for name in ("sym-lib-table", "fp-lib-table"):
                self.assertTrue(os.path.samefile(project / name, shared / name))

    def test_legacy_models_path_points_to_models_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            project = root / "project"
            models = root / "shared" / "3dmodels"
            project.mkdir()

            ensure_project_legacy_models_link(project, models)

            self.assertTrue((project / "EASYEDA_MODELS").exists())
            self.assertTrue(os.path.samefile(project / "EASYEDA_MODELS", models))


if __name__ == "__main__":
    unittest.main()
