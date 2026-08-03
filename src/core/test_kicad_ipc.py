"""Tests for the KiCad IPC runtime boundary."""

import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import threading
import unittest
import uuid

from .kicad_ipc import KicadIpcProvider, _project_directory
from .kicad_lifecycle import KicadLifecycleMonitor, local_ipc_path
from .plugin_paths import get_plugin_cache_path, get_plugin_data_path
from .single_instance import SingleInstanceCoordinator


class _FakeClient:
    def __init__(
        self,
        project_path: str,
        board_name: str = "demo.kicad_pcb",
        project_available: bool = True,
    ):
        document = SimpleNamespace(
            board_filename=board_name,
            project=(
                SimpleNamespace(path=project_path, name="demo")
                if project_available
                else None
            ),
        )
        self.board = SimpleNamespace(name=board_name, document=document)

    def get_board(self):
        return self.board

    def get_version(self):
        return SimpleNamespace(major=10, minor=0, patch=5, full_version="10.0.5")

    def get_kicad_binary_path(self, name: str):
        return f"/opt/kicad/{name}"

    def get_plugin_settings_path(self, _identifier: str):
        return "/tmp/jlcpcb-ipc-settings"


class KicadIpcTests(unittest.TestCase):
    def test_local_ipc_path_parses_filesystem_endpoints(self):
        self.assertEqual(
            local_ipc_path("ipc:///tmp/kicad/api.sock"),
            Path("/tmp/kicad/api.sock"),
        )
        self.assertEqual(
            local_ipc_path("unix:///tmp/kicad/api%20socket"),
            Path("/tmp/kicad/api socket"),
        )
        self.assertIsNone(local_ipc_path("tcp://127.0.0.1:1234"))

    def test_lifecycle_monitor_notifies_when_ipc_socket_disappears(self):
        with TemporaryDirectory() as temp_dir:
            endpoint = Path(temp_dir) / "api.sock"
            endpoint.touch()
            exited = threading.Event()
            monitor = KicadLifecycleMonitor(
                exited.set,
                environ={"KICAD_API_SOCKET": f"ipc://{endpoint}"},
                parent_pid=999_999_999,
                poll_interval=0.01,
            )
            try:
                monitor.start()
                endpoint.unlink()
                self.assertTrue(exited.wait(1.0))
            finally:
                monitor.close()

    def test_project_directory_accepts_project_file(self):
        root = Path("/tmp/ipc-project")
        self.assertEqual(
            _project_directory(root / "demo.kicad_pro", "demo.kicad_pcb"),
            root,
        )

    def test_provider_builds_context_from_ipc_document(self):
        root = Path("/tmp/ipc-project")
        provider = KicadIpcProvider(_FakeClient(str(root)))
        context = provider.get_project_context()

        self.assertEqual(context.project_path, root)
        self.assertEqual(context.board_name, "demo.kicad_pcb")
        self.assertEqual(context.schematic_name, "demo.kicad_sch")

    def test_provider_exports_path_environment(self):
        root = Path("/tmp/ipc-project")
        provider = KicadIpcProvider(_FakeClient(str(root)))
        env: dict[str, str] = {}

        provider.prepare_environment(env)

        self.assertEqual(env["KIPRJMOD"], str(root))
        self.assertEqual(env["KICAD_VERSION"], "10.0.5")
        self.assertEqual(env["KICAD_CLI"], "/opt/kicad/kicad-cli")
        self.assertEqual(env["JLCPCB_PLUGIN_SETTINGS_PATH"], "/tmp/jlcpcb-ipc-settings")

    def test_unsaved_project_uses_board_file_directory(self):
        board_path = "/tmp/ipc-project/demo.kicad_pcb"
        provider = KicadIpcProvider(
            _FakeClient("", board_path, project_available=False)
        )

        context = provider.get_project_context()

        self.assertEqual(context.project_path, Path("/tmp/ipc-project"))
        self.assertEqual(context.board_name, "demo.kicad_pcb")

    def test_manifest_declares_ipc_action_and_existing_assets(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["$schema"], "https://go.kicad.org/api/schemas/v1")
        self.assertEqual(manifest["runtime"]["type"], "python")
        action = manifest["actions"][0]
        self.assertEqual(action["scopes"], ["pcb"])
        self.assertTrue((root / action["entrypoint"]).is_file())
        for key in ("icons-light", "icons-dark"):
            for asset in action[key]:
                self.assertTrue((root / asset).is_file(), asset)

    def test_ipc_settings_path_is_used_for_new_runtime_data(self):
        with TemporaryDirectory() as temp_dir:
            plugin_root = Path(temp_dir) / "plugin"
            plugin_root.mkdir()
            settings_root = Path(temp_dir) / "settings"
            env = {"JLCPCB_PLUGIN_SETTINGS_PATH": str(settings_root)}

            self.assertEqual(
                get_plugin_data_path(environ=env, plugin_path=plugin_root),
                settings_root / "jlcpcb",
            )
            self.assertEqual(
                get_plugin_cache_path(environ=env, plugin_path=plugin_root),
                settings_root / "cache",
            )

    def test_existing_database_is_not_duplicated_during_migration(self):
        with TemporaryDirectory() as temp_dir:
            plugin_root = Path(temp_dir) / "plugin"
            legacy = plugin_root / "jlcpcb"
            legacy.mkdir(parents=True)
            (legacy / "parts-fts5.db").touch()
            env = {"JLCPCB_PLUGIN_SETTINGS_PATH": str(Path(temp_dir) / "settings")}

            self.assertEqual(
                get_plugin_data_path(environ=env, plugin_path=plugin_root),
                legacy,
            )

    def test_repeated_action_activates_existing_process(self):
        key = f"test-{uuid.uuid4()}"
        primary = SingleInstanceCoordinator(key)
        duplicate = SingleInstanceCoordinator(key)
        activated = threading.Event()
        try:
            self.assertTrue(primary.acquire())
            primary.set_activation_callback(activated.set)

            self.assertFalse(duplicate.acquire())
            self.assertTrue(activated.wait(1.0))
        finally:
            duplicate.close()
            primary.close()

    def test_early_activation_is_delivered_when_ui_becomes_ready(self):
        key = f"test-{uuid.uuid4()}"
        primary = SingleInstanceCoordinator(key)
        duplicate = SingleInstanceCoordinator(key)
        activated = threading.Event()
        try:
            self.assertTrue(primary.acquire())
            self.assertFalse(duplicate.acquire())

            primary.set_activation_callback(activated.set)
            self.assertTrue(activated.wait(1.0))
        finally:
            duplicate.close()
            primary.close()

    def test_kicad_tokens_share_one_instance_for_the_same_socket(self):
        first = SingleInstanceCoordinator.for_kicad_environment(
            "org.example.plugin",
            {
                "KICAD_API_SOCKET": "ipc:///tmp/kicad/api.sock",
                "KICAD_API_TOKEN": "first-launch-token",
            },
        )
        second = SingleInstanceCoordinator.for_kicad_environment(
            "org.example.plugin",
            {
                "KICAD_API_SOCKET": "ipc:///tmp/kicad/api.sock",
                "KICAD_API_TOKEN": "second-launch-token",
            },
        )
        activated = threading.Event()
        try:
            self.assertTrue(first.acquire())
            first.set_activation_callback(activated.set)

            self.assertFalse(second.acquire())
            self.assertTrue(activated.wait(1.0))
        finally:
            second.close()
            first.close()


if __name__ == "__main__":
    unittest.main()
