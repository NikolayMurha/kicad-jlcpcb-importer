"""Tests for source database archive inspection."""

from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import patch

import click

from db_build.jlcparts_db_convert import _volume_count


class VolumeCountTests(unittest.TestCase):
    @staticmethod
    def _inspect(stdout: str, returncode: int = 0, stderr: str = "") -> int:
        result = CompletedProcess(
            args=["7z", "l"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        with patch("db_build.jlcparts_db_convert.subprocess.run", return_value=result):
            return _volume_count("7z", Path("cache.zip"))

    def test_accepts_7zip_volume_index(self) -> None:
        output = "ERROR = Missing volume : cache.z01\nVolume Index = 42\n"
        self.assertEqual(self._inspect(output, returncode=2), 42)

    def test_accepts_p7zip_volume_field(self) -> None:
        output = "ERROR = Missing volume : cache.z01\nVolume = 42\n"
        self.assertEqual(self._inspect(output, returncode=2), 42)

    def test_regular_zip_requires_no_extra_volumes(self) -> None:
        self.assertEqual(self._inspect("Type = zip\nPhysical Size = 123\n"), 0)

    def test_unrecognized_missing_volume_output_is_actionable(self) -> None:
        with self.assertRaisesRegex(
            click.ClickException,
            "Unable to determine split archive volume count",
        ):
            self._inspect(
                "ERROR = Missing volume : cache.z01\nUnknown volume format\n",
                returncode=2,
            )


if __name__ == "__main__":
    unittest.main()
