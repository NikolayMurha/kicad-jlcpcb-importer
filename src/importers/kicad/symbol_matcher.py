"""Symbol matching helpers for KiCad builtin selection."""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Dict, Iterable, List, Optional


DEFAULT_SYMBOL_MAP: Dict[str, List[str]] = {
    "resistor": ["Device:R", "Device:R_Small", "Device:R_US"],
    "capacitor": ["Device:C", "Device:C_Small", "Device:C_Polarized"],
    "inductor": ["Device:L", "Device:L_Small"],
    "ferrite_bead": ["Device:FerriteBead", "Device:L", "Device:L_Small"],
    "diode": ["Device:D", "Device:D_Small"],
    "zener": ["Device:D_Zener", "Device:D_Zener_Small", "Device:D"],
    "tvs": ["Device:D_TVS", "Device:D_Zener", "Device:D"],
    "schottky": ["Device:D_Schottky", "Device:D_Schottky_Small", "Device:D"],
    "led": ["Device:LED", "Device:LED_Small"],
    "bjt": ["Transistor_BJT:Q_NPN_BCE", "Transistor_BJT:Q_PNP_BCE"],
    "fet": ["Transistor_FET:Q_NMOS_GDS", "Transistor_FET:Q_PMOS_GDS"],
    "ic": ["Device:U"],
    "default": ["Device:U"],
}

_REF_PREFIX_KIND_MAP: Dict[str, str] = {
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
    "FB": "ferrite_bead",
    "D": "diode",
    "ZD": "zener",
    "LED": "led",
    "Q": "bjt",
    "U": "ic",
    "IC": "ic",
}

_GENERIC_SYMBOL_TOKENS = {
    "DEVICE",
    "SYMBOL",
    "COMPONENT",
    "PART",
    "CAPACITOR",
    "RESISTOR",
    "INDUCTOR",
    "DIODE",
    "LED",
    "TRANSISTOR",
    "MOSFET",
    "CONNECTOR",
    "HEADER",
    "SOCKET",
    "DEFAULT",
}
_PKG_ONLY_RE = re.compile(
    r"(?:SOT|SOIC|SOP|SSOP|TSSOP|MSOP|TSOP|QFN|DFN|LQFP|TQFP|QFP|BGA|DIP|PDIP|SOD|DO|TO)\d{1,4}"
)
_CHIP_ONLY_RE = re.compile(r"(?:0?(?:201|402|603|805|1206|1210|2010|2512)(?:\d{4})?(?:METRIC)?)")


