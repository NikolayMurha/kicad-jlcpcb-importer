"""Integration tests for both source adapters and the stable output schema."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zipfile import ZipFile

from db_build.db_normalize import normalize_price
from db_build.db_output import PART_COLUMNS, PartsDatabaseBuilder, package_database
from db_build.db_source import SourceDatabase


def create_v2(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('format', 'source-db-v2');
            CREATE TABLE jlc_components (
                lcsc INTEGER PRIMARY KEY NOT NULL,
                present INTEGER NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL,
                mfr TEXT NOT NULL,
                package TEXT NOT NULL,
                joints INTEGER NOT NULL,
                manufacturer TEXT NOT NULL,
                library_type TEXT NOT NULL,
                description TEXT NOT NULL,
                datasheet TEXT NOT NULL,
                price TEXT NOT NULL,
                stock INTEGER NOT NULL,
                attributes TEXT NOT NULL,
                rohs INTEGER
            );
            CREATE TABLE lcsc_components (
                lcsc INTEGER PRIMARY KEY NOT NULL,
                manufacturer TEXT NOT NULL,
                attributes TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO jlc_components VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    1002,
                    1,
                    "Filters",
                    "Ferrite Beads",
                    "GZ1608D601TF",
                    "0603",
                    2,
                    "",
                    "base",
                    "600Ω 0603 Ferrite Beads ROHS",
                    "https://example.test/C1002.pdf",
                    "1-199:0.0188,200-599:0.0162,600-:0.0091",
                    123,
                    json.dumps({"Number of Circuits": "1", "JLC only": "yes"}),
                    1,
                ),
                (
                    1003,
                    1,
                    "Filters",
                    "Ferrite Beads",
                    "GZ1608D151TF",
                    "0603",
                    2,
                    "Sunlord",
                    "expand",
                    "",
                    "",
                    "",
                    0,
                    json.dumps({"JLC only": "kept"}),
                    0,
                ),
                (
                    9999,
                    0,
                    "Old",
                    "Removed",
                    "OBSOLETE",
                    "",
                    0,
                    "",
                    "expand",
                    "obsolete",
                    "",
                    "1-:1.0",
                    0,
                    "{}",
                    0,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO lcsc_components VALUES (?, ?, ?)",
            (
                1002,
                "Sunlord LCSC",
                json.dumps({"Circuits": "1", "LCSC only": "yes"}),
            ),
        )
        connection.commit()


def create_v1(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO manufacturers VALUES (7, 'Legacy Mfr');
            CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT, subcategory TEXT);
            INSERT INTO categories VALUES (3, 'Legacy Category', 'Legacy Subcategory');
            CREATE TABLE components (
                lcsc, category_id, mfr, package, joints, manufacturer_id,
                basic, description, datasheet, stock, price, ignored, extra
            );
            """
        )
        connection.execute(
            "INSERT INTO components VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                42,
                3,
                "OLD-42",
                "SOT-23",
                3,
                7,
                1,
                "Legacy Subcategory SOT-23 ROHS",
                "legacy.pdf",
                9,
                json.dumps([{"qFrom": "1", "qTo": None, "price": "0.25"}]),
                0,
                json.dumps({"attributes": {"Voltage": "5V"}}),
            ),
        )
        connection.commit()


