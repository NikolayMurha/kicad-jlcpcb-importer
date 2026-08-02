"""Tests for Linux and macOS KiCad runtime path discovery."""

from pathlib import Path
import unittest

from src.core.platform_support import (
    detected_kicad_major,
    find_kicad_cli,
    is_supported_system,
    resolve_system_library_root,
)


class PlatformSupportTests(unittest.TestCase):
    def test_supported_systems(self) -> None:
        self.assertTrue(is_supported_system("Darwin"))
        self.assertTrue(is_supported_system("Linux"))
        self.assertFalse(is_supported_system("Windows"))

    def test_kicad_major_and_environment_override(self) -> None:
        self.assertEqual(detected_kicad_major("10.0.5"), 10)
        path = resolve_system_library_root(
            "/plugin",
            system_name="Linux",
            environ={"KICAD10_3RD_PARTY": "/custom/thirdparty"},
            home="/home/test",
            version_text="10.0.5",
        )
        self.assertEqual(path, Path("/custom/thirdparty"))

    def test_native_system_library_roots(self) -> None:
        linux = resolve_system_library_root(
            "/plugin",
            system_name="Linux",
            environ={},
            home="/home/test",
            version_text="9.0.6",
        )
        mac = resolve_system_library_root(
            "/plugin",
            system_name="Darwin",
            environ={},
            home="/Users/test",
            version_text="10.0.5",
        )
        self.assertEqual(linux, Path("/home/test/.local/share/kicad/9.0/3rdparty"))
        self.assertEqual(mac, Path("/Users/test/Documents/KiCad/10.0/3rdparty"))

    def test_cli_environment_override(self) -> None:
        expected = Path("/opt/kicad/bin/kicad-cli")
        result = find_kicad_cli(
            system_name="Linux",
            environ={"KICAD_CLI": str(expected)},
            which=lambda _name: None,
            is_executable=lambda path: path == expected,
        )
        self.assertEqual(result, str(expected))

    def test_flatpak_and_macos_cli_candidates(self) -> None:
        flatpak = Path("/app/bin/kicad-cli")
        mac = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
        self.assertEqual(
            find_kicad_cli(
                system_name="Linux",
                environ={},
                executable="/app/bin/python3",
                which=lambda _name: None,
                is_executable=lambda path: path == flatpak,
            ),
            str(flatpak),
        )
        self.assertEqual(
            find_kicad_cli(
                system_name="Darwin",
                environ={},
                executable="/tmp/python3",
                which=lambda _name: None,
                is_executable=lambda path: path == mac,
            ),
            str(mac),
        )


if __name__ == "__main__":
    unittest.main()