def uniq(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalized_reference_prefix(value: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.lstrip("=~")
    text = text.strip("()[]{}")
    text = text.split()[0]
    m = re.match(r"^([A-Z]+)", text)
    if not m:
        return ""
    prefix = m.group(1)
    if len(prefix) == 1 or prefix in {"LED", "IC", "FB", "ZD"}:
        return prefix
    return prefix


def is_specific_symbol_token(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    if len(compact) < 5:
        return False
    if compact in _GENERIC_SYMBOL_TOKENS:
        return False
    if not re.search(r"[A-Z]", compact):
        return False
    if not re.search(r"\d", compact):
        return False
    if _PKG_ONLY_RE.fullmatch(compact):
        return False
    if _CHIP_ONLY_RE.fullmatch(compact):
        return False
    return True


def pick_wildcard_symbol_key(
    pattern: str,
    candidates: Iterable[str],
    min_score: int = 80,
    min_gap: int = 14,
) -> Optional[str]:
    key = str(pattern or "").strip().lower()
    if not key:
        return None
    if not any(ch in key for ch in ("*", "?", "[")):
        return None
    stem = re.sub(r"[\*\?\[\]]+", "", key)
    ranked: List[tuple[str, int]] = []
    for raw in candidates:
        cand = str(raw or "").strip().lower()
        if not cand:
            continue
        if not fnmatch.fnmatch(cand, key):
            continue
        common = len(os.path.commonprefix([cand, stem])) if stem else 0
        score = common * 8
        if stem and cand.startswith(stem):
            score += 220
        if cand == stem:
            score += 320
        score -= abs(len(cand) - len(stem)) * 2
        ranked.append((cand, score))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[1], len(item[0])))
    best_key, best_score = ranked[0]
    if best_score < min_score:
        return None
    if len(ranked) > 1 and (best_score - ranked[1][1]) < min_gap:
        return None
    return best_key


def component_kind(
    category: str,
    description: str,
    attrs: Dict[str, str],
    meta: Optional[Dict] = None,
) -> str:
    meta = meta or {}
    raw_text = " ".join(
        [
            str(category or ""),
            str(description or ""),
            " ".join(str(v or "") for v in attrs.values()),
        ]
    )
    text = raw_text.lower()
    fet_re = r"\b(mosfets?|jfets?|igbts?|fets?)\b"
    bjt_re = r"\b(transistors?|bjts?)\b"
    if re.search(r"\b(buzzer|piezo|piezoelectric|sounder|electromagnetic\s+buzzer)\b", text):
        return "default"
    if re.search(r"\b(connector|socket|header|terminal|plug|receptacle)\b", text):
        return "default"
    if re.search(r"\b(crystal|oscillator|resonator|tcxo|vcxo|xtal)\b", text):
        return "default"
    if re.search(r"\b(relay|switch|button|keyswitch|tactile|fuse|varistor|ptc|thermistor)\b", text):
        return "default"
    if re.search(r"\b(transformer|balun|coupler|isolator)\b", text):
        return "default"
    if re.search(r"\b(?:qfn|dfn|qfp|lqfp|tqfp|bga|lga|lcc|soic|ssop|tssop|msop|tsop|dip|pdip)-?\d+\b", text):
        return "ic"

    ref_hint = normalized_reference_prefix(str(meta.get("reference_prefix") or attrs.get("Reference") or ""))
    mapped_kind = _REF_PREFIX_KIND_MAP.get(ref_hint)
    if ref_hint == "Q":
        if re.search(fet_re, text):
            return "fet"
        if re.search(bjt_re, text):
            return "bjt"
        return "bjt"
    elif mapped_kind and mapped_kind not in ("diode",):
        # For diode ("D" prefix) fall through to sub-type detection below.
        return mapped_kind

    value_hint = str(meta.get("value") or attrs.get("Value") or "").strip().lower()
    if re.search(r"\d+(?:\.\d+)?\s*(?:p|n|u|m)?f\b", value_hint):
        return "capacitor"
    if re.search(r"\d+(?:\.\d+)?\s*(?:n|u|m)?h\b", value_hint):
        return "inductor"
    if re.search(r"\d+(?:\.\d+)?\s*(?:m|k)?(?:ohm|r)\b", value_hint):
        return "resistor"

    if re.search(fet_re, text):
        return "fet"
    if re.search(bjt_re, text):
        return "bjt"
    if re.search(
        r"\b(microcontroller|controller|amplifier|logic|integrated(?:\s+circuit)?|ic|sensor|accelerometer|gyroscope|imu|opamp|regulator|adc|dac|gate\s+driver|led\s+driver|motor\s+driver)\b",
        text,
    ):
        return "ic"
    if re.search(r"\b(resistor|resistance)\b", text):
        return "resistor"
    if re.search(r"\b(capacitor|capacitance)\b", text):
        return "capacitor"
    if re.search(r"\b(ferrite|ferrite\s+beads?|beads?)\b", text):
        return "ferrite_bead"
    if re.search(r"\binductor\b", text):
        return "inductor"
    if re.search(r"\bled\b", text):
        return "led"
    # Diode sub-type detection: check before falling back to generic "diode".
    # Also triggered when ref_hint mapped to "diode" (prefix "D").
    is_diode_ref = mapped_kind == "diode"
    is_diode_text = bool(re.search(r"\b(diode|esd|tvs|surge|transient|electrostatic)\b", text))
    if is_diode_ref or is_diode_text:
        if re.search(r"\bschottky\b", text):
            return "schottky"
        if re.search(r"\b(zener|vz|voltage\s+regulator\s+diode)\b", text):
            return "zener"
        if re.search(r"\b(esd|tvs|surge\s+protect|transient\s+voltage|electrostatic)\b", text):
            return "tvs"
        return "diode"
    return "default"


def family_symbol_candidates(part_number: str) -> List[str]:
    text = str(part_number or "").strip()
    if not text:
        return []
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", text):
        return []

    out: List[str] = []
    upper = text.upper()
    if len(upper) < 6:
        if len(upper) >= 5:
            out.append(upper + "*")
            out.append(upper[:-1] + "*")
            if upper[-1].isdigit():
                out.append(upper + "A")
                out.append(upper + "A*")
        return uniq(out)

    # Prefer exact-prefix extension first: AO3401 -> AO3401* (matches AO3401A).
    out.append(upper + "*")

    for cut in (1, 2):
        if len(upper) - cut < 5:
            continue
        prefix = upper[:-cut]
        out.append(prefix + "*")
        out.append(prefix + "X*")

    if upper[-1].isdigit():
        out.append(upper[:-1] + "X")
        out.append(upper[:-1] + "X*")
    if upper[-1].isalpha():
        out.append(upper[:-1] + "X*")

    if "STM32" in upper and len(upper) >= 8:
        out.append(upper[:-1] + "X*")
        out.append(upper[:-2] + "X*")
    return uniq(out)


def exact_symbol_candidates(meta: Dict, attrs: Dict[str, str]) -> List[str]:
    def _is_placeholder_like(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        if text.startswith("="):
            return True
        if "{" in text or "}" in text:
            return True
        if "$" in text:
            return True
        if re.fullmatch(r"\(?\s*[A-Za-z_][A-Za-z0-9_ ]*\s*\)?", text) and " " in text:
            return True
        return False

    raw: List[str] = []
    for key in ("mfr_part", "part_no", "mpn", "symbol_name"):
        text = str(meta.get(key) or "").strip()
        if text:
            raw.append(text)

    for key in (
        "Manufacturer Part",
        "MFR.Part",
        "MPN",
        "Part Number",
        "PartNumber",
        "Part No",
        "Device",
        "Name",
    ):
        text = str(attrs.get(key) or "").strip()
        if text:
            raw.append(text)

    candidates: List[str] = []
    for text in raw:
        token = str(text or "").strip()
        if _is_placeholder_like(token):
            continue
        if not is_specific_symbol_token(token):
            continue
        candidates.append(token)
        compact = re.sub(r"[ \t/\\\-]+", "_", token).strip("_")
        if compact:
            candidates.append(compact)
        no_space = re.sub(r"\s+", "", token)
        if no_space:
            candidates.append(no_space)
        for pattern in family_symbol_candidates(no_space or compact or token):
            candidates.append(pattern)
    return uniq(candidates)
