"""Tests for missing EasyEDA CAD data and catalog-only matching."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.importers.easyedapro.component_loader import (
    CadDataUnavailableError,
    ComponentLoader,
)
from src.importers.kicad import footprint_matcher, symbol_matcher
from src.importers.kicad.importer import KicadImporter


class _EmptyCadAPI:
    @staticmethod
    def easyeda_search_by_codes(_codes):
        return {"success": True, "code": 0, "result": []}


class _Catalog:
    @staticmethod
    def get_part_details(_lcsc_id):
        return {
            "lcsc": "C5598104",
            "part_no": "BLM21SP700SN1D",
            "description": "170Ω@100MHz 0805 Ferrite Beads",
            "package": "0805",
            "category": "Filters",
            "type": "Extended",
        }


class CadFallbackTests(unittest.TestCase):
    def test_empty_search_result_is_a_specific_error_and_creates_no_elibz(self):
        with TemporaryDirectory() as temp_dir:
            loader = ComponentLoader(
                kiprjmod=temp_dir,
                target_path=temp_dir,
                target_name="C5598104",
                progress=lambda _current, _total: None,
            )
            loader.lcsc_api = _EmptyCadAPI()

            with self.assertRaises(CadDataUnavailableError):
                loader.downloadAll(["C5598104"], skip_models=True)

            self.assertFalse((Path(temp_dir) / "C5598104.elibz").exists())

    def test_ferrite_bead_uses_dedicated_symbol_and_inductor_footprints(self):
        kind = symbol_matcher.component_kind(
            "Filters Ferrite Beads",
            "170Ω@100MHz 0805 Ferrite Beads",
            {},
        )

        self.assertEqual(kind, "ferrite_bead")
        self.assertEqual(symbol_matcher.DEFAULT_SYMBOL_MAP[kind][0], "Device:FerriteBead")
        self.assertIn(kind, footprint_matcher.PASSIVE_KINDS)
        self.assertEqual(
            footprint_matcher.DEFAULT_FP_LIB_PRIORITY[kind][0],
            "Inductor_SMD",
        )

    def test_catalog_metadata_is_available_to_kicad_matcher(self):
        importer = object.__new__(KicadImporter)
        importer.parent_window = SimpleNamespace(library=_Catalog())

        meta = importer._catalog_meta("C5598104")
        attrs = json.loads(meta["attributes_json"])

        self.assertEqual(meta["mfr_part"], "BLM21SP700SN1D")
        self.assertEqual(meta["package"], "0805")
        self.assertEqual(attrs["LCSC Part"], "C5598104")
        self.assertIn("Ferrite Beads", attrs["Description"])


if __name__ == "__main__":
    unittest.main()
