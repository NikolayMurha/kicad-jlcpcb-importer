"""EasyEDA to KiCad import orchestrator for LCSC parts.

Encapsulates the end-to-end flow previously in AssignLCSCMainDialog._import_part_via_easyeda.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Dict, Optional, Tuple
import subprocess
import wx  # type: ignore
from .events import LogboxAppendEvent

from .helpers import PLUGIN_PATH
from .symbol_editor import SymbolEditor
from .lib_tables import LibTablesManager
from .footprint_editor import FootprintEditor


class EasyedaImporter:
    """Run easyeda2kicad, adjust symbol/footprints, and update project tables."""

    def __init__(
        self,
        project_path: Path | str,
        python_exe: str,
        parent_window: Optional[wx.Window] = None,
        scope: str = "project",
        lib_dir: Optional[Path | str] = None,
    ) -> None:
        self.project_path = Path(project_path)
        self.python_exe = python_exe
        # support both names for clarity
        self.parent_window = parent_window
        self.scope = str(scope).lower()
        self.lib_dir = Path(lib_dir) if lib_dir is not None else (Path(PLUGIN_PATH) / "lib")

    def resolve_nickname_prefix(self) -> str:
        """Resolve KiCad 3rd-party library nickname prefix."""
        # 1) KiCad settings (best effort)
        try:
            try:
                import pcbnew as _kicad_pcbnew  # type: ignore
            except Exception:
                _kicad_pcbnew = None
            base = None
            if _kicad_pcbnew is not None:
                try:
                    base = _kicad_pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
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

        return "PCM_"

    @staticmethod
    def _safe_remove(path: Path) -> int:
        try:
            if path.exists() and path.is_dir():
                import shutil
                shutil.rmtree(path)
                return 1
            if path.exists() and path.is_file():
                path.unlink()
                return 1
        except Exception:
            pass
        return 0

    def _build_commands(self, lcsc_id: str, sym_out: Path, fp_out: Path, m3d_out: Path) -> list[list[str]]:
        return [
            [
                self.python_exe,
                "-m",
                "easyeda2kicad",
                "--symbol",
                "--overwrite",
                f"--output={sym_out}",
                f"--lcsc_id={lcsc_id}",
            ],
            [
                self.python_exe,
                "-m",
                "easyeda2kicad",
                "--3d",
                "--overwrite",
                f"--output={m3d_out}",
                f"--lcsc_id={lcsc_id}",
            ],
            [
                self.python_exe,
                "-m",
                "easyeda2kicad",
                "--footprint",
                "--overwrite",
                f"--output={fp_out}",
                f"--lcsc_id={lcsc_id}",
            ]
        ]

    def _compute_outputs(self, category: str) -> Tuple[Path, Path, Path, Path, bool]:
        if self.is_system_scope:
            third_party = os.environ.get("KICAD9_3RD_PARTY")
            if third_party and isinstance(third_party, str) and third_party.strip():
                base_path = Path(third_party)
            else:
                base_path = Path(PLUGIN_PATH) / "libraries"
            # Зверніть уваагу, що це не імя каталогу! Для easyedat2kicad це базове імя для файлів та каталогів;
            # easyedat2kicad на базі цього створить target_output_name.kicad_sym, target_output_name.pretty, target_output_name.3dshapes і т.д.
            target_output_name = f"LCSC_{category}"
            
            plugin_folder = Path(PLUGIN_PATH).resolve().name
            symbols_path = base_path / "symbols" / plugin_folder / target_output_name
            footprints_path = base_path / "footprints" / plugin_folder / target_output_name
            models_3d_path = base_path / "3dmodels" / plugin_folder / target_output_name
            
            for folder in ("symbols", "footprints", "3dmodels"):
                (base_path / folder / plugin_folder).mkdir(parents=True, exist_ok=True)
            
            self.log(
                f"Режим зберігання: system (спільна тека)\n  Symbols → {symbols_path}\n  Footprints → {footprints_path}\n  3D → {models_3d_path}\n"
            )
        else:
            symbols_path = footprints_path = models_3d_path = (
                Path(self.project_path) / "library" / target_output_name
            )
            self.log(f"Режим зберігання: project (всередині проєкту) → {symbols_path}\n")
        return symbols_path, footprints_path, models_3d_path

    def import_part(
        self,
        lcsc_id: str,
        category: str,
        meta: Optional[Dict] = None,
    ) -> Tuple[bool, Path]:
        category = self._sanitize(category or "Misc")
        symbols_path, footprints_path, models_3d_path = self._compute_outputs(category)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.lib_dir) + (
            (os.pathsep + env.get("PYTHONPATH", "")) if env.get("PYTHONPATH") else ""
        )
        commands = self._build_commands(lcsc_id, symbols_path, footprints_path, models_3d_path)
        self.log(f"commands: {commands}\n")
        
        ret = 0
        for cmd in commands:
            self.log("Запуск: " + " ".join(str(x) for x in cmd) + "\n")
            ret = self._run_and_stream(cmd, env=env)
            if ret != 0:
                break
            
        if ret != 0:
            return False

        # Patch symbol and properties
        try:
            prefix = self.resolve_nickname_prefix() if self.is_system_scope else ""
            symbol_file = symbols_path.with_suffix(".kicad_sym")
            symbol_name = (meta or {}).get("mfr_part") or ""
            editor = SymbolEditor(symbol_file, symbol_name, self.parent_window)
            if self.is_system_scope:
                self.log("ensure_footprint_prefix: \n")
                editor.ensure_footprint_prefix(prefix)
            
            # Build props
            props: Dict[str, str] = {}
            
            for key in ("Manufacturer", "Manufacturer Part", "Description"):
                kmeta = key.lower().replace(" ", "_")
                val = (meta or {}).get(kmeta) or ""
                if val:
                    props[key] = val
            attrs_json = (meta or {}).get("attributes_json") or ""
            
            try:
                attrs = json.loads(attrs_json) if attrs_json else {}
            except Exception:
                attrs = {}
            
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    if v is None:
                        continue
                    props[str(k)] = str(v)
            editor.apply_properties(
                props,
                category=category,
                update_empty_only=True,
                hidden=True,
                exclude_equal_to_value=True,
            )
            editor.save(strip_ids=True)

        except Exception as e:
            self.log(f"Не вдалося оновити символ: {e}\n")

        
        # Cleanup cross-generated artifacts in wrong folders (system mode)
        if self.is_system_scope:
            # symbols folder: drop 3dshapes/pretty
            if symbols_path != footprints_path:
                self._safe_remove(Path(f"{symbols_path}.pretty"))
                self._safe_remove(Path(f"{footprints_path}.kicad_sym"))
            
            if symbols_path != models_3d_path:
                self._safe_remove(Path(f"{symbols_path}.3dshapes"))
                self._safe_remove(Path(f"{models_3d_path}.kicad_sym"))
            
            if footprints_path != models_3d_path:
                self._safe_remove(Path(f"{footprints_path}.3dshapes"))
                self._safe_remove(Path(f"{models_3d_path}.pretty"))

        # Update lib tables + relativize 3D (project mode)
        else:
            try:
                mgr = LibTablesManager(self.project_path, log=self.log)
                _sym_found, _fp_found, lib_base = mgr.ensure_project_lib_tables(lib_base, use_project_relative=True)
            except Exception as e:
                self.log(f"Не вдалося оновити library tables: {e}\n")
            try:
                changes = FootprintEditor(self.project_path).relativize_3d_model_paths(Path(lib_base))
                self.log(f"Виправлено шляхів до 3D моделей: {changes}\n")
            except Exception as e:
                self.log(f"Не вдалося оновити 3D-шляхи: {e}\n")
        
        self.log(f"Імпорт завершено у: {symbols_path}, {footprints_path}, {models_3d_path}\n")
        return True, lib_base

    @property
    def is_system_scope(self) -> bool:
        return self.scope == "system"
    
    @staticmethod
    def _sanitize(name: str) -> str:
        try:
            import re
            cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
            return cleaned or "Misc"
        except Exception:
            return "Misc"
        
    def log(self, msg: str) -> None:
        try:
            if self.parent_window is not None:
                wx.PostEvent(self.parent_window, LogboxAppendEvent(msg=msg))
        except Exception:
            pass

    def _run_and_stream(self, cmd, env=None) -> int:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout = subprocess.PIPE,
                stderr = subprocess.STDOUT,
                text = True,
                bufsize = 1,
                env = env,
            )
            if proc.stdout is not None:
                for line in proc.stdout:
                    self.log(line)
            return proc.wait()
        except Exception as e:
            self.log(f"Помилка виконання: {e}\n")
            return 1
