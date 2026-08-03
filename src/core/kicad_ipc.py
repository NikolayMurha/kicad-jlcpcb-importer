"""Runtime adapter for KiCad's out-of-process IPC API."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional


PLUGIN_IDENTIFIER = "com.github.nikolaymurha.kicad-jlcpcb-importer"
MINIMUM_KICAD_MAJOR = 10


@dataclass(frozen=True)
class ProjectContext:
    """Paths and names for the document that launched the IPC action."""

    project_path: Path
    board_name: str
    schematic_name: str


def _project_directory(raw_path: str | Path, board_name: str) -> Path:
    """Normalize the project path supplied by different KiCad API revisions."""

    path = Path(raw_path).expanduser() if str(raw_path or "").strip() else Path.cwd()
    if path.suffix.lower() in {".kicad_pro", ".pro"}:
        return path.parent
    if path.suffix.lower() == ".kicad_pcb":
        return path.parent
    if not path.is_absolute() and board_name:
        path = (Path.cwd() / path).resolve()
    return path


class KicadIpcProvider:
    """Small boundary around ``kipy`` used by the rest of the application."""

    def __init__(self, client: Any):
        self.client = client
        self._board: Optional[Any] = None
        self._context: Optional[ProjectContext] = None

    @classmethod
    def connect(cls) -> "KicadIpcProvider":
        """Connect using the socket and token injected by KiCad."""

        if not os.environ.get("KICAD_API_SOCKET"):
            raise RuntimeError(
                "KICAD_API_SOCKET is missing. Launch this action from KiCad 10 or newer."
            )
        from kipy import KiCad

        provider = cls(
            KiCad(
                client_name="com.github.nikolaymurha.kicad-jlcpcb-importer",
                timeout_ms=5000,
            )
        )
        provider.client.ping()
        version = provider.get_version()
        if version[0] < MINIMUM_KICAD_MAJOR:
            raise RuntimeError(
                f"KiCad {MINIMUM_KICAD_MAJOR} or newer is required for this IPC plugin; "
                f"connected to {version[3]}."
            )
        return provider

    def get_version(self) -> tuple[int, int, int, str]:
        """Return ``(major, minor, patch, full_version)`` from KiCad."""

        version = self.client.get_version()
        return (
            int(version.major),
            int(version.minor),
            int(version.patch),
            str(version.full_version),
        )

    def get_board(self) -> Any:
        """Return and cache the active IPC board wrapper."""

        if self._board is None:
            self._board = self.client.get_board()
        return self._board

    def get_project_context(self) -> ProjectContext:
        """Return the active board's project directory and file names."""

        if self._context is not None:
            return self._context

        board = self.get_board()
        document = board.document
        raw_board_filename = str(getattr(document, "board_filename", "") or "")
        board_name = Path(
            raw_board_filename or str(getattr(board, "name", "") or "board.kicad_pcb")
        ).name
        project = getattr(document, "project", None)
        raw_project_path = str(getattr(project, "path", "") or raw_board_filename)
        project_path = _project_directory(raw_project_path, board_name)
        project_name = str(getattr(project, "name", "") or "")
        schematic_name = f"{project_name or Path(board_name).stem}.kicad_sch"
        self._context = ProjectContext(project_path, board_name, schematic_name)
        return self._context

    def get_kicad_cli_path(self) -> str:
        """Ask KiCad for the matching ``kicad-cli`` executable."""

        return str(self.client.get_kicad_binary_path("kicad-cli"))

    def get_plugin_settings_path(self) -> Path:
        """Return KiCad's persistent settings directory for this plugin."""

        return Path(self.client.get_plugin_settings_path(PLUGIN_IDENTIFIER))

    def prepare_environment(
        self,
        environ: Optional[MutableMapping[str, str]] = None,
    ) -> Mapping[str, str]:
        """Expose IPC-derived paths to code that consumes KiCad environment variables."""

        env = os.environ if environ is None else environ
        context = self.get_project_context()
        version = self.get_version()
        env["KIPRJMOD"] = str(context.project_path)
        env["KICAD_VERSION"] = version[3]
        env["KICAD_CLI"] = self.get_kicad_cli_path()
        env["JLCPCB_PLUGIN_SETTINGS_PATH"] = str(self.get_plugin_settings_path())
        return env
