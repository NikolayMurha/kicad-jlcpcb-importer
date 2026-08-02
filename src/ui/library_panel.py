"""Library Manager dialog for vendoring symbols/footprints into local KiCad libraries.

Destination is always KiCad format:
- <Library Name Prefix>_<Category>.kicad_sym (when grouping is enabled)
- <Library Name>.kicad_sym (when grouping is disabled)
- <project lib path>/3dmodels/*
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import wx

from ..core.helpers import HighResWxSize, sanitize_lib_name, apply_button_label_tooltips
from ..core.lib_paths import (
    resolve_group_by_category,
    resolve_lib_root,
    resolve_library_base_name,
    resolve_target_library_name,
)
from ..core.sym_lib_reader import (
    get_connected_elibz_libs,
    get_connected_fp_libs,
    get_connected_sym_libs,
    list_symbols_elibz_meta,
    list_symbols_kicad_meta,
)
from ..core.library_vendor import LibraryVendorService

_OK_COLOUR = wx.Colour(0, 128, 0)
_ERR_COLOUR = wx.Colour(180, 0, 0)
_DIM_COLOUR = wx.Colour(100, 100, 100)


class LibraryManagerDialog(wx.Dialog):
    """Browse connected/custom libraries and vendor selected items locally."""

    _ALL_LABEL = "— All libraries —"

    def __init__(self, parent, picker_mode: bool = False, picker_kind: str = "symbol"):
        picker_mode_flag = bool(picker_mode)
        picker_kind_key = str(picker_kind or "symbol").strip().lower()
        title = (
            "Select Footprints from Libraries"
            if picker_mode_flag and picker_kind_key == "footprint"
            else ("Select Symbols from Libraries" if picker_mode_flag else "Import from other libraries")
        )
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
            size=HighResWxSize(parent.window, wx.Size(940, 560)),
        )
        self.parent = parent
        self._picker_mode = picker_mode_flag
        self._picker_kind = picker_kind_key
        self._picked_items: List[Tuple[str, str, Path, str, str, str]] = []

        self._sym_libs: List[Tuple[str, Path]] = []
        self._fp_libs: List[Tuple[str, Path]] = []
        self._visible_libs: List[Tuple[str, Path]] = []
        self._all_items: List[Tuple[str, str, Path, str, str, str]] = []
        self._filtered: List[Tuple[str, str, Path, str, str, str]] = []
        self._fp_model_cache: dict[str, bool] = {}
        self._import_running = False

        self._build_ui()
        if self._picker_mode:
            if self._picker_kind == "footprint":
                self._mode_choice.SetSelection(1)
            else:
                self._mode_choice.SetSelection(0)
            self._mode_choice.Enable(False)
        apply_button_label_tooltips(self, overwrite=True)
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
        self._browse_btn = browse_btn
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

        self._list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
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
        self._import_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Use selected" if self._picker_mode else "Import selected",
        )
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
        return lib_dir / lib_name / f"{lib_name}.kicad_sym"

    def _get_dest_pretty_path(self, category_hint: str = "Misc") -> Path:
        dest = self._get_dest_lib_path(category_hint)
        return dest.parent / f"{dest.stem}.pretty"

    def _category_hint_from_source(self, src_path: Path) -> str:
        raw = sanitize_lib_name(src_path.stem or "Misc")
        general = self._general_settings()
        if not resolve_group_by_category(general, default=False):
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

        if self._picker_mode:
            # Mapping picker must expose only stable KiCad nicknames that can be
            # resolved by builtin mapping logic; skip project tables and custom browse sources.
            self._sym_libs = [
                (name, lib_path)
                for name, lib_path in get_connected_sym_libs(
                    project_path,
                    include_project_tables=False,
                    include_user_tables=False,
                )
                if not self._is_current_library_path(lib_path)
            ]
            self._fp_libs = [
                (name, lib_path)
                for name, lib_path in get_connected_fp_libs(
                    project_path,
                    include_project_tables=False,
                    include_user_tables=False,
                )
                if not self._is_current_library_path(lib_path)
            ]
            try:
                self._browse_btn.Enable(False)
            except Exception:
                pass
        else:
            kicad_libs = list(get_connected_sym_libs(project_path, include_user_tables=False))
            elibz_libs = get_connected_elibz_libs(project_path, include_user_tables=False)
            self._sym_libs = [
                (name, lib_path)
                for name, lib_path in (kicad_libs + elibz_libs)
                if not self._is_current_library_path(lib_path)
            ]
            self._fp_libs = [
                (name, lib_path)
                for name, lib_path in get_connected_fp_libs(project_path, include_user_tables=False)
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
            self._sync_import_button_state()
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
        self._sync_import_button_state()

    def _on_lib_selected(self, _evt):
        sel = self._lib_list.GetSelection()
        if sel == 0:
            self._load_all_libraries()
        elif 1 <= sel <= len(self._visible_libs):
            lib_name, lib_path = self._visible_libs[sel - 1]
            self._load_library(lib_path, lib_name)

    def _on_browse(self, _evt):
        if self._picker_mode:
            return
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
        self._sync_import_button_state()

    def _on_item_deselected(self, _evt):
        self._sync_import_button_state()

    def _on_import_activated(self, _evt):
        self._do_import_selected()

    def _on_import_btn(self, _evt):
        self._do_import_selected()

    def _selected_filtered_items(self) -> List[Tuple[str, str, Path, str, str, str]]:
        out: List[Tuple[str, str, Path, str, str, str]] = []
        sel = self._list.GetFirstSelected()
        while sel != -1:
            if 0 <= sel < len(self._filtered):
                out.append(self._filtered[sel])
            sel = self._list.GetNextSelected(sel)
        return out

    def _sync_import_button_state(self):
        try:
            enabled = (not self._import_running) and self._list.GetSelectedItemCount() > 0
            self._import_btn.Enable(enabled)
        except Exception:
            self._import_btn.Enable(False)

    def _build_vendor_service(self) -> LibraryVendorService:
        return LibraryVendorService(
            project_path=Path(self.parent.project_path),
            general_settings=self._general_settings(),
            fp_libs=self._fp_libs,
        )

    def _do_import_selected(self):
        selected_items = self._selected_filtered_items()
        if not selected_items:
            return
        if self._picker_mode:
            self._picked_items = list(selected_items)
            self.EndModal(wx.ID_OK)
            return
        is_footprint_mode = self._is_footprint_mode()
        vendor = self._build_vendor_service()
        item_label = "footprint" if is_footprint_mode else "symbol"
        self._import_running = True
        self._sync_import_button_state()
        total = len(selected_items)
        if total == 1:
            self._set_status(f"Importing {item_label} {selected_items[0][0]}…", dim=True)
        else:
            self._set_status(f"Importing {total} {self._active_item_label()}…", dim=True)

        def _worker():
            results: List[Tuple[str, bool, str]] = []
            for idx, (item_name, _desc, src_path, src_alias, _fp_label, _has3d) in enumerate(selected_items, start=1):
                wx.CallAfter(self._set_status, f"Importing {item_label} {item_name}… ({idx}/{total})", True)
                category_hint = self._category_hint_from_source(src_path)
                if is_footprint_mode:
                    ok, msg = vendor.import_footprint(item_name, src_path, src_alias, category_hint)
                else:
                    ok, msg = vendor.import_symbol(item_name, src_path, category_hint)
                results.append((item_name, ok, msg))
            wx.CallAfter(self._on_import_batch_done, results)

        threading.Thread(target=_worker, daemon=True).start()

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

    def _source_fp_libs(self, src_sym_path: Path) -> List[Tuple[str, Path]]:
        libs = list(self._fp_libs)
        if src_sym_path.suffix.lower() == ".kicad_sym":
            sibling = src_sym_path.with_suffix(".pretty")
            if sibling.is_dir():
                alias = src_sym_path.stem
                if not any(name == alias and p.resolve() == sibling.resolve() for name, p in libs):
                    libs.append((alias, sibling))
        return libs

    def _on_import_batch_done(self, results: List[Tuple[str, bool, str]]):
        self._import_running = False
        self._sync_import_button_state()
        if not results:
            self._set_status("No items imported.", dim=True)
            return

        if len(results) == 1:
            _name, ok, msg = results[0]
            colour = _OK_COLOUR if ok else _ERR_COLOUR
            self._set_status(("✓ " if ok else "✗ ") + msg, colour=colour)
            return

        ok_count = sum(1 for _n, ok, _m in results if ok)
        fail = [(n, m) for n, ok, m in results if not ok]
        if not fail:
            self._set_status(
                f"✓ Imported {ok_count}/{len(results)} {self._active_item_label()}",
                colour=_OK_COLOUR,
            )
            return

        first_name, first_msg = fail[0]
        suffix = f"; +{len(fail) - 1} more failed" if len(fail) > 1 else ""
        self._set_status(
            f"✗ Imported {ok_count}/{len(results)}. First error: {first_name}: {first_msg}{suffix}",
            colour=_ERR_COLOUR,
        )

    def _set_status(self, text: str, dim: bool = False, colour: Optional[wx.Colour] = None):
        try:
            c = colour if colour is not None else (_DIM_COLOUR if dim else wx.NullColour)
            self._status_label.SetForegroundColour(c)
            self._status_label.SetLabel(text)
            self.Layout()
        except Exception:
            pass

    def get_picked_items(self) -> List[Tuple[str, str, Path, str, str, str]]:
        return list(self._picked_items)
