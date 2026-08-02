"""Regression tests for atomic parts database installation."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from src.importers.unzip_parts import _replace_database, install_parts_database


def _make_database(path: Path, marker: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE VIRTUAL TABLE parts USING fts5(value)")
        connection.execute("INSERT INTO parts VALUES (?)", (marker,))
        connection.execute("CREATE TABLE meta(value)")
        connection.execute("INSERT INTO meta VALUES (?)", (marker,))
        connection.execute("CREATE TABLE categories(value)")
        connection.commit()


def _split_archive(directory: Path, database: Path, chunk_size: int = 97) -> list[Path]:
    archive = directory / "source.zip"
    with ZipFile(archive, "w") as output:
        output.write(database, arcname="parts-fts5.db")
    chunks = []
    with archive.open("rb") as source:
        index = 1
        while data := source.read(chunk_size):
            chunk = directory / f"parts-fts5.db.zip.{index:03d}"
            chunk.write_bytes(data)
            chunks.append(chunk)
            index += 1
    archive.unlink()
    return chunks


class InstallPartsDatabaseTests(unittest.TestCase):
    def test_replace_retries_transient_windows_file_lock(self) -> None:
        calls = []
        sleeps = []

        def replace(source: Path, destination: Path) -> None:
            calls.append((source, destination))
            if len(calls) < 3:
                raise PermissionError("sharing violation")

        source = Path("source.db")
        destination = Path("parts-fts5.db")
        _replace_database(
            source,
            destination,
            replace=replace,
            sleep=sleeps.append,
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.05, 0.1])

    def test_replaces_only_after_valid_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            current = directory / "parts-fts5.db"
            replacement = directory / "replacement.db"
            _make_database(current, "old")
            _make_database(replacement, "new")
            chunks = _split_archive(directory, replacement)

            installed = install_parts_database(directory)

            self.assertEqual(installed, current)
            with closing(sqlite3.connect(current)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM meta").fetchone()[0], "new")
            self.assertTrue(all(not chunk.exists() for chunk in chunks))

    def test_invalid_archive_preserves_current_database_and_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            current = directory / "parts-fts5.db"
            _make_database(current, "old")
            chunk = directory / "parts-fts5.db.zip.001"
            chunk.write_bytes(b"not a zip")

            with self.assertRaises(ValueError):
                install_parts_database(directory)

            with closing(sqlite3.connect(current)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM meta").fetchone()[0], "old")
            self.assertTrue(chunk.exists())


if __name__ == "__main__":
    unittest.main()
