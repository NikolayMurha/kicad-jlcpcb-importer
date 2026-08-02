"""Atomic generation and packaging of the plugin's FTS5 parts database."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
import os
from pathlib import Path
import sqlite3
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from .db_normalize import normalize_part
from .db_source import SourceDatabase


PART_COLUMNS = (
    "LCSC Part",
    "First Category",
    "Second Category",
    "MFR.Part",
    "Package",
    "Solder Joint",
    "Manufacturer",
    "Library Type",
    "Description",
    "Datasheet",
    "Price",
    "Stock",
    "Attributes",
)


class OutputValidationError(RuntimeError):
    """Raised when a generated database fails its final checks."""


class PartsDatabaseBuilder:
    """Generate a backwards-compatible database without risking the old file."""

    def __init__(
        self,
        source_path: Path | str,
        output_path: Path | str,
        include_not_present: bool = False,
        deep_validation: bool = False,
        batch_size: int = 10_000,
    ):
        self.source = SourceDatabase(source_path, include_not_present)
        self.output_path = Path(output_path)
        self.deep_validation = deep_validation
        self.batch_size = batch_size

    @staticmethod
    def _create_tables(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE VIRTUAL TABLE parts USING fts5 (
                'LCSC Part',
                'First Category',
                'Second Category',
                'MFR.Part',
                'Package',
                'Solder Joint' unindexed,
                'Manufacturer',
                'Library Type',
                'Description',
                'Datasheet' unindexed,
                'Price' unindexed,
                'Stock' unindexed,
                'Attributes' unindexed,
                tokenize='trigram'
            )
            """
        )
        connection.execute(
            "CREATE TABLE mapping ('footprint', 'value', 'LCSC')"
        )
        connection.execute(
            """
            CREATE TABLE meta (
                'filename', 'size', 'partcount', 'date', 'last_update'
            )
            """
        )
        connection.execute(
            "CREATE TABLE categories ('First Category', 'Second Category')"
        )
        connection.execute(
            "CREATE TABLE build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    def _populate(self, connection: sqlite3.Connection) -> int:
        expected = self.source.count_parts()
        count = 0
        batch = []
        source_parts = self.source.iter_parts()
        try:
            for source_part in source_parts:
                batch.append(normalize_part(source_part).as_tuple())
                if len(batch) >= self.batch_size:
                    connection.executemany(
                        "INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        batch,
                    )
                    count += len(batch)
                    batch.clear()
        finally:
            source_parts.close()
        if batch:
            connection.executemany(
                "INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            count += len(batch)
        if count != expected:
            raise OutputValidationError(
                f"Source count changed during generation: expected {expected}, wrote {count}"
            )
        return count

    def _finalize(self, connection: sqlite3.Connection, part_count: int) -> None:
        connection.execute(
            """
            INSERT INTO categories
            SELECT DISTINCT "First Category", "Second Category"
            FROM parts
            ORDER BY UPPER("First Category"), UPPER("Second Category")
            """
        )
        connection.execute("INSERT INTO parts(parts) VALUES('optimize')")
        connection.execute(
            "INSERT INTO meta VALUES (?, ?, ?, ?, ?)",
            (
                "cache.sqlite3",
                0,
                part_count,
                date.today().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        build_meta = {
            "source_format": self.source.format,
            "output_schema": "parts-fts5-v1+attributes",
            "attribute_policy": "lcsc-if-present-else-jlc",
            "source_part_count": str(part_count),
            "validation": "deep" if self.deep_validation else "fast",
        }
        connection.executemany(
            "INSERT INTO build_meta VALUES (?, ?)", build_meta.items()
        )
        connection.commit()

    @staticmethod
    def validate(
        path: Path | str,
        expected_count: int | None = None,
        deep: bool = False,
    ) -> None:
        """Validate the output contract, optionally scanning the whole database."""

        path = Path(path)
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            columns = tuple(
                row[1] for row in connection.execute("PRAGMA table_info(parts)")
            )
            if columns != PART_COLUMNS:
                raise OutputValidationError(
                    f"Unexpected parts schema: {columns!r}"
                )
            meta_rows = connection.execute("SELECT partcount FROM meta").fetchall()
            if len(meta_rows) != 1:
                raise OutputValidationError("Generated database has invalid meta table")
            stored_count = int(meta_rows[0][0])
            if expected_count is not None and stored_count != expected_count:
                raise OutputValidationError(
                    "Unexpected metadata part count: "
                    f"expected {expected_count}, got {stored_count}"
                )
            sample = connection.execute(
                'SELECT json_valid("Attributes") FROM parts LIMIT 1'
            ).fetchone()
            if sample is not None and sample[0] != 1:
                raise OutputValidationError(
                    "Generated database has an invalid sample Attributes value"
                )
            if deep:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise OutputValidationError(
                        f"SQLite integrity check failed: {integrity}"
                    )
                count = int(
                    connection.execute("SELECT count(*) FROM parts").fetchone()[0]
                )
                if expected_count is not None and count != expected_count:
                    raise OutputValidationError(
                        f"Unexpected part count: expected {expected_count}, got {count}"
                    )
                invalid_json = int(
                    connection.execute(
                        'SELECT count(*) FROM parts '
                        'WHERE NOT json_valid("Attributes")'
                    ).fetchone()[0]
                )
                if invalid_json:
                    raise OutputValidationError(
                        "Generated database has "
                        f"{invalid_json} invalid Attributes values"
                    )

    def build(self) -> int:
        """Build to a sibling temporary file and atomically replace on success."""

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{self.output_path.name}.",
            suffix=".tmp",
            dir=self.output_path.parent,
        )
        os.close(fd)
        temp_path = Path(raw_temp_path)
        temp_path.unlink()
        try:
            with closing(sqlite3.connect(temp_path)) as connection:
                self._create_tables(connection)
                part_count = self._populate(connection)
                self._finalize(connection, part_count)
            size = temp_path.stat().st_size
            with closing(sqlite3.connect(temp_path)) as connection:
                connection.execute("UPDATE meta SET size = ?", (size,))
                connection.commit()
            self.validate(temp_path, part_count, deep=self.deep_validation)
            os.replace(temp_path, self.output_path)
            return part_count
        finally:
            if temp_path.exists():
                temp_path.unlink()


def package_database(
    output_path: Path | str,
    chunk_num_path: Path | str,
    chunk_size: int = 80_000_000,
    cleanup: bool = True,
    deep_validation: bool = False,
) -> int:
    """Create validated split ZIP chunks and publish the manifest last."""

    output_path = Path(output_path)
    chunk_num_path = Path(chunk_num_path)
    zip_path = Path(f"{output_path}.zip")
    staged_zip = Path(f"{zip_path}.tmp")
    staged_chunks: list[tuple[Path, Path]] = []
    try:
        with ZipFile(staged_zip, "w", ZIP_DEFLATED) as archive:
            archive.write(output_path, arcname=output_path.name)
        with ZipFile(staged_zip, "r") as archive:
            members = archive.infolist()
            if (
                len(members) != 1
                or members[0].filename != output_path.name
                or members[0].file_size != output_path.stat().st_size
            ):
                raise OutputValidationError(
                    "Generated ZIP archive has unexpected contents"
                )
            if deep_validation and archive.testzip() is not None:
                raise OutputValidationError("Generated ZIP archive failed validation")

        with staged_zip.open("rb") as source:
            index = 1
            while data := source.read(chunk_size):
                final = Path(f"{zip_path}.{index:03d}")
                staged = Path(f"{final}.tmp")
                staged.write_bytes(data)
                staged_chunks.append((staged, final))
                index += 1

        for staged, final in staged_chunks:
            os.replace(staged, final)

        keep = {final for _, final in staged_chunks}
        for old_chunk in output_path.parent.glob(f"{zip_path.name}.[0-9][0-9][0-9]"):
            if old_chunk not in keep:
                old_chunk.unlink()

        manifest_temp = Path(f"{chunk_num_path}.tmp")
        manifest_temp.write_text(str(len(staged_chunks)), encoding="utf-8")
        os.replace(manifest_temp, chunk_num_path)

        if cleanup:
            staged_zip.unlink()
            output_path.unlink()
        else:
            os.replace(staged_zip, zip_path)
        return len(staged_chunks)
    finally:
        if staged_zip.exists():
            staged_zip.unlink()
        for staged, _ in staged_chunks:
            if staged.exists():
                staged.unlink()
