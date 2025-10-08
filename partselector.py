"""Contains the part selector modal window."""

import logging
import time
import webbrowser

import wx  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from .datamodel import PartSelectorDataModel
from .derive_params import params_for_part  # pylint: disable=import-error
from .events import AssignPartsEvent, UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled, GetScaleFactor
from .partdetails import PartDetailsDialog
from .lcsc_api import LCSC_API

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
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(20, -1)),
            0,
        )
        self.ohm_button.SetToolTip("Insert Ω at cursor in the search field")

        self.micro_button = wx.Button(
            self,
            wx.ID_ANY,
            "µ",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(20, -1)),
            0,
        )
        self.micro_button.SetToolTip("Insert µ at cursor in the search field")

        manufacturer_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Manufacturer",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.manufacturer = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.manufacturer.SetHint("e.g. Vishay")

        package_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Package",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.package = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.package.SetHint("e.g. 0603")

        category_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Category",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.category = wx.ComboBox(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            choices=(self.library or getattr(self.parent, "library", None)).categories if (self.library or getattr(self.parent, "library", None)) else [],
            style=wx.CB_READONLY,
        )
        self.category.SetHint("e.g. Resistors")

        part_no_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Part number",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.part_no = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.part_no.SetHint("e.g. DS2411")

        solder_joints_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Solder joints",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.solder_joints = wx.TextCtrl(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            wx.TE_PROCESS_ENTER,
        )
        self.solder_joints.SetHint("e.g. 2")

        subcategory_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Subcategory",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.subcategory = wx.ComboBox(
            self,
            wx.ID_ANY,
            "",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            style=wx.CB_READONLY,
        )
        self.subcategory.SetHint("e.g. Variable Resistors")

        basic_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include basic parts",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.basic_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Basic",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            0,
            name="basic",
        )
        extended_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Include extended parts",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.extended_checkbox = wx.CheckBox(
            self,
            wx.ID_ANY,
            "Extended",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(200, 24)),
            0,
            name="extended",
        )
        stock_label = wx.StaticText(
            self,
            wx.ID_ANY,
            "Only show parts in stock",
            size=HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 15)),
        )
        self.assert_stock_checkbox = wx.CheckBox(
            self,
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
            self,
            wx.ID_ANY,
            "Help",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(100, -1)),
            0,
        )

        keyword_search_row1 = wx.BoxSizer(wx.HORIZONTAL)
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
        keyword_search_row1.Add(self.search_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

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

        search_sizer_row2 = wx.StaticBoxSizer(wx.HORIZONTAL, self)
        search_sizer_row2.Add(search_sizer_one, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_two, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_three, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_four, 0, wx.RIGHT, 20)
        search_sizer_row2.Add(search_sizer_five, 0, wx.RIGHT, 20)
        # search_sizer.Add(help_button, 0, wx.RIGHT, 20)

        search_sizer.Add(search_sizer_row2, 0, wx.EXPAND)

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

        # Enable type-ahead selection for read-only ComboBoxes (category/subcategory)
        # self._category_typeahead = _ComboTypeAhead(self.category)
        # self._subcategory_typeahead = _ComboTypeAhead(self.subcategory)

        # ---------------------------------------------------------------------
        # ------------------------ Result status line -------------------------
        # ---------------------------------------------------------------------

        self.result_count = wx.StaticText(
            self, wx.ID_ANY, "0 Results", wx.DefaultPosition, wx.DefaultSize
        )

        result_sizer = wx.BoxSizer(wx.HORIZONTAL)
        result_sizer.Add(self.result_count, 0, wx.LEFT | wx.TOP, 5)

        # ---------------------------------------------------------------------
        # ------------------------- Result Part list --------------------------
        # ---------------------------------------------------------------------

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)

        table_scroller = wx.ScrolledWindow(self, style=wx.HSCROLL | wx.VSCROLL)
        table_scroller.SetScrollRate(20, 20)

        self.part_list = dv.DataViewCtrl(
            table_scroller,
            style=wx.BORDER_THEME | dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_SINGLE,
        )

        lcsc = self.part_list.AppendTextColumn(
            "LCSC",
            0,
            width=int(self.scale_factor * 60),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        mfr_number = self.part_list.AppendTextColumn(
            "MFR Number",
            1,
            width=int(self.scale_factor * 140),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        package = self.part_list.AppendTextColumn(
            "Package",
            2,
            width=int(self.scale_factor * 100),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        pins = self.part_list.AppendTextColumn(
            "Pins",
            3,
            width=int(self.scale_factor * 40),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        parttype = self.part_list.AppendTextColumn(
            "Type",
            4,
            width=int(self.scale_factor * 50),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        params = self.part_list.AppendTextColumn(
            "Params",
            5,
            width=int(self.scale_factor * 150),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        stock = self.part_list.AppendTextColumn(
            "Stock",
            6,
            width=int(self.scale_factor * 50),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        mfr = self.part_list.AppendTextColumn(
            "Manufacturer",
            7,
            width=int(self.scale_factor * 100),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        description = self.part_list.AppendTextColumn(
            "Description",
            8,
            width=int(self.scale_factor * 300),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )
        price = self.part_list.AppendTextColumn(
            "Price",
            9,
            width=int(self.scale_factor * 100),
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_LEFT,
        )

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
        self.part_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.select_part)
        scrolled_sizer = wx.BoxSizer(wx.VERTICAL)
        scrolled_sizer.Add(self.part_list, 1, wx.EXPAND)
        table_scroller.SetSizer(scrolled_sizer)

        table_sizer.Add(table_scroller, 20, wx.ALL | wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------ Right side toolbar -------------------------
        # ---------------------------------------------------------------------

        self.select_part_button = wx.Button(
            self,
            wx.ID_ANY,
            "Import part",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, -1)),
            0,
        )
        self.part_details_button = wx.Button(
            self,
            wx.ID_ANY,
            "Show part details",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, -1)),
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
        # Image preview
        self.preview_image = wx.StaticBitmap(
            self,
            wx.ID_ANY,
            loadBitmapScaled("placeholder.png", self.scale_factor, static=True),
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 150)),
            0,
        )
        # Fix preview target size to avoid layout jumps
        _size = HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, 150))
        self._preview_w, self._preview_h = _size.GetWidth(), _size.GetHeight()
        self.preview_image.SetMinSize(wx.Size(self._preview_w, self._preview_h))
        self.preview_image.SetMaxSize(wx.Size(self._preview_w, self._preview_h))

        self.select_part_button.Bind(wx.EVT_BUTTON, self.select_part)
        self.part_details_button.Bind(wx.EVT_BUTTON, self.get_part_details)
        # self.open_datasheet_button.Bind(wx.EVT_BUTTON, self.open_datasheet)
        # self.open_lcsc_button.Bind(wx.EVT_BUTTON, self.open_lcsc_page)

        self.select_part_button.SetBitmap(
            loadBitmapScaled(
                "mdi-check.png",
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

        tool_sizer = wx.BoxSizer(wx.VERTICAL)
        tool_sizer.Add(self.select_part_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.part_details_button, 0, wx.ALL, 5)
        # tool_sizer.Add(self.open_datasheet_button, 0, wx.ALL, 5)
        # tool_sizer.Add(self.open_lcsc_button, 0, wx.ALL, 5)
        tool_sizer.Add(self.preview_image, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 5)

        # Clear log button placed directly under preview
        self.clear_log_button = wx.Button(
            self,
            wx.ID_ANY,
            "Clear log",
            wx.DefaultPosition,
            HighResWxSize(getattr(self.parent, "window", self), wx.Size(150, -1)),
            0,
        )
        self.clear_log_button.SetBitmap(
            loadBitmapScaled("mdi-trash-can-outline.png", self.scale_factor)
        )
        self.clear_log_button.SetBitmapMargins((2, 0))
        # Delegate action to the parent host if available
        self.clear_log_button.Bind(
            wx.EVT_BUTTON,
            lambda _evt: getattr(self.parent, "_clear_log", lambda *_: None)(),
        )
        tool_sizer.Add(self.clear_log_button, 0, wx.ALL, 5)
        table_sizer.Add(tool_sizer, 3, wx.EXPAND, 5)

        # ---------------------------------------------------------------------
        # ------------------------------ Sizers  ------------------------------
        # ---------------------------------------------------------------------

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(search_sizer, 1, wx.ALL, 5)
        # layout.Add(self.search_button, 5, wx.ALL, 5)
        layout.Add(result_sizer, 1, wx.LEFT, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)

        self.part_list_model = PartSelectorDataModel()
        self.part_list.AssociateModel(self.part_list_model)

        self.SetSizer(layout)
        self.Layout()
        self.Centre(wx.BOTH)
        self.enable_toolbar_buttons(False)

        # initiate the initial search now that the window has been constructed
        self.search(None)

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

    def populate_part_list(self, parts, search_duration):
        """Populate the list with the result of the search."""
        search_duration_text = (
            f"{search_duration:.2f}s"
            if search_duration > 1
            else f"{search_duration * 1000.0:.0f}ms"
        )
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
        for p in parts:
            item = [str(c) for c in p]
            pricecol = 8  # Must match order in library.py search function
            price = round(self.get_price(len(self.parts), item[pricecol]), 3)
            if price > 0:
                sum = round(price * len(self.parts), 3)
                item[pricecol] = (
                    f"{len(self.parts)} parts: ${price} each / ${sum} total"
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
        self.preview_image.SetBitmap(
            loadBitmapScaled("placeholder.png", self.scale_factor, static=True)
        )
        self.Layout()

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
                pass
        if not result or not result.get("success"):
            # Reset
            self._pdfurl = ""
            self._pageurl = ""
            self.preview_image.SetBitmap(
                loadBitmapScaled("placeholder.png", self.scale_factor, static=True)
            )
            self.Layout()
            return
        data = result.get("data", {}).get("data", {})
        self._pdfurl = data.get("dataManualUrl") or ""
        self._pageurl = data.get("lcscGoodsUrl") or ""
        # resolve image
        picture = data.get("minImage")
        if picture:
            picture = picture.replace("96x96", "900x900")
        else:
            imageId = data.get("productBigImageAccessId")
            if imageId:
                picture = f"https://jlcpcb.com/api/file/downloadByFileSystemAccessId/{imageId}"
        
        if picture:
            bmp = self.get_scaled_bitmap(picture, int(self._preview_w), int(self._preview_h))
            self.preview_image.SetBitmap(bmp)
            self.Layout()
        else:
            self.preview_image.SetBitmap(
                loadBitmapScaled("placeholder.png", self.scale_factor, static=True)
            )
            self.Layout()
            
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
