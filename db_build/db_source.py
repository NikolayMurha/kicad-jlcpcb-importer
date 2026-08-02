"""Readers for the legacy and source-db-v2 component cache formats."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from .db_records import SourcePart


class SourceSchemaError(RuntimeError):
    """Raised when the source database does not match a supported schema."""


V2_REQUIRED_COLUMNS = {
    "lcsc",
    "present",
    "category",
    "subcategory",
    "mfr",
    "package",
    "joints",
    "manufacturer",
    "library_type",
    "description",
    "datasheet",
    "price",
    "stock",
    "attributes",
    "rohs",
}


def _json_object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise SourceSchemaError(f"Invalid JSON in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceSchemaError(f"Expected JSON object in {label}")
    return value


class SourceDatabase:
    """Read components from either known cache format."""

    def __init__(self, path: Path | str, include_not_present: bool = False):
        self.path = Path(path)
        self.include_not_present = include_not_present
        if not self.path.is_file():
            raise SourceSchemaError(f"Source database not found: {self.path}")
        self.format = self._detect_and_validate()

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}

    def _detect_and_validate(self) -> str:
        with closing(self._connect()) as connection:
            tables = self._tables(connection)
            if {"jlc_components", "lcsc_components", "meta"} <= tables:
                missing = V2_REQUIRED_COLUMNS - self._columns(
                    connection, "jlc_components"
                )
                if missing:
                    raise SourceSchemaError(
                        "source-db-v2 is missing jlc_components columns: "
                        + ", ".join(sorted(missing))
                    )
                lcsc_missing = {
                    "lcsc",
                    "manufacturer",
                    "attributes",
                } - self._columns(connection, "lcsc_components")
                if lcsc_missing:
                    raise SourceSchemaError(
                        "source-db-v2 is missing lcsc_components columns: "
                        + ", ".join(sorted(lcsc_missing))
                    )
                return "source-db-v2"
            if {"components", "manufacturers", "categories"} <= tables:
                return "source-db-v1"
        raise SourceSchemaError(
            "Unsupported source database schema; expected source-db-v2 tables "
            "(jlc_components, lcsc_components, meta) or legacy tables "
            "(components, manufacturers, categories)"
        )

    def count_parts(self) -> int:
        with closing(self._connect()) as connection:
            if self.format == "source-db-v2":
                where = "" if self.include_not_present else " WHERE present = 1"
                return int(
                    connection.execute(
                        f"SELECT count(*) FROM jlc_components{where}"
                    ).fetchone()[0]
                )
            return int(
                connection.execute("SELECT count(*) FROM components").fetchone()[0]
            )

    def iter_parts(self, batch_size: int = 100_000) -> Iterator[SourcePart]:
        if self.format == "source-db-v2":
            yield from self._iter_v2(batch_size)
        else:
            yield from self._iter_v1(batch_size)

    def _iter_v2(self, batch_size: int) -> Iterator[SourcePart]:
        where = "" if self.include_not_present else "WHERE j.present = 1"
        query = f"""
            SELECT
                j.lcsc, j.category, j.subcategory, j.mfr, j.package, j.joints,
                j.manufacturer, j.library_type, j.description, j.datasheet,
                j.price, j.stock, j.attributes, j.rohs,
                l.lcsc AS enrichment_lcsc,
                l.manufacturer AS lcsc_manufacturer,
                l.attributes AS lcsc_attributes
            FROM jlc_components AS j
            LEFT JOIN lcsc_components AS l ON l.lcsc = j.lcsc
            {where}
            ORDER BY j.lcsc
        """
        with closing(self._connect()) as connection:
            cursor = connection.execute(query)
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    raw_type = str(row["library_type"] or "").lower()
                    yield SourcePart(
                        lcsc=int(row["lcsc"]),
                        category=str(row["category"] or ""),
                        subcategory=str(row["subcategory"] or ""),
                        mfr=str(row["mfr"] or ""),
                        package=str(row["package"] or ""),
                        joints=int(row["joints"] or 0),
                        manufacturer=str(row["manufacturer"] or ""),
                        library_type="Basic" if raw_type == "base" else "Extended",
                        description=str(row["description"] or ""),
                        description_fallback="",
                        datasheet=str(row["datasheet"] or ""),
                        price=str(row["price"] or ""),
                        price_format="ranges",
                        stock=int(row["stock"] or 0),
                        jlc_attributes=_json_object(
                            row["attributes"], f"jlc_components.attributes C{row['lcsc']}"
                        ),
                        lcsc_attributes=(
                            _json_object(
                                row["lcsc_attributes"],
                                f"lcsc_components.attributes C{row['lcsc']}",
                            )
                            if row["enrichment_lcsc"] is not None
                            else None
                        ),
                        lcsc_manufacturer=str(row["lcsc_manufacturer"] or ""),
                        rohs=(bool(row["rohs"]) if row["rohs"] is not None else None),
                    )

    def _iter_v1(self, batch_size: int) -> Iterator[SourcePart]:
        """Read the positional legacy schema behind a single compatibility wall."""

        with closing(self._connect()) as connection:
            manufacturers = dict(connection.execute("SELECT * FROM manufacturers"))
            categories = {
                row[0]: (row[1], row[2])
                for row in connection.execute("SELECT * FROM categories")
            }
            cursor = connection.execute("SELECT * FROM components")
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    if len(row) < 13:
                        raise SourceSchemaError(
                            "Legacy components table has fewer than 13 columns"
                        )
                    extra = _json_object(row[12], f"components.extra C{row[0]}")
                    category, subcategory = categories[row[1]]
                    attributes = extra.get("attributes", {})
                    if not isinstance(attributes, dict):
                        attributes = {}
                    yield SourcePart(
                        lcsc=int(row[0]),
                        category=str(category or ""),
                        subcategory=str(subcategory or ""),
                        mfr=str(row[2] or ""),
                        package=str(row[3] or ""),
                        joints=int(row[4] or 0),
                        manufacturer=str(manufacturers.get(row[5], "")),
                        library_type="Basic" if row[6] else "Extended",
                        description=str(row[7] or ""),
                        description_fallback=str(extra.get("description", "") or ""),
                        datasheet=str(row[8] or ""),
                        price=str(row[10] or ""),
                        price_format="legacy-json",
                        stock=int(row[9] or 0),
                        jlc_attributes=attributes,
                        lcsc_attributes=attributes,
                        lcsc_manufacturer="",
                        rohs=None,
                    )
