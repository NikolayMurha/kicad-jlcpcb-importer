"""Assign LCSC main dialog wrapping the part selector as primary UI."""

import os
import re
import json
import sys
import logging
import subprocess
import shutil
import threading
from pathlib import Path
import wx
from typing import Optional, List, Tuple, Dict

from .partselector import PartSelectorDialog
from .settings import SettingsDialog
from .helpers import HighResWxSize, loadBitmapScaled, GetScaleFactor, PLUGIN_PATH
from .events import (
    EVT_LOGBOX_APPEND_EVENT,
    EVT_MESSAGE_EVENT,
    EVT_DOWNLOAD_STARTED_EVENT,
    EVT_DOWNLOAD_PROGRESS_EVENT,
    EVT_DOWNLOAD_COMPLETED_EVENT,
    EVT_UNZIP_COMBINING_STARTED_EVENT,
    EVT_UNZIP_COMBINING_PROGRESS_EVENT,
    EVT_UNZIP_EXTRACTING_STARTED_EVENT,
    EVT_UNZIP_EXTRACTING_PROGRESS_EVENT,
    EVT_UNZIP_EXTRACTING_COMPLETED_EVENT,
    LogboxAppendEvent,
    MessageEvent,
)
from .library import Library

import pcbnew as kicad_pcbnew  # pylint: disable=import-error


class KicadProvider:
    """KiCad provider for board access."""

    def get_pcbnew(self):  # pragma: no cover - depends on KiCad runtime
        return kicad_pcbnew