def test_v2_build_preserves_output_contract(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "parts-fts5.db"
    create_v2(source)

    builder = PartsDatabaseBuilder(source, output, deep_validation=True)
    assert builder.source.format == "source-db-v2"
    assert builder.build() == 2

    with closing(sqlite3.connect(output)) as connection:
        assert tuple(row[1] for row in connection.execute("PRAGMA table_info(parts)")) == PART_COLUMNS
        rows = connection.execute(
            'SELECT "LCSC Part", "Manufacturer", "Library Type", '
            '"Description", "Price", "Attributes" FROM parts ORDER BY "LCSC Part"'
        ).fetchall()
        assert rows[0][0:3] == ("C1002", "Sunlord LCSC", "Basic")
        assert rows[0][3] == "600Ω"
        assert rows[0][4] == "1-199:0.019,200-:0.016"
        assert json.loads(rows[0][5]) == {"Circuits": "1", "LCSC only": "yes"}
        assert rows[1][1:3] == ("Sunlord", "Extended")
        assert json.loads(rows[1][5]) == {"JLC only": "kept"}
        assert connection.execute("SELECT count(*) FROM categories").fetchone()[0] == 1
        assert dict(connection.execute("SELECT key, value FROM build_meta"))[
            "source_format"
        ] == "source-db-v2"
        assert dict(connection.execute("SELECT key, value FROM build_meta"))[
            "validation"
        ] == "deep"


def test_v2_can_explicitly_include_not_present(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "parts-fts5.db"
    create_v2(source)
    assert PartsDatabaseBuilder(source, output, include_not_present=True).build() == 3


def test_legacy_adapter_keeps_old_normalization(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    output = tmp_path / "parts-fts5.db"
    create_v1(source)
    assert SourceDatabase(source).format == "source-db-v1"
    assert PartsDatabaseBuilder(source, output).build() == 1
    with closing(sqlite3.connect(output)) as connection:
        row = connection.execute(
            'SELECT "LCSC Part", "Manufacturer", "Description", "Attributes" FROM parts'
        ).fetchone()
    assert row == ("C42", "Legacy Mfr", "", '{"Voltage": "5V"}')


def test_price_normalization_supports_both_source_formats() -> None:
    ranges = "1-99:0.1234,100-199:0.1001,200-:0.009"
    legacy = json.dumps(
        [
            {"qFrom": "1", "qTo": "99", "price": "0.1234"},
            {"qFrom": "100", "qTo": "199", "price": "0.1001"},
            {"qFrom": "200", "qTo": None, "price": "0.009"},
        ]
    )
    expected = "1-99:0.123,100-:0.100"
    assert normalize_price(ranges, "ranges") == expected
    assert normalize_price(legacy, "legacy-json") == expected


def test_conversion_failure_does_not_replace_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "parts-fts5.db"
    create_v2(source)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            "UPDATE jlc_components SET price = 'not-a-price' WHERE lcsc = 1002"
        )
        connection.commit()
    output.write_bytes(b"known-good")
    try:
        PartsDatabaseBuilder(source, output).build()
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid source row was accepted")
    assert output.read_bytes() == b"known-good"


def test_packaging_writes_valid_chunks_and_manifest_last(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    output = tmp_path / "parts-fts5.db"
    manifest = tmp_path / "chunk_num_fts5.txt"
    create_v2(source)
    PartsDatabaseBuilder(source, output).build()
    chunks = package_database(
        output,
        manifest,
        chunk_size=128,
        cleanup=False,
        deep_validation=True,
    )
    assert int(manifest.read_text(encoding="utf-8")) == chunks
    zip_path = Path(f"{output}.zip")
    with ZipFile(zip_path) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [output.name]


class PipelineTests(unittest.TestCase):
    """Expose the fixture-oriented checks to unittest discovery."""

    def run_in_temp(self, test_function) -> None:
        with tempfile.TemporaryDirectory() as raw_path:
            test_function(Path(raw_path))

    def test_v2_output(self) -> None:
        self.run_in_temp(test_v2_build_preserves_output_contract)

    def test_v2_not_present(self) -> None:
        self.run_in_temp(test_v2_can_explicitly_include_not_present)

    def test_v1_output(self) -> None:
        self.run_in_temp(test_legacy_adapter_keeps_old_normalization)

    def test_prices(self) -> None:
        test_price_normalization_supports_both_source_formats()

    def test_invalid_source_atomicity(self) -> None:
        self.run_in_temp(test_conversion_failure_does_not_replace_existing_output)

    def test_packaging(self) -> None:
        self.run_in_temp(test_packaging_writes_valid_chunks_and_manifest_last)
