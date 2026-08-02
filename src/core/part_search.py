"""Safe SQL construction for the parts FTS5 database."""

from __future__ import annotations

from typing import Mapping


SEARCH_COLUMNS = (
    "LCSC Part",
    "MFR.Part",
    "Package",
    "Solder Joint",
    "Library Type",
    "Stock",
    "Manufacturer",
    "Description",
    "Price",
    "First Category",
    "Attributes",
)

ORDER_COLUMNS = frozenset(SEARCH_COLUMNS[:9])


def _fts_string(value: object) -> str:
    """Return one FTS5 string literal, including escaped double quotes."""

    return '"' + str(value or "").replace('"', '""') + '"'


def build_parts_search_query(
    parameters: Mapping[str, object],
    order_by: str,
    order_dir: str,
) -> tuple[str, list[object]]:
    """Build a bound-parameter query for the user-facing part search."""

    if order_by not in ORDER_COLUMNS:
        raise ValueError(f"Unsupported search sort column: {order_by}")
    direction = str(order_dir or "ASC").upper()
    if direction not in {"ASC", "DESC"}:
        raise ValueError(f"Unsupported search sort direction: {order_dir}")

    keyword = str(parameters.get("keyword") or "")
    part_number = str(parameters.get("part_no") or "")
    if not keyword and not part_number:
        return "", []

    match_terms: list[str] = []
    where: list[str] = []
    values: list[object] = []

    for word in keyword.split():
        if len(word) < 3:
            where.append('"Description" LIKE ?')
            values.append(f"%{word}%")
        else:
            match_terms.append(_fts_string(word))

    fts_filters = (
        ("manufacturer", "Manufacturer"),
        ("package", "Package"),
        ("category", "First Category"),
        ("subcategory", "Second Category"),
        ("part_no", "MFR.Part"),
        ("solder_joints", "Solder Joint"),
    )
    for key, column in fts_filters:
        value = str(parameters.get(key) or "")
        if not value or (key == "category" and value == "All"):
            continue
        match_terms.append(f'{_fts_string(column)}:{_fts_string(value)}')

    if match_terms:
        where.insert(0, "parts MATCH ?")
        values.insert(0, " AND ".join(match_terms))

    library_types = []
    if parameters.get("basic"):
        library_types.append("Basic")
    if parameters.get("extended"):
        library_types.append("Extended")
    if library_types:
        placeholders = ",".join("?" for _ in library_types)
        where.append(f'"Library Type" IN ({placeholders})')
        values.extend(library_types)

    if parameters.get("stock"):
        where.append('CAST("Stock" AS INTEGER) > 0')

    if not where:
        return "", []

    columns = ",".join(f'"{column}"' for column in SEARCH_COLUMNS)
    query = (
        f"SELECT {columns} FROM parts WHERE "
        + " AND ".join(where)
        + f' ORDER BY "{order_by}" COLLATE naturalsort {direction} LIMIT 1000'
    )
    return query, values
