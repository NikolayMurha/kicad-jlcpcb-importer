"""Footprint matching helpers for KiCad builtin selection."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple


PASSIVE_KINDS = {"resistor", "capacitor", "inductor", "led", "diode", "fet", "bjt"}

CHIP_SIZE_IMPERIAL_TO_METRIC: Dict[str, str] = {
    "0201": "0603",
    "0402": "1005",
    "0603": "1608",
    "0805": "2012",
    "1206": "3216",
    "1210": "3225",
    "1806": "4516",
    "1812": "4532",
    "2010": "5025",
    "2512": "6332",
}
CHIP_SIZE_METRIC_TO_IMPERIAL: Dict[str, str] = {
    metric: imperial for imperial, metric in CHIP_SIZE_IMPERIAL_TO_METRIC.items()
}

DEFAULT_FP_LIB_PRIORITY: Dict[str, List[str]] = {
    "resistor": ["Resistor_SMD", "Resistor_THT"],
    "capacitor": ["Capacitor_SMD", "Capacitor_THT"],
    "inductor": ["Inductor_SMD", "Inductor_THT"],
    "diode": ["Diode_SMD", "Diode_THT"],
    "led": ["LED_SMD", "LED_THT"],
    "bjt": ["Package_TO_SOT_SMD", "Package_TO_SOT_THT"],
    "fet": ["Package_TO_SOT_SMD", "Package_TO_SOT_THT"],
    "ic": ["Package_SO", "Package_QFP", "Package_DFN_QFN", "Package_DIP", "Package_BGA", "Package_LGA"],
    "default": [],
}

_PKG_PIN_FAMILY_1 = "SOIC|SOP|SSOP|TSSOP|MSOP|TSOP|QFN|DFN|LQFP|TQFP|QFP|BGA|LGA|LCC|DIP|PDIP"
_PKG_PIN_FAMILY_2 = "SOT|SOD|DO|TO"
_STRICT_PKG_STOPWORDS = {
    "DEVICE",
    "COMPONENT",
    "PACKAGE",
    "FOOTPRINT",
    "MODEL",
    "TITLE",
    "PART",
    "TYPE",
    "CAPACITOR",
    "RESISTOR",
    "INDUCTOR",
    "DIODE",
    "TRANSISTOR",
    "MOSFET",
    "IC",
}


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


def package_variants(value: str) -> List[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    text = raw.replace("_", "-").replace(" ", "")
    out = [raw, text, text.upper()]

    upper = text.upper()
    compact = re.sub(r"[^A-Z0-9]", "", upper)
    if compact:
        out.append(compact)

    m = re.search(
        r"\b(SOT|SOIC|SOP|SSOP|TSSOP|MSOP|TSOP|QFN|DFN|LQFP|TQFP|QFP|BGA|LGA|LCC|DIP|PDIP|SOD|DO|TO)-?(\d{1,3})\b",
        upper,
    )
    if m:
        out.append(f"{m.group(1)}-{m.group(2)}")
        out.append(f"{m.group(1)}{m.group(2)}")

    m_passive = re.search(r"\b0(402|603|805|1206|1210|2010|2512)\b", upper)
    if m_passive:
        imperial = "0" + m_passive.group(1)
        metric = {
            "0402": "1005",
            "0603": "1608",
            "0805": "2012",
            "1206": "3216",
            "1210": "3225",
            "2010": "5025",
            "2512": "6332",
        }.get(imperial)
        out.append(imperial)
        if metric:
            out.append(f"{imperial}_{metric}Metric")
            out.append(f"{imperial}Metric")

    return uniq(out)


def extract_package_candidates(meta: Dict, attrs: Dict[str, str]) -> List[str]:
    raw: List[str] = []
    for key in ("package", "footprint", "footprint_display", "footprint_title", "model_title"):
        value = str(meta.get(key) or "").strip()
        if value:
            raw.extend(re.split(r"[,\n;/]+", value))

    for key, value in attrs.items():
        key_lower = str(key or "").strip().lower()
        if any(
            token in key_lower
            for token in (
                "package",
                "footprint",
                "case",
                "housing",
                "encapsulation",
                "outline",
                "3d model title",
            )
        ):
            raw.extend(re.split(r"[,\n;/]+", str(value or "")))

    description = str(meta.get("description") or "")
    raw.extend(
        re.findall(
            r"\b(?:SOT|SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|LQFP|TQFP|QFP|BGA|LGA|LCC|DIP|PDIP|SOD|DO|TO)-?\d+\b",
            description,
            flags=re.IGNORECASE,
        )
    )
    raw.extend(re.findall(r"\b0(?:402|603|805|1206|1210|2010|2512)\b", description, flags=re.IGNORECASE))

    expanded: List[str] = []
    for item in raw:
        expanded.extend(package_variants(item))
        expanded.extend(package_family_variants(item))
    return [item for item in uniq(expanded) if item and not is_uuid_like(item)]


def is_uuid_like(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", str(value or "").strip()))


def package_family_variants(value: str) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    up = text.upper()
    out: List[str] = []

    first = re.split(r"[_\s,;/]+", up)[0].strip()
    if len(first) >= 2 and re.search(r"[A-Z]", first):
        out.append(first)
        parts = [part for part in re.split(r"[-]+", first) if len(part) >= 2]
        out.extend(parts)

    for token in re.findall(r"[A-Z][A-Z0-9\-]{2,}", up):
        out.append(token)
    return uniq(out)


def is_dimension_like_token(value: str) -> bool:
    token = str(value or "").strip().lower().replace(" ", "")
    return bool(re.fullmatch(r"\d+(?:\.\d+)?x\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?(?:mm)?", token))


def is_generic_pkg_token(value: str) -> bool:
    token = str(value or "").strip().upper()
    return token in {"SMD", "SMT", "THT", "TH", "PKG", "PACKAGE", "FOOTPRINT"}


def family_keywords(value: str) -> List[str]:
    up = str(value or "").upper()
    if not up:
        return []
    tokens = re.findall(r"[A-Z][A-Z0-9\-]{2,}", up)
    out: List[str] = []
    for tok in tokens:
        if is_generic_pkg_token(tok):
            continue
        if tok.endswith("MM"):
            continue
        out.append(tok)
        out.extend([part for part in tok.split("-") if len(part) >= 3 and not is_generic_pkg_token(part)])
    unique = uniq(out)
    return [token for token in unique if re.search(r"[A-Z]", token)]


def keywords_overlap(a_keys: set[str], b_keys: set[str]) -> bool:
    if not a_keys or not b_keys:
        return False
    for a in a_keys:
        for b in b_keys:
            aa = str(a or "").lower().strip()
            bb = str(b or "").lower().strip()
            if not aa or not bb:
                continue
            if aa == bb:
                return True
            if len(aa) >= 3 and len(bb) >= 3 and (aa in bb or bb in aa):
                return True
    return False


def strict_package_keywords(package_candidates: List[str]) -> List[str]:
    out: List[str] = []
    for pkg in package_candidates:
        base = str(pkg or "").strip()
        if not base:
            continue
        tokens = [base] + family_keywords(base)
        for token in tokens:
            raw = str(token or "").strip()
            if not raw:
                continue
            compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
            if not compact:
                continue
            if len(compact) < 3:
                continue
            if compact in _STRICT_PKG_STOPWORDS:
                continue
            if compact.endswith("MM"):
                continue
            if re.fullmatch(r"\d+", compact):
                continue
            if is_dimension_like_token(raw):
                continue
            if is_generic_pkg_token(raw):
                continue
            if is_uuid_like(compact):
                continue
            out.append(compact)
    return uniq(out)


def has_strict_keyword_overlap(mod_name: str, strict_keywords: List[str]) -> bool:
    if not strict_keywords:
        return True
    compact = re.sub(r"[^A-Z0-9]", "", str(mod_name or "").upper())
    if not compact:
        return False
    mod_keys = {
        re.sub(r"[^A-Z0-9]", "", key.upper())
        for key in family_keywords(mod_name)
        if key
    }
    mod_keys = {key for key in mod_keys if len(key) >= 3}
    for key in strict_keywords:
        token = re.sub(r"[^A-Z0-9]", "", str(key or "").upper())
        if not token:
            continue
        if token in compact:
            return True
        for mod_key in mod_keys:
            if token in mod_key or (len(mod_key) >= 4 and mod_key in token):
                return True
    return False


def _norm_dim_num(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        num = float(text)
    except Exception:
        return text
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.6f}".rstrip("0").rstrip(".")


def dimension_tokens(value: str) -> set[str]:
    text = str(value or "")
    out: set[str] = set()
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)", text):
        a_raw = _norm_dim_num(match.group(1))
        b_raw = _norm_dim_num(match.group(2))
        if not a_raw or not b_raw:
            continue
        try:
            a_num = float(a_raw)
            b_num = float(b_raw)
            lo = _norm_dim_num(str(min(a_num, b_num)))
            hi = _norm_dim_num(str(max(a_num, b_num)))
            out.add(f"{lo}X{hi}")
        except Exception:
            vals = sorted([a_raw, b_raw])
            out.add(f"{vals[0]}X{vals[1]}")
    return out


def package_dimension_tokens(package_candidates: List[str]) -> set[str]:
    out: set[str] = set()
    for pkg in package_candidates:
        out.update(dimension_tokens(pkg))
    return out


def dimension_match_adjustment(mod_name: str, package_dims: set[str], strict: bool = False) -> int:
    if not package_dims:
        return 0
    mod_dims = dimension_tokens(mod_name)
    if not mod_dims:
        return -80 if strict else -35
    if package_dims & mod_dims:
        return 260 if strict else 170
    return -260 if strict else -180


def package_pin_tokens(package_candidates: List[str]) -> List[str]:
    """Extract strict package+pin tokens (normalized) such as QFN44, SOIC8, SOT235."""
    out: List[str] = []
    for pkg in package_candidates:
        text = str(pkg or "").upper()
        if not text:
            continue

        # Families where first numeric part is usually pin count: SOIC-8, QFN-44, etc.
        for match in re.finditer(rf"\b(?:{_PKG_PIN_FAMILY_1})[-_ ]?\d{{1,3}}\b", text):
            token = re.sub(r"[^A-Z0-9]", "", match.group(0))
            if token:
                out.append(token)

        # Families where package code + pin count are commonly encoded together: SOT-23-5, TO-263-5, etc.
        for match in re.finditer(rf"\b(?:{_PKG_PIN_FAMILY_2})[-_ ]?\d{{1,3}}[-_ ]\d{{1,3}}\b", text):
            token = re.sub(r"[^A-Z0-9]", "", match.group(0))
            if token:
                out.append(token)
    return uniq(out)


def chip_size_codes(value: str) -> Tuple[set[str], set[str]]:
    text = str(value or "").upper()
    if not text:
        return set(), set()
    compact = re.sub(r"[^A-Z0-9]", "", text)
    imperial: set[str] = set()
    metric: set[str] = set()
    for imp, met in CHIP_SIZE_IMPERIAL_TO_METRIC.items():
        if re.search(rf"(?<!\d){re.escape(imp)}(?!\d)", text) or imp in compact:
            imperial.add(imp)
            metric.add(met)
    for met, imp in CHIP_SIZE_METRIC_TO_IMPERIAL.items():
        if re.search(rf"(?<!\d){re.escape(met)}(?!\d)", text) or met in compact:
            metric.add(met)
            imperial.add(imp)
    return imperial, metric


def chip_size_hints(package_candidates: List[str]) -> Tuple[set[str], set[str]]:
    imperial: set[str] = set()
    metric: set[str] = set()
    for pkg in package_candidates:
        imp, met = chip_size_codes(pkg)
        imperial.update(imp)
        metric.update(met)
    return imperial, metric


def chip_size_footprint_adjustment(
    mod_name: str,
    lib_alias: str,
    component_kind_value: str,
    hinted_imperial: set[str],
    hinted_metric: set[str],
) -> int:
    if component_kind_value not in PASSIVE_KINDS:
        return 0
    if not hinted_imperial and not hinted_metric:
        return 0
    name_up = str(mod_name or "").upper()
    alias_up = str(lib_alias or "").upper()
    name_compact = re.sub(r"[^A-Z0-9]", "", name_up)
    adjustment = 0

    if any(code in name_compact for code in hinted_imperial):
        adjustment += 90
    if any(code in name_compact for code in hinted_metric):
        adjustment += 45
    if any(code in name_compact for code in hinted_imperial) and any(code in name_compact for code in hinted_metric):
        adjustment += 20

    if "SMD" in alias_up:
        adjustment += 12
    if "THT" in alias_up:
        adjustment -= 120
    if any(tok in name_up for tok in ("AXIAL", "RADIAL", "HORIZONTAL", "VERTICAL")):
        adjustment -= 120

    mod_imperial, mod_metric = chip_size_codes(mod_name)
    if mod_imperial and hinted_imperial and not (mod_imperial & hinted_imperial):
        adjustment -= 55
    elif mod_metric and hinted_metric and not (mod_metric & hinted_metric):
        adjustment -= 35
    return adjustment


def score_fp_name(mod_name: str, package_candidates: List[str]) -> int:
    target = str(mod_name or "").lower()
    target_norm = re.sub(r"[^a-z0-9]", "", target)
    best = 0
    mod_keywords = {key.lower() for key in family_keywords(mod_name)}
    for pkg in package_candidates:
        p = str(pkg or "").lower()
        if not p:
            continue
        if is_uuid_like(p):
            continue
        p_norm = re.sub(r"[^a-z0-9]", "", p)
        if not p_norm:
            continue
        pkg_has_digits = bool(re.search(r"\d", p_norm))
        is_dim = is_dimension_like_token(p)
        is_generic = is_generic_pkg_token(p)
        pkg_keywords = {key.lower() for key in family_keywords(p)}
        has_family_overlap = keywords_overlap(pkg_keywords, mod_keywords)

        if target_norm == p_norm:
            if pkg_has_digits and not is_dim:
                score = 230
            else:
                score = 120 if not is_dim else 45
            best = max(best, score)
        if p_norm in target_norm:
            if pkg_has_digits and not is_dim:
                # Strong signal: concrete package code from source is contained in footprint name.
                best = max(best, 210 + min(30, len(p_norm)))
            elif is_dim:
                idx = target_norm.find(p_norm)
                in_ep_segment = idx >= 2 and target_norm[idx - 2:idx] in ("ep", "1e")
                if not in_ep_segment:
                    best = max(best, 35)
            elif is_generic:
                best = max(best, 10)
            elif pkg_keywords and not has_family_overlap:
                best = max(best, 15)
            else:
                best = max(best, 90 + min(20, len(p_norm)))
        if p.lower() in target:
            if pkg_has_digits and not is_dim:
                best = max(best, 190 + min(25, len(p)))
            elif is_dim:
                idx2 = target.find(p.lower())
                in_ep_segment2 = idx2 >= 2 and target[idx2 - 2:idx2].rstrip("_-") in ("ep", "1ep")
                if not in_ep_segment2:
                    best = max(best, 25)
            elif is_generic:
                best = max(best, 8)
            elif pkg_keywords and not has_family_overlap:
                best = max(best, 12)
            else:
                best = max(best, 70 + min(20, len(p)))
        if has_family_overlap:
            best = max(best, 85 + min(20, len(max(pkg_keywords, key=len))))
    return best


def explicit_fp_ref_bonus(mod_name: str, explicit_refs: List[str]) -> int:
    """Bonus for direct containment/equality with explicit footprint references."""
    target = str(mod_name or "").lower()
    target_norm = re.sub(r"[^a-z0-9]", "", target)
    if not target_norm:
        return 0
    best = 0
    for ref in explicit_refs:
        token = str(ref or "").strip()
        if not token:
            continue
        if ":" in token:
            token = token.split(":", 1)[1].strip()
        if not token:
            continue
        if any(ch in token for ch in ("*", "?", "[")):
            continue
        if is_uuid_like(token):
            continue
        token_low = token.lower()
        token_norm = re.sub(r"[^a-z0-9]", "", token_low)
        if not token_norm or len(token_norm) < 3:
            continue
        has_digits = bool(re.search(r"\d", token_norm))
        if target_norm == token_norm:
            score = 260 if has_digits else 120
            best = max(best, score)
            continue
        if token_norm in target_norm:
            score = (190 if has_digits else 80) + min(25, len(token_norm))
            best = max(best, score)
            continue
        if token_low in target:
            score = (160 if has_digits else 60) + min(20, len(token_low))
            best = max(best, score)
    return best
