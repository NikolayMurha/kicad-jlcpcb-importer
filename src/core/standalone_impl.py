"""Minimal provider for launching the UI outside KiCad during development."""

from pathlib import Path

from .kicad_ipc import ProjectContext


class KicadStub:
    """Provide deterministic project context without a live KiCad session."""

    def get_project_context(self) -> ProjectContext:
        """Return a fake board in the current working directory."""

        return ProjectContext(
            project_path=Path.cwd(),
            board_name="fake_test_board.kicad_pcb",
            schematic_name="fake_test_board.kicad_sch",
        )
