#!/usr/bin/env python3

"""Build the plugin parts database from yaqwsx/jlcparts component caches."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import click
import humanize

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_build.db_output import (  # noqa: E402
    OutputValidationError,
    PartsDatabaseBuilder,
    package_database,
)
from db_build.db_source import SourceDatabase, SourceSchemaError  # noqa: E402


UPSTREAM_BASE_URL = "https://yaqwsx.github.io/jlcparts/data"


class DownloadProgress:
    """Display throttled urllib download progress."""

    def __init__(self):
        self.last_print_time = 0.0

    def progress_hook(self, count: int, block_size: int, total_size: int) -> None:
        downloaded = count * block_size
        now = time.monotonic()
        if now - self.last_print_time >= 0.5 or downloaded >= total_size:
            percent = int(downloaded * 100 / total_size) if total_size > 0 else 0
            sys.stdout.write(
                f"\rDownloading: {percent}% ({downloaded}/{total_size} bytes)"
            )
            sys.stdout.flush()
            self.last_print_time = now
        if downloaded >= total_size:
            print()


def _find_7zip() -> str:
    for name in ("7z", "7zz"):
        path = shutil.which(name)
        if path:
            return path
    raise click.ClickException("Unable to find 7z or 7zz")


def _volume_count(seven_zip: str, archive: Path) -> int:
    result = subprocess.run(
        [seven_zip, "l", str(archive)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 and "ERROR = Missing volume" not in result.stdout:
        raise click.ClickException(
            f"Unable to inspect {archive.name}: {result.stdout} {result.stderr}"
        )
    for line in result.stdout.splitlines():
        if "Volume Index =" in line:
            try:
                return int(line.split("=")[-1].strip())
            except ValueError as exc:
                raise click.ClickException(
                    f"Invalid Volume Index line: {line!r}"
                ) from exc
    raise click.ClickException("7z output does not contain a Volume Index")


def fetch_source_database(destination: Path) -> None:
    """Fetch, extract, validate, and atomically replace the source cache."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    seven_zip = _find_7zip()
    with tempfile.TemporaryDirectory(
        prefix="jlcparts-download-", dir=destination.parent
    ) as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        first_file = temp_dir / "cache.zip"
        progress = DownloadProgress()
        print(f"Fetching source database from {UPSTREAM_BASE_URL}")
        urllib.request.urlretrieve(
            f"{UPSTREAM_BASE_URL}/cache.zip",
            first_file,
            reporthook=progress.progress_hook,
        )
        count = _volume_count(seven_zip, first_file)
        for index in range(1, count + 1):
            filename = f"cache.z{index:02d}"
            print(f"Fetching {filename}")
            urllib.request.urlretrieve(
                f"{UPSTREAM_BASE_URL}/{filename}",
                temp_dir / filename,
                reporthook=DownloadProgress().progress_hook,
            )

        result = subprocess.run(
            [seven_zip, "x", "-y", first_file.name],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Unable to extract source database: {result.stdout} {result.stderr}"
            )
        extracted = temp_dir / "cache.sqlite3"
        try:
            source = SourceDatabase(extracted)
        except SourceSchemaError as exc:
            raise click.ClickException(str(exc)) from exc
        print(
            f"Validated {source.format} source with "
            f"{humanize.intcomma(source.count_parts())} parts"
        )
        os.replace(extracted, destination)


@click.command()
@click.option(
    "--skip-cleanup",
    is_flag=True,
    default=False,
    help="Keep the generated database and unsplit ZIP after packaging.",
)
@click.option(
    "--fetch-parts-db",
    is_flag=True,
    default=False,
    help="Fetch and validate the upstream component cache.",
)
@click.option(
    "--skip-generate",
    is_flag=True,
    default=False,
    help="Skip conversion and packaging.",
)
@click.option(
    "--include-not-present",
    is_flag=True,
    default=False,
    help="Include source-db-v2 rows not present in the current JLC catalog.",
)
@click.option(
    "--deep-validate",
    is_flag=True,
    default=False,
    help="Run full SQLite, JSON, row-count, and ZIP CRC scans.",
)
def main(
    skip_cleanup: bool,
    fetch_parts_db: bool,
    skip_generate: bool,
    include_not_present: bool,
    deep_validate: bool,
) -> None:
    """Fetch and/or generate the distributable FTS5 database."""

    working_directory = Path(__file__).resolve().parent / "db_working"
    working_directory.mkdir(parents=True, exist_ok=True)
    source_path = working_directory / "cache.sqlite3"

    if fetch_parts_db:
        fetch_source_database(source_path)
    if skip_generate:
        return

    output_path = working_directory / "parts-fts5.db"
    started = time.monotonic()
    try:
        builder = PartsDatabaseBuilder(
            source_path,
            output_path,
            include_not_present=include_not_present,
            deep_validation=deep_validate,
        )
        print(
            f"Generating {output_path.name} from {builder.source.format} "
            f"({humanize.intcomma(builder.source.count_parts())} source rows)"
        )
        part_count = builder.build()
        chunk_count = package_database(
            output_path,
            working_directory / "chunk_num_fts5.txt",
            cleanup=not skip_cleanup,
            deep_validation=deep_validate,
        )
    except (OutputValidationError, SourceSchemaError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    elapsed = time.monotonic() - started
    print(
        f"Generated {humanize.intcomma(part_count)} parts in "
        f"{humanize.precisedelta(elapsed, minimum_unit='seconds')}; "
        f"created {chunk_count} chunks"
    )


if __name__ == "__main__":
    main()
