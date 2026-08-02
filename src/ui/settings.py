"""Contains the settings dialog for the LCSC plugin."""

import logging
import os
from pathlib import Path

import wx  # pylint: disable=import-error

from ..core.events import UpdateSetting
from ..core.helpers import HighResWxSize, loadBitmapScaled, apply_button_label_tooltips, as_bool
from ..core.lib_paths import (
    DEFAULT_LIB_PATH,
    DEFAULT_LIB_DIR_NAME,
    resolve_group_by_category,
    resolve_library_base_name,
)


class MappingIndexSettingsDialog(wx.Dialog):
    """Dialog for KiCad mapping and indexing settings."""

    _DEFAULT_KINDS = [
        "default",
        "resistor",
        "capacitor",
        "inductor",
        "diode",
        "led",
        "bjt",
        "fet",
        "ic",
    ]

    def __init__(self, parent, general: dict):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="KiCad Mapping and Indexing Settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(760, 820)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self._main_window = parent
        # Proxy fields so nested pickers can use this dialog as parent while
        # still accessing project/settings context expected by LibraryManagerDialog.
        self.window = getattr(parent, "window", parent)
        self.settings = getattr(parent, "settings", {})
        self.project_path = getattr(parent, "project_path", "")
        self._symbol_map_model = self._normalize_map_dict(general.get("kicad_symbol_map"))
        self._footprint_map_model = self._normalize_map_dict(general.get("kicad_footprint_map"))
        self._values = {}

        root = wx.BoxSizer(wx.VERTICAL)

        self.kicad_builtin_first_ctrl = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Prefer KiCad built-in symbol/footprint mapping before EasyEDA import",
        )
        self.kicad_builtin_first_ctrl.SetValue(as_bool(general.get("kicad_builtin_first"), True))
        root.Add(self.kicad_builtin_first_ctrl, 0, wx.ALL | wx.EXPAND, 8)

        grid = wx.FlexGridSizer(11, 2, 8, 12)
        grid.AddGrowableCol(1, 1)

        label = wx.StaticText(self, wx.ID_ANY, "Symbol index max libraries:")
        label.SetToolTip("Maximum number of symbol libraries scanned while building the symbol index.")
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.symbol_index_max_libs_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=20,
            max=5000,
            initial=self._as_int(general.get("kicad_symbol_index_max_libs"), 300, 20, 5000),
        )
        self.symbol_index_max_libs_ctrl.SetToolTip("Maximum number of symbol libraries scanned while building the symbol index.")
        grid.Add(self.symbol_index_max_libs_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Symbol index cache TTL (seconds):")
        label.SetToolTip("How long to keep the symbol index cache. 0 disables time-based expiration.")
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.symbol_index_ttl_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=31536000,
            initial=self._as_int(general.get("kicad_symbol_index_ttl_sec"), 86400, 0, 31536000),
        )
        self.symbol_index_ttl_ctrl.SetToolTip("How long to keep the symbol index cache. 0 disables time-based expiration.")
        grid.Add(self.symbol_index_ttl_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint fuzzy scan max libraries:")
        label.SetToolTip("Maximum number of footprint libraries scanned during fuzzy footprint matching.")
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_fuzzy_max_libs_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=1,
            max=1000,
            initial=self._as_int(general.get("kicad_footprint_fuzzy_max_libs"), 12, 1, 1000),
        )
        self.fp_fuzzy_max_libs_ctrl.SetToolTip("Maximum number of footprint libraries scanned during fuzzy footprint matching.")
        grid.Add(self.fp_fuzzy_max_libs_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint fuzzy scan max files per library:")
        label.SetToolTip("Maximum .kicad_mod files scanned per library during fuzzy footprint matching.")
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_fuzzy_max_files_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=100,
            max=200000,
            initial=self._as_int(
                general.get("kicad_footprint_fuzzy_max_files_per_lib"),
                2500,
                100,
                200000,
            ),
        )
        self.fp_fuzzy_max_files_ctrl.SetToolTip("Maximum .kicad_mod files scanned per library during fuzzy footprint matching.")
        grid.Add(self.fp_fuzzy_max_files_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Symbol wildcard min score:")
        label.SetToolTip(
            "Minimum score for wildcard symbol candidates (example: PART*). "
            "Higher value reduces false positives but increases misses."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.symbol_wildcard_min_score_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=500,
            initial=self._as_int(general.get("kicad_symbol_wildcard_min_score"), 80, 0, 500),
        )
        self.symbol_wildcard_min_score_ctrl.SetToolTip(
            "Minimum score for wildcard symbol candidates (example: PART*). "
            "Higher value reduces false positives but increases misses."
        )
        grid.Add(self.symbol_wildcard_min_score_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Symbol wildcard min gap:")
        label.SetToolTip(
            "Minimum score gap between the best and second wildcard symbol candidate. "
            "If gap is too small, match is rejected as ambiguous."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.symbol_wildcard_min_gap_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=200,
            initial=self._as_int(general.get("kicad_symbol_wildcard_min_gap"), 14, 0, 200),
        )
        self.symbol_wildcard_min_gap_ctrl.SetToolTip(
            "Minimum score gap between the best and second wildcard symbol candidate. "
            "If gap is too small, match is rejected as ambiguous."
        )
        grid.Add(self.symbol_wildcard_min_gap_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint fuzzy min score (passive):")
        label.SetToolTip(
            "Minimum fuzzy score for passives (R/C/L/diode/LED/transistor families). "
            "Higher value is stricter and reduces false positives."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_fuzzy_min_score_passive_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=400,
            initial=self._as_int(general.get("kicad_footprint_fuzzy_min_score_passive"), 70, 0, 400),
        )
        self.fp_fuzzy_min_score_passive_ctrl.SetToolTip(
            "Minimum fuzzy score for passives (R/C/L/diode/LED/transistor families). "
            "Higher value is stricter and reduces false positives."
        )
        grid.Add(self.fp_fuzzy_min_score_passive_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint fuzzy min score (IC):")
        label.SetToolTip(
            "Minimum fuzzy score for IC-like packages. "
            "Higher value is stricter and reduces false positives."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_fuzzy_min_score_ic_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=400,
            initial=self._as_int(general.get("kicad_footprint_fuzzy_min_score_ic"), 95, 0, 400),
        )
        self.fp_fuzzy_min_score_ic_ctrl.SetToolTip(
            "Minimum fuzzy score for IC-like packages. "
            "Higher value is stricter and reduces false positives."
        )
        grid.Add(self.fp_fuzzy_min_score_ic_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint fuzzy min gap (passive):")
        label.SetToolTip(
            "Minimum score difference between best and second passive footprint candidate. "
            "Smaller gap is treated as ambiguous and rejected."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_fuzzy_min_gap_passive_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=200,
            initial=self._as_int(general.get("kicad_footprint_fuzzy_min_gap_passive"), 16, 0, 200),
        )
        self.fp_fuzzy_min_gap_passive_ctrl.SetToolTip(
            "Minimum score difference between best and second passive footprint candidate. "
            "Smaller gap is treated as ambiguous and rejected."
        )
        grid.Add(self.fp_fuzzy_min_gap_passive_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint fuzzy min gap (non-passive):")
        label.SetToolTip(
            "Minimum score difference between best and second non-passive candidate. "
            "Smaller gap is treated as ambiguous and rejected."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_fuzzy_min_gap_nonpassive_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=200,
            initial=self._as_int(general.get("kicad_footprint_fuzzy_min_gap_nonpassive"), 12, 0, 200),
        )
        self.fp_fuzzy_min_gap_nonpassive_ctrl.SetToolTip(
            "Minimum score difference between best and second non-passive candidate. "
            "Smaller gap is treated as ambiguous and rejected."
        )
        grid.Add(self.fp_fuzzy_min_gap_nonpassive_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Footprint strict package+pin min gap:")
        label.SetToolTip(
            "Minimum score difference for strict package+pin pass (example QFN-44, SOT-23-5). "
            "If candidates are too close, strict match is rejected as ambiguous."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_strict_pkg_pin_min_gap_ctrl = wx.SpinCtrl(
            self,
            wx.ID_ANY,
            min=0,
            max=200,
            initial=self._as_int(general.get("kicad_footprint_strict_pkg_pin_min_gap"), 14, 0, 200),
        )
        self.fp_strict_pkg_pin_min_gap_ctrl.SetToolTip(
            "Minimum score difference for strict package+pin pass (example QFN-44, SOT-23-5). "
            "If candidates are too close, strict match is rejected as ambiguous."
        )
        grid.Add(self.fp_strict_pkg_pin_min_gap_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Require package token overlap:")
        label.SetToolTip(
            "Require overlap between extracted package tokens and footprint name tokens in fuzzy/strict passes. "
            "Keeps matching conservative and reduces false positives."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_require_keyword_overlap_ctrl = wx.CheckBox(self, wx.ID_ANY, "")
        self.fp_require_keyword_overlap_ctrl.SetValue(
            as_bool(general.get("kicad_footprint_require_keyword_overlap"), True)
        )
        self.fp_require_keyword_overlap_ctrl.SetToolTip(
            "Require overlap between extracted package tokens and footprint name tokens in fuzzy/strict passes. "
            "Keeps matching conservative and reduces false positives."
        )
        grid.Add(self.fp_require_keyword_overlap_ctrl, 1, wx.EXPAND)

        label = wx.StaticText(self, wx.ID_ANY, "Passive size-hint strict mode:")
        label.SetToolTip(
            "When passive package size hint exists (example 0402/1005), require strict chip-size match. "
            "If strict match is missing, fuzzy fallback is skipped."
        )
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.fp_passive_size_strict_mode_ctrl = wx.CheckBox(self, wx.ID_ANY, "")
        self.fp_passive_size_strict_mode_ctrl.SetValue(
            as_bool(general.get("kicad_footprint_passive_size_strict_mode"), True)
        )
        self.fp_passive_size_strict_mode_ctrl.SetToolTip(
            "When passive package size hint exists (example 0402/1005), require strict chip-size match. "
            "If strict match is missing, fuzzy fallback is skipped."
        )
        grid.Add(self.fp_passive_size_strict_mode_ctrl, 1, wx.EXPAND)
        root.Add(grid, 0, wx.ALL | wx.EXPAND, 8)

        guidance = wx.StaticText(
            self,
            wx.ID_ANY,
            (
                "How it works: entries in these lists are checked first for matching component kinds. "
                "If nothing matches, importer falls back to fuzzy footprint matching, "
                "then to EasyEDA import."
            ),
        )
        guidance.Wrap(HighResWxSize(parent.window, wx.Size(700, -1)).GetWidth())
        root.Add(guidance, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self._build_mapping_section(
            root,
            item_kind="symbol",
            title="Symbol Mapping Priority",
            insert_button_label="Insert Symbol from Libraries",
        )
        self._build_mapping_section(
            root,
            item_kind="footprint",
            title="Footprint Mapping Priority",
            insert_button_label="Insert Footprint from Libraries",
        )

        btn_sizer = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if btn_sizer is not None:
            root.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizer(root)
        self._refresh_map_list("symbol")
        self._refresh_map_list("footprint")
        self.Layout()
        self.CentreOnParent()
        apply_button_label_tooltips(self, overwrite=False)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    @staticmethod
    def _as_int(value, default: int, min_v: int, max_v: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(min_v, min(max_v, parsed))

    @staticmethod
    def _normalize_kind(raw_value: str) -> str:
        kind = str(raw_value or "").strip().lower()
        return kind or "default"

    @staticmethod
    def _normalize_map_dict(value) -> dict:
        if not isinstance(value, dict):
            return {}
        out = {}
        for key, entry in value.items():
            kind = MappingIndexSettingsDialog._normalize_kind(str(key))
            if isinstance(entry, (list, tuple)):
                items_raw = list(entry)
            else:
                items_raw = [entry]
            items = []
            for item in items_raw:
                text = str(item or "").strip()
                if text and text not in items:
                    items.append(text)
            if items:
                out[kind] = items
        return out

    def _build_mapping_section(
        self,
        root: wx.BoxSizer,
        item_kind: str,
        title: str,
        insert_button_label: str,
    ) -> None:
        section = wx.StaticBoxSizer(wx.VERTICAL, self, label=title)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, wx.ID_ANY, "Component kind:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        kind_ctrl = wx.ComboBox(
            self,
            wx.ID_ANY,
            "default",
            choices=self._DEFAULT_KINDS,
            style=wx.CB_READONLY,
        )
        kind_ctrl.Bind(wx.EVT_COMBOBOX, lambda _evt, k=item_kind: self._refresh_map_list(k))
        row.Add(kind_ctrl, 1, wx.EXPAND)
        section.Add(row, 0, wx.ALL | wx.EXPAND, 6)

        list_ctrl = wx.ListBox(self, wx.ID_ANY)
        list_ctrl.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 120)))
        section.Add(list_ctrl, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        buttons = wx.BoxSizer(wx.HORIZONTAL)

        insert_btn = wx.Button(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.window, wx.Size(36, 28)),
        )
        insert_btn.SetBitmap(loadBitmapScaled("mdi-database-import-outline.png", self._main_window.scale_factor))
        insert_btn.SetBitmapMargins((2, 0))
        insert_btn.Bind(wx.EVT_BUTTON, lambda _evt, k=item_kind: self._on_insert_from_libraries(k))
        buttons.Add(insert_btn, 0, wx.RIGHT, 4)

        add_btn = wx.Button(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.window, wx.Size(36, 28)),
        )
        add_btn.SetBitmap(loadBitmapScaled("mdi-plus.png", self._main_window.scale_factor))
        add_btn.SetBitmapMargins((2, 0))
        add_btn.SetMinSize(HighResWxSize(self.window, wx.Size(36, 28)))
        add_btn.Bind(wx.EVT_BUTTON, lambda _evt, k=item_kind: self._on_add_entry(k))
        buttons.Add(add_btn, 0, wx.RIGHT, 4)

        edit_btn = wx.Button(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.window, wx.Size(36, 28)),
        )
        edit_btn.SetBitmap(loadBitmapScaled("mdi-pencil.png", self._main_window.scale_factor))
        edit_btn.SetBitmapMargins((2, 0))
        edit_btn.SetMinSize(HighResWxSize(self.window, wx.Size(36, 28)))
        edit_btn.Bind(wx.EVT_BUTTON, lambda _evt, k=item_kind: self._on_edit_entry(k))
        buttons.Add(edit_btn, 0, wx.RIGHT, 4)

        remove_btn = wx.Button(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.window, wx.Size(36, 28)),
        )
        remove_btn.SetBitmap(loadBitmapScaled("mdi-trash-can-outline.png", self._main_window.scale_factor))
        remove_btn.SetBitmapMargins((2, 0))
        remove_btn.Bind(wx.EVT_BUTTON, lambda _evt, k=item_kind: self._on_remove_entry(k))
        buttons.Add(remove_btn, 0, wx.RIGHT, 4)
        
        up_btn = wx.Button(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.window, wx.Size(36, 28)),
        )
        up_btn.SetBitmap(loadBitmapScaled("mdi-chevron-up.png", self._main_window.scale_factor))
        up_btn.SetBitmapMargins((2, 0))
        up_btn.Bind(wx.EVT_BUTTON, lambda _evt, k=item_kind: self._on_move_entry(k, -1))
        buttons.Add(up_btn, 0, wx.RIGHT, 4)

        down_btn = wx.Button(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.window, wx.Size(36, 28)),
        )
        down_btn.SetBitmap(loadBitmapScaled("mdi-chevron-down.png", self._main_window.scale_factor))
        down_btn.SetBitmapMargins((2, 0))
        down_btn.Bind(wx.EVT_BUTTON, lambda _evt, k=item_kind: self._on_move_entry(k, 1))
        buttons.Add(down_btn, 0)
        
        insert_btn.SetToolTip("Insert from Libraries")
        add_btn.SetToolTip("Add Entry")
        edit_btn.SetToolTip("Edit Entry")
        remove_btn.SetToolTip("Remove Entry")
        up_btn.SetToolTip("Move Up")
        down_btn.SetToolTip("Move Down")
        section.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        setattr(self, f"{item_kind}_kind_ctrl", kind_ctrl)
        setattr(self, f"{item_kind}_list_ctrl", list_ctrl)
        root.Add(section, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

    def _model_for_kind(self, item_kind: str) -> dict:
        return self._symbol_map_model if item_kind == "symbol" else self._footprint_map_model

    def _current_kind(self, item_kind: str) -> str:
        ctrl = getattr(self, f"{item_kind}_kind_ctrl")
        return self._normalize_kind(ctrl.GetValue())

    def _current_entries(self, item_kind: str) -> list[str]:
        model = self._model_for_kind(item_kind)
        return list(model.get(self._current_kind(item_kind), []))

    def _store_entries(self, item_kind: str, entries: list[str]) -> None:
        model = self._model_for_kind(item_kind)
        kind = self._current_kind(item_kind)
        cleaned = [str(v or "").strip() for v in entries if str(v or "").strip()]
        unique: list[str] = []
        for entry in cleaned:
            if entry not in unique:
                unique.append(entry)
        if unique:
            model[kind] = unique
        elif kind in model:
            del model[kind]

    def _refresh_map_list(self, item_kind: str) -> None:
        list_ctrl: wx.ListBox = getattr(self, f"{item_kind}_list_ctrl")
        list_ctrl.Clear()
        for entry in self._current_entries(item_kind):
            list_ctrl.Append(entry)

    def _pick_library_refs(self, item_kind: str) -> list[str]:
        from .library_panel import LibraryManagerDialog

        dlg = None
        try:
            dlg = LibraryManagerDialog(
                self,
                picker_mode=True,
                picker_kind=item_kind,
            )
            if dlg.ShowModal() != wx.ID_OK:
                return []
            refs: list[str] = []
            for name, _desc, _src_path, src_alias, _fp_label, _has3d in dlg.get_picked_items():
                alias = str(src_alias or "").strip()
                symbol_or_fp = str(name or "").strip()
                if not alias or not symbol_or_fp:
                    continue
                ref = f"{alias}:{symbol_or_fp}"
                if ref not in refs:
                    refs.append(ref)
            return refs
        finally:
            if dlg is not None:
                dlg.Destroy()

    def _append_refs_to_model(self, item_kind: str, refs: list[str]) -> None:
        if not refs:
            return
        bucket = self._current_entries(item_kind)
        for ref in refs:
            if ref not in bucket:
                bucket.append(ref)
        self._store_entries(item_kind, bucket)
        self._refresh_map_list(item_kind)

    def _on_insert_from_libraries(self, item_kind: str):
        self._append_refs_to_model(item_kind, self._pick_library_refs(item_kind))

    def _on_add_entry(self, item_kind: str):
        dlg = wx.TextEntryDialog(
            self,
            f"Enter {item_kind} reference (for example Lib:Name):",
            "Add Mapping Entry",
            "",
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            value = str(dlg.GetValue() or "").strip()
            if not value:
                return
            self._append_refs_to_model(item_kind, [value])
        finally:
            dlg.Destroy()

    def _on_edit_entry(self, item_kind: str):
        list_ctrl: wx.ListBox = getattr(self, f"{item_kind}_list_ctrl")
        idx = list_ctrl.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        entries = self._current_entries(item_kind)
        if idx < 0 or idx >= len(entries):
            return
        dlg = wx.TextEntryDialog(
            self,
            f"Edit {item_kind} reference:",
            "Edit Mapping Entry",
            entries[idx],
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            value = str(dlg.GetValue() or "").strip()
            if not value:
                return
            entries[idx] = value
            self._store_entries(item_kind, entries)
            self._refresh_map_list(item_kind)
            list_ctrl.SetSelection(idx)
        finally:
            dlg.Destroy()

    def _on_remove_entry(self, item_kind: str):
        list_ctrl: wx.ListBox = getattr(self, f"{item_kind}_list_ctrl")
        idx = list_ctrl.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        entries = self._current_entries(item_kind)
        if idx < 0 or idx >= len(entries):
            return
        entries.pop(idx)
        self._store_entries(item_kind, entries)
        self._refresh_map_list(item_kind)
        if entries:
            list_ctrl.SetSelection(min(idx, len(entries) - 1))

    def _on_move_entry(self, item_kind: str, step: int):
        list_ctrl: wx.ListBox = getattr(self, f"{item_kind}_list_ctrl")
        idx = list_ctrl.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        entries = self._current_entries(item_kind)
        if idx < 0 or idx >= len(entries):
            return
        new_idx = idx + int(step)
        if new_idx < 0 or new_idx >= len(entries):
            return
        entries[idx], entries[new_idx] = entries[new_idx], entries[idx]
        self._store_entries(item_kind, entries)
        self._refresh_map_list(item_kind)
        list_ctrl.SetSelection(new_idx)

    def _on_ok(self, _evt):
        self._values = {
            "kicad_builtin_first": bool(self.kicad_builtin_first_ctrl.GetValue()),
            "kicad_symbol_index_max_libs": int(self.symbol_index_max_libs_ctrl.GetValue()),
            "kicad_symbol_index_ttl_sec": int(self.symbol_index_ttl_ctrl.GetValue()),
            "kicad_footprint_fuzzy_max_libs": int(self.fp_fuzzy_max_libs_ctrl.GetValue()),
            "kicad_footprint_fuzzy_max_files_per_lib": int(self.fp_fuzzy_max_files_ctrl.GetValue()),
            "kicad_symbol_wildcard_min_score": int(self.symbol_wildcard_min_score_ctrl.GetValue()),
            "kicad_symbol_wildcard_min_gap": int(self.symbol_wildcard_min_gap_ctrl.GetValue()),
            "kicad_footprint_fuzzy_min_score_passive": int(self.fp_fuzzy_min_score_passive_ctrl.GetValue()),
            "kicad_footprint_fuzzy_min_score_ic": int(self.fp_fuzzy_min_score_ic_ctrl.GetValue()),
            "kicad_footprint_fuzzy_min_gap_passive": int(self.fp_fuzzy_min_gap_passive_ctrl.GetValue()),
            "kicad_footprint_fuzzy_min_gap_nonpassive": int(self.fp_fuzzy_min_gap_nonpassive_ctrl.GetValue()),
            "kicad_footprint_strict_pkg_pin_min_gap": int(self.fp_strict_pkg_pin_min_gap_ctrl.GetValue()),
            "kicad_footprint_require_keyword_overlap": bool(self.fp_require_keyword_overlap_ctrl.GetValue()),
            "kicad_footprint_passive_size_strict_mode": bool(self.fp_passive_size_strict_mode_ctrl.GetValue()),
            "kicad_symbol_map": self._normalize_map_dict(self._symbol_map_model),
            "kicad_footprint_map": self._normalize_map_dict(self._footprint_map_model),
        }
        self.EndModal(wx.ID_OK)

    def get_values(self) -> dict:
        return dict(self._values)


class SettingsDialog(wx.Dialog):
    """Settings dialog for storage scope and generation options."""

    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB importer plugin settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(620, 520)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        self._initial_values: dict = {}
        self._initial_library_name = ""

        # Hotkeys
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)
        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))

        layout = wx.BoxSizer(wx.VERTICAL)

        # Storage scope (Project vs System)
        self.library_scope_box = wx.RadioBox(
            self,
            id=wx.ID_ANY,
            label="Where to store symbols and models?",
            choices=["Project", "System", "Shared Library"],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
            name="general_library_scope",
        )
        self.library_scope_box.SetToolTip(wx.ToolTip(
            "Project — libraries are stored inside the project folder.\n"
            "System — libraries are stored in a shared plugin folder.\n"
            "Shared Library — libraries and tables are stored in one shared folder."
        ))
        self.library_scope_box.Bind(wx.EVT_RADIOBOX, self.update_settings)

        storage_scope_sizer = wx.BoxSizer(wx.HORIZONTAL)
        storage_scope_sizer.Add(
            wx.StaticBitmap(
                self,
                wx.ID_ANY,
                loadBitmapScaled("database-outline.png", self.parent.scale_factor, static=True),
                wx.DefaultPosition,
                wx.DefaultSize,
                0,
            ),
            10,
            wx.ALL | wx.EXPAND,
            5,
        )
        storage_scope_sizer.Add(self.library_scope_box, 100, wx.ALL | wx.EXPAND, 5)
        layout.Add(storage_scope_sizer, 0, wx.ALL | wx.EXPAND, 5)

        # Generation options box
        gen_box = wx.StaticBoxSizer(wx.VERTICAL, self, label="Generated libraries")

        # Library format
        format_row = wx.BoxSizer(wx.HORIZONTAL)
        format_row.Add(wx.StaticText(self, label="Library format:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_format_ctrl = wx.Choice(
            self,
            wx.ID_ANY,
            choices=["EasyEDA Pro", "KiCad"],
            name="general_lib_format",
        )
        self.lib_format_ctrl.SetToolTip(wx.ToolTip("Select output library format."))
        self.lib_format_ctrl.Bind(wx.EVT_CHOICE, self.update_settings)
        format_row.Add(self.lib_format_ctrl, 1, wx.EXPAND)
        gen_box.Add(format_row, 0, wx.ALL | wx.EXPAND, 5)

        # Grouping mode
        group_row = wx.BoxSizer(wx.HORIZONTAL)
        self.group_by_category_ctrl = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Group components by category",
            name="general_group_by_category",
        )
        self.group_by_category_ctrl.SetToolTip(wx.ToolTip("Group components by category."))
        self.group_by_category_ctrl.Bind(wx.EVT_CHECKBOX, self.update_settings)
        group_row.Add(self.group_by_category_ctrl, 1, wx.EXPAND)
        gen_box.Add(group_row, 0, wx.ALL | wx.EXPAND, 5)

        hide_btn_labels_row = wx.BoxSizer(wx.HORIZONTAL)
        self.hide_button_labels_ctrl = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Hide Labels on Buttons",
            name="general_hide_button_labels",
        )
        self.hide_button_labels_ctrl.SetToolTip(wx.ToolTip("Hide text labels on top toolbar and Search button."))
        self.hide_button_labels_ctrl.Bind(wx.EVT_CHECKBOX, self.update_settings)
        hide_btn_labels_row.Add(self.hide_button_labels_ctrl, 1, wx.EXPAND)
        gen_box.Add(hide_btn_labels_row, 0, wx.ALL | wx.EXPAND, 5)

        debug_log_row = wx.BoxSizer(wx.HORIZONTAL)
        self.debug_log_ctrl = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Debug Log (include JSON payloads)",
            name="general_debug_log",
        )
        self.debug_log_ctrl.SetToolTip(wx.ToolTip("Enable verbose debug JSON output in plugin logs."))
        self.debug_log_ctrl.Bind(wx.EVT_CHECKBOX, self.update_settings)
        debug_log_row.Add(self.debug_log_ctrl, 1, wx.EXPAND)
        gen_box.Add(debug_log_row, 0, wx.ALL | wx.EXPAND, 5)

        # Library name prefix
        prefix_row = wx.BoxSizer(wx.HORIZONTAL)
        self._lib_name_label = wx.StaticText(self, label="Library name prefix:")
        prefix_row.Add(self._lib_name_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_prefix_ctrl = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            size=HighResWxSize(self.parent.window, wx.Size(200, -1)),
            name="general_lib_prefix",
        )
        self.lib_prefix_ctrl.SetToolTip(wx.ToolTip("Prefix prepended to generated library names (e.g. JLCPCB)."))
        self.lib_prefix_ctrl.Bind(wx.EVT_TEXT, self.update_settings)
        prefix_row.Add(self.lib_prefix_ctrl, 1, wx.EXPAND)
        gen_box.Add(prefix_row, 0, wx.ALL | wx.EXPAND, 5)

        # Library location:
        # - Project scope: directory name under ${KIPRJMOD}
        # - Shared scope: path to a shared library folder
        lib_path_row = wx.BoxSizer(wx.HORIZONTAL)
        self._lib_path_label = wx.StaticText(self, label="Library path:")
        lib_path_row.Add(self._lib_path_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lib_path_ctrl = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            name="general_lib_path",
        )
        self.lib_path_ctrl.SetToolTip(wx.ToolTip("Library location setting depends on selected storage scope."))
        self.lib_path_ctrl.Bind(wx.EVT_TEXT, self.update_settings)
        self._lib_path_browse_btn = wx.Button(self, wx.ID_ANY, "Browse\u2026", style=wx.BU_EXACTFIT)
        self._lib_path_browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_lib_path)
        lib_path_row.Add(self.lib_path_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        lib_path_row.Add(self._lib_path_browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        gen_box.Add(lib_path_row, 0, wx.ALL | wx.EXPAND, 5)

        self.kicad_symbol_matching_enabled_ctrl = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Enable KiCad built-in matching",
            name="general_kicad_symbol_matching_enabled",
        )
        self.kicad_symbol_matching_enabled_ctrl.SetToolTip(
            wx.ToolTip(
                "Enable KiCad built-in symbol/footprint matching before EasyEDA conversion. "
                "When disabled, all KiCad matching is skipped and importer uses EasyEDA conversion only."
            )
        )
        self.kicad_symbol_matching_enabled_ctrl.Bind(wx.EVT_CHECKBOX, self.update_settings)
        gen_box.Add(self.kicad_symbol_matching_enabled_ctrl, 0, wx.ALL | wx.EXPAND, 5)

        maintenance_row = wx.BoxSizer(wx.HORIZONTAL)
        self._mapping_index_btn = wx.Button(self, wx.ID_ANY, "Mapping & Indexing Settings")
        self._mapping_index_btn.SetToolTip(
            wx.ToolTip("Configure KiCad symbol/footprint mapping and index scan parameters.")
        )
        self._mapping_index_btn.Bind(wx.EVT_BUTTON, self._on_open_mapping_index_settings)
        maintenance_row.Add(self._mapping_index_btn, 0, wx.TOP | wx.LEFT, 2)
        gen_box.Add(maintenance_row, 0, wx.ALL | wx.EXPAND, 5)

        layout.Add(gen_box, 0, wx.ALL | wx.EXPAND, 5)

        button_row = wx.StdDialogButtonSizer()
        self._save_btn = wx.Button(self, wx.ID_OK, "Save")
        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, "Cancel")
        self._save_btn.Bind(wx.EVT_BUTTON, self._on_save)
        self._cancel_btn.Bind(wx.EVT_BUTTON, self.quit_dialog)
        button_row.AddButton(self._save_btn)
        button_row.AddButton(self._cancel_btn)
        button_row.Realize()
        layout.Add(button_row, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizer(layout)
        self.SetMinSize(HighResWxSize(parent.window, wx.Size(620, 520)))
        layout.Fit(self)
        self.Layout()
        self.Centre(wx.BOTH)
        apply_button_label_tooltips(self, overwrite=True)

        self.load_settings()

    def load_settings(self):
        general = self.parent.settings.get("general", {})
        self._initial_library_name = resolve_library_base_name(
            general,
            project_path=Path(self.parent.project_path),
        )
        self.update_library_scope(general.get("library_scope", "project"))
        self.update_group_by_category(resolve_group_by_category(general, default=False))
        self.update_hide_button_labels(as_bool(general.get("hide_button_labels"), default=False))
        self.update_debug_log(as_bool(general.get("debug_log"), default=False))
        self.update_lib_prefix(self._initial_library_name)
        self.update_lib_format(general.get("lib_format", "easyeda_pro"))
        self.update_kicad_symbol_matching_enabled(
            as_bool(general.get("kicad_symbol_matching_enabled"), default=False)
        )
        self.update_lib_path(self._resolve_lib_path_setting(general))
        self._initial_values = self._current_values()
        try:
            self.parent.log(
                "SettingsDialog: loaded "
                f"initial_library_name={self._initial_library_name!r} "
                f"initial_values={self._initial_values!r}\n"
            )
        except Exception:
            pass

    def _resolve_lib_path_setting(self, general: dict) -> str:
        scope = str(general.get("library_scope", "project")).strip().lower()
        raw = str(general.get("lib_path") or "").strip()
        if scope == "project":
            if raw.startswith("${KIPRJMOD}/"):
                raw = raw[len("${KIPRJMOD}/"):]
            raw = raw.strip().strip("/\\")
            if not raw:
                return DEFAULT_LIB_DIR_NAME
            # Project mode expects a directory name under KIPRJMOD.
            if "/" in raw or "\\" in raw:
                p = Path(raw)
                return p.name or DEFAULT_LIB_DIR_NAME
            return raw
        if scope == "shared":
            if not raw:
                return "${KIPRJMOD}/../library"
            if "${" not in raw and "/" not in raw and "\\" not in raw:
                return f"${{KIPRJMOD}}/../{raw}"
            return raw
        return raw or DEFAULT_LIB_PATH

    def update_settings(self, event):
        """Update dependent controls without saving settings."""
        obj = event.GetEventObject()
        raw_name = obj.GetName()
        if "_" not in raw_name:
            return
        section, name = raw_name.split("_", 1)
        if hasattr(obj, "GetValue"):
            value = obj.GetValue()
        elif hasattr(obj, "GetSelection"):
            sel = obj.GetSelection()
            if name == "library_scope":
                value = "project" if sel == 0 else ("system" if sel == 1 else "shared")
            elif name == "lib_format":
                value = "easyeda_pro" if sel == 0 else "kicad"
            else:
                value = sel
        else:
            value = None
        try:
            if section == "general" and name in ("lib_prefix", "lib_path", "lib_format", "library_scope"):
                current = ""
                try:
                    current = str(obj.GetValue())
                except Exception:
                    current = str(value)
                self.parent.log(
                    f"SettingsDialog: change {section}.{name} value={value!r} control={current!r}\n"
                )
        except Exception:
            pass
        getattr(self, f"update_{name}")(value)
        if name == "library_scope":
            scope_key = str(value or "project").strip().lower()
            normalized_lib_path = self._normalize_lib_path_for_scope(
                scope_key,
                self.lib_path_ctrl.GetValue(),
            )
            if self.lib_path_ctrl.GetValue() != normalized_lib_path:
                self.lib_path_ctrl.ChangeValue(normalized_lib_path)
        if name == "library_scope" and str(value).lower() == "shared":
            self._show_shared_scope_notice()

    def _current_values(self) -> dict:
        try:
            scope_sel = self.library_scope_box.GetSelection()
        except Exception:
            scope_sel = 0
        scope = "project" if scope_sel == 0 else ("system" if scope_sel == 1 else "shared")

        try:
            fmt_sel = self.lib_format_ctrl.GetSelection()
        except Exception:
            fmt_sel = 0
        lib_format = "kicad" if fmt_sel == 1 else "easyeda_pro"

        lib_path = self._normalize_lib_path_for_scope(scope, self.lib_path_ctrl.GetValue())
        return {
            "library_scope": scope,
            "lib_format": lib_format,
            "group_by_category": bool(self.group_by_category_ctrl.GetValue()),
            "hide_button_labels": bool(self.hide_button_labels_ctrl.GetValue()),
            "debug_log": bool(self.debug_log_ctrl.GetValue()),
            "lib_prefix": str(self.lib_prefix_ctrl.GetValue() or "").strip(),
            "lib_path": lib_path,
            "kicad_symbol_matching_enabled": bool(self.kicad_symbol_matching_enabled_ctrl.GetValue()),
        }

    def _on_save(self, _evt=None):
        values = self._current_values()
        try:
            self.parent.log(f"SettingsDialog: save clicked values={values!r}\n")
        except Exception:
            pass
        current = (self.parent.settings.get("general", {}) or {})
        baseline = self._initial_values or current
        for setting, value in values.items():
            if baseline.get(setting) == value:
                continue
            event = UpdateSetting(section="general", setting=setting, value=value)
            if setting == "lib_prefix":
                try:
                    event.previous_value = self._initial_library_name
                except Exception:
                    pass
            try:
                self.parent._on_update_setting(event)
            except Exception as exc:
                try:
                    self.parent.log(f"SettingsDialog: direct save handler failed for {setting}: {exc}\n")
                except Exception:
                    pass
                wx.PostEvent(self.parent, event)
        self.EndModal(wx.ID_OK)

    def quit_dialog(self, *_):
        self.EndModal(wx.ID_CANCEL)

    def update_library_scope(self, scope):
        if isinstance(scope, str):
            key = scope.lower()
            if key == "project":
                idx = 0
            elif key == "system":
                idx = 1
            else:
                idx = 2
        else:
            idx = int(scope) if scope in (0, 1, 2) else 0
        try:
            self.library_scope_box.SetSelection(idx)
        except Exception:
            pass
        scope_key = "project" if idx == 0 else ("system" if idx == 1 else "shared")

        try:
            self._lib_path_label.Enable(scope_key != "system")
            self.lib_path_ctrl.Enable(scope_key != "system")
        except Exception:
            pass

        if scope_key == "project":
            self._lib_path_label.SetLabel("Library dir name:")
            self.lib_path_ctrl.SetHint("library")
            self.lib_path_ctrl.SetToolTip(wx.ToolTip(
                "Directory name under ${KIPRJMOD} where plugin libraries are stored."
            ))
            self._lib_path_browse_btn.Enable(False)
        elif scope_key == "shared":
            self._lib_path_label.SetLabel("Shared library path:")
            self.lib_path_ctrl.SetHint("${KIPRJMOD}/../library")
            self.lib_path_ctrl.SetToolTip(wx.ToolTip(
                "Shared library folder. Tables are stored there and linked into each project."
            ))
            self._lib_path_browse_btn.Enable(True)
        else:
            self._lib_path_label.SetLabel("Library path:")
            self._lib_path_browse_btn.Enable(False)

        try:
            normalized = self._normalize_lib_path_for_scope(scope_key, self.lib_path_ctrl.GetValue())
            if self.lib_path_ctrl.GetValue() != normalized:
                self.lib_path_ctrl.ChangeValue(normalized)
        except Exception:
            pass

        self.Layout()

    def _normalize_lib_path_for_scope(self, scope_key: str, raw_value: str) -> str:
        scope = str(scope_key or "").strip().lower()
        raw = str(raw_value or "").strip()
        if scope == "project":
            if raw.startswith("${KIPRJMOD}/"):
                raw = raw[len("${KIPRJMOD}/"):]
            raw = raw.strip().strip("/\\")
            if not raw:
                return DEFAULT_LIB_DIR_NAME
            if "/" in raw or "\\" in raw:
                p = Path(raw)
                return p.name or DEFAULT_LIB_DIR_NAME
            return raw
        if scope == "shared":
            if not raw:
                return "${KIPRJMOD}/../library"
            if "${" not in raw and "/" not in raw and "\\" not in raw:
                return f"${{KIPRJMOD}}/../{raw}"
            return raw
        return raw or DEFAULT_LIB_PATH

    def update_lib_prefix(self, value: str):
        try:
            self.lib_prefix_ctrl.ChangeValue(str(value) if value is not None else "")
        except Exception:
            pass

    def update_group_by_category(self, value):
        try:
            enabled = as_bool(value, default=True)
            self.group_by_category_ctrl.SetValue(enabled)
            self._lib_name_label.SetLabel(
                "Library name prefix:" if enabled else "Library name:"
            )
            tip = (
                "Prefix prepended to generated library names (e.g. JLCPCB)."
                if enabled
                else "Fixed name for generated library files (e.g. JLCPCB)."
            )
            self.lib_prefix_ctrl.SetToolTip(wx.ToolTip(tip))
            self.Layout()
        except Exception:
            pass

    def update_hide_button_labels(self, value):
        try:
            self.hide_button_labels_ctrl.SetValue(as_bool(value, default=False))
        except Exception:
            pass

    def update_debug_log(self, value):
        try:
            self.debug_log_ctrl.SetValue(as_bool(value, default=False))
        except Exception:
            pass

    def update_lib_format(self, value: str):
        if isinstance(value, str):
            key = value.strip().lower()
            idx = 1 if key == "kicad" else 0
        else:
            idx = int(value) if value in (0, 1) else 0
        try:
            self.lib_format_ctrl.SetSelection(idx)
        except Exception:
            pass

    def update_kicad_symbol_matching_enabled(self, value):
        enabled = as_bool(value, default=False)
        try:
            self.kicad_symbol_matching_enabled_ctrl.SetValue(enabled)
        except Exception:
            pass
        try:
            self._mapping_index_btn.Enable(enabled)
        except Exception:
            pass

    def update_lib_path(self, value: str):
        try:
            self.lib_path_ctrl.ChangeValue(str(value) if value is not None else "")
        except Exception:
            pass

    def _on_browse_lib_path(self, _evt):
        """Open a directory picker; store result as ${KIPRJMOD}/... path."""
        from pathlib import Path

        project_path = Path(self.parent.project_path)

        try:
            scope = self.library_scope_box.GetSelection()
        except Exception:
            scope = 0
        if scope != 2:
            return

        # Resolve current value to a real path for the dialog start directory
        current = self.lib_path_ctrl.GetValue().strip() or "${KIPRJMOD}/../library"
        resolved_str = current.replace("${KIPRJMOD}", str(project_path))
        resolved = Path(resolved_str)
        start_dir = str(resolved) if resolved.exists() else str(project_path)

        dlg = wx.DirDialog(
            self,
            message="Choose shared library folder",
            defaultPath=start_dir,
            style=wx.DD_DEFAULT_STYLE,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                chosen = Path(dlg.GetPath())
                # Express as ${KIPRJMOD}/... using os.path.relpath (handles ..)
                rel = os.path.relpath(chosen, project_path)
                result = "${KIPRJMOD}/" + Path(rel).as_posix()
                self.lib_path_ctrl.SetValue(result)
        finally:
            dlg.Destroy()

    def _show_shared_scope_notice(self):
        wx.MessageBox(
            "Shared Library mode is a project-level workflow for one repository that contains multiple KiCad subprojects. "
            "It lets those subprojects reuse one vendored component library stored inside the repository "
            "(symbols, footprints, and 3D models), so everything travels with git. "
            "Unlike System libraries, this mode does not rely on machine-wide KiCad setup and stays consistent across different computers. "
            "Because of KiCad path limitations, projects that share this library should be kept at the same relative directory level "
            "to preserve stable path expansion.\n\n"
            "Shared Library mode conventions:\n"
            "- sym-lib-table / fp-lib-table are stored inside the shared library directory.\n"
            "- Each project receives links to these tables.\n"
            "- Shared metadata (including library name) is stored in jlcpcb_shared.json.\n\n"
            "Symlink note:\n"
            "Some filesystems and sandboxes restrict symlink creation.\n"
            "If linking fails, the plugin will copy table files instead.",
            "Shared Library Mode",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _on_open_mapping_index_settings(self, _evt):
        dlg = None
        try:
            general = (self.parent.settings.get("general", {}) or {})
            if not as_bool(general.get("kicad_symbol_matching_enabled"), default=False):
                return
            dlg = MappingIndexSettingsDialog(self.parent, general)
            if dlg.ShowModal() != wx.ID_OK:
                return
            values = dlg.get_values()
            for setting, value in values.items():
                wx.PostEvent(
                    self.parent,
                    UpdateSetting(section="general", setting=setting, value=value),
                )
        finally:
            if dlg is not None:
                dlg.Destroy()
