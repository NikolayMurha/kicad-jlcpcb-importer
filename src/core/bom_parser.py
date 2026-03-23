"""BOM file parser — extracts LCSC part numbers from CSV and XLSX files."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import List

# LCSC codes: letter C followed by at least 4 digits
_LCSC_RE = re.compile(r"\bC\d{4,}\b")


def _looks_like_lcsc(value: str) -> bool:
    return bool(_LCSC_RE.fullmatch(value.strip()))


def _find_col(headers: list[str], *names: str) -> int:
    """Return index of first header that case-insensitively matches any name."""
    lower = [h.strip().lower() for h in headers]
    for name in names:
        try:
            return lower.index(name.lower())
        except ValueError:
            pass
    return -1


def _extract_from_rows(
    headers: list[str], rows: list[list[str]]
) -> list[str]:
    supplier_col = _find_col(headers, "Supplier")
    part_col = _find_col(headers, "Supplier Part", "Supplier Part #", "LCSC Part #", "LCSC")

    seen: dict[str, None] = {}  # ordered set

    for row in rows:
        def cell(idx: int) -> str:
            try:
                return str(row[idx]).strip() if idx >= 0 and idx < len(row) else ""
            except Exception:
                return ""

        lcsc_id = ""

        # Priority 1: Supplier == LCSC and Supplier Part column exists
        if supplier_col >= 0 and part_col >= 0:
            if cell(supplier_col).strip().upper() == "LCSC":
                candidate = cell(part_col)
                if _looks_like_lcsc(candidate):
                    lcsc_id = candidate

        # Priority 2: Supplier Part column but no matching Supplier
        if not lcsc_id and part_col >= 0:
            candidate = cell(part_col)
            if _looks_like_lcsc(candidate):
                lcsc_id = candidate

        # Priority 3: regex scan of all cells
        if not lcsc_id:
            for value in row:
                m = _LCSC_RE.search(str(value))
                if m:
                    lcsc_id = m.group()
                    break

        if lcsc_id:
            seen[lcsc_id] = None

    return list(seen.keys())


def _parse_csv(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return []
    return _extract_from_rows(rows[0], rows[1:])


def _parse_xlsx(path: Path) -> list[str]:
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required to parse .xlsx files: pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    all_rows = [
        [str(cell.value) if cell.value is not None else "" for cell in row]
        for row in ws.iter_rows()
    ]
    wb.close()
    if not all_rows:
        return []
    return _extract_from_rows(all_rows[0], all_rows[1:])


def parse_bom_lcsc_ids(file_path: str | Path) -> List[str]:
    """Parse a BOM file and return a deduplicated ordered list of LCSC part IDs.

    Supports .csv and .xlsx formats. Extraction strategy:
    1. If a "Supplier" column == "LCSC" and a "Supplier Part" column exists → use that value.
    2. If a "Supplier Part" column contains a C\\d{4,} value → use it.
    3. Fallback: regex-scan every cell for the first C\\d{4,} match in the row.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _parse_xlsx(path)
    if suffix in (".csv", ".tsv", ".txt"):
        return _parse_csv(path)
    # Try CSV for unknown extensions
    try:
        return _parse_csv(path)
    except Exception:
        return []