class AssignLCSCMainDialog(PartSelectorDialog):
    """Main plugin window that focuses on assigning LCSC numbers without legacy mainwindow."""

    def __init__(self, kicad_provider: Optional[KicadProvider] = None):
        # Minimal context expected by PartSelectorDialog
        self.pcbnew = (kicad_provider or KicadProvider()).get_pcbnew()
        self.window = self  # fallback until wx top-level is available
        self.scale_factor = 1.0

        # Project context
        try:
            self.project_path, self.board_name, self.schematic_name = self._detect_project_context()
        except Exception:
            self.project_path = os.getcwd()
            self.board_name = "board.kicad_pcb"
            self.schematic_name = "board.kicad_sch"

        # Settings and library context
        self.settings = {}
        self._load_settings()
        self.library = Library(self)
        # Dependencies state
        self._deps_ready = False

        # Build the PartSelectorDialog with self as the logical parent context
        super().__init__(self, parts={})

        # Now that wx is initialized, update window and scale factor
        self.window = wx.GetTopLevelParent(self) or self
        self.scale_factor = GetScaleFactor(self.window)

        # Insert a topbar with Update button
        topbar = wx.BoxSizer(wx.HORIZONTAL)
        self.update_db_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Update database",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(160, -1)),
            0,
        )
        self.update_db_btn.SetBitmap(
            loadBitmapScaled("mdi-database-import-outline.png", self.scale_factor)
        )
        self.update_db_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.update_db_btn, 0, wx.ALL, 5)

        # Settings button
        self.settings_btn = wx.Button(
            self,
            wx.ID_ANY,
            "Settings",
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(120, -1)),
            0,
        )
        self.settings_btn.SetBitmap(
            loadBitmapScaled("mdi-cog-outline.png", self.scale_factor)
        )
        self.settings_btn.SetBitmapMargins((2, 0))
        topbar.Add(self.settings_btn, 0, wx.ALL, 5)

        layout = self.GetSizer()
        if layout:
            layout.Insert(0, topbar, 0, wx.EXPAND | wx.ALL, 5)

        # Add bottom console
        self.console = wx.TextCtrl(
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.console.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 140)))
        # Progress gauge under the console
        self.gauge = wx.Gauge(
            self,
            wx.ID_ANY,
            100,
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(100, -1)),
            wx.GA_HORIZONTAL,
        )
        self.gauge.SetValue(0)
        self.gauge.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 5)))
        if layout:
            layout.Add(self.console, 0, wx.ALL | wx.EXPAND, 5)
            layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)
            self.Layout()

        # Wire events and actions
        self.update_db_btn.Bind(wx.EVT_BUTTON, lambda _evt: self.update_library())
        # Handle messages and progress locally
        self.Bind(EVT_LOGBOX_APPEND_EVENT, self._append_log)
        self.Bind(EVT_MESSAGE_EVENT, self._show_message)
        self.Bind(EVT_DOWNLOAD_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_DOWNLOAD_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_DOWNLOAD_COMPLETED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_COMBINING_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_COMBINING_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_UNZIP_EXTRACTING_STARTED_EVENT, self._on_progress_reset)
        self.Bind(EVT_UNZIP_EXTRACTING_PROGRESS_EVENT, self._on_progress_update)
        self.Bind(EVT_UNZIP_EXTRACTING_COMPLETED_EVENT, self._on_progress_reset)
        # Settings updates from PartSelectorDialog
        from .events import EVT_UPDATE_SETTING  # local import to avoid cycle
        self.Bind(EVT_UPDATE_SETTING, self._on_update_setting)
        self.settings_btn.Bind(wx.EVT_BUTTON, self._open_settings)

        # Initialize logging to forward to the bottom console
        self._init_logger()
        # Log resolved project context for clarity
        try:
            self._append_log(
                LogboxAppendEvent(
                    msg=(
                        f"Проєкт: {self.project_path}\n"
                        f"Плата: {self.board_name}\n"
                        f"Схема: {self.schematic_name}\n"
                    )
                )
            )
        except Exception:
            pass

        # On first window launch, verify deps and offer installation if missing
        self._check_and_offer_install_deps()
        # Ensure UI matches current deps state
        self._update_select_enabled()

    def _resolve_python_exe(self) -> str:
        try:
            exe = sys.executable or ""
            name = os.path.basename(exe).lower()
            if exe and ("python" in name):
                return exe
            major, minor = sys.version_info.major, sys.version_info.minor
            candidates = [
                Path(sys.exec_prefix) / "bin" / f"python{major}.{minor}",
                Path(sys.exec_prefix) / "bin" / "python3",
                Path(sys.exec_prefix) / "bin" / "python",
            ]
            if sys.platform.startswith("win"):
                candidates.extend([Path(sys.exec_prefix) / "python.exe"])
            import shutil as _sh
            for c in [*candidates, _sh.which("python3"), _sh.which("python")]:
                if not c:
                    continue
                p = str(c)
                if os.path.exists(p) and os.access(p, os.X_OK):
                    return p
        except Exception:
            pass
        return sys.executable or "python3"

    def _run_and_stream(self, cmd, env=None) -> int:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            if proc.stdout is not None:
                for line in proc.stdout:
                    wx.PostEvent(self, LogboxAppendEvent(msg=line))
            return proc.wait()
        except Exception as e:
            wx.PostEvent(self, LogboxAppendEvent(msg=f"Помилка виконання: {e}\n"))
            return 1

    # Override: do not assign in this simplified window — show placeholder
    def select_part(self, *_):  # noqa: N802 (KiCad naming)
        if not getattr(self, "_deps_ready", False):
            # Re-offer installation when user attempts to select
            self._check_and_offer_install_deps(force_prompt=True)
            return
        try:
            if getattr(self, "part_list", None) is None:
                wx.PostEvent(
                    self,
                    MessageEvent(title="Помилка", text="Список компонентів недоступний.", style="error"),
                )
                return
            if self.part_list.GetSelectedItemsCount() <= 0:
                wx.PostEvent(
                    self,
                    MessageEvent(title="Немає вибору", text="Оберіть елемент у списку.", style="warning"),
                )
                return
            item = self.part_list.GetSelection()
            lcsc_id = str(self.part_list_model.get_lcsc(item)).strip()
            # Also capture metadata for symbol augmentation
            try:
                category = str(self.part_list_model.get_category(item)).strip()
            except Exception:
                category = ""
            try:
                mfr_part = str(self.part_list_model.get_mfr_number(item)).strip()
            except Exception:
                mfr_part = ""
            try:
                manufacturer = str(self.part_list_model.get_manufacturer(item)).strip()
            except Exception:
                manufacturer = ""
            try:
                descr = str(self.part_list_model.get_description(item)).strip()
            except Exception:
                descr = ""
            try:
                attributes_json = str(self.part_list_model.get_attributes(item)).strip()
            except Exception:
                attributes_json = ""
            if not lcsc_id:
                wx.PostEvent(
                    self,
                    MessageEvent(title="Немає LCSC", text="У вибраному рядку немає LCSC ID.", style="warning"),
                )
                return
        except Exception:
            wx.PostEvent(
                self,
                MessageEvent(title="Помилка", text="Не вдалося отримати LCSC ID.", style="error"),
            )
            return

        meta = {
            "mfr_part": mfr_part,
            "manufacturer": manufacturer,
            "description": descr,
            "attributes_json": attributes_json,
        }
        self._import_part_via_easyeda(lcsc_id, category, meta)

    def _import_part_via_easyeda(self, lcsc_id: str, category: str = "", meta: Optional[Dict] = None):
        base = Path(__file__).resolve().parent
        lib_dir = base / "lib"
        # Determine storage scope from settings, ask if missing
        scope = self._ensure_library_scope_selected()
        if not scope:
            # User canceled selection
            wx.PostEvent(
                self,
                LogboxAppendEvent(msg="Імпорт скасовано: не обрано місце збереження бібліотек.\n"),
            )
            return
        is_system = str(scope).lower() == "system"
        
        # Resolve category folder name
        cat_name = category or "Misc"
        cat_dir = self._sanitize_name(cat_name)

        if is_system:
            # Use KiCad 3rd-party if provided, otherwise fallback to plugin path
            third_party = os.environ.get("KICAD9_3RD_PARTY")
            if third_party and isinstance(third_party, str) and third_party.strip():
                base_out = Path(third_party)
            else:
                base_out = Path(PLUGIN_PATH) / "libraries"
                self._append_log(
                    LogboxAppendEvent(
                        msg=(
                            "KICAD9_3RD_PARTY не задано. Використовую теку плагіна як системну базу.\n"
                        )
                    )
                )
            plugin_folder_name = Path(PLUGIN_PATH).resolve().name
            
            sym_out = base_out / "symbols" / plugin_folder_name / f"LCSC_{cat_dir}"
            fp_out = base_out / "footprints" / plugin_folder_name / f"LCSC_{cat_dir}"
            m3d_out = base_out / "3dmodels" / plugin_folder_name / f"LCSC_{cat_dir}"
            out_dir = base_out / plugin_folder_name  # logical group for message
            for folder in ["symbols", "footprints", "3dmodels"]:
                (base_out / folder / plugin_folder_name).mkdir(parents=True, exist_ok=True)
            
            self._append_log(
                LogboxAppendEvent(
                    msg=(
                        "Режим зберігання: system (спільна тека)\n"
                        f"  Symbols → {sym_out}\n  Footprints → {fp_out}\n  3D → {m3d_out}\n"
                    )
                )
            )
        else:
            out_dir = m3d_out = fp_out = sym_out = Path(self.project_path) / "library" / f"LCSC_{cat_dir}"
            self._append_log(
                LogboxAppendEvent(
                    msg=f"Режим зберігання: project (всередині проєкту) → {out_dir}\n"
                )
            )
        py_exe = self._resolve_python_exe()
        self._append_log(LogboxAppendEvent(msg=f"Імпорт через easyeda2kicad для {lcsc_id}\n"))
        self._append_log(LogboxAppendEvent(msg=f"Інтерпретатор: {py_exe}\n"))

        # Build commands
        commands = [
            [
                py_exe,
                "-m",
                "easyeda2kicad",
                "--symbol",
                "--overwrite",
                f"--output={sym_out}",
                f"--lcsc_id={lcsc_id}",
            ],
            [
                py_exe,
                "-m",
                "easyeda2kicad",
                "--3d",
                "--overwrite",
                f"--output={m3d_out}",
                f"--lcsc_id={lcsc_id}",
            ],
            [
                py_exe,
                "-m",
                "easyeda2kicad",
                "--footprint",
                "--overwrite",
                f"--output={fp_out}",
                f"--lcsc_id={lcsc_id}",
            ],
        ]

        env = os.environ.copy()
        env["PYTHONPATH"] = (
            str(lib_dir)
            + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
        )

        btn = getattr(self, "select_part_button", None)
        if btn is not None:
            btn.Enable(False)

        def _worker():
            wx.BeginBusyCursor()
            try:
                ret = 0
                for c in commands:
                    self._append_log(LogboxAppendEvent(msg=f"Запуск: {' '.join(str(x) for x in c)}\n"))
                    ret = self._run_and_stream(c, env=env)
                    if ret != 0:
                        break
            finally:
                try:
                    wx.EndBusyCursor()
                except Exception:
                    pass
                
            if btn is not None:
                wx.CallAfter(btn.Enable, True)
            
            if ret == 0:
                # Ensure library tables (project) or patch symbol references (system)
                try:
                    if is_system:
                        # KiCad auto-adds 3rdparty libraries with a nickname prefix (default 'PCM_').
                        # Patch Footprint references inside symbol files to include that prefix.
                        prefix = self._resolve_nickname_prefix()
                        try:
                            patched = self._patch_symbol_footprint_prefix(sym_out, cat_dir, prefix)
                            wx.PostEvent(
                                self,
                                LogboxAppendEvent(
                                    msg=f"Оновлено Footprint посилань у символах (+{patched}). Префікс: {prefix}\n",
                                ),
                            )
                        except Exception as e:
                            wx.PostEvent(self, LogboxAppendEvent(msg=f"Не вдалося оновити префікс у символах: {e}\n"))

                        # Augment symbol with metadata (Manufacturer, MFR Part, Description, Value)
                        try:
                            self._augment_symbol_metadata(sym_out.with_suffix(".kicad_sym"), category, meta or {})
                        except Exception as e:
                            wx.PostEvent(self, LogboxAppendEvent(msg=f"Не вдалося оновити атрибути символу: {e}\n"))
                        
                        lib_base = out_dir
                        
                        # Clean up any cross-generated artifacts in wrong folders, safely.
                        # Only remove if paths differ and targets exist.
                        # For symbols folder, drop any stray 3dshapes/pretty
                        if sym_out != m3d_out:
                            self._safe_remove(Path(f"{m3d_out}.kicad_sym"))
                            self._safe_remove(Path(f"{sym_out}.3dshapes"))
                        
                        if sym_out != fp_out:
                            self._safe_remove(Path(f"{sym_out}.pretty"))
                            self._safe_remove(Path(f"{fp_out}.kicad_sym"))
                        
                        if fp_out != m3d_out:
                            self._safe_remove(Path(f"{fp_out}.3dshapes"))
                            self._safe_remove(Path(f"{m3d_out}.pretty"))

                    else:
                        _sym_found, _fp_found, lib_base = self._ensure_project_lib_tables(
                            out_dir, use_project_relative=True
                        )
                        try:
                            self._augment_symbol_metadata(sym_out.with_suffix(".kicad_sym"), category, meta or {})
                        except Exception as e:
                            wx.PostEvent(self, LogboxAppendEvent(msg=f"Не вдалося оновити атрибути символу: {e}\n"))
                except Exception as e:
                    wx.PostEvent(self, LogboxAppendEvent(msg=f"Не вдалося оновити бібліотеки/посилання: {e}\n"))
                    lib_base = out_dir

                # Normalize absolute 3D model paths inside project to ${KIPRJMOD}
                if not is_system:
                    try:
                        changes = self._relativize_3d_model_paths(Path(lib_base))
                        wx.PostEvent(
                            self,
                            LogboxAppendEvent(
                                msg=f"Виправлено шляхів до 3D моделей: {changes}\n",
                            ),
                        )
                    except Exception as e:
                        wx.PostEvent(self, LogboxAppendEvent(msg=f"Не вдалося оновити 3D-шляхи: {e}\n"))

                wx.PostEvent(
                    self,
                    LogboxAppendEvent(
                        msg=f"Імпорт завершено. Файли у: {lib_base}\n",
                    ),
                )
                wx.CallAfter(
                    wx.MessageBox,
                    f"Імпортовано {lcsc_id} у проєкт.\nПапка: {lib_base}",
                    "Готово",
                    wx.ICON_INFORMATION,
                )
            else:
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(msg="Імпорт завершився з помилкою.\n"),
                )
                wx.CallAfter(
                    wx.MessageBox,
                    "Не вдалося імпортувати компонент. Перевірте лог вище.",
                    "Помилка",
                    wx.ICON_ERROR,
                )
        threading.Thread(target=_worker, daemon=True).start()

    def _ensure_library_scope_selected(self) -> Optional[str]:
        """Return existing library scope or ask the user to choose.

        Returns "project" or "system". Returns None if the user cancels.
        """
        try:
            scope = None
            if isinstance(self.settings, dict):
                scope = (self.settings.get("general", {}) or {}).get("library_scope")
            if scope in ("project", "system"):
                return scope

            # Ask user
            choice = self._ask_library_scope_dialog()
            if choice in ("project", "system"):
                if "general" not in self.settings:
                    self.settings["general"] = {}
                self.settings["general"]["library_scope"] = choice
                self._save_settings()
                return choice
            return None
        except Exception:
            return None

    def _ask_library_scope_dialog(self) -> Optional[str]:
        dlg = wx.Dialog(
            self,
            title="Де зберігати бібліотеки?",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=HighResWxSize(self, wx.Size(420, 180)),
        )
        try:
            vbox = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(
                dlg,
                label=(
                    "Де зберігати та яку бібліотеку використовувати для збереження?\n"
                    "Виберіть місце для символів і футпрінтів."
                ),
            )
            vbox.Add(text, 0, wx.ALL | wx.EXPAND, 10)

            hbox = wx.BoxSizer(wx.HORIZONTAL)
            btn_project = wx.Button(dlg, wx.ID_ANY, "На рівні проекту")
            btn_system = wx.Button(dlg, wx.ID_ANY, "На рівні системи")
            hbox.Add(btn_project, 1, wx.ALL | wx.EXPAND, 5)
            hbox.Add(btn_system, 1, wx.ALL | wx.EXPAND, 5)
            vbox.Add(hbox, 0, wx.ALL | wx.EXPAND, 5)

            dlg.SetSizer(vbox)
            dlg.Layout()

            result: dict[str, str | None] = {"choice": None}

            def _choose_project(_evt):
                result["choice"] = "project"
                dlg.EndModal(wx.ID_OK)

            def _choose_system(_evt):
                result["choice"] = "system"
                dlg.EndModal(wx.ID_OK)

            btn_project.Bind(wx.EVT_BUTTON, _choose_project)
            btn_system.Bind(wx.EVT_BUTTON, _choose_system)

            dlg.CentreOnParent()
            dlg.ShowModal()
            return result["choice"]
        finally:
            try:
                dlg.Destroy()
            except Exception:
                pass

    def _ensure_project_lib_tables(self, out_dir: Path, use_project_relative: bool = True):
        project_dir = Path(self.project_path)
        sym_tbl = project_dir / "sym-lib-table"
        fp_tbl = project_dir / "fp-lib-table"

        lib_dir = out_dir
        # Discover libs produced by easyeda2kicad
        sym_files = sorted(lib_dir.rglob("*.kicad_sym"))
        pretty_dirs = sorted(p for p in lib_dir.rglob("*.pretty") if p.is_dir())
        lib_base = lib_dir

        # Early visibility in log about what we found
        wx.PostEvent(
            self,
            LogboxAppendEvent(
                msg=(
                    f"Оновлення таблиць бібліотек у: {project_dir}\n"
                    f"Знайдено символів: {len(sym_files)}; footprint libs: {len(pretty_dirs)}\n"
                )
            ),
        )
        # Helpers
        def _read(path: Path) -> str:
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                return ""

        def _write(path: Path, content: str):
            path.write_text(content, encoding="utf-8")

        def _init_table(kind: str) -> str:
            if kind == "sym":
                return "(sym_lib_table\r\n  (version 7))\r\n"
            else:
                return "(fp_lib_table\r\n  (version 7))\r\n"

        def _ensure_entry(tbl_path: Path, tbl_kind: str, name: str, uri: str):
            entry = f"  (lib (name \"{name}\")(type \"KiCad\")(uri \"{uri}\")(options \"\")(descr \"\"))\n"
            content = _read(tbl_path)
            if not content:
                content = _init_table(tbl_kind)
            # Skip if already present (by uri or name)
            if f"(uri \"{uri}\")" in content or f"(name \"{name}\")" in content:
                return False
            stripped = content.rstrip()
            # Find final closing paren and insert before it
            idx = stripped.rfind(")")
            if idx == -1:
                # Corrupt file, rewrite minimal table
                stripped = _init_table(tbl_kind).rstrip()
                idx = stripped.rfind(")")
            new_content = stripped[:idx] + entry + stripped[idx:] + "\n"
            _write(tbl_path, new_content)
            return True

        # Load existing names to avoid collisions
        sym_content = _read(sym_tbl)
        fp_content = _read(fp_tbl)

        def _unique_name(base: str, existing: str) -> str:
            # Ensure name doesn't collide within table content
            if existing and f"(name \"{base}\")" in existing:
                i = 1
                while f"(name \"{base}_{i}\")" in existing:
                    i += 1
                return f"{base}_{i}"
            return base

        changes = 0

        # Add symbol libraries
        for sym in sym_files:
            if use_project_relative:
                try:
                    rel = sym.resolve().relative_to(project_dir.resolve()).as_posix()
                except Exception:
                    rel = sym.resolve().as_posix()
                uri = f"${{KIPRJMOD}}/{rel}"
            else:
                uri = sym.resolve().as_posix()
            name = _unique_name(sym.stem, sym_content)
            if _ensure_entry(sym_tbl, "sym", name, uri):
                wx.PostEvent(self, LogboxAppendEvent(msg=f"Додано symbol lib: {name} -> {uri}\n"))
                sym_content = _read(sym_tbl)
                changes += 1

        # Add footprint libraries
        for pd in pretty_dirs:
            if use_project_relative:
                try:
                    rel = pd.resolve().relative_to(project_dir.resolve()).as_posix()
                except Exception:
                    rel = pd.resolve().as_posix()
                uri = f"${{KIPRJMOD}}/{rel}"
            else:
                uri = pd.resolve().as_posix()
            name = _unique_name(pd.stem, fp_content)
            if _ensure_entry(fp_tbl, "fp", name, uri):
                wx.PostEvent(self, LogboxAppendEvent(msg=f"Додано footprint lib: {name} -> {uri}\n"))
                fp_content = _read(fp_tbl)
                changes += 1

        if changes == 0:
            # Clarify when nothing changed so the user is not confused
            if not sym_files and not pretty_dirs:
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(
                        msg=(
                            "Не знайдено нових бібліотек для додавання. "
                            "Переконайтеся, що easyeda2kicad згенерував *.kicad_sym та *.pretty у вказаній теці.\n"
                        )
                    ),
                )
            else:
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(
                        msg=(
                            "Бібліотеки вже присутні у sym-lib-table / fp-lib-table. Змін не потрібно.\n"
                        )
                    ),
                )

        return sym_files, pretty_dirs, lib_base

    def _relativize_3d_model_paths(self, search_root: Path) -> int:
        """Scan `.kicad_mod`/`.mod` under search_root and rewrite absolute 3D model paths
        that point inside the project directory to `${KIPRJMOD}`-relative paths.

        Returns the number of replacements made.
        """
        project_dir = Path(self.project_path).resolve()
        try:
            files = list(search_root.rglob("*.kicad_mod")) + list(search_root.rglob("*.mod"))
        except Exception:
            files = []
        if not files:
            return 0

        # match: (model "...path...")
        pattern = re.compile(r"\(model\s+\"([^\"]+)\"")

        def is_abs(p: str) -> bool:
            if p.startswith("/"):
                return True
            if re.match(r"^[A-Za-z]:[\\/]", p):
                return True
            if p.startswith("\\\\"):
                return True
            return False

        total = 0
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue

            changed_any = False
            changes_here = 0

            def repl(m) -> str:
                nonlocal changed_any, changes_here
                original = m.group(1)
                if "${" in original:
                    return m.group(0)
                norm = original.replace("\\", "/")
                if not is_abs(norm):
                    return m.group(0)
                try:
                    rel = Path(norm).resolve().relative_to(project_dir)
                except Exception:
                    return m.group(0)
                newp = f"${{KIPRJMOD}}/{rel.as_posix()}"
                if newp != original:
                    changed_any = True
                    changes_here += 1
                    return m.group(0).replace(f'"{original}"', f'"{newp}"', 1)
                return m.group(0)

            new_text = pattern.sub(repl, text)
            if changed_any and new_text != text:
                try:
                    f.write_text(new_text, encoding="utf-8")
                    total += changes_here
                except Exception:
                    pass

        return total

    def _resolve_nickname_prefix(self) -> str:
        """Resolve KiCad 3rd-party library nickname prefix.
        """
        # 1) KiCad settings file
        try:
            base = None
            try:
                base = self.pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
            except Exception:
                base = None
            if base:
                settings_path = Path(base) / "kicad.json"
                if settings_path.exists():
                    try:
                        data = json.loads(settings_path.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            pcm = data.get("pcm", {}) or {}
                            prefix = pcm.get("lib_prefix")
                            if isinstance(prefix, str) and prefix.strip():
                                return prefix.strip()
                    except Exception:
                        pass
        except Exception:
            pass
        # 3) Default
        return "PCM_"

    def _patch_symbol_footprint_prefix(self, sym_out: Path, cat_dir: str, prefix: str) -> int:
        # TODO: Потрібно винести редагування в окремий класс
        # та розширити можливість редагування параметрів символу
        # В першу чергу потрібно додати такі атрибути як
        # Manufacturer, Manufacturer Part, Description
        # Також зараз у "Value" записується Maufacturer Part.
        # Але для простих компонентів потрібно виділяти їх основний параметр та записуваіи його в Value.
        # Для резистрорів це опір, для конденсаторів ємність і для котушок індуктивність, тд. 
        # Я додав поле Attributes яке являє собою JSON з атрибутами з LCSC
        # Ось приклад поля для резистора ( категорія "Resistors" ): {"Resistance": "1.3k\u03a9", "Power(Watts)": "250mW", "Type": "Thick Film Resistors", "Overload Voltage (Max)": "200V", "Operating Temperature Range": "-55\u2103~+155\u2103", "Tolerance": "\u00b15%", "Temperature Coefficient": "\u00b1100ppm/\u2103"}
        # Ось приклад поля для конденсатора ( категорія "Capacitors" ): {"Voltage Rated": "50V", "Tolerance": "\u00b110%", "Capacitance": "150pF", "Temperature Coefficient": "X7R"}
        # Ось приклад поля для бусинок (Категорія: "Filters/EMI Optimization", Підкатегорія: "Ferrite Beads"): {"DC Resistance": "200m\u03a9", "Impedance @ Frequency": "120\u03a9@100MHz", "Circuits": "1", "Current Rating": "300mA", "Tolerance": "\u00b125%"}
        # Ось приклад поля для індуктивностей (Категорії що мають в назві: "inductor" або "coil", Підкатегорії що мають назви: "inductor" або "Inductor"): {"Inductance": "10uH", "Tolerance": "\u00b120%", "Saturation Current (Isat)": "1.6A", "Rated Current": "1.44A", "DC Resistance (DCR)": "100m\u03a9"}
        # Потрібно додати ці параметри в параметри символу
        # Але при умові, що така назва атрибуту ще не визначена
        # Врахуй, що в kicad_sym файлі зберігається багато символів
           
        
        """Patch Footprint property inside .kicad_sym files to include nickname prefix.

        Works with both single-line and multi-line property forms, e.g.:
          (property "Footprint" "LCSC_X:Y" ...)
          (property\n  "Footprint"\n  "LCSC_X:Y"\n  ...)

        Adds the prefix only when the value starts with 'LCSC_{cat_dir}:' and is not
        already prefixed with '{prefix}'. Returns the number of updated occurrences.
        """
        sym_path = sym_out.with_suffix(".kicad_sym")
        text = _read_text(sym_path)
        if not text:
            return 0

        # Regex tolerant to newlines/whitespace; captures Footprint value
        pattern = re.compile(
            r"\(property\s*(?:\n\s*)?\"Footprint\"\s*(?:\n\s*)?\"([^\"]+)\"",
            re.MULTILINE,
        )

        updates = 0
        def repl(m) -> str:
            nonlocal updates
            val = m.group(1)
            # Already prefixed; skip
            if val.startswith(f"{prefix}LCSC_"):
                return m.group(0)
            # Match our category and add prefix
            if val.startswith(f"LCSC_{cat_dir}:"):
                updates += 1
                return m.group(0).replace(f'"{val}"', f'"{prefix}{val}"', 1)
            return m.group(0)
        

        new_text, _ = pattern.subn(repl, text)
        if new_text != text:
            _write_text(sym_path, new_text)

        return updates

    def _augment_symbol_metadata(self, sym_path: Path, category: str, meta: Dict) -> int:
        """Augment symbol(s) in sym_path with metadata and smarter Value.

        - Adds properties (if missing): Manufacturer, Manufacturer Part, Description
        - Sets Value to primary param for simple passives (resistor/capacitor/inductor/bead)

        Returns number of changes applied across all symbols in file.
        """
        text = _read_text(sym_path)
        if not text:
            return 0

        def find_blocks(src: str) -> List[Tuple[int, int]]:
            blocks = []
            idx = 0
            while True:
                start = src.find("(symbol ", idx)
                if start == -1:
                    break
                # Walk parentheses
                depth = 0
                i = start
                end = -1
                while i < len(src):
                    c = src[i]
                    if c == '(':
                        depth += 1
                    elif c == ')':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                    i += 1
                if end != -1:
                    blocks.append((start, end))
                idx = end if end != -1 else start + 7
            return blocks

        def _prop_exists(block: str, name: str) -> bool:
            return re.search(rf"\(property\s*\"{re.escape(name)}\"\s*\"", block) is not None

        def _extract_ids(block: str) -> int:
            ids = [int(m.group(1)) for m in re.finditer(r"\(id\s+(\d+)\)", block)]
            return max(ids) + 1 if ids else 1

        def _extract_template(block: str) -> Tuple[str, str, str]:
            # Try Footprint first, then Value
            m = re.search(r"\(property\s*\"Footprint\"[\s\S]*?\)", block)
            if not m:
                m = re.search(r"\(property\s*\"Value\"[\s\S]*?\)", block)
            prop = m.group(0) if m else "(property \"X\" \"Y\")"
            # Indentation
            line = block.splitlines()
            indent = "  "
            for ln in line:
                if ln.strip().startswith("(property "):
                    indent = ln[: len(ln) - len(ln.lstrip())]
                    break
            at = re.search(r"\(at[^\)]*\)", prop)
            effects = re.search(r"\(effects[\s\S]*?\)", prop)
            return indent, (at.group(0) if at else ""), (effects.group(0) if effects else "")

        def _ensure_hide_effects(effects: str) -> str:
            # Ensure we have an effects block with hide yes
            if not effects or "(effects" not in effects:
                return "(effects (font (size 1.27 1.27)) (hide yes))"
            if "hide" in effects:
                return effects
            # Insert (hide yes) before the last closing parenthesis of effects block
            idx = effects.rfind(")")
            if idx == -1:
                return effects + " (hide yes)"
            return effects[:idx] + " (hide yes)" + effects[idx:]

        def _insert_prop(block: str, name: str, value: str) -> tuple[str, bool]:
            if _prop_exists(block, name):
                return block, False
            indent, at, effects = _extract_template(block)
            safe_val = value.replace('"', "'")
            # Build property line; include at/effects when available
            eff = _ensure_hide_effects(effects)
            # Do NOT include (id ...) in generated properties
            extra = (f" {at}" if at else "") + (f" {eff}" if eff else "")
            new_line = f"{indent}(property \"{name}\" \"{safe_val}\"{extra})\n"
            # Insert before last ')' of symbol block
            insert_at = block.rfind(')')
            if insert_at == -1:
                return block, False
            new_block = block[:insert_at] + new_line + block[insert_at:]
            return new_block, True

        def _get_prop_value(block: str, name: str) -> Optional[str]:
            m = re.search(rf"\(property\s*\"{re.escape(name)}\"\s*\"([^\"]*)\"", block)
            return m.group(1) if m else None

        def _set_prop_value(block: str, name: str, new_value: str) -> tuple[str, bool]:
            m = re.search(rf"\(property\s*\"{re.escape(name)}\"\s*\"([^\"]*)\"", block)
            if not m:
                return block, False
            old_val = m.group(1)
            if old_val == new_value:
                return block, False
            safe_val = new_value.replace('"', "'")
            new_block = re.sub(
                rf"(\(property\s*\"{re.escape(name)}\"\s*\")([^\"]*)(\")",
                lambda mm: mm.group(1) + safe_val + mm.group(3),
                block,
                count=1,
            )
            return new_block, True

        def _primary_value_for(category: str, attrs_json: Optional[str]) -> Optional[str]:
            try:
                attrs = json.loads(attrs_json) if attrs_json else {}
            except Exception:
                attrs = {}
            cat = (category or "").lower()
            # Heuristics per category
            if "resistor" in cat:
                return attrs.get("Resistance")
            if "capacitor" in cat:
                return attrs.get("Capacitance")
            if "inductor" in cat or "coil" in cat:
                return attrs.get("Inductance")
            if "ferrite" in cat or "bead" in cat:
                return attrs.get("Impedance @ Frequency") or attrs.get("Impedance")
            # Fallback: none
            return None

        blocks = find_blocks(text)
        if not blocks:
            return 0
        changes = 0
        new_text_parts = []
        last_idx = 0
        for (bstart, bend) in blocks:
            new_text_parts.append(text[last_idx:bstart])
            block = text[bstart:bend]
            # Add/Update properties: Manufacturer, Manufacturer Part, Description
            mfr = (meta.get("manufacturer", "") if meta else "").strip()
            mfr_part = (meta.get("mfr_part", "") if meta else "").strip()
            descr = (meta.get("description", "") if meta else "").strip()
            for name, val in (("Manufacturer", mfr), ("Manufacturer Part", mfr_part), ("Description", descr)):
                if not val:
                    continue
                if _prop_exists(block, name):
                    current = _get_prop_value(block, name) or ""
                    if current.strip() == "":
                        block, modified = _set_prop_value(block, name, val)
                        if modified:
                            changes += 1
                else:
                    block, added = _insert_prop(block, name, val)
                    if added:
                        changes += 1

            attrs_json = (meta.get("attributes_json") if meta else None) or ""
            parsed_attrs = {}
            if attrs_json:
                try:
                    parsed_attrs = json.loads(attrs_json)
                    if not isinstance(parsed_attrs, dict):
                        parsed_attrs = {}
                except Exception:
                    parsed_attrs = {}
            if parsed_attrs:
                # Exclude attributes that equal the symbol Value to avoid duplication
                current_value_str = (_get_prop_value(block, "Value") or "").strip()
                for k, v in parsed_attrs.items():
                    if v is None:
                        continue
                    base_name = str(k).strip()
                    pref_name = f"JLCPCB:{base_name}"
                    sval = str(v)
                    if sval.strip() == current_value_str:
                        # Skip attributes that duplicate the Value text
                        continue
                    # Prefer non-prefixed name if it doesn't exist; otherwise use existing
                    if _prop_exists(block, base_name):
                        current = _get_prop_value(block, base_name) or ""
                        if current.strip() == "":
                            block, modified = _set_prop_value(block, base_name, sval)
                            if modified:
                                changes += 1
                    elif _prop_exists(block, pref_name):
                        current = _get_prop_value(block, pref_name) or ""
                        if current.strip() == "":
                            block, modified = _set_prop_value(block, pref_name, sval)
                            if modified:
                                changes += 1
                    else:
                        # Unique property: add without JLCPCB prefix
                        block, added = _insert_prop(block, base_name, sval)
                        if added:
                            changes += 1

            # Smarter Value based on Attributes JSON (prefer meta) and category
            attrs_val = attrs_json if attrs_json else None
            primary = _primary_value_for(category, attrs_val)
            if primary:
                current_value = _get_prop_value(block, "Value") or ""
                # Update when current equals MFR part or is empty
                if (meta and current_value.strip() == mfr_part) or current_value.strip() == "":
                    block, modified = _set_prop_value(block, "Value", primary)
                    if modified:
                        changes += 1

            new_text_parts.append(block)
            last_idx = bend
        new_text_parts.append(text[last_idx:])
        new_text = "".join(new_text_parts)
        # Strip any (id N) tokens from properties across the file
        try:
            new_text = re.sub(r"\(id\s+\d+\)", "", new_text)
        except Exception:
            pass
        if new_text != text:
            _write_text(sym_path, new_text)
        return changes

    # --- safe filesystem utilities ---
    @staticmethod
    def _safe_remove(path: Path) -> int:
        # Remove 
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
            return 1
        if path.exists() and path.is_file():
            path.unlink()
            return 1
        return 0

    @staticmethod
    def _safe_unlink(path: Path) -> int:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                return 1
        except Exception:
            pass
        return 0

    def _detect_project_context(self):
        """Detect project root and names robustly.

        Priority:
        1) KIPRJMOD env (KiCad project dir)
        2) Directory of the open board
        3) Current working directory
        """
        # 1) Use KIPRJMOD when available
        kiprjmod = os.environ.get("KIPRJMOD")
        board = None
        try:
            board = self.pcbnew.GetBoard()
        except Exception:
            board = None

        if kiprjmod and os.path.isdir(kiprjmod):
            project_dir = kiprjmod
            board_name = os.path.split(board.GetFileName())[1] if board else "board.kicad_pcb"
        else:
            # 2) Fall back to board directory
            if board:
                board_path = board.GetFileName()
                project_dir = os.path.split(board_path)[0]
                board_name = os.path.split(board_path)[1]
            else:
                # 3) CWD as last resort
                project_dir = os.getcwd()
                board_name = "board.kicad_pcb"

        schematic_name = f"{board_name.split('.')[0]}.kicad_sch"
        return project_dir, board_name, schematic_name

    # Settings persistence
    def _load_settings(self):
        try:
            with open(os.path.join(PLUGIN_PATH, "settings.json"), encoding="utf-8") as j:
                self.settings = json.load(j)
        except Exception:
            self.settings = {}

    def _save_settings(self):
        try:
            with open(os.path.join(PLUGIN_PATH, "settings.json"), "w", encoding="utf-8") as j:
                json.dump(self.settings, j)
        except Exception:
            pass

    # Called when PartSelectorDialog posts UpdateSetting
    def _on_update_setting(self, e):
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value
        self._save_settings()

    # Expose update for button
    def update_library(self):
        self.library.update()

    # Local handlers
    def _append_log(self, e):
        if hasattr(self, "console") and self.console:
            # AppendText works reliably with read-only TextCtrl
            self.console.AppendText(e.msg)

    def _show_message(self, e):
        styles = {"info": wx.ICON_INFORMATION, "warning": wx.ICON_WARNING, "error": wx.ICON_ERROR}
        wx.MessageBox(e.text, e.title, style=styles.get(e.style, wx.ICON_INFORMATION))

    def _clear_log(self, *_):
        if hasattr(self, "console") and self.console:
            self.console.Clear()

    def _open_settings(self, *_):
        dlg = None
        try:
            dlg = SettingsDialog(self)
            dlg.ShowModal()
        finally:
            try:
                if dlg is not None:
                    dlg.Destroy()
            except Exception:
                pass

    # Progress handlers
    def _on_progress_reset(self, *_):
        if hasattr(self, "gauge") and self.gauge:
            self.gauge.SetRange(100)
            self.gauge.SetValue(0)

    def _on_progress_update(self, e):
        if hasattr(self, "gauge") and self.gauge:
            try:
                val = int(e.value)
            except Exception:
                val = 0
            self.gauge.SetValue(max(0, min(100, val)))

    # Logging setup similar to legacy mainwindow to capture logs in UI
    def _init_logger(self):
        root = logging.getLogger()
        # Avoid stale/duplicate handlers
        try:
            root.handlers.clear()
        except Exception:
            # Fallback for older Python where handlers is read-only
            for h in list(root.handlers):
                root.removeHandler(h)
        root.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(funcName)s -  %(message)s",
            datefmt="%Y.%m.%d %H:%M:%S",
        )

        # Keep stderr quiet to avoid external popups; only emit severe errors
        if sys.stderr is not None:
            self._stderr_handler = logging.StreamHandler(sys.stderr)
            self._stderr_handler.setLevel(logging.ERROR)
            self._stderr_handler.setFormatter(formatter)
            root.addHandler(self._stderr_handler)

        self._ui_log_handler = _LogBoxHandler(self)
        self._ui_log_handler.setLevel(logging.DEBUG)
        self._ui_log_handler.setFormatter(formatter)
        root.addHandler(self._ui_log_handler)

    # Dependency check and interactive installer
    def _check_and_offer_install_deps(self, force_prompt: bool = False):
        try:
            __import__("easyeda2kicad")
            self._deps_ready = True
            self._update_select_enabled()
            return
        except Exception:
            pass

        msg = (
            "Бібліотека 'easyeda2kicad' не знайдена.\n"
            "Встановити залежності зараз? (буде використано локальну теку 'lib/')"
        )
        if force_prompt or not self._deps_ready:
            dlg = wx.MessageDialog(
                self,
                message=msg,
                caption="Встановити залежності",
                style=wx.YES_NO | wx.ICON_QUESTION,
            )
            res = dlg.ShowModal()
            dlg.Destroy()
            if res == wx.ID_YES:
                # While installing, keep selection disabled
                self._deps_ready = False
                self._update_select_enabled()
                self._install_requirements()
            else:
                # User declined: lock selection until installed
                self._deps_ready = False
                self._update_select_enabled()

    def _install_requirements(self):
        base = Path(__file__).resolve().parent
        req = base / "requirements.txt"
        lib_dir = base / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)

        if not req.exists():
            self._append_log(LogboxAppendEvent(msg="requirements.txt не знайдено.\n"))
            wx.MessageBox("requirements.txt не знайдено", "Помилка", style=wx.ICON_ERROR)
            return

        # Resolve a real Python interpreter (KiCad on macOS sets sys.executable to the app binary)
        py_exe = self._resolve_python_exe()
        cmd = [
            py_exe,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "-r",
            str(req),
            "--target",
            str(lib_dir),
        ]
        self._append_log(LogboxAppendEvent(msg=f"Інтерпретатор: {py_exe}\n"))
        self._append_log(LogboxAppendEvent(msg=f"Запуск: {' '.join(cmd)}\n"))
        wx.BeginBusyCursor()

        def _worker():
            try:
                wx.PostEvent(self, LogboxAppendEvent(msg=f"Виконую: {' '.join(cmd)}\n"))
                ret = self._run_and_stream(cmd)
            except Exception as e:  # process spawn/read failure
                wx.PostEvent(self, LogboxAppendEvent(msg=f"Помилка встановлення: {e}\n"))
                wx.CallAfter(
                    wx.MessageBox,
                    "Не вдалося запустити встановлення залежностей.",
                    "Помилка",
                    wx.ICON_ERROR,
                )
                ret = 1
            finally:
                if wx.IsBusy():
                    wx.CallAfter(wx.EndBusyCursor)

            if ret != 0:
                # Try to bootstrap pip if it was missing
                try:
                    wx.PostEvent(self, LogboxAppendEvent(msg="Спроба встановити pip через ensurepip...\n"))
                    ensure_cmd = [cmd[0], "-m", "ensurepip", "--upgrade"]
                    ret2 = self._run_and_stream(ensure_cmd)
                    if ret2 == 0:
                        wx.PostEvent(self, LogboxAppendEvent(msg="pip встановлено. Повторюю встановлення залежностей...\n"))
                        ret = self._run_and_stream(cmd)
                except Exception:
                    pass
                if ret != 0:
                    wx.PostEvent(
                        self,
                        LogboxAppendEvent(msg="pip завершився з помилкою.\n"),
                    )
                    wx.CallAfter(
                        wx.MessageBox,
                        "Не вдалося встановити залежності. Перевірте мережу/доступ до pip.",
                        "Помилка",
                        wx.ICON_ERROR,
                    )
                    return

            try:
                if str(lib_dir) not in sys.path:
                    sys.path.append(str(lib_dir))
                __import__("easyeda2kicad")
                self._deps_ready = True
                wx.PostEvent(self, LogboxAppendEvent(msg="Залежності встановлено успішно.\n"))
                wx.CallAfter(self._update_select_enabled)
                wx.CallAfter(wx.MessageBox, "Залежності встановлено успішно.", "Готово", wx.ICON_INFORMATION)
            except Exception:
                wx.PostEvent(
                    self,
                    LogboxAppendEvent(msg="Встановлення завершено, але імпорт не вдалось.\n"),
                )
                self._deps_ready = False
                wx.CallAfter(self._update_select_enabled)
                wx.CallAfter(
                    wx.MessageBox,
                    "Встановлення завершено, але імпорт не вдалось. Спробуйте перезапустити KiCad.",
                    "Попередження",
                    wx.ICON_WARNING,
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _update_select_enabled(self):
        try:
            btn = getattr(self, "select_part_button", None)
            if btn is not None:
                btn.Enable(bool(self._deps_ready))
                if self._deps_ready:
                    btn.SetToolTip("")
                else:
                    btn.SetToolTip("Залежності не встановлено. Встановіть, щоб активувати.")
        except Exception:
            pass

    def Destroy(self):  # noqa: N802 - wx override
        # Clean up logging handlers to avoid duplicates on reopen
        try:
            root = logging.getLogger()
            if hasattr(self, "_stderr_handler"):
                root.removeHandler(self._stderr_handler)
            if hasattr(self, "_ui_log_handler"):
                root.removeHandler(self._ui_log_handler)
        except Exception:
            pass
        return super().Destroy()

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Convert category name to a safe folder name."""
        try:
            import re as _re
            cleaned = _re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
            return cleaned or "Misc"
        except Exception:
            return "Misc"

class _LogBoxHandler(logging.StreamHandler):
    
    """Forward Python logging records to the wx UI via events."""

    def __init__(self, event_destination):
        super().__init__()
        self._event_destination = event_destination

    def emit(self, record):
        try:
            msg = self.format(record)
            wx.PostEvent(self._event_destination, LogboxAppendEvent(msg=f"{msg}\n"))
        except Exception:
            # Never raise from logging
            pass

    # --- helpers for system-wide 3rdparty integration ---
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""

def _write_text(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8")
    except Exception:
        pass
