"""Utilities to edit KiCad .kicad_sym files for a specific symbol."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple, Optional
import re
import wx  # type: ignore
from .events import LogboxAppendEvent

class SymbolEditor:
    """Edit properties of a specific symbol in a .kicad_sym file.

    - Loads file lazily and keeps the whole text in memory
    - Locates target (symbol "<symbol_id>") block and edits only that block
    - Provides helpers to set properties with hidden effects and to patch footprint prefix
    - Can update Value based on attributes for passive categories
    - Saves back to file with optional stripping of (id ..) tokens
    """

    def __init__(self, sym_path: Path | str, symbol_id: str, parent_window: Optional[wx.Window] = None):
        self.parent_window = parent_window
        self.sym_path = Path(sym_path)
        self.symbol_id = symbol_id
        self._text: str = ""
        self._block_span: Optional[Tuple[int, int]] = None
        self._block: str = ""

    # ---------- low-level helpers ----------
    def _load(self) -> None:
        if not self._text:
            self._text = self.sym_path.read_text(encoding="utf-8")

    @staticmethod
    def _find_blocks(src: str) -> List[Tuple[int, int]]:
        blocks: List[Tuple[int, int]] = []
        idx = 0
        while True:
            start = src.find("(symbol ", idx)
            if start == -1:
                break
            depth = 0
            i = start
            end = -1
            while i < len(src):
                c = src[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                i += 1
            if end != -1:
                blocks.append((start, end))
            idx = end if end != -1 else start + 7
        return blocks

    @staticmethod
    def _find_pin_blocks(src: str) -> List[Tuple[int, int]]:
        """Return spans (start, end) for each top-level (pin ...) block in src.

        Uses balanced parentheses scanning starting from each "(pin " occurrence.
        Spans are relative to the provided src string.
        """
        pins: List[Tuple[int, int]] = []
        idx = 0
        n = len(src)
        while True:
            start = src.find("(pin ", idx)
            if start == -1:
                break
            depth = 0
            i = start
            end = -1
            while i < n:
                c = src[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                i += 1
            if end != -1:
                pins.append((start, end))
                idx = end
            else:
                # Fallback to avoid infinite loop on malformed content
                idx = start + 1
        return pins

    @staticmethod
    def _prop_exists(block: str, name: str) -> bool:
        return re.search(rf"\(property\s*\"{re.escape(name)}\"\s*\"", block) is not None

    @staticmethod
    def _get_prop_value(block: str, name: str) -> Optional[str]:
        m = re.search(rf"\(property\s*\"{re.escape(name)}\"\s*\"([^\"]*)\"", block)
        return m.group(1) if m else None

    @staticmethod
    def _set_prop_value(block: str, name: str, new_value: str) -> Tuple[str, bool]:
        m = re.search(rf"\(property\s*\"{re.escape(name)}\"\s*\"([^\"]*)\"", block)
        if not m:
            return block, False
        old_val = m.group(1)
        if old_val == new_value:
            return block, False
        safe_val = (new_value or "").replace('"', "'")
        new_block = re.sub(
            rf"(\(property\s*\"{re.escape(name)}\"\s*\")([^\"]*)(\")",
            lambda mm: mm.group(1) + safe_val + mm.group(3),
            block,
            count=1,
        )
        return new_block, True

    @staticmethod
    def _extract_template(block: str) -> Tuple[str, str, str]:
        # Try Footprint first, then Value
        m = re.search(r"\(property\s*\"Footprint\"[\s\S]*?\)", block)
        if not m:
            m = re.search(r"\(property\s*\"Value\"[\s\S]*?\)", block)
        prop = m.group(0) if m else "(property \"X\" \"Y\")"
        # Indentation
        indent = "  "
        for ln in block.splitlines():
            if ln.strip().startswith("(property "):
                indent = ln[: len(ln) - len(ln.lstrip())]
                break
        at = re.search(r"\(at[^\)]*\)", prop)
        effects = re.search(r"\(effects[\s\S]*?\)", prop)
        return indent, (at.group(0) if at else ""), (effects.group(0) if effects else "")

    @staticmethod
    def _ensure_hide_effects(effects: str) -> str:
        if not effects or "(effects" not in effects:
            return "(effects (font (size 1.27 1.27)) (hide yes))"
        if "hide" in effects:
            return effects
        idx = effects.rfind(")")
        if idx == -1:
            return effects + " (hide yes)"
        return effects[:idx] + " (hide yes)" + effects[idx:]

    def _ensure_loaded_block(self) -> None:
        self._load()
        if self._block_span is not None:
            return
        # Locate the symbol block with matching ID
        blocks = self._find_blocks(self._text)
        for (s, e) in blocks:
            head = self._text[s : min(e, s + 200)]
            if re.search(rf"\(symbol\s+\"{re.escape(self.symbol_id)}\"", head):
                self._block_span = (s, e)
                self._block = self._text[s:e]
                break
        if self._block_span is None:
            # As a fallback, if file has only one block, edit it
            if len(blocks) == 1:
                self._block_span = blocks[0]
                s, e = blocks[0]
                self._block = self._text[s:e]
            else:
                raise ValueError("Symbol block not found in file")

    # ---------- public API ----------
    def ensure_footprint_prefix(self, prefix: str) -> int:
        """Ensure Footprint property is prefixed with self.prefix.

        - If the Footprint value already starts with the prefix, leave as-is.
        - Otherwise, add prefix when the library name starts with "LCSC_".
          (Category is not strictly required for the change.)
        Returns number of updates (0/1).
        """
        self._ensure_loaded_block()
        pattern = re.compile(r"\(property\s*(?:\n\s*)?\"Footprint\"\s*(?:\n\s*)?\"([^\"]+)\"", re.MULTILINE)
        updates = 0

        def repl(m):
            nonlocal updates
            val = m.group(1)
            # Already prefixed
            if val.startswith(f"{prefix}"):
                return m.group(0)

            updates += 1
            return m.group(0).replace(f'"{val}"', f'"{prefix}{val}"', 1)
            
        new_block, _ = pattern.subn(repl, self._block)
        if new_block != self._block:
            self._block = new_block
        return updates

    def apply_properties(
        self,
        properties: Dict[str, str],
        category: Optional[str] = None,
        update_empty_only: bool = True,
        hidden: bool = True,
        exclude_equal_to_value: bool = True,
    ) -> int:
        """Apply properties to the symbol block and optionally update Value.

        - Treats all entries of `properties` as symbol properties to add/update
        - If `category` is provided, sets Value from primary attribute
          (Resistance/Capacitance/Inductance/Impedance) when appropriate
        - update_empty_only: existing non-empty properties left unchanged
        - hidden: added properties include (effects ... (hide yes))
        - exclude_equal_to_value: skip props whose value equals current Value
        Returns number of changes (including a possible Value change).
        """
        self._ensure_loaded_block()
        changes = 0
        current_value = (self._get_prop_value(self._block, "Value") or "").strip()

        for name, val in properties.items():
            if val is None:
                continue
            # Avoid trying to set Footprint/Value through generic path
            if name.strip() in ("Footprint", "Value"):
                continue
            
            sval = str(val)
            if exclude_equal_to_value and sval.strip() == current_value:
                continue
            
            if self._prop_exists(self._block, name):
                if update_empty_only:
                    current = self._get_prop_value(self._block, name) or ""
                    if current.strip() == "":
                        self._block, modified = self._set_prop_value(self._block, name, sval)
                        if modified:
                            changes += 1
            else:
                indent, at, effects = self._extract_template(self._block)
                eff = self._ensure_hide_effects(effects) if hidden else (effects or "")
                extra = (f" {at}" if at else "") + (f" {eff}" if eff else "")
                safe_val = sval.replace('"', "'")
                new_line = f"{indent}(property \"{name}\" \"{safe_val}\"{extra})\n"
                insert_at = self._block.rfind(')')
                if insert_at != -1:
                    self._block = self._block[:insert_at] + new_line + self._block[insert_at:]
                    changes += 1
        # Optionally update Value from primary attributes present in `properties`
        if category:
            cat = (category or "").lower()
            primary: Optional[str] = None
            if "resistor" in cat:
                primary = properties.get("Resistance")
            elif "capacitor" in cat:
                primary = properties.get("Capacitance")
            elif "inductor" in cat or "coil" in cat:
                primary = properties.get("Inductance")
            elif "ferrite" in cat or "bead" in cat:
                primary = properties.get("Impedance @ Frequency") or properties.get("Impedance")
                
            if primary:
                current_value = self._get_prop_value(self._block, "Value") or ""
                mfr_part_prop = (properties.get("Manufacturer Part") or properties.get("MFR.Part") or "").strip()
                if (current_value.strip() == "") or (mfr_part_prop and current_value.strip() == mfr_part_prop):
                    self._block, modified = self._set_prop_value(self._block, "Value", primary)
                    if modified:
                        changes += 1

            # If the category is one of the passives above, and symbol has <= 2 pins,
            # normalize pins to "input line" and set pin names to "~".
            if any(k in cat for k in ("resistor", "capacitor", "inductor", "coil", "ferrite", "bead")):
                pin_changes = self._normalize_two_or_fewer_pins_to_input_with_tilde()
                changes += pin_changes
        return changes

    # ---------- pin normalization helpers ----------
    def _normalize_two_or_fewer_pins_to_input_with_tilde(self) -> int:
        """If the symbol has <= 2 pins, make each pin "input line" and name "~".

        - Keeps existing pin position, length, number and effects intact
        - Only updates the pin header tokens and the (name "...") value
        Returns number of pin edits performed.
        """
        pins = self._find_pin_blocks(self._block)
        if not pins or len(pins) > 2:
            return 0

        # Build a new block by replacing each pin block in order
        new_parts: List[str] = []
        prev = 0
        edits = 0

        # Regex that matches the pin header allowing line breaks between tokens
        header_re = re.compile(r'^\(pin(?:\s+|\s*\n\s*)\S+(?:\s+|\s*\n\s*)\S+', re.MULTILINE)
        # Regex to force pin name to "~"
        name_re = re.compile(r'(\(name(?:\s+|\s*\n\s*)")(.*?)(")', re.MULTILINE)

        for (s, e) in pins:
            new_parts.append(self._block[prev:s])
            pin_text = self._block[s:e]

            updated = pin_text
            # Replace header to "(pin input line"
            updated2, n1 = header_re.subn("(pin input line", updated, count=1)
            # Replace pin name to "~"
            updated3, n2 = name_re.subn(r'\1~\3', updated2, count=1)

            if updated3 != pin_text:
                edits += (1 if n1 else 0) + (1 if n2 else 0)
            new_parts.append(updated3)
            prev = e

        new_parts.append(self._block[prev:])
        new_block = "".join(new_parts)
        if new_block != self._block:
            self._block = new_block
        return edits

    def update_value_from_attributes(
        self,
        category: str,
        attrs: Optional[Dict[str, str]],
        mfr_part: Optional[str] = None,
    ) -> bool:
        """Backward-compatible wrapper that delegates to apply_properties.

        mfr_part is merged into properties under "Manufacturer Part" for compatibility.
        """
        props = dict(attrs or {})
        if mfr_part:
            props.setdefault("Manufacturer Part", mfr_part)
        return bool(self.apply_properties(props, category=category))

    def save(self, strip_ids: bool = True) -> None:
        """Persist changes back to the file."""
        if self._block_span is None:
            return
        s, e = self._block_span
        new_text = self._text[:s] + self._block + self._text[e:]
        if strip_ids:
            new_text = re.sub(r"\(id\s+\d+\)", "", new_text)
        if new_text != self._text:
            self.sym_path.write_text(new_text, encoding="utf-8")
        self._text = new_text
        # Update span end in case sizes changed
        self._block_span = (s, s + len(self._block))
