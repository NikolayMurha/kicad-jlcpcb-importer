"""Native ELIBZ conversion helpers based on kicad-cli."""

from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Optional

from .helpers import strip_lcsc_suffix
from .platform_support import find_kicad_cli
from .sym_lib_reader import list_symbols_kicad_meta


def load_elibz_payload(elibz_path: Path) -> Dict:
    with zipfile.ZipFile(elibz_path, "r") as zf:
        payload = json.loads(zf.read("device.json").decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid ELIBZ device.json payload")
    return payload


def device_display_name(device_entry: Dict) -> str:
    return strip_lcsc_suffix(
        (
            device_entry.get("display_title")
            or device_entry.get("title")
            or device_entry.get("name")
            or device_entry.get("product_code")
            or ""
        ).strip()
    )


def find_device_by_name(payload: Dict, symbol_name: str) -> Optional[Dict]:
    wanted_raw = str(symbol_name or "").strip()
    wanted = strip_lcsc_suffix(wanted_raw)
    if not wanted_raw and not wanted:
        return None
    devices = payload.get("devices", {}) or {}
    if not isinstance(devices, dict):
        return None
    for dev in devices.values():
        candidate_raw = (
            dev.get("display_title")
            or dev.get("title")
            or dev.get("name")
            or dev.get("product_code")
            or ""
        ).strip()
        candidate = strip_lcsc_suffix(candidate_raw)
        if candidate == wanted or candidate_raw == wanted_raw:
            return dev
    return None


def pick_device(payload: Dict, lcsc_id: str = "", symbol_name: str = "") -> Optional[Dict]:
    devices = payload.get("devices", {}) or {}
    if not isinstance(devices, dict) or not devices:
        return None

    wanted_code = str(lcsc_id or "").strip().upper()
    if wanted_code:
        for dev in devices.values():
            code = str((dev or {}).get("product_code") or "").strip().upper()
            if code and code == wanted_code:
                return dev

    by_name = find_device_by_name(payload, symbol_name)
    if by_name is not None:
        return by_name

    for dev in devices.values():
        attrs = (dev or {}).get("attributes", {}) or {}
        if attrs.get("Symbol") and attrs.get("Footprint"):
            return dev
    return next(iter(devices.values()))


def source_symbol_candidates(device_entry: Dict, requested_name: str = "") -> list[str]:
    attrs = device_entry.get("attributes", {}) or {}
    symbol_obj = device_entry.get("symbol", {}) or {}
    raw = [
        requested_name,
        symbol_obj.get("display_title"),
        symbol_obj.get("title"),
        attrs.get("Name"),
        device_entry.get("display_title"),
        device_entry.get("title"),
        device_entry.get("name"),
    ]
    out: list[str] = []
    seen = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if text not in seen:
            seen.add(text)
            out.append(text)
        clean = strip_lcsc_suffix(text)
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def pick_symbol_name_from_converted(
    converted_sym: Path,
    device_entry: Dict,
    requested_name: str = "",
) -> Optional[str]:
    rows = list_symbols_kicad_meta(converted_sym)
    if not rows:
        return None
    names = [name for name, _desc, _fp in rows]
    by_clean = {strip_lcsc_suffix(name): name for name in names}
    candidates = source_symbol_candidates(device_entry, requested_name)
    for cand in candidates:
        if cand in names:
            return cand
        clean = strip_lcsc_suffix(cand)
        if clean in by_clean:
            return by_clean[clean]
    wanted = device_display_name(device_entry)
    if wanted in by_clean:
        return by_clean[wanted]
    return names[0]


def rename_symbol_block(symbol_block: str, old_name: str, new_name: str) -> str:
    if not old_name or not new_name or old_name == new_name:
        return symbol_block

    def _repl(match: re.Match) -> str:
        current = match.group(1)
        if current == old_name:
            return f'(symbol "{new_name}"'
        if current.startswith(old_name + "_"):
            suffix = current[len(old_name) :]
            return f'(symbol "{new_name}{suffix}"'
        return match.group(0)

    return re.sub(r'\(symbol\s+"([^"]+)"', _repl, symbol_block)


def resolve_footprint_mod_path(pretty_dir: Path, footprint_name: str) -> Optional[Path]:
    wanted = str(footprint_name or "").strip()
    if not wanted:
        return None
    exact = pretty_dir / f"{wanted}.kicad_mod"
    if exact.exists():
        return exact
    wanted_low = wanted.lower()
    for item in pretty_dir.glob("*.kicad_mod"):
        if item.stem.lower() == wanted_low:
            return item
    return None


def _find_kicad_cli() -> str:
    return find_kicad_cli()


def convert_elibz_with_kicad_cli(elibz_path: Path, out_sym: Path, out_pretty: Path) -> None:
    cli = _find_kicad_cli()

    if out_sym.exists():
        out_sym.unlink()
    if out_pretty.exists():
        shutil.rmtree(out_pretty)

    sym_proc = subprocess.run(
        [cli, "sym", "upgrade", "-o", str(out_sym), str(elibz_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if sym_proc.returncode != 0:
        err = (sym_proc.stderr or sym_proc.stdout or "").strip()
        raise RuntimeError(f"kicad-cli sym upgrade failed: {err}")

    fp_proc = subprocess.run(
        [cli, "fp", "upgrade", "-o", str(out_pretty), str(elibz_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if fp_proc.returncode != 0:
        err = (fp_proc.stderr or fp_proc.stdout or "").strip()
        raise RuntimeError(f"kicad-cli fp upgrade failed: {err}")
