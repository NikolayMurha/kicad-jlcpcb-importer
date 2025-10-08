"""Utilities to edit KiCad .kicad_sym files for a specific symbol (regex/text-based)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re
import wx  # type: ignore

try:  # optional UI logger event
    from .events import LogboxAppendEvent  # type: ignore
except Exception:  # pragma: no cover
    LogboxAppendEvent = None  # type: ignore


class SymbolEditor:
    """Edit a specific symbol block inside a .kicad_sym via text operations.

    - Loads/saves the whole file text
    - Locates target (symbol "<symbol_id>") block and edits only that block
    - Updates properties; can add hidden ones; ensures footprint prefix
    - Always updates Value for passives from primary attribute
    - For any symbol with <=2 pins: make pins `(pin passive line ...)` and name `~`; hide pin numbers
    """

    def __init__(self, sym_path: Path | str, symbol_id: str, parent_window: Optional[wx.Window] = None):
        self.parent_window = parent_window
        self.sym_path = Path(sym_path)
        self.symbol_id = symbol_id
        self._text: str = ""
        self._block_span: Optional[Tuple[int, int]] = None
        self._block: str = ""

    def _log(self, msg: str) -> None:
        try:
            if self.parent_window is not None and LogboxAppendEvent is not None:
                wx.PostEvent(self.parent_window, LogboxAppendEvent(msg=f"[SymbolEditor] {msg}\n"))
            else:
                print(f"[SymbolEditor] {msg}")
        except Exception:
            pass

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

    def _ensure_loaded_block(self) -> None:
        self._load()
        if self._block_span is not None:
            return
        blocks = self._find_blocks(self._text)
        for (s, e) in blocks:
            head = self._text[s : min(e, s + 200)]
            if re.search(rf"\(symbol\s+\"{re.escape(self.symbol_id)}\"", head):
                self._block_span = (s, e)
                self._block = self._text[s:e]
                break
        if self._block_span is None:
            if len(blocks) == 1:
                self._block_span = blocks[0]
                s, e = blocks[0]
                self._block = self._text[s:e]
            else:
                raise ValueError("Symbol block not found in file")

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
        # Try Footprint first, then Value, else a minimal property
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

    @staticmethod
    def _find_pin_blocks(block: str) -> List[Tuple[int, int]]:
        pins: List[Tuple[int, int]] = []
        idx = 0
        n = len(block)
        while True:
            start = block.find("(pin ", idx)
            if start == -1:
                break
            depth = 0
            i = start
            end = -1
            while i < n:
                c = block[i]
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
                idx = start + 1
        return pins

    @staticmethod
    def _has_pin_numbers_hide_yes(block: str) -> bool:
        return re.search(r"\(pin_numbers\b[\s\S]*?\(hide\s+yes\)\s*\)", block) is not None

    def _ensure_pin_numbers_hidden(self) -> int:
        """Ensure `(pin_numbers (hide yes))` exists (or is normalized) in current symbol block.

        Returns 1 if modified, else 0.
        """
        # Already correct
        if self._has_pin_numbers_hide_yes(self._block):
            return 0
        # Normalize existing pin_numbers token
        m = re.search(r"(?m)^(\s*)\(pin_numbers\b[\s\S]*?\)$", self._block)
        if m:
            indent = m.group(1)
            normalized = f"{indent}(pin_numbers (hide yes))"
            new_block = re.sub(r"(?m)^(\s*)\(pin_numbers\b[\s\S]*?\)$", normalized, self._block, count=1)
            if new_block != self._block:
                self._block = new_block
                return 1
            return 0
        # Insert a new pin_numbers token before the last ')'
        indent, _at, _eff = self._extract_template(self._block)
        line = f"{indent}(pin_numbers (hide yes))\n"
        insert_at = self._block.rfind(')')
        if insert_at != -1:
            self._block = self._block[:insert_at] + line + self._block[insert_at:]
            return 1
        return 0

    # ---------- public API ----------
    def ensure_footprint_prefix(self, prefix: str) -> int:
        self._ensure_loaded_block()
        pattern = re.compile(r"\(property\s*(?:\n\s*)?\"Footprint\"\s*(?:\n\s*)?\"([^\"]+)\"", re.MULTILINE)
        updates = 0

        def repl(m):
            nonlocal updates
            val = m.group(1)
            if val.startswith(prefix):
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
        self._ensure_loaded_block()
        changes = 0
        current_value = (self._get_prop_value(self._block, "Value") or "").strip()

        for name, val in (properties or {}).items():
            if val is None:
                continue
            if name.strip() in ("Footprint", "Value"):
                continue
            sval = str(val)
            if exclude_equal_to_value and sval.strip() == current_value:
                continue
            if self._prop_exists(self._block, name):
                if update_empty_only:
                    cur = self._get_prop_value(self._block, name) or ""
                    if cur.strip() == "":
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

        if category:
            cat = (category or "").lower()
            primary: Optional[str] = None
            if "resistor" in cat:
                primary = (properties or {}).get("Resistance")
            elif "capacitor" in cat:
                primary = (properties or {}).get("Capacitance")
            elif "inductor" in cat or "coil" in cat:
                primary = (properties or {}).get("Inductance")
            elif "ferrite" in cat or "bead" in cat:
                primary = (properties or {}).get("Impedance @ Frequency") or (properties or {}).get("Impedance")
            if primary:
                curv = self._get_prop_value(self._block, "Value") or ""
                if curv != primary:
                    self._block, modified = self._set_prop_value(self._block, "Value", primary)
                    if modified:
                        changes += 1

        # Normalize any two-pin symbol: passive/line, name '~', hide numbers
        pins = self._find_pin_blocks(self._block)
        if pins and (len(pins) <= 2 or (len(pins) == 3 and "diodes" in cat)): 
            new_parts: List[str] = []
            prev = 0
            header_re = re.compile(r'^\(pin(?:\s+|\s*\n\s*)\S+(?:\s+|\s*\n\s*)\S+', re.MULTILINE)
            name_re = re.compile(r'(\(name(?:\s+|\s*\n\s*)")(.*?)(")', re.MULTILINE)
            for (s, e) in pins:
                new_parts.append(self._block[prev:s])
                pin_text = self._block[s:e]
                pin_text = header_re.sub("(pin passive line", pin_text, count=1)
                pin_text = name_re.sub(r'\1~\3', pin_text, count=1)
                new_parts.append(pin_text)
                prev = e
            new_parts.append(self._block[prev:])
            self._block = "".join(new_parts)
            changes += self._ensure_pin_numbers_hidden()
        return changes

    def update_value_from_attributes(
        self,
        category: str,
        attrs: Optional[Dict[str, str]],
        mfr_part: Optional[str] = None,
    ) -> bool:
        props = dict(attrs or {})
        if mfr_part:
            props.setdefault("Manufacturer Part", mfr_part)
        return bool(self.apply_properties(props, category=category))

    def save(self, strip_ids: bool = True) -> None:
        if self._block_span is None:
            return
        # Ensure '(symbol ...' and closing ')' lines start with exactly two spaces
        try:
            lines = self._block.splitlines(True)
            if lines:
                # First line: '(symbol ...' → force two-space indent
                lines[0] = re.sub(r"^\s*", "  ", lines[0])
                # Last non-empty line: ')' → force two-space indent
                # Find index of last line that contains only optional whitespace and a ')'
                last_idx = len(lines) - 1
                # Trim trailing empty lines
                while last_idx >= 0 and lines[last_idx].strip() == "":
                    last_idx -= 1
                if last_idx >= 0:
                    lines[last_idx] = re.sub(r"^\s*\)\s*$", "  )" + ("\n" if not lines[last_idx].endswith("\n") else ""), lines[last_idx])
                self._block = "".join(lines)
        except Exception:
            pass
        s, e = self._block_span
        new_text = self._text[:s] + self._block + self._text[e:]
        if strip_ids:
            new_text = re.sub(r"\(id\s+[^\)]+\)", "", new_text)
        try:
            def _tabs_to_spaces(m):
                return " " * (2 * len(m.group(0)))
            new_text = re.sub(r"(?m)^(\t+)", _tabs_to_spaces, new_text)
        except Exception:
            pass
        if new_text != self._text:
            self.sym_path.write_text(new_text, encoding="utf-8")
        self._text = new_text
        self._block_span = (s, s + len(self._block))
