"""Contains the part selector modal window."""

import logging
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error
try:
    from PIL import Image, ImageOps  # pylint: disable=import-error
except Exception:  # Pillow is declared in requirements.txt
    Image = None
    ImageOps = None

from .datamodel import PartSelectorDataModel
from ..core.derive_params import params_for_part  # pylint: disable=import-error
from ..core.events import AssignPartsEvent, UpdateSetting
from ..core.helpers import HighResWxSize, loadBitmapScaled, GetScaleFactor
from .partdetails import PartDetailsDialog
from ..core.lcsc_api import LCSC_API
from ..core.library import LibraryState

class PartSelectorDialog(wx.Dialog):
    """The part selector window."""

    def __init__(self, parent, parts):
        # Allow "parent" to be a non-window context. If it's not a wx.Window (or
        # it's this instance during subclassing), don't set a wx parent.
        wx_parent = None
        try:
            if parent is not None and isinstance(parent, wx.Window) and parent is not self:
                wx_parent = parent
        except Exception:
            wx_parent = None

        wx.Dialog.__init__(
            self,
            wx_parent,
            id=wx.ID_ANY,
            title="JLCPCB Library",
            pos=wx.DefaultPosition,
            # Avoid calling HighResWxSize(self, ...) before wx.Dialog is initialized
            size=wx.Size(1400, 800),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent
        # Local fallbacks for commonly used context values
        _scale_window = getattr(self.parent, "window", self)
        self.scale_factor = getattr(self.parent, "scale_factor", GetScaleFactor(_scale_window))
        self.library = getattr(self.parent, "library", None)
        self.settings = getattr(self.parent, "settings", {})
        self.parts = parts
        self._lcsc_api = LCSC_API()
        self._selected_lcsc = None
        self._pdfurl = ""
        self._pageurl = ""
        lcsc_selection = self.get_existing_selection(parts)

        # ------------------------- UI tuning constants ------------------------
        # Row height multiplier (2.0-3.0 recommended)
        self.ROW_HEIGHT_MULTIPLIER = 2.5
        # Explicit thumbnail height in pixels (None = auto via multiplier)
        self.THUMB_HEIGHT_PX = 100
        # Parallel thumbnail downloads
        self.THUMB_MAX_WORKERS = 4
        # Prefetch rows ahead of viewport
        self.THUMB_PREFETCH_ROWS = 0
        # Base pixel size for 1x scale
        self._base_row_px = 24

        self._debounce_ms = 600  # pause between typing and search (ms)
        self.search_timer = wx.Timer(self)
        # Bind specifically to this timer to avoid handling other timers
        self.Bind(wx.EVT_TIMER, self.search, self.search_timer)

        # ---------------------------------------------------------------------
        # ---------------------------- Hotkeys --------------------------------
        # ---------------------------------------------------------------------
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)

        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        accel = wx.AcceleratorTable(entries)
        self.SetAcceleratorTable(accel)

        # ---------------------------------------------------------------------
        # --------------------------- Search bar ------------------------------
        # ---------------------------------------------------------------------

        keyword_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Keywords",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(100, 15)),
            style=wx.ALIGN_RIGHT,
        )
        self.keyword = wx.TextCtrl(
            self,
            wx.ID_ANY,
            lcsc_selection,
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(800, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.keyword.SetHint("e.g. 10k 0603")

        self.ohm_button = wx.Button(
            self,
            wx.ID_ANY,
            "Ω",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, 24)),
            0,
        )
        self.ohm_button.SetToolTip("Insert Ω at cursor in the search field")
        try:
            f = self.ohm_button.GetFont()
            f.SetPointSize(max(12, f.GetPointSize() + 2))
            self.ohm_button.SetFont(f)
        except Exception:
            pass

        self.micro_button = wx.Button(
            self,
            wx.ID_ANY,
            "µ",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, 24)),
            0,
        )
        self.micro_button.SetToolTip("Insert µ at cursor in the search field")
        try:
            f2 = self.micro_button.GetFont()
            f2.SetPointSize(max(12, f2.GetPointSize() + 4))
            self.micro_button.SetFont(f2)
        except Exception:
            pass
        self.advanced_panel = wx.Panel(self)
        
        manufacturer_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Manufacturer",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.manufacturer = wx.TextCtrl(
            self.advanced_panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.manufacturer.SetHint("e.g. Vishay")

        package_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Package",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.package = wx.TextCtrl(
            self.advanced_panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.package.SetHint("e.g. 0603")

        category_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Category",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.category = wx.ComboBox(
            self.advanced_panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            choices=self._initial_categories(),
            style=wx.CB_READONLY,
        )
        self.category.SetHint("e.g. Resistors")

        part_no_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Part number",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.part_no = wx.TextCtrl(
            self.advanced_panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.part_no.SetHint("e.g. DS2411")

        solder_joints_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Solder joints",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.solder_joints = wx.TextCtrl(
            self.advanced_panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.solder_joints.SetHint("e.g. 2")

        subcategory_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Subcategory",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.subcategory = wx.ComboBox(
            self.advanced_panel,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            style=wx.CB_READONLY,
        )
        self.subcategory.SetHint("e.g. Variable Resistors")

        basic_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Include basic parts",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.basic_checkbox = wx.CheckBox(
            self.advanced_panel,
            wx.ID_ANY,
            "Basic",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            0,
            name="basic",
        )
        extended_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Include extended parts",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.extended_checkbox = wx.CheckBox(
            self.advanced_panel,
            wx.ID_ANY,
            "Extended",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            0,
            name="extended",
        )
        stock_label = wx.StaticText(
            self.advanced_panel,
            wx.ID_ANY,
            "Only show parts in stock",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.assert_stock_checkbox = wx.CheckBox(
            self.advanced_panel,
            wx.ID_ANY,
            "in Stock",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            0,
            name="stock",
        )

        self.basic_checkbox.SetValue(
            (self.settings or {}).get("partselector", {}).get("basic", True)
        )
        self.extended_checkbox.SetValue(
            (self.settings or {}).get("partselector", {}).get("extended", True)
        )
        self.assert_stock_checkbox.SetValue(
            (self.settings or {}).get("partselector", {}).get("stock", False)
        )

        self.basic_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)
        self.extended_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)
        self.assert_stock_checkbox.Bind(wx.EVT_CHECKBOX, self.update_settings)

        help_button = wx.Button(
            self.advanced_panel,
            wx.ID_ANY,
            "Help",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(100, -1)),
            0,
        )

        self._advanced_visible = bool(
            (self.settings or {}).get("partselector", {}).get("advanced_visible", False)
        )
        self.advanced_toggle_button = wx.Button(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, 24)),
            0,
        )
        keyword_search_row1 = wx.BoxSizer(wx.HORIZONTAL)
        keyword_search_row1.Add(self.advanced_toggle_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL, 5)
        keyword_search_row1.Add(keyword_label, 0, wx.ALL, 5)
        # Let the keyword field take remaining horizontal space and keep
        # the buttons at their natural size on the right.
        keyword_search_row1.Add(
            self.keyword,
            1,  # stretch to fill
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        keyword_search_row1.Add(
            self.ohm_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        keyword_search_row1.Add(
            self.micro_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        # Explicit search button to the right of the keyword field
        self.search_button = wx.Button(
            self,
            wx.ID_ANY,
            "Search",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(100, -1)),
            0,
        )
        self.search_button.SetBitmap(loadBitmapScaled("mdi-magnify.png", self.scale_factor))
        self.search_button.SetBitmapMargins((2, 0))
        keyword_search_row1.Add(self.search_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL, 5)

        # Advanced toggle button (to the right of Search)
      
        # Match advanced toggle height to Search button height while keeping 24px width
        try:
            sh = self.search_button.GetSize().GetHeight()
            self.advanced_toggle_button.SetMinSize(wx.Size(36, sh))
            self.advanced_toggle_button.SetSize(wx.Size(36, sh))
        except Exception:
            pass

        search_sizer_one = wx.BoxSizer(wx.VERTICAL)
        search_sizer_one.Add(manufacturer_label, 0, wx.ALL, 5)
        search_sizer_one.Add(
            self.manufacturer,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_one.Add(package_label, 0, wx.ALL, 5)
        search_sizer_one.Add(
            self.package,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_two = wx.BoxSizer(wx.VERTICAL)
        search_sizer_two.Add(category_label, 0, wx.ALL, 5)
        search_sizer_two.Add(
            self.category,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_two.Add(part_no_label, 0, wx.ALL, 5)
        search_sizer_two.Add(
            self.part_no,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_two.Add(solder_joints_label, 0, wx.ALL, 5)
        search_sizer_two.Add(
            self.solder_joints,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_three = wx.BoxSizer(wx.VERTICAL)
        search_sizer_three.Add(subcategory_label, 0, wx.ALL, 5)
        search_sizer_three.Add(
            self.subcategory,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_four = wx.BoxSizer(wx.VERTICAL)
        search_sizer_four.Add(basic_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.basic_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_four.Add(extended_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.extended_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )
        search_sizer_four.Add(stock_label, 0, wx.ALL, 5)
        search_sizer_four.Add(
            self.assert_stock_checkbox,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        search_sizer_five = wx.BoxSizer(wx.VERTICAL)
        search_sizer_five.Add(
            help_button,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            5,
        )

        help_button.SetBitmap(
            loadBitmapScaled(
                "mdi-help-circle-outline.png",
                self.scale_factor,
            )
        )
        help_button.SetBitmapMargins((2, 0))

        search_sizer = wx.StaticBoxSizer(wx.VERTICAL, self)
        # Make both rows expand to the full width of the StaticBox
        search_sizer.Add(keyword_search_row1, 0, wx.EXPAND)

        # Create StaticBoxSizer for advanced panel to ensure proper parenting
        adv_box = wx.StaticBox(self.advanced_panel, wx.ID_ANY, "")
        search_sizer_row2 = wx.StaticBoxSizer(adv_box, wx.HORIZONTAL)
        search_sizer_row2.Add(search_sizer_one, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_two, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_three, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_four, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_five, 0, wx.RIGHT, 20)

        # Wrap advanced row into a panel to toggle visibility as a block
        self.advanced_panel.SetSizer(search_sizer_row2)
        search_sizer.Add(self.advanced_panel, 0, wx.EXPAND)
        # Apply persisted visibility preference
        try:
            self.advanced_panel.Show(self._advanced_visible)
        except Exception:
            pass
        self._update_advanced_toggle_visuals()

        # Remove automatic search on typing; bind Enter to perform search
        self.keyword.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.ohm_button.Bind(wx.EVT_BUTTON, self.add_ohm_symbol)
        self.micro_button.Bind(wx.EVT_BUTTON, self.add_micro_symbol)
        self.manufacturer.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.package.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.category.Bind(wx.EVT_COMBOBOX, self.update_subcategories)
        self.category.Bind(wx.EVT_TEXT, self.update_subcategories)
        self.part_no.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.solder_joints.Bind(wx.EVT_TEXT_ENTER, self.search)
        self.search_button.Bind(wx.EVT_BUTTON, self.search)
        help_button.Bind(wx.EVT_BUTTON, self.help)
        self.advanced_toggle_button.Bind(wx.EVT_BUTTON, self._toggle_advanced)
       
        # Enable type-ahead selection for read-only ComboBoxes (category/subcategory)
        # self._category_typeahead = _ComboTypeAhead(self.category)
        # self._subcategory_typeahead = _ComboTypeAhead(self.subcategory)

        # ---------------------------------------------------------------------
        # ------------------------- Result Part list --------------------------
        # ---------------------------------------------------------------------

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Restore ScrolledWindow wrapper around the DataViewCtrl
        table_scroller = wx.ScrolledWindow(self, style=wx.HSCROLL | wx.VSCROLL)
        table_scroller.SetScrollRate(20, 20)
        self.table_scroller = table_scroller

        self.part_list = dv.DataViewCtrl(
            table_scroller,
            style=wx.BORDER_THEME | dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_MULTIPLE,
        )
    
        # First column: thumbnail image
        self._thumb_px = int((self.THUMB_HEIGHT_PX or (self._base_row_px * self.ROW_HEIGHT_MULTIPLIER * self.scale_factor)))
        # Some wx versions support per-row height; set if available
        try:
            if hasattr(self.part_list, "SetRowHeight"):
                self.part_list.SetRowHeight(max(24, self._thumb_px + 2))
        except Exception:
            self.logger.exception("SetRowHeight failed")

        thumb_col = self.part_list.AppendBitmapColumn(
            "",
            0,
            width=int(self._thumb_px + 8),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        thumb_col.SetSortable(False)

        lcsc = self.part_list.AppendTextColumn(
            "LCSC",
            1,
            width=int(self.scale_factor * 60),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        mfr_number = self.part_list.AppendTextColumn(
            "MFR Number",
            2,
            width=int(self.scale_factor * 140),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        package = self.part_list.AppendTextColumn(
            "Package",
            3,
            width=int(self.scale_factor * 100),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        pins = self.part_list.AppendTextColumn(
            "Pins",
            4,
            width=int(self.scale_factor * 40),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        price = self.part_list.AppendTextColumn(
            "Price",
            10,
            width=int(self.scale_factor * 100),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        params = self.part_list.AppendTextColumn(
            "Params",
            6,
            width=int(self.scale_factor * 150),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        stock = self.part_list.AppendTextColumn(
            "Stock",
            7,
            width=int(self.scale_factor * 70),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        mfr = self.part_list.AppendTextColumn(
            "Manufacturer",
            8,
            width=int(self.scale_factor * 100),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        description = self.part_list.AppendTextColumn(
            "Description",
            9,
            width=int(self.scale_factor * 300),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        parttype = self.part_list.AppendTextColumn(
            "Type",
            5,
            width=int(self.scale_factor * 50),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )

        # ---------------------------------------------------------------------
        # ------------------------ Result status line -------------------------
        # ---------------------------------------------------------------------

        self.result_count = wx.StaticText(
            self, wx.ID_ANY, "0 Results", wx.DefaultPosition, wx.DefaultSize
        )
        # Toggle thumbnail size button (icon-only, 60x60 <-> 100x100)
        self.thumb_toggle_button = wx.Button(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, -1)),
            0,
        )
        try:
            # Default state is 100px; clicking collapses → show collapse icon
            self.thumb_toggle_button.SetBitmap(
                loadBitmapScaled("mdi-arrow-collapse-vertical.png", self.scale_factor)
            )
            self.thumb_toggle_button.SetBitmapMargins((2, 0))
            self.thumb_toggle_button.SetToolTip("Decrease thumbnail size")
        except Exception:
            pass
        self.thumb_toggle_button.Bind(wx.EVT_BUTTON, self._toggle_thumb_size)

        # Import status label (Success: N  Failed: N) — shown after import, auto-hides
        self.import_status_label = wx.StaticText(self, wx.ID_ANY, "")
        self._import_status_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_import_status_timer, self._import_status_timer)

        result_sizer = wx.BoxSizer(wx.HORIZONTAL)
        # Place button left to the label with right margin
        result_sizer.Add(self.thumb_toggle_button, 0, wx.LEFT | wx.TOP | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5)
        result_sizer.Add(self.result_count, 0, wx.TOP | wx.ALIGN_CENTER_VERTICAL, 5)
        result_sizer.AddStretchSpacer(1)
        result_sizer.Add(self.import_status_label, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 10)

        lcsc.SetSortable(True)
        mfr_number.SetSortable(True)
        package.SetSortable(True)
        pins.SetSortable(True)
        parttype.SetSortable(True)
        params.SetSortable(True)
        stock.SetSortable(True)
        mfr.SetSortable(True)
        description.SetSortable(True)
        price.SetSortable(True)

        self.part_list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.OnPartSelected)
        # Open details on double-click / Enter
        self.part_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.get_part_details)

        scrolled_sizer = wx.BoxSizer(wx.VERTICAL)
        scrolled_sizer.Add(self.part_list, 1, wx.EXPAND)
        table_scroller.SetSizer(scrolled_sizer)
        table_sizer.Add(table_scroller, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        # Icon-only buttons; no labels
        self.select_part_button = wx.Button(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, 36)),
            0,
        )
        self.part_details_button = wx.Button(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, 36)),
            0,
        )
        # New action buttons
        # self.open_datasheet_button = wx.Button(
        #     self,
        #     wx.ID_ANY,
        #     "Open Datasheet",
        #     wx.DefaultPosition,
        #     HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, -1)),
        #     0,
        # )
        # self.open_lcsc_button = wx.Button(
        #     self,
        #     wx.ID_ANY,
        #     "Open LCSC Page",
        #     wx.DefaultPosition,
        #     HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, -1)),
        #     0,
        # )
        # Remove right-side preview image per request
        self._preview_w, self._preview_h = 0, 0

        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)
        # self.open_datasheet_button.Bind(wx.EVT_BUTTON, self.open_datasheet)
        # self.open_lcsc_button.Bind(wx.EVT_BUTTON, self.open_lcsc_page)

        self.select_part_button.SetBitmap(
            loadBitmapScaled(
                "mdi-arrow-collapse-down.png",
                self.scale_factor,
            )
        )
        self.select_part_button.SetBitmapMargins((2, 0))

        self.part_details_button.SetBitmap(
            loadBitmapScaled(
                "mdi-text-box-search-outline.png",
                self.scale_factor,
            )
        )
        self.part_details_button.SetBitmapMargins((2, 0))

        # self.open_datasheet_button.SetBitmap(
        #     loadBitmapScaled(
        #         "mdi-file-document-outline.png",
        #         self.scale_factor,
        #     )
        # )
        # self.open_datasheet_button.SetBitmapMargins((2, 0))
        # self.open_lcsc_button.SetBitmap(
        #     loadBitmapScaled(
        #         "mdi-earth.png",
        #         self.scale_factor,
        #     )
        # )
        # self.open_lcsc_button.SetBitmapMargins((2, 0))

        self.log_toggle_button = wx.Button(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(36, 36)),
            0,
        )
        try:
            self.log_toggle_button.SetBitmap(
                loadBitmapScaled("mdi-console.png", self.scale_factor)
            )
            self.log_toggle_button.SetBitmapMargins((2, 0))
            self.log_toggle_button.SetToolTip("Show/hide log")
        except Exception:
            pass
        self.log_toggle_button.Bind(wx.EVT_BUTTON, self._toggle_log)

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 5)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 5)
        tool_sizer.Add(self.log_toggle_button, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 5)

        # Reduce the right column width to button width only
        table_sizer.Add(tool_sizer, 0, wx.TOP | wx.BOTTOM | wx.RIGHT, 5)

        # ---------------------------------------------------------------------
        # ------------------------------ Sizers  ------------------------------
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(search_sizer, 1, wx.ALL | wx.EXPAND, 5)
        # layout.Add(self.search_button, 5, wx.ALL, 5)
        layout.Add(result_sizer, 0, wx.LEFT | wx.EXPAND, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        # Prepare placeholder bitmap for thumbnails scaled to thumb size
        _ph_bmp = loadBitmapScaled("placeholder.png", self.scale_factor, static=True)
        try:
            if isinstance(_ph_bmp, wx.Bitmap):
                bmp = _ph_bmp
            else:
                bmp = _ph_bmp.GetBitmap(wx.Size(self._thumb_px, self._thumb_px))
            img = bmp.ConvertToImage()
            img = img.Scale(self._thumb_px, self._thumb_px, wx.IMAGE_QUALITY_HIGH)
            placeholder_bmp = wx.Bitmap(img)
        except Exception:
            placeholder_bmp = wx.Bitmap(_ph_bmp) if isinstance(_ph_bmp, wx.Bitmap) else wx.Bitmap()

        self.part_list_model = PartSelectorDataModel(placeholder_bmp)
        self.part_list.AssociateModel(self.part_list_model)

        # Bind scroll/resize to trigger lazy thumbnail loading
        # Bind scroll directly on the DataViewCtrl only (avoid duplicate events)
        # try:
        #     self.table_scroller.Bind(wx.EVT_SCROLLWIN, self.on_scroll)
        #     self.scrolled_sizer.Bind(wx.EVT_SCROLLWIN, self.on_scroll)
        #     self.part_list.Bind(wx.EVT_SCROLLWIN, self.on_scroll)
        #     pass
        # except Exception:
        #     self.logger.exception("Binding EVT_SCROLLWIN to part_list failed")
        
        self.part_list.Bind(wx.EVT_SIZE, self._on_table_viewport_changed)
        # Ensure mousewheel scrolling keeps working: call Skip()
        self.part_list.Bind(wx.EVT_MOUSEWHEEL, self._on_table_viewport_changed)

        # Async thumbnail loading state
        self._thumb_executor = ThreadPoolExecutor(max_workers=self.THUMB_MAX_WORKERS)
        self._thumb_scheduled = set()
        self._thumb_cache = {}
        self._thumb_lock = Lock()
        self._search_generation = 0
        self._top_index = 0  # Initialize scroll position tracker
        
        # Timer for debouncing scroll events
        self._scroll_timer = wx.Timer(self)
        self._scroll_delay = 300  # ms to wait after scroll stops
        self.Bind(wx.EVT_TIMER, self._on_scroll_timer, self._scroll_timer)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        self.enable_toolbar_buttons(False)

        # initiate the initial search now that the window has been constructed
        ready = False
        try:
            lib = (self.library or getattr(self.parent, "library", None))
            ready = bool(lib and getattr(lib, "state", None) == LibraryState.INITIALIZED)
        except Exception:
            self.logger.exception("Error checking library readiness")
            ready = False
        self.set_db_ready(ready)
        if ready:
            self.search(None)

    def on_scroll(self, evt):
        self.logger.debug(evt.GetPosition())  # touch to avoid "unused variable" warning

    def on_wheel(self, event):
        self.logger.debug("on_wheel event")
        # self.logger.debug(self.table_scroller.GetPosition())
        self.logger.debug(self.scrolled_sizer.GetPosition())
        # self.logger.debug(self.part_list.GetPosition())
        # self.logger.debug("on_wheel event", self.part_list.GetScrollPos(wx.VERTICAL))
        event.Skip()

    # -------------------- DB readiness helpers --------------------
    def _initial_categories(self):
        lib = (self.library or getattr(self.parent, "library", None))
        if not lib:
            return []
        try:
            return lib.categories
        except Exception:
            self.logger.exception("Failed to fetch initial categories")
            return []

    def set_db_ready(self, ready: bool):
        try:
            self.search_button.Enable(bool(ready))
            self.keyword.Enable(bool(ready))
            self.manufacturer.Enable(bool(ready))
            self.package.Enable(bool(ready))
            self.category.Enable(bool(ready))
            self.subcategory.Enable(bool(ready))
            self.part_no.Enable(bool(ready))
            self.solder_joints.Enable(bool(ready))
        except Exception:
            self.logger.exception("Error enabling/disabling widgets in set_db_ready")

        if ready:
            self.refresh_categories()

    def refresh_categories(self):
        try:
            cats = self._initial_categories()
            self.category.Clear()
            if cats:
                self.category.AppendItems(cats)
                try:
                    self.category.SetSelection(0)
                except Exception:
                    self.logger.exception("category SetSelection failed")
                self.update_subcategories()
        except Exception:
            self.logger.exception("refresh_categories failed")

    def update_settings(self, event):
        """Update the settings on change."""
        wx.PostEvent(
            (self.parent or self),
            UpdateSetting(
                section="partselector",
                setting=event.GetEventObject().GetName(),
                value=event.GetEventObject().GetValue(),
            ),
        )

        # No automatic search; user triggers search explicitly

    @staticmethod
    def get_existing_selection(parts):
        """Check if exactly one LCSC part number is amongst the selected parts."""
        s = set(parts.values())
        if len(s) != 1:
            return ""
        return list(s)[0]

    def quit_dialog(self, *_):
        """Close this window."""
        self.Destroy()
        self.EndModal(0)

    def OnSortPartList(self, e):
        """Set order_by to the clicked column and trigger list refresh."""
        (self.library or getattr(self.parent, "library", None)).set_order_by(e.GetColumn())
        self.search(None)

    def OnPartSelected(self, *_):
        """Enable the toolbar buttons when a selection was made."""
        if self.part_list.GetSelectedItemsCount() > 0:
            self.enable_toolbar_buttons(True)
            # Update preview and links for selected part
            self.update_selected_part_details()
        else:
            self.enable_toolbar_buttons(False)

    def enable_toolbar_buttons(self, state):
        """Control the state of all the buttons in toolbar on the right side."""
        for b in [
            self.select_part_button,
            self.part_details_button,
            # self.open_datasheet_button,
            # self.open_lcsc_button,
        ]:
            b.Enable(bool(state))

    def add_ohm_symbol(self, *_):
        """Insert the Ω symbol at the current caret position."""
        # Insert at caret (or replace selection) and return focus to the field
        self.keyword.WriteText("Ω")
        self.keyword.SetFocus()

    def add_micro_symbol(self, *_):
        """Insert the µ symbol at the current caret position."""
        self.keyword.WriteText("µ")
        self.keyword.SetFocus()

    def search_dwell(self, *_):
        """Initiate a search once the timeout expires.

        Used to avoid continous searches
        when input fields are still being changed by the user.
        """
        self.search_timer.StartOnce(self._debounce_ms)

    def search(self, *_):
        """Search the library for parts that meet the search criteria."""
        parameters = {
            "keyword": self.keyword.GetValue(),
            "manufacturer": self.manufacturer.GetValue(),
            "package": self.package.GetValue(),
            "category": self.category.GetValue(),
            "subcategory": self.subcategory.GetValue(),
            "part_no": self.part_no.GetValue(),
            "solder_joints": self.solder_joints.GetValue(),
            "basic": self.basic_checkbox.GetValue(),
            "extended": self.extended_checkbox.GetValue(),
            "stock": self.assert_stock_checkbox.GetValue(),
        }
        start = time.time()
        lib = (self.library or getattr(self.parent, "library", None))
        result = lib.search(parameters) if lib else []
        self.logger.debug("len(result) %d", len(result))
        # self.logger.debug(result)
        search_duration = time.time() - start
        self.populate_part_list(result, search_duration)

    def populate_from_lcsc_ids(self, ids: list) -> None:
        """Search the database for the given LCSC IDs and populate the parts list."""
        import time as _time
        lib = self.library or getattr(self.parent, "library", None)
        if lib is None:
            return
        start = _time.time()
        results = lib.search_by_lcsc_ids(ids)
        self.populate_part_list(results, _time.time() - start)

    def update_subcategories(self, *_):
        """Update the possible subcategory selection."""
        self.subcategory.Clear()
        if self.category.GetSelection() != wx.NOT_FOUND:
            lib = (self.library or getattr(self.parent, "library", None))
            subcategories = lib.get_subcategories(
                self.category.GetValue()
            )
            self.subcategory.AppendItems(subcategories)

        # Do not trigger an automatic search on category change

    def get_price(self, quantity, prices) -> float:
        """Find the price for the number of selected parts accordning to the price ranges."""
        price_ranges = prices.split(",")
        if not price_ranges[0]:
            return -1.0
        min_quantity = int(price_ranges[0].split("-")[0])
        if quantity <= min_quantity:
            range, price = price_ranges[0].split(":")
            return float(price)
        for p in price_ranges:
            range, price = p.split(":")
            lower, upper = range.split("-")
            if not upper:  # upper bound of price ranges
                return float(price)
            lower = int(lower)
            upper = int(upper)
            if lower <= quantity < upper:
                return float(price)
        return -1.0

    @staticmethod
    def _format_currency(value: float) -> str:
        """Format monetary values with trimmed trailing zeros."""
        formatted = f"{value:.3f}".rstrip("0").rstrip(".")
        return f"${formatted or '0'}"

    def populate_part_list(self, parts, search_duration):
        """Populate the list with the result of the search."""
        search_duration_text = (
            f"{search_duration:.2f}s"
            if search_duration > 1
            else f"{search_duration * 1000.0:.0f}ms"
        )
        # Invalidate any in-flight thumbnail work and clear caches
        self._search_generation += 1
        with self._thumb_lock:
            self._thumb_cache.clear()
            self._thumb_scheduled.clear()
        self.part_list_model.RemoveAll()
        if parts is None:
            return
        count = len(parts)
        if count >= 1000:
            self.result_count.SetLabel(
                f"{count} Results (limited) in {search_duration_text}"
            )
        else:
            self.result_count.SetLabel(f"{count} Results in {search_duration_text}")
        qty = len(self.parts)
        for p in parts:
            item = [str(c) for c in p]
            pricecol = 8  # Must match order in library.py search function
            price = round(self.get_price(qty, item[pricecol]), 3)
            if price > 0:
                total = round(price * qty, 3)
                item[pricecol] = (
                    f"{qty}x{self._format_currency(price)} -> {self._format_currency(total)}"
                )
            else:
                item[pricecol] = "Error in price data"
            params = params_for_part(
                {"description": item[7], "category": item[9], "package": item[2]}
            )
            item.insert(5, params)
            # self.logger.debug(item)
            self.part_list_model.AddEntry(item)

        # Reset preview/links after repopulating
        self._selected_lcsc = None
        self._pdfurl = ""
        self._pageurl = ""
        self.Layout()
        # trigger initial visible thumbnails loading
        self._schedule_visible_thumbnails()

    def select_part(self, *_):
        """Save the selected part number and close the modal."""
        if self.part_list.GetSelectedItemsCount() > 0:
            item = self.part_list.GetSelection()
            wx.PostEvent(
                self.parent,
                AssignPartsEvent(
                    lcsc=self.part_list_model.get_lcsc(item),
                    type=self.part_list_model.get_type(item),
                    stock=self.part_list_model.get_stock(item),
                    references=self.parts.keys(),
                ),
            )
            self.EndModal(wx.ID_OK)

    def get_part_details(self, *_):
        """Fetch part details from LCSC and show them in a modal."""
        if self.part_list.GetSelectedItemsCount() > 0:
            item = self.part_list.GetSelection()
            busy_cursor = wx.BusyCursor()
            # Use this dialog as parent to avoid side effects on hidden contexts
            dialog = PartDetailsDialog(self, self.part_list_model.get_lcsc(item))
            del busy_cursor
            dialog.ShowModal()

    def update_selected_part_details(self):
        """Fetch preview image and urls for currently selected item."""
        try:
            item = self.part_list.GetSelection()
            lcsc = self.part_list_model.get_lcsc(item)
        except Exception:
            return
        if not lcsc or lcsc == self._selected_lcsc:
            return
        self._selected_lcsc = lcsc
        try:
            wx.BeginBusyCursor()
            result = self._lcsc_api.get_part_data(lcsc)
        except Exception:
            result = {"success": False}
        finally:
            try:
                wx.EndBusyCursor()
            except Exception:
                self.logger.exception("wx.EndBusyCursor failed in update_selected_part_details")
        if not result or not result.get("success"):
            # Reset
            self._pdfurl = ""
            self._pageurl = ""
            return
        data = result.get("data", {}).get("data", {})
        self._pdfurl = data.get("dataManualUrl") or ""
        self._pageurl = data.get("lcscGoodsUrl") or ""
        # Right-side preview image removed; keep URLs only
        
    def get_scaled_bitmap(self, url, width, height):
        """Download a picture from a URL and convert it into a wx Bitmap.

        Falls back to placeholder if data is not an image or decode fails.
        """
        try:
            io_bytes = self._lcsc_api.download_bitmap(url)
            if not io_bytes:
                raise ValueError("Non-image content")
            image = wx.Image(io_bytes, wx.BITMAP_TYPE_ANY)
            if not image.IsOk():
                raise ValueError("Decode failed")
            image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
            return wx.Bitmap(image)
        except Exception:
            return loadBitmapScaled("placeholder.png", self.scale_factor, static=True)

    def open_datasheet(self, *_):
        if self._pdfurl:
            webbrowser.open(self._pdfurl)

    def open_lcsc_page(self, *_):
        if self._pageurl:
            webbrowser.open(self._pageurl)

    def help(self, *_):
        """Show message box with help instructions."""
        title = "Help"
        text = """
        Use % as wildcard selector. \n
        For example DS24% will match DS2411\n
        %QFP% wil match LQFP-64 as well as TQFP-32\n
        The keyword search box is automatically post- and prefixed with wildcard operators.
        The others are not by default.\n
        The keyword search field is applied to "LCSC Part", "Description", "MFR.Part",
        "Package" and "Manufacturer".\n
        Click the Search button or press Enter in a search field to update results.\n
        The results are limited to 1000.
        """
        wx.MessageBox(text, title, style=wx.ICON_INFORMATION)

        
    # ---------------------- Thumbnails (lazy loading) ----------------------
    def _on_table_viewport_changed(self, evt):
        # Allow the event to propagate so scrolling still works
        try:
            evt.Skip()
        except Exception:
            self.logger.exception("evt.Skip() failed in _on_table_viewport_changed")

        # Recalculate visible range purely via HitTest (no wheel math)
        try:
            row_h = max(24, int(self._thumb_px + 2))
            if hasattr(self.part_list, 'GetRowHeight'):
                try:
                    rh = self.part_list.GetRowHeight()
                    if rh > 0:
                        row_h = rh
                except Exception:
                    pass

            main = self.part_list.GetMainWindow() # if hasattr(self.part_list, 'GetMainWindow') else self.part_list
            size = main.GetClientSize()
            h = size.GetHeight()
           
            # Determine first and last visible rows by hit-testing points
            # Use a few sample x positions within the first column area
            test_x = 10
            start_pt = (test_x, row_h)
            end_pt = (test_x, h-1)

            start_item, start_col = self.part_list.HitTest(start_pt)
            end_item, end_col = self.part_list.HitTest(end_pt)

            # DEBUG HITTEST: disabled noisy logs
            # try:
            #     self.logger.info(
            #         "[HITTEST] start_pt=%s start_col=%s start_item_valid=%s; end_pt=%s end_col=%s end_item_valid=%s",
            #         start_pt,
            #         start_col,
            #         bool(start_item and start_item.IsOk() if hasattr(start_item, 'IsOk') else start_item),
            #         end_pt,
            #         end_col,
            #         bool(end_item and end_item.IsOk() if hasattr(end_item, 'IsOk') else end_item),
            #     )
            # except Exception:
            #     pass

            def _item_to_index(item):
                try:
                    obj = self.part_list_model.ItemToObject(item)
                    return self.part_list_model.get_all().index(obj)
                except Exception:
                    return -1

            start_idx = _item_to_index(start_item)
            end_idx = _item_to_index(end_item)

            # DEBUG HITTEST: disabled noisy logs
            # try:
            #     self.logger.info(
            #         "[HITTEST] start_idx=%d end_idx=%d viewport_h=%d", start_idx, end_idx, h
            #     )
            # except Exception:
            #     pass

            # Fallback: if HitTest misses, estimate using row height
            if start_idx < 0 or end_idx < 0:
                
                visible_count = max(1, (h + row_h - 1) // row_h)
                total = len(self.part_list_model.get_all()) if hasattr(self.part_list_model, 'get_all') else 0
                # keep existing top_index but clamp to bounds
                max_start = max(0, total - visible_count)
                self._top_index = max(0, min(getattr(self, '_top_index', 0), max_start))
            else:
                self._top_index = max(0, min(start_idx, end_idx))

            # DEBUG SCROLL: disabled noisy logs
            # try:
            #     self.logger.info("[SCROLL DEBUG] top_index=%d (HitTest)", self._top_index)
            # except Exception:
            #     pass
        except Exception:
            pass

        # Reset the scroll timer to debounce rapid scroll events
        try:
            self._scroll_timer.Stop()
            self._scroll_timer.StartOnce(40)
        except Exception:
            pass

    def _on_scroll_timer(self, evt):
        """Called when scroll timer expires - now safe to load thumbnails"""
        self._schedule_visible_thumbnails()
        # pass
        
    def _schedule_visible_thumbnails(self):
        """Schedule loading of thumbnails for visible items"""
        try:
            items = self._get_visible_items(prefetch_rows=self.THUMB_PREFETCH_ROWS)
        except Exception:
            items = []
        try:
            # Збираємо лише список видимих LCSC
            visible_lcsc = []
            for item in items:
                try:
                    lcsc = self.part_list_model.get_lcsc(item)
                    if lcsc is not None:
                        visible_lcsc.append(str(lcsc))
                except Exception:
                    continue

            # Запускаємо завантаження прев'ю для видимих елементів
            rows = self.part_list_model.get_all() if hasattr(self.part_list_model, 'get_all') else []
            for item in items:
                try:
                    lcsc = self.part_list_model.get_lcsc(item)
                    if not lcsc:
                        continue
                    with self._thumb_lock:
                        if lcsc in self._thumb_cache or lcsc in self._thumb_scheduled:
                            continue
                        self._thumb_scheduled.add(lcsc)
                    try:
                        row_obj = self.part_list_model.ItemToObject(item)
                        row_index = rows.index(row_obj)
                    except Exception:
                        row_index = -1
                    self._thumb_executor.submit(
                        self._load_thumbnail_worker,
                        lcsc,
                        self._thumb_px,
                        self._thumb_px,
                        self._search_generation,
                        row_index,
                    )
                except Exception:
                    continue

            # Узагальнений лог по видимих рядках і їх LCSC
            # DEBUG VISIBLE: disabled noisy logs
            # try:
            #     self.logger.info(
            #         "[VISIBLE] top_index=%d visible=%d ids=%s",
            #         getattr(self, '_top_index', 0), len(visible_lcsc), ",".join(visible_lcsc)
            #     )
            # except Exception:
            #     pass
        except Exception:
            self.logger.exception("Error preparing visible thumbnails in _schedule_visible_thumbnails")
        # В режимі дебагу тільки логуємо але не завантажуємо
        pass

    def _get_visible_items(self, prefetch_rows=0):
        """Calculate which items are currently visible in the viewport.
        
        Args:
            prefetch_rows: Number of additional rows to include before and after the visible range
            
        Returns:
            List of visible DataViewItem objects
        """
        try:
            # Get dimensions
            main = self.part_list.GetMainWindow() if hasattr(self.part_list, 'GetMainWindow') else self.part_list
            view_h = main.GetClientSize().GetHeight()
            row_h = max(24, int(self._thumb_px + 2))
            
            if hasattr(self.part_list, 'GetRowHeight'):
                try:
                    h = self.part_list.GetRowHeight()
                    if h > 0:
                        row_h = h
                except Exception:
                    pass

            # Calculate visible range
            visible_count = max(1, (view_h + row_h - 1) // row_h)
            total = len(self.part_list_model.get_all()) if hasattr(self.part_list_model, 'get_all') else 0
            
            if total == 0:
                return []

            # Add prefetch padding
            start = max(0, self._top_index - prefetch_rows)
            end = min(total, self._top_index + visible_count + prefetch_rows)
            
            # Get DataViewItems for the visible range
            items = []
            for i in range(start, end):
                try:
                    row = self.part_list_model.get_all()[i]
                    item = self.part_list_model.ObjectToItem(row)
                    items.append(item)
                except Exception:
                    continue
                    
            return items

        except Exception:
            self.logger.exception("Error calculating visible items")
            return []

    def _toggle_thumb_size(self, *_):
        """Toggle thumbnail size and adjust UI accordingly."""
        try:
            # Toggle between 60 and 100 px
            new_size = 100 if self._thumb_px <= 60 else 60
            self.THUMB_HEIGHT_PX = new_size
            self._thumb_px = int(self.THUMB_HEIGHT_PX)

            # Update button icon and tooltip to reflect next action
            try:
                if self._thumb_px >= 100:
                    # Now large; clicking will collapse
                    self.thumb_toggle_button.SetBitmap(
                        loadBitmapScaled("mdi-arrow-collapse-vertical.png", self.scale_factor)
                    )
                    self.thumb_toggle_button.SetToolTip("Decrease thumbnail size")
                else:
                    # Now small; clicking will expand
                    self.thumb_toggle_button.SetBitmap(
                        loadBitmapScaled("mdi-arrow-expand-vertical.png", self.scale_factor)
                    )
                    self.thumb_toggle_button.SetToolTip("Increase thumbnail size")
            except Exception:
                pass

            # Adjust row height if supported
            try:
                if hasattr(self.part_list, 'SetRowHeight'):
                    self.part_list.SetRowHeight(max(24, self._thumb_px + 2))
            except Exception:
                pass

            # Adjust first column width to match thumbnail
            try:
                col = self.part_list.GetColumn(0)
                col.SetWidth(int(self._thumb_px + 8))
            except Exception:
                pass

            # Invalidate thumbs so they get regenerated at new size
            with self._thumb_lock:
                self._thumb_cache.clear()
                self._thumb_scheduled.clear()

            # Force refresh of visible thumbnails
            self._schedule_visible_thumbnails()
            try:
                self.part_list.Refresh()
                self.table_scroller.Layout()
                self.Layout()
            except Exception:
                pass
        except Exception:
            self.logger.exception("Failed to toggle thumbnail size")

    def _toggle_advanced(self, *_):
        """Show/hide the advanced filters block under the search row."""
        try:
            self._advanced_visible = not bool(getattr(self, "_advanced_visible", False))
            try:
                self.advanced_panel.Show(self._advanced_visible)
            except Exception:
                pass
            self._update_advanced_toggle_visuals()
            # Relayout to give more room to results
            try:
                self.Layout()
                self.part_list.Refresh()
                self.table_scroller.Layout()
            except Exception:
                pass
            try:
                wx.PostEvent(
                    (self.parent or self),
                    UpdateSetting(
                        section="partselector",
                        setting="advanced_visible",
                        value=self._advanced_visible,
                    ),
                )
            except Exception:
                pass
        except Exception:
            self.logger.exception("Failed to toggle advanced filters visibility")

    def _toggle_log(self, *_):
        """Show or hide the bottom log console."""
        try:
            console = getattr(self, "console", None)
            if console is None:
                return
            visible = not console.IsShown()
            console.Show(visible)
            # Also hide/show the clear button together with the console
            clear_btn = getattr(self, "clear_log_button", None)
            if clear_btn is not None:
                clear_btn.Show(visible)
            try:
                if visible:
                    self.log_toggle_button.SetBitmap(
                        loadBitmapScaled("mdi-console.png", self.scale_factor)
                    )
                    self.log_toggle_button.SetToolTip("Hide log")
                else:
                    self.log_toggle_button.SetBitmap(
                        loadBitmapScaled("mdi-console-line.png", self.scale_factor)
                    )
                    self.log_toggle_button.SetToolTip("Show log")
            except Exception:
                pass
            # Re-layout the entire window
            top = self
            while top.GetParent():
                top = top.GetParent()
            top.Layout()
            top.Refresh()
        except Exception:
            self.logger.exception("Failed to toggle log panel")

    def show_import_status(self, ok: int, failed: int) -> None:
        """Display a temporary success/failure summary in the results row."""
        try:
            lbl = self.import_status_label

            # Build rich text with colours using wx.richtext or coloured segments.
            # wx.StaticText only supports a single colour, so we rebuild the label
            # as two separate StaticText widgets — but they were not pre-created.
            # Instead, use Unicode coloured indicators + plain text and tint the label.
            total = ok + failed
            if total == 1:
                text = "Imported" if ok else "Failed"
            else:
                parts = []
                if ok:
                    parts.append(f"{ok} imported")
                if failed:
                    parts.append(f"{failed} failed")
                text = ", ".join(parts)

            lbl.SetLabel(text)

            # Colour the whole label: green if no failures, red if any failures,
            # orange if mixed.
            if failed == 0 and ok > 0:
                colour = wx.Colour(34, 139, 34)   # forest green
            elif ok == 0:
                colour = wx.Colour(180, 0, 0)      # dark red
            else:
                colour = wx.Colour(160, 80, 0)     # amber

            lbl.SetForegroundColour(colour)
            lbl.SetToolTip(f"Imported: {ok}  Failed: {failed}")
            lbl.Show()
            self.Layout()
            self.Refresh()

            # Auto-hide after 5 s
            self._import_status_timer.StartOnce(5000)
        except Exception:
            self.logger.exception("show_import_status failed")

    def _on_import_status_timer(self, *_):
        """Hide the import status label after the delay."""
        try:
            self.import_status_label.SetLabel("")
            self.import_status_label.Hide()
            self.Layout()
        except Exception:
            pass

    def _update_advanced_toggle_visuals(self):
        """Sync toggle button icon/tooltip with the current visibility state."""
        try:
            if self._advanced_visible:
                self.advanced_toggle_button.SetBitmap(
                    loadBitmapScaled("mdi-chevron-up.png", self.scale_factor)
                )
                self.advanced_toggle_button.SetBitmapMargins((0, 2))
                self.advanced_toggle_button.SetToolTip("Hide advanced filters")
            else:
                self.advanced_toggle_button.SetBitmap(
                    loadBitmapScaled("mdi-chevron-down.png", self.scale_factor)
                )
                self.advanced_toggle_button.SetBitmapMargins((0, 2))
                self.advanced_toggle_button.SetToolTip("Show advanced filters")
        except Exception:
            pass

    def _load_thumbnail_worker(self, lcsc: str, width: int, height: int, generation: int, row_index: int):
        # Resolve image URL via API
        try:
            # DEBUG THUMB: disabled noisy logs
            # try:
            #     self.logger.info("thumb start: row=%d lcsc=%s", row_index, lcsc)
            # except Exception:
            #     self.logger.exception("Logging thumb start failed")
            result = self._lcsc_api.get_part_data(lcsc)
            if not result or not result.get("success"):
                raise RuntimeError("part data not found")
            data = result.get("data", {}).get("data", {})
            picture = self._lcsc_api.resolve_part_image_url(data)
            if not picture:
                raise RuntimeError("no picture url")
            # DEBUG THUMB: disabled noisy logs
            # try:
            #     self.logger.info("thumb url: row=%d lcsc=%s url=%s", row_index, lcsc, picture)
            # except Exception:
            #     self.logger.exception("Logging thumb url failed")
            io_bytes = self._lcsc_api.download_bitmap(picture)
            if not io_bytes:
                raise RuntimeError("download failed")
            bmp = None
            if Image is not None:
                # Pillow path
                img = Image.open(io_bytes)
                img = img.convert("RGBA")
                if ImageOps is not None:
                    # Crop/fit to fill cell height and width
                    img = ImageOps.fit(img, (width, height), Image.LANCZOS, centering=(0.5, 0.5))
                else:
                    # Fallback to contain
                    img.thumbnail((width, height), Image.LANCZOS)
                w, h = img.size
                buf = img.tobytes()  # RGBA
                try:
                    image = wx.Image(w, h)
                    # Split RGBA to RGB + Alpha for wx.Image
                    rgb = bytearray()
                    alpha = bytearray()
                    for i in range(0, len(buf), 4):
                        r, g, b, a = buf[i : i + 4]
                        rgb.extend((r, g, b))
                        alpha.append(a)
                    image.SetData(bytes(rgb))
                    image.SetAlpha(bytes(alpha))
                    bmp = wx.Bitmap(image)
                except Exception:
                    try:
                        bmp = wx.Bitmap.FromBufferRGBA(w, h, buf)
                    except Exception:
                        bmp = None
            if bmp is None:
                # Fallback to wx.Image decode (use LoadStream to avoid type issues)
                try:
                    io_bytes.seek(0)
                    image = wx.Image()
                    ok = image.LoadStream(io_bytes)
                    if not ok or not image.IsOk():
                        raise RuntimeError("Decode failed")
                    image = image.Scale(width, height, wx.IMAGE_QUALITY_HIGH)
                    bmp = wx.Bitmap(image)
                except Exception as exc:
                    # Last resort placeholder to avoid empty cell
                    self.logger.warning(
                        "thumb failed: row=%d lcsc=%s url=%s err=%s",
                        row_index,
                        lcsc,
                        locals().get('picture', None),
                        exc,
                    )
                    image = wx.Image(width, height)
                    image.SetRGBRect(wx.Rect(0, 0, width, height), 240, 240, 240)
                    bmp = wx.Bitmap(image)
                   
        except Exception as exc:  # noqa: BLE001
            # Mark as attempted to avoid retry storms
            with self._thumb_lock:
                self._thumb_cache[lcsc] = None
                self._thumb_scheduled.discard(lcsc)
            try:
                # Log URL that failed to decode
                self.logger.warning(
                    "thumb failed: row=%d lcsc=%s url=%s err=%s",
                    row_index,
                    lcsc,
                    locals().get('picture', None),
                    exc,
                )
            except Exception:
                self.logger.exception("Logging thumb failure warning failed")
            return
        # Update UI on main thread
        def _apply():
            # Discard if a newer search repopulated the model
            if generation != self._search_generation:
                return
            try:
                with self._thumb_lock:
                    self._thumb_cache[lcsc] = bmp
                    self._thumb_scheduled.discard(lcsc)
                self.part_list_model.set_thumbnail_for_lcsc(lcsc, bmp)
                # DEBUG THUMB APPLIED: disabled noisy logs
                # try:
                #     self.logger.info("thumb applied: row=%d lcsc=%s size=%dx%d", row_index, lcsc, bmp.GetWidth(), bmp.GetHeight())
                # except Exception:
                #     self.logger.exception("Logging thumb applied info failed")
            except Exception:
                with self._thumb_lock:
                    self._thumb_cache[lcsc] = None
                    self._thumb_scheduled.discard(lcsc)
                self.logger.exception("Failed to apply thumbnail to model/UI")
        wx.CallAfter(_apply)
