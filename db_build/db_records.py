"""Typed records shared by the parts database conversion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SourcePart:
    """Canonical representation of a component read from a source database."""

    lcsc: int
    category: str
    subcategory: str
    mfr: str
    package: str
    joints: int
    manufacturer: str
    library_type: str
    description: str
    description_fallback: str
    datasheet: str
    price: str
    price_format: str
    stock: int
    jlc_attributes: dict[str, Any]
    lcsc_attributes: Optional[dict[str, Any]]
    lcsc_manufacturer: str
    rohs: Optional[bool]


@dataclass(frozen=True)
class OutputPart:
    """One row in the backwards-compatible ``parts`` FTS5 table."""

    lcsc: str
    category: str
    subcategory: str
    mfr: str
    package: str
    joints: int
    manufacturer: str
    library_type: str
    description: str
    datasheet: str
    price: str
    stock: str
    attributes: str

    def as_tuple(self) -> tuple:
        """Return values in the exact on-disk schema order."""

        return (
            self.lcsc,
            self.category,
            self.subcategory,
            self.mfr,
            self.package,
            self.joints,
            self.manufacturer,
            self.library_type,
            self.description,
            self.datasheet,
            self.price,
            self.stock,
            self.attributes,
        )
