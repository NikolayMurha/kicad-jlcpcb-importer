"""Normalization rules for the generated JLCPCB parts database."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Optional

from .db_records import OutputPart, SourcePart


@dataclass
class PriceEntry:
    """Price for an inclusive quantity range."""

    min_quantity: int
    max_quantity: Optional[int]
    price_dollars_str: str

    @property
    def price_dollars(self) -> float:
        return float(self.price_dollars_str)


def parse_legacy_price_json(raw: str) -> list[PriceEntry]:
    """Parse the JSON price representation used by source-db-v1."""

    if not raw:
        return []
    result = []
    for item in json.loads(raw):
        result.append(
            PriceEntry(
                min_quantity=int(item["qFrom"]),
                max_quantity=(
                    int(item["qTo"]) if item.get("qTo") is not None else None
                ),
                price_dollars_str=str(item["price"]),
            )
        )
    return result


def parse_price_ranges(raw: str) -> list[PriceEntry]:
    """Parse the compact ``1-99:0.1,100-:0.08`` source-db-v2 format."""

    if not raw:
        return []
    result = []
    for value in raw.split(","):
        quantity_range, price = value.split(":", 1)
        lower, upper = quantity_range.split("-", 1)
        result.append(
            PriceEntry(
                min_quantity=int(lower),
                max_quantity=int(upper) if upper else None,
                price_dollars_str=price,
            )
        )
    return result


def reduce_price_precision(entries: list[PriceEntry]) -> list[PriceEntry]:
    """Preserve the existing three-decimal output policy."""

    result = copy.deepcopy(entries)
    for entry in result:
        entry.price_dollars_str = f"{entry.price_dollars:.3f}"
    return result


def filter_prices_below_cutoff(
    entries: list[PriceEntry], cutoff_price_dollars: float = 0.01
) -> list[PriceEntry]:
    """Keep the first price and later tiers at or above the legacy cutoff."""

    if not entries:
        return []
    result = [copy.deepcopy(entries[0])]
    result.extend(
        copy.deepcopy(entry)
        for entry in entries[1:]
        if entry.price_dollars >= cutoff_price_dollars
    )
    result[-1].max_quantity = None
    return result


def filter_duplicate_prices(entries: list[PriceEntry]) -> list[PriceEntry]:
    """Merge adjacent ranges that have the same normalized price."""

    result: list[PriceEntry] = []
    for entry in entries:
        current = copy.deepcopy(entry)
        if result and result[-1].price_dollars_str == current.price_dollars_str:
            result[-1].max_quantity = current.max_quantity
        else:
            result.append(current)
    if result:
        result[-1].max_quantity = None
    return result


def normalize_price(raw: str, price_format: str) -> str:
    """Normalize either supported source price format to the plugin contract."""

    if not raw:
        return ""
    if price_format == "legacy-json":
        entries = parse_legacy_price_json(raw)
    elif price_format == "ranges":
        entries = parse_price_ranges(raw)
    else:
        raise ValueError(f"Unsupported price format: {price_format}")
    entries = reduce_price_precision(entries)
    entries = filter_prices_below_cutoff(entries)
    entries = filter_duplicate_prices(entries)
    return ",".join(
        f"{entry.min_quantity}-{entry.max_quantity if entry.max_quantity is not None else ''}:{entry.price_dollars_str}"
        for entry in entries
    )


def normalize_description(part: SourcePart) -> str:
    """Apply the existing description cleanup using named source fields."""

    description = str(part.description or part.description_fallback or "")
    if part.rohs is True:
        description = re.sub(r"\s+ROHS\b", "", description, flags=re.IGNORECASE)
    elif part.rohs is False:
        if not re.search(r"\bnot\s+ROHS\b", description, flags=re.IGNORECASE):
            description = re.sub(
                r"\s+ROHS\b", "", description, flags=re.IGNORECASE
            )
            description += " not ROHS"
    elif re.search(r"\s+ROHS\b", description, flags=re.IGNORECASE):
        description = re.sub(r"\s+ROHS\b", "", description, flags=re.IGNORECASE)
    else:
        description += " not ROHS"

    for duplicate in (part.subcategory, part.package):
        if duplicate:
            description = description.replace(duplicate, "")
    return re.sub(r"\s+", " ", description).strip()


def normalize_part(part: SourcePart) -> OutputPart:
    """Convert a canonical source record to the stable output schema."""

    manufacturer = part.manufacturer.strip() or part.lcsc_manufacturer.strip()
    attributes = (
        part.lcsc_attributes
        if part.lcsc_attributes is not None
        else part.jlc_attributes
    )
    return OutputPart(
        lcsc=f"C{part.lcsc}",
        category=part.category,
        subcategory=part.subcategory,
        mfr=part.mfr,
        package=part.package,
        joints=int(part.joints),
        manufacturer=manufacturer,
        library_type=part.library_type,
        description=normalize_description(part),
        datasheet=part.datasheet,
        price=normalize_price(part.price, part.price_format),
        stock=str(part.stock),
        # Keep the serialization contract of the original converter.  Some
        # consumers compare or display this value as text instead of parsing it.
        attributes=json.dumps(attributes),
    )
