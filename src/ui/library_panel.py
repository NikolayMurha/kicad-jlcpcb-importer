"""Library Manager dialog for vendoring symbols/footprints into local KiCad libraries.

Destination is always KiCad format:
- <Library Name Prefix>_<Category>.kicad_sym (when grouping is enabled)
- <Library Name>.kicad_sym (when grouping is disabled)
- <project lib path>/3dmodels/*
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import wx

from ..core.helpers import HighResWxSize, sanitize_lib_name
from ..core.lib_paths import (
    resolve_group_by_category,
    resolve_lib_root,
    resolve_library_base_name,
    resolve_target_library_name,
)
from ..core.lib_tables import LibTablesManager
from ..core.shared_lib import (
    ensure_project_legacy_models_link,
    ensure_project_table_links,
    ensure_shared_meta,
)
from ..core.sym_lib_reader import (
    add_or_replace_symbol,
    copy_footprint_to_pretty,
    extract_symbol_block,
    get_connected_elibz_libs,
    get_connected_fp_libs,
    get_connected_sym_libs,
    get_footprint_property,
    list_symbols_elibz_meta,
    list_symbols_kicad_meta,
    patch_footprint_property,
)

_OK_COLOUR = wx.Colour(0, 128, 0)
_ERR_COLOUR = wx.Colour(180, 0, 0)
_DIM_COLOUR = wx.Colour(100, 100, 100)
_BUILTIN_3D_PREFIXES = ("${KICAD", "$ENV{KICAD", "$(KICAD")


class LibraryManagerDialog(wx.Dialog):
    """Browse connected/custom libraries and vendor selected items locally."""

    _ALL_LABEL = "— All libraries —"

    def __init__(self, parent):
        super().__init__(
            parent,
            title="Import from other libraries",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
            size=HighResWxSize(parent.window, wx.Size(940, 560)),
        )
        self.parent = parent

        self._sym_libs: List[Tuple[str, Path]] = []
        self._fp_libs: List[Tuple[str, Path]] = []
        self._visible_libs: List[Tuple[str, Path]] = []
        self._all_items: List[Tuple[str, str, Path, str, str, str]] = []
        self._filtered: List[Tuple[str, str, Path, str, str, str]] = []
        self._fp_model_cache: dict[str, bool] = {}

        self._build_ui()
        self._refresh_connected_libs()

    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        content = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        type_row = wx.BoxSizer(wx.HORIZONTAL)
        type_row.Add(wx.StaticText(self, label="Type:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._mode_choice = wx.Choice(self, wx.ID_ANY, choices=["Symbols", "Footprints"])
        self._mode_choice.SetSelection(0)
        self._mode_choice.Bind(wx.EVT_CHOICE, self._on_mode_changed)
        type_row.Add(self._mode_choice, 1, wx.EXPAND)
        left.Add(type_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        left.Add(wx.StaticText(self, label="Library filter:"), 0, wx.BOTTOM, 4)
        self._lib_filter = wx.SearchCtrl(self, wx.ID_ANY)
        self._lib_filter.ShowCancelButton(True)
        self._lib_filter.Bind(wx.EVT_TEXT, lambda _: self._rebuild_source_choice())
        self._lib_filter.Bind(
            wx.EVT_SEARCHCTRL_CANCEL_BTN,
            lambda _: (self._lib_filter.SetValue(""), self._rebuild_source_choice()),
        )
        left.Add(self._lib_filter, 0, wx.EXPAND | wx.BOTTOM, 8)

        left.Add(wx.StaticText(self, label="Libraries:"), 0, wx.BOTTOM, 4)
        self._lib_list = wx.ListBox(self, wx.ID_ANY, style=wx.LB_SINGLE)
        self._lib_list.Bind(wx.EVT_LISTBOX, self._on_lib_selected)
        self._lib_list.SetMinSize(HighResWxSize(self.parent.window, wx.Size(260, 300)))
        left.Add(self._lib_list, 1, wx.EXPAND)

        left_btns = wx.BoxSizer(wx.HORIZONTAL)
        browse_btn = wx.Button(self, wx.ID_ANY, "Browse…", style=wx.BU_EXACTFIT)
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        left_btns.Add(browse_btn, 0, wx.RIGHT, 6)
        refresh_btn = wx.Button(self, wx.ID_ANY, "Refresh", style=wx.BU_EXACTFIT)
        refresh_btn.Bind(wx.EVT_BUTTON, lambda _: self._refresh_connected_libs())
        left_btns.Add(refresh_btn, 0)
        left.Add(left_btns, 0, wx.TOP, 8)

        right = wx.BoxSizer(wx.VERTICAL)
        item_filter_row = wx.BoxSizer(wx.HORIZONTAL)
        item_filter_row.Add(wx.StaticText(self, label="Item filter:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._search = wx.SearchCtrl(self, wx.ID_ANY)
        self._search.ShowCancelButton(True)
        self._search.Bind(wx.EVT_TEXT, lambda _: self._apply_filter())
        self._search.Bind(
            wx.EVT_SEARCHCTRL_CANCEL_BTN,
            lambda _: (self._search.SetValue(""), self._apply_filter()),
        )
        item_filter_row.Add(self._search, 1, wx.EXPAND)
        right.Add(item_filter_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        self._list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self._list.InsertColumn(0, "Name", width=280)
        self._list.InsertColumn(1, "Library", width=180)
        self._list.InsertColumn(2, "Footprint", width=120)
        self._list.InsertColumn(3, "3D", width=70)
        self._list.InsertColumn(4, "Description", width=420)
        self._list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_import_activated)
        self._list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_sym_selected)
        self._list.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_item_deselected)
        right.Add(self._list, 1, wx.EXPAND)

        content.Add(left, 0, wx.ALL | wx.EXPAND, 8)
        content.Add(right, 1, wx.TOP | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        main.Add(content, 1, wx.EXPAND)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self._status_label = wx.StaticText(self, label="")
        self._status_label.SetForegroundColour(_DIM_COLOUR)
        bottom.Add(self._status_label, 1, wx.ALIGN_CENTER_VERTICAL)
        self._import_btn = wx.Button(self, wx.ID_ANY, "Import selected")
        self._import_btn.Enable(False)
        self._import_btn.Bind(wx.EVT_BUTTON, self._on_import_btn)
        bottom.Add(self._import_btn, 0, wx.LEFT, 8)
        main.Add(bottom, 0, wx.ALL | wx.EXPAND, 8)

        self.SetSizer(main)
        self.Layout()

    def _general_settings(self) -> dict:
        settings = getattr(self.parent, "settings", {}) or {}
        return settings.get("general", {}) or {}

    def _current_lib_root(self) -> Optional[Path]:
        try:
            lib_dir, _uri_prefix = resolve_lib_root(
                self._general_settings(),
                Path(self.parent.project_path),
            )
            return lib_dir.resolve()
        except Exception:
            return None

    def _is_current_library_path(self, path: Path) -> bool:
        root = self._current_lib_root()
        if root is None:
            return False
        try:
            p = path.resolve()
        except Exception:
            p = path
        try:
            return p == root or root in p.parents
        except Exception:
            return False

    def _dest_lib_name(self, category_hint: str = "Misc") -> str:
        general = self._general_settings()
        return resolve_target_library_name(
            general,
            sanitize_lib_name(category_hint or "Misc"),
            sanitize=sanitize_lib_name,
            project_path=Path(self.parent.project_path),
        )

    def _get_dest_lib_path(self, category_hint: str = "Misc") -> Path:
        general = self._general_settings()
        lib_dir, _uri_prefix = resolve_lib_root(general, Path(self.parent.project_path))
        lib_name = self._dest_lib_name(category_hint)
        return lib_dir / f"{lib_name}.kicad_sym"

    def _get_dest_pretty_path(self, category_hint: str = "Misc") -> Path:
        dest = self._get_dest_lib_path(category_hint)
        return dest.parent / f"{dest.stem}.pretty"

    def _get_models_dir(self) -> Path:
        lib_dir, _uri_prefix = resolve_lib_root(
            self._general_settings(),
            Path(self.parent.project_path),
        )
        return lib_dir / "3dmodels"

    def _uri_prefix(self) -> str:
        _lib_dir, uri_prefix = resolve_lib_root(
            self._general_settings(),
            Path(self.parent.project_path),
        )
        return uri_prefix

    def _category_hint_from_source(self, src_path: Path) -> str:
        raw = sanitize_lib_name(src_path.stem or "Misc")
        general = self._general_settings()
        if not resolve_group_by_category(general, default=True):
            return raw

        base = sanitize_lib_name(
            resolve_library_base_name(
                general,
                project_path=Path(self.parent.project_path),
            )
        )
        prefix = f"{base}_"
        if raw.startswith(prefix):
            trimmed = sanitize_lib_name(raw[len(prefix):])
            return trimmed or "Misc"
        if raw == base:
            return "Misc"
        return raw

    def _refresh_connected_libs(self):
        try:
            project_path = Path(self.parent.project_path)
        except Exception:
            project_path = None

        kicad_libs = list(get_connected_sym_libs(project_path))
        elibz_libs = get_connected_elibz_libs(project_path)
        self._sym_libs = [
            (name, lib_path)
            for name, lib_path in (kicad_libs + elibz_libs)
            if not self._is_current_library_path(lib_path)
        ]
        self._fp_libs = [
            (name, lib_path)
            for name, lib_path in get_connected_fp_libs(project_path)
            if not self._is_current_library_path(lib_path)
        ]
        self._rebuild_source_choice()

    def _is_footprint_mode(self) -> bool:
        try:
            return self._mode_choice.GetSelection() == 1
        except Exception:
            return False

    def _active_libs(self) -> List[Tuple[str, Path]]:
        return self._fp_libs if self._is_footprint_mode() else self._sym_libs

    def _active_item_label(self) -> str:
        return "footprints" if self._is_footprint_mode() else "symbols"

    def _on_mode_changed(self, _evt):
        self._rebuild_source_choice()

    def _rebuild_source_choice(self):
        prev_sel = self._selected_visible_library()
        prev_sel_path = None
        if prev_sel is not None:
            try:
                prev_sel_path = prev_sel[1].resolve()
            except Exception:
                prev_sel_path = prev_sel[1]

        q_lib = self._lib_filter.GetValue().strip().lower()
        all_libs = self._active_libs()
        self._visible_libs = [
            (name, path)
            for name, path in all_libs
            if not q_lib or q_lib in name.lower()
        ]

        self._lib_list.Clear()
        self._lib_list.Append(self._ALL_LABEL)
        for name, _ in self._visible_libs:
            self._lib_list.Append(name)

        selected_idx = wx.NOT_FOUND
        if prev_sel_path is not None:
            for i, (_name, path) in enumerate(self._visible_libs, start=1):
                try:
                    cand = path.resolve()
                except Exception:
                    cand = path
                if cand == prev_sel_path:
                    selected_idx = i
                    break
        if selected_idx == wx.NOT_FOUND:
            selected_idx = 1 if self._visible_libs else 0

        if self._lib_list.GetCount() > 0:
            self._lib_list.SetSelection(selected_idx)

        if self._visible_libs:
            self._on_lib_selected(None)
        else:
            self._list.DeleteAllItems()
            self._all_items = []
            self._filtered = []
            self._import_btn.Enable(False)
            kind = "footprint" if self._is_footprint_mode() else "symbol"
            if all_libs and q_lib:
                self._set_status(f"No {kind} libraries match this filter.", dim=True)
            else:
                self._set_status(f"No {kind} libraries found.", dim=True)

    def _selected_visible_library(self) -> Optional[Tuple[str, Path]]:
        sel = self._lib_list.GetSelection()
        if sel == wx.NOT_FOUND or sel <= 0:
            return None
        idx = sel - 1
        if 0 <= idx < len(self._visible_libs):
            return self._visible_libs[idx]
        return None

    def _select_visible_library(self, path: Path, alias: str = "") -> bool:
        try:
            target = path.resolve()
        except Exception:
            target = path

        for i, (name, lib_path) in enumerate(self._visible_libs, start=1):
            try:
                cand = lib_path.resolve()
            except Exception:
                cand = lib_path
            if cand == target and (not alias or name == alias):
                self._lib_list.SetSelection(i)
                self._on_lib_selected(None)
                return True

        if alias:
            for i, (name, _lib_path) in enumerate(self._visible_libs, start=1):
                if name == alias:
                    self._lib_list.SetSelection(i)
                    self._on_lib_selected(None)
                    return True
        return False

    @staticmethod
    def _to_yes_no(value: bool) -> str:
        return "yes" if value else "no"

    def _list_symbols(self, path: Path) -> List[Tuple[str, str, str, str]]:
        if path.suffix.lower() == ".elibz":
            return self._list_symbols_elibz_with_links(path)
        return self._list_symbols_kicad_with_links(path)

    def _list_symbols_kicad_with_links(self, path: Path) -> List[Tuple[str, str, str, str]]:
        rows: List[Tuple[str, str, str, str]] = []
        src_fp_libs = self._source_fp_libs(path)
        for name, desc, fp_ref in list_symbols_kicad_meta(path):
            fp_exists = False
            has_3d = False
            if fp_ref and ":" in fp_ref:
                fp_mod = self._resolve_footprint_mod_path(fp_ref, src_fp_libs)
                if fp_mod is not None:
                    fp_exists = True
                    has_3d = self._footprint_has_3d(fp_mod)
            rows.append((name, desc, self._to_yes_no(fp_exists), self._to_yes_no(has_3d if fp_exists else False)))
        return rows

    def _list_symbols_elibz_with_links(self, path: Path) -> List[Tuple[str, str, str, str]]:
        rows: List[Tuple[str, str, str, str]] = []
        for name, desc, fp, has_3d in list_symbols_elibz_meta(path):
            fp_exists = bool(fp)
            rows.append((name, desc, self._to_yes_no(fp_exists), self._to_yes_no(has_3d if fp_exists else False)))
        return rows

    def _list_footprints(self, path: Path) -> List[Tuple[str, str, str, str]]:
        if not path.is_dir():
            return []
        out: List[Tuple[str, str, str, str]] = []
        for mod in sorted(path.glob("*.kicad_mod")):
            desc = ""
            try:
                text = mod.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'\(descr\s+"([^"]*)"', text)
                if m:
                    desc = m.group(1).strip()
            except Exception:
                pass
            out.append((mod.stem, desc, "yes", self._to_yes_no(self._footprint_has_3d(mod))))
        return out

    def _load_library(self, path: Path, alias: str):
        self._list.DeleteAllItems()
        self._all_items = []
        self._filtered = []
        self._set_status(f"Loading {path.name}…", dim=True)
        is_footprint_mode = self._is_footprint_mode()

        def _worker():
            if is_footprint_mode:
                loaded = [(n, d, path, alias, fp, has3d) for n, d, fp, has3d in self._list_footprints(path)]
            else:
                loaded = [(n, d, path, alias, fp, has3d) for n, d, fp, has3d in self._list_symbols(path)]
            wx.CallAfter(self._on_items_loaded, loaded)

        threading.Thread(target=_worker, daemon=True).start()

    def _load_all_libraries(self):
        self._list.DeleteAllItems()
        self._all_items = []
        self._filtered = []
        self._fp_model_cache.clear()
        libs = list(self._visible_libs)
        self._set_status(f"Loading {len(libs)} libraries…", dim=True)
        is_footprint_mode = self._is_footprint_mode()

        def _worker():
            collected: List[Tuple[str, str, Path, str, str, str]] = []
            for lib_name, lib_path in libs:
                try:
                    rows = (
                        self._list_footprints(lib_path)
                        if is_footprint_mode
                        else self._list_symbols(lib_path)
                    )
                    for n, d, fp, has3d in rows:
                        collected.append((n, d, lib_path, lib_name, fp, has3d))
                except Exception:
                    pass
            wx.CallAfter(self._on_items_loaded, collected)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_items_loaded(self, items: List[Tuple[str, str, Path, str, str, str]]):
        self._all_items = sorted(items, key=lambda t: t[0].lower())
        self._apply_filter()

    def _apply_filter(self):
        q = self._search.GetValue().strip().lower()
        self._filtered = [
            t
            for t in self._all_items
            if (
                not q or q in t[0].lower() or q in t[1].lower() or q in t[4].lower() or q in t[3].lower()
            )
        ]

        self._list.DeleteAllItems()
        for name, desc, _src_path, src_alias, fp_label, has3d in self._filtered:
            idx = self._list.InsertItem(self._list.GetItemCount(), name)
            self._list.SetItem(idx, 1, src_alias)
            self._list.SetItem(idx, 2, fp_label)
            self._list.SetItem(idx, 3, has3d)
            self._list.SetItem(idx, 4, desc)

        total = len(self._all_items)
        shown = len(self._filtered)
        count_str = f"{shown}/{total}" if q else str(total)
        category_hint = "Misc"
        selected_lib = self._selected_visible_library()
        if selected_lib is not None:
            category_hint = self._category_hint_from_source(selected_lib[1])
        dest_preview = (
            self._get_dest_pretty_path(category_hint)
            if self._is_footprint_mode()
            else self._get_dest_lib_path(category_hint)
        )
        self._set_status(
            f"{count_str} {self._active_item_label()}  ->  {dest_preview}",
            dim=True,
        )
        self._import_btn.Enable(False)

    def _on_lib_selected(self, _evt):
        sel = self._lib_list.GetSelection()
        if sel == 0:
            self._load_all_libraries()
        elif 1 <= sel <= len(self._visible_libs):
            lib_name, lib_path = self._visible_libs[sel - 1]
            self._load_library(lib_path, lib_name)

    def _on_browse(self, _evt):
        if self._is_footprint_mode():
            dlg = wx.DirDialog(
                self,
                message="Choose a source .pretty library folder",
                style=wx.DD_DEFAULT_STYLE,
            )
            try:
                if dlg.ShowModal() == wx.ID_OK:
                    path = Path(dlg.GetPath())
                    if not path.is_dir():
                        self._set_status("Selected path is not a folder.", colour=_ERR_COLOUR)
                        return
                    if self._is_current_library_path(path):
                        self._set_status("Current local library cannot be used as import source.", colour=_ERR_COLOUR)
                        return
                    if path.suffix.lower() != ".pretty" and not any(path.glob("*.kicad_mod")):
                        self._set_status("Folder is not a KiCad footprint library.", colour=_ERR_COLOUR)
                        return
                    alias = sanitize_lib_name(path.stem or path.name or "Custom")
                    if not any(n == alias and p.resolve() == path.resolve() for n, p in self._fp_libs):
                        self._fp_libs.append((alias, path))
                    self._lib_filter.SetValue("")
                    self._rebuild_source_choice()
                    if not self._select_visible_library(path, alias):
                        self._set_status("Added source library, but failed to select it.", colour=_ERR_COLOUR)
            finally:
                dlg.Destroy()
            return

        wildcard = (
            "Symbol libraries (*.kicad_sym;*.elibz)|*.kicad_sym;*.elibz"
            "|KiCad (*.kicad_sym)|*.kicad_sym"
            "|EasyEDA Pro (*.elibz)|*.elibz"
            "|All files (*.*)|*.*"
        )
        dlg = wx.FileDialog(
            self,
            message="Choose a source library",
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                path = Path(dlg.GetPath())
                if self._is_current_library_path(path):
                    self._set_status("Current local library cannot be used as import source.", colour=_ERR_COLOUR)
                    return
                label = f"{path.stem}  [{path.parent}]"
                if not any(n == label and p.resolve() == path.resolve() for n, p in self._sym_libs):
                    self._sym_libs.append((label, path))
                self._lib_filter.SetValue("")
                self._rebuild_source_choice()
                if not self._select_visible_library(path, label):
                    self._set_status("Added source library, but failed to select it.", colour=_ERR_COLOUR)
        finally:
            dlg.Destroy()

    def _on_sym_selected(self, _evt):
        self._import_btn.Enable(True)

    def _on_item_deselected(self, _evt):
        self._import_btn.Enable(False)

    def _on_import_activated(self, _evt):
        self._do_import_selected()

    def _on_import_btn(self, _evt):
        self._do_import_selected()

    def _do_import_selected(self):
        sel = self._list.GetFirstSelected()
        if sel < 0 or sel >= len(self._filtered):
            return
        item_name, _desc, src_path, src_alias, _fp_label, _has3d = self._filtered[sel]
        is_footprint_mode = self._is_footprint_mode()
        item_label = "footprint" if is_footprint_mode else "symbol"
        self._import_btn.Enable(False)
        self._set_status(f"Importing {item_label} {item_name}…", dim=True)

        def _worker():
            if is_footprint_mode:
                ok, msg = self._import_footprint(item_name, src_path, src_alias)
            else:
                ok, msg = self._import_symbol(item_name, src_path)
            wx.CallAfter(self._on_import_done, item_name, ok, msg)

        threading.Thread(target=_worker, daemon=True).start()

    def _import_symbol(self, symbol_name: str, src_path: Path) -> Tuple[bool, str]:
        category_hint = self._category_hint_from_source(src_path)
        dest = self._get_dest_lib_path(category_hint)
        try:
            if src_path.suffix.lower() == ".elibz":
                return self._import_from_elibz(symbol_name, src_path, dest, category_hint)
            return self._import_from_kicad_sym(symbol_name, src_path, dest, category_hint)
        except Exception as exc:
            return False, f"Import failed: {exc}"

    def _import_footprint(self, fp_name: str, src_pretty: Path, src_alias: str) -> Tuple[bool, str]:
        category_hint = self._category_hint_from_source(src_pretty)
        dest_pretty = self._get_dest_pretty_path(category_hint)
        dest_sym = self._get_dest_lib_path(category_hint)
        src_mod = src_pretty / f"{fp_name}.kicad_mod"
        if not src_mod.exists():
            return False, f"Footprint '{fp_name}' not found in source"

        try:
            dest_pretty.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_mod, dest_pretty / src_mod.name)
            model_copied = self._copy_kicad_3d_model(
                f"{src_alias}:{fp_name}",
                dest_pretty,
                [(src_alias, src_pretty)],
                source_root=src_pretty.parent,
            )
            self._update_lib_tables(dest_sym)
            parts = [f"footprint '{fp_name}' vendored"]
            if model_copied:
                parts.append("3D model copied")
            return True, " — ".join(parts)
        except Exception as exc:
            return False, f"Footprint import failed: {exc}"

    def _import_from_elibz(
        self,
        symbol_name: str,
        src_path: Path,
        dest: Path,
        category_hint: str,
    ) -> Tuple[bool, str]:
        import json
        import zipfile

        from easyeda2kicad.easyeda.easyeda_importer import (
            Easyeda3dModelImporter,
            EasyedaFootprintImporter,
            EasyedaSymbolImporter,
        )
        from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
        from easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
        from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad
        from easyeda2kicad.kicad.parameters_kicad_symbol import KicadVersion

        try:
            with zipfile.ZipFile(src_path, "r") as zf:
                payload = json.loads(zf.read("device.json").decode("utf-8"))
                device_entry = None
                for _uuid, dev in payload.get("devices", {}).items():
                    candidate = (
                        dev.get("display_title")
                        or dev.get("title")
                        or dev.get("name")
                        or dev.get("product_code")
                        or ""
                    ).strip()
                    if candidate == symbol_name:
                        device_entry = dev
                        break
                if device_entry is None:
                    return False, f"Device '{symbol_name}' not found in source"

                attrs = device_entry.get("attributes", {}) or {}
                sym_uuid = attrs.get("Symbol")
                fp_uuid = attrs.get("Footprint")
                if not sym_uuid or not fp_uuid:
                    return False, f"Device '{symbol_name}' has no linked symbol/footprint"

                sym_data = json.loads(zf.read(f"SYMBOL/{sym_uuid}.esym").decode("utf-8"))
                fp_data = json.loads(zf.read(f"FOOTPRINT/{fp_uuid}.efoo").decode("utf-8"))
        except Exception as exc:
            return False, f"Unable to read source .elibz: {exc}"

        try:
            sym_head = sym_data.setdefault("head", {})
            sym_c_para = sym_head.setdefault("c_para", {})
            if not str(sym_c_para.get("name", "")).strip():
                sym_c_para["name"] = symbol_name
            if not str(sym_c_para.get("pre", "")).strip():
                sym_c_para["pre"] = "U"

            fp_head = fp_data.setdefault("head", {})
            fp_c_para = fp_head.setdefault("c_para", {})
            if not str(fp_c_para.get("package", "")).strip():
                fp_c_para["package"] = str(attrs.get("Footprint") or symbol_name)

            if not str(sym_c_para.get("package", "")).strip():
                sym_c_para["package"] = fp_c_para.get("package", "")
        except Exception:
            pass

        product_code = str(device_entry.get("product_code") or "").strip()
        datasheet_url = str(device_entry.get("dataManualUrl") or device_entry.get("datasheet") or "").strip()
        fp_type = str(device_entry.get("footprint_type") or "").lower()

        cad_data = {
            "dataStr": sym_data,
            "packageDetail": {
                "dataStr": fp_data,
                "title": str(attrs.get("Footprint") or symbol_name),
            },
            "SMT": "smd" in fp_type,
            "lcsc": {"url": datasheet_url, "number": product_code},
        }

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest_pretty = self._get_dest_pretty_path(category_hint)
            dest_pretty.mkdir(parents=True, exist_ok=True)
            dest_models = self._get_models_dir()
            dest_models.mkdir(parents=True, exist_ok=True)

            symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
            kicad_symbol = ExporterSymbolKicad(
                symbol=symbol, kicad_version=KicadVersion.v6
            ).export(footprint_lib_name=dest.stem)
            add_or_replace_symbol(dest, symbol.info.name, kicad_symbol)

            footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()
            footprint_path = dest_pretty / f"{footprint.info.name}.kicad_mod"
            ExporterFootprintKicad(footprint=footprint).export(
                footprint_full_path=str(footprint_path),
                model_3d_path=self._model_uri_base(),
            )

            model_copied = False
            try:
                model = Easyeda3dModelImporter(
                    easyeda_cp_cad_data=cad_data, download_raw_3d_model=True
                ).output
                exporter = Exporter3dModelKicad(model_3d=model)
                if exporter.output:
                    (dest_models / f"{exporter.output.name}.wrl").write_text(
                        exporter.output.raw_wrl, encoding="utf-8"
                    )
                    model_copied = True
                if exporter.output_step and exporter.output:
                    (dest_models / f"{exporter.output.name}.step").write_bytes(exporter.output_step)
                    model_copied = True
                elif exporter.output_step and model:
                    (dest_models / f"{model.name}.step").write_bytes(exporter.output_step)
                    model_copied = True
            except Exception:
                model_copied = False

            if not model_copied:
                model_copied = self._copy_elibz_step_model(symbol_name, src_path)
                if model_copied:
                    try:
                        content = footprint_path.read_text(encoding="utf-8", errors="replace")
                        content = re.sub(r"\.wrl\"", '.step"', content)
                        footprint_path.write_text(content, encoding="utf-8")
                    except Exception:
                        pass

            self._update_lib_tables(dest)
            parts = [f"'{symbol_name}' vendored", "symbol+footprint converted"]
            if model_copied:
                parts.append("3D model copied")
            return True, " — ".join(parts)
        except Exception as exc:
            return False, f"ELIBZ → KiCad conversion failed: {exc}"

    def _copy_elibz_step_model(self, symbol_name: str, src_path: Path) -> bool:
        import json
        import zipfile

        try:
            with zipfile.ZipFile(src_path, "r") as zf:
                devices = json.loads(zf.read("device.json").decode("utf-8"))
            model_title = None
            for _uuid, dev in devices.get("devices", {}).items():
                candidate = (
                    dev.get("display_title")
                    or dev.get("title")
                    or dev.get("name")
                    or dev.get("product_code")
                    or ""
                ).strip()
                if candidate == symbol_name:
                    model_title = (dev.get("attributes") or {}).get("3D Model Title")
                    break
        except Exception:
            return False

        if not model_title:
            return False

        candidates = [
            src_path.parent / "3dmodels" / f"{model_title}.step",
            src_path.parent / "EASYEDA_MODELS" / f"{model_title}.step",
            Path(self.parent.project_path) / "3dmodels" / f"{model_title}.step",
            Path(self.parent.project_path) / "EASYEDA_MODELS" / f"{model_title}.step",
        ]
        src_step = next((p for p in candidates if p.exists()), None)
        if src_step is None:
            return False

        dest_models = self._get_models_dir()
        dest_models.mkdir(parents=True, exist_ok=True)
        dest_step = dest_models / src_step.name
        if not dest_step.exists():
            shutil.copy2(src_step, dest_step)
        return True

    def _import_from_kicad_sym(
        self,
        symbol_name: str,
        src_path: Path,
        dest: Path,
        category_hint: str,
    ) -> Tuple[bool, str]:
        block = extract_symbol_block(src_path, symbol_name)
        if block is None:
            return False, f"Symbol '{symbol_name}' not found in source"

        fp_ref = get_footprint_property(block)
        fp_copied = False
        model_copied = False
        src_fp_libs = self._source_fp_libs(src_path)

        if fp_ref and ":" in fp_ref:
            dest_pretty = self._get_dest_pretty_path(category_hint)
            fp_copied = copy_footprint_to_pretty(fp_ref, dest_pretty, src_fp_libs)
            if not fp_copied and src_path.suffix.lower() == ".kicad_sym":
                fp_name = fp_ref.split(":", 1)[1]
                fp_copied = self._copy_fp_from_sibling_pretty(src_path, fp_name, dest_pretty)
            if fp_copied:
                block = patch_footprint_property(block, dest.stem)
                model_copied = self._copy_kicad_3d_model(
                    fp_ref,
                    dest_pretty,
                    src_fp_libs,
                    source_root=src_path.parent,
                )

        add_or_replace_symbol(dest, symbol_name, block)
        self._update_lib_tables(dest)

        parts = [f"'{symbol_name}' vendored"]
        if fp_copied:
            parts.append("footprint copied")
        elif fp_ref and ":" in fp_ref:
            parts.append(f"footprint '{fp_ref}' not found")
        if model_copied:
            parts.append("3D model copied")
        return True, " — ".join(parts)

    def _copy_kicad_3d_model(
        self,
        fp_ref: str,
        dest_pretty: Path,
        fp_libs: List[Tuple[str, Path]],
        source_root: Optional[Path] = None,
    ) -> bool:
        if ":" not in fp_ref:
            return False
        lib_name, fp_name = fp_ref.split(":", 1)

        kicad_mod = dest_pretty / f"{fp_name}.kicad_mod"
        if not kicad_mod.exists():
            return False

        content = kicad_mod.read_text(encoding="utf-8", errors="replace")
        model_paths = re.findall(r'\(model\s+"([^"]+)"', content)
        if not model_paths:
            return False

        copied = False
        new_content = content
        dest_models_dir = self._get_models_dir()
        models_base = self._model_uri_base().rstrip("/")

        for model_path in model_paths:
            if any(model_path.startswith(p) for p in _BUILTIN_3D_PREFIXES):
                continue

            src_model = self._resolve_model_path(
                model_path,
                lib_name,
                fp_libs=fp_libs,
                source_root=source_root,
            )
            if src_model is None or not src_model.exists():
                legacy_ref = self._normalize_legacy_model_ref(model_path, models_base)
                if legacy_ref and legacy_ref != model_path:
                    new_content = new_content.replace(f'"{model_path}"', f'"{legacy_ref}"', 1)
                    copied = True
                continue

            dest_models_dir.mkdir(parents=True, exist_ok=True)
            dest_model = dest_models_dir / src_model.name
            if not dest_model.exists():
                shutil.copy2(src_model, dest_model)

            new_ref = f"{models_base}/{dest_model.name}"
            new_content = new_content.replace(f'"{model_path}"', f'"{new_ref}"', 1)
            copied = True

        if new_content != content:
            kicad_mod.write_text(new_content, encoding="utf-8")
        return copied

    def _resolve_footprint_mod_path(
        self,
        fp_ref: str,
        src_fp_libs: List[Tuple[str, Path]],
    ) -> Optional[Path]:
        if ":" not in fp_ref:
            return None
        lib_name, fp_name = fp_ref.split(":", 1)
        for alias, pretty in src_fp_libs:
            if alias != lib_name:
                continue
            mod = pretty / f"{fp_name}.kicad_mod"
            if mod.exists():
                return mod
        return None

    def _footprint_has_3d(self, kicad_mod: Path) -> bool:
        key = str(kicad_mod.resolve())
        if key in self._fp_model_cache:
            return self._fp_model_cache[key]
        has_3d = False
        try:
            content = kicad_mod.read_text(encoding="utf-8", errors="replace")
            has_3d = bool(re.search(r'\(model\s+"[^"]+"', content))
        except Exception:
            has_3d = False
        self._fp_model_cache[key] = has_3d
        return has_3d

    def _resolve_model_path(
        self,
        model_path: str,
        lib_name: str,
        fp_libs: List[Tuple[str, Path]],
        source_root: Optional[Path] = None,
    ) -> Optional[Path]:
        src_pretty: Optional[Path] = None
        for name, p in fp_libs:
            if name == lib_name:
                src_pretty = p
                break

        original = model_path
        resolved = model_path
        if "${KIPRJMOD}" in resolved:
            if src_pretty is not None:
                resolved = resolved.replace("${KIPRJMOD}", str(src_pretty.parent))
            elif source_root is not None:
                resolved = resolved.replace("${KIPRJMOD}", str(source_root))
            else:
                resolved = resolved.replace("${KIPRJMOD}", str(self.parent.project_path))

        p = Path(resolved)
        if p.is_absolute() and p.exists():
            return p

        if src_pretty is not None:
            candidate = src_pretty.parent / resolved
            if candidate.exists():
                return candidate
            candidate = src_pretty / resolved
            if candidate.exists():
                return candidate

        if source_root is not None:
            candidate = source_root / resolved
            if candidate.exists():
                return candidate

        if "${KIPRJMOD}" in original:
            alt = original.replace("${KIPRJMOD}", str(self.parent.project_path))
            p2 = Path(alt)
            if p2.is_absolute() and p2.exists():
                return p2

        if "EASYEDA_MODELS" in original:
            alt2 = original.replace("EASYEDA_MODELS", "3dmodels")
            if "${KIPRJMOD}" in alt2:
                alt2 = alt2.replace("${KIPRJMOD}", str(self.parent.project_path))
            p3 = Path(alt2)
            if p3.is_absolute() and p3.exists():
                return p3

        return None

    @staticmethod
    def _normalize_legacy_model_ref(model_path: str, models_base: str) -> Optional[str]:
        norm = model_path.replace("\\", "/")
        marker = "/EASYEDA_MODELS/"
        if marker not in norm:
            return None
        tail = norm.split(marker, 1)[1].lstrip("/")
        if not tail:
            return None
        return f"{models_base}/{tail}"

    def _source_fp_libs(self, src_sym_path: Path) -> List[Tuple[str, Path]]:
        libs = list(self._fp_libs)
        if src_sym_path.suffix.lower() == ".kicad_sym":
            sibling = src_sym_path.with_suffix(".pretty")
            if sibling.is_dir():
                alias = src_sym_path.stem
                if not any(name == alias and p.resolve() == sibling.resolve() for name, p in libs):
                    libs.append((alias, sibling))
        return libs

    @staticmethod
    def _copy_fp_from_sibling_pretty(src_sym_path: Path, fp_name: str, dest_pretty: Path) -> bool:
        try:
            src = src_sym_path.with_suffix(".pretty") / f"{fp_name}.kicad_mod"
            if not src.exists():
                return False
            dest_pretty.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_pretty / f"{fp_name}.kicad_mod")
            return True
        except Exception:
            return False

    def _model_uri_base(self) -> str:
        models_dir = self._get_models_dir().resolve()
        try:
            rel = os.path.relpath(models_dir, Path(self.parent.project_path).resolve())
            return "${KIPRJMOD}/" + Path(rel).as_posix()
        except Exception:
            return "${KIPRJMOD}/3dmodels"

    def _update_lib_tables(self, dest: Path) -> None:
        try:
            general = self._general_settings()
            scope = str(general.get("library_scope", "project")).strip().lower()
            if scope == "shared":
                shared_root = dest.parent
                default_name = resolve_library_base_name(
                    general,
                    project_path=Path(self.parent.project_path),
                )
                ensure_shared_meta(shared_root, default_name, log=lambda _: None)
                _shared_root, shared_uri_prefix = resolve_lib_root(
                    general,
                    Path(self.parent.project_path),
                )
                LibTablesManager(shared_root, log=lambda _: None).ensure_project_lib_tables(
                    shared_root,
                    use_project_relative=False,
                    uri_prefix=shared_uri_prefix,
                )
                ensure_project_table_links(
                    Path(self.parent.project_path),
                    shared_root,
                    log=lambda _: None,
                )
            else:
                LibTablesManager(Path(self.parent.project_path), log=lambda _: None).ensure_project_lib_tables(
                    dest.parent,
                    uri_prefix=self._uri_prefix(),
                )
            ensure_project_legacy_models_link(
                Path(self.parent.project_path),
                self._get_models_dir(),
                log=lambda _: None,
            )
        except Exception:
            pass

    def _on_import_done(self, _symbol_name: str, ok: bool, msg: str):
        self._import_btn.Enable(True)
        colour = _OK_COLOUR if ok else _ERR_COLOUR
        self._set_status(("✓ " if ok else "✗ ") + msg, colour=colour)

    def _set_status(self, text: str, dim: bool = False, colour: Optional[wx.Colour] = None):
        try:
            c = colour if colour is not None else (_DIM_COLOUR if dim else wx.NullColour)
            self._status_label.SetForegroundColour(c)
            self._status_label.SetLabel(text)
            self.Layout()
        except Exception:
            pass
