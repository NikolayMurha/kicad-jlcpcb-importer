#!/usr/bin/env python3
"""Combine and atomically install the split parts database archive."""

from __future__ import annotations

from contextlib import closing
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Callable, Optional
from zipfile import BadZipFile, ZipFile


ProgressCallback = Optional[Callable[[float], None]]
_CHUNK_RE = re.compile(r"^parts-fts5\.db\.zip\.(\d{3})$")
_DATABASE_NAME = "parts-fts5.db"
_LOGGER = logging.getLogger(__name__)


def _replace_database(
    source: Path,
    destination: Path,
    *,
    attempts: int = 6,
    replace: Callable[[Path, Path], None] = os.replace,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Replace a database, tolerating short-lived Windows sharing locks."""

    for attempt in range(attempts):
        try:
            replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            sleep(0.05 * (attempt + 1))


def _safe_unlink(path: Path) -> None:
    """Remove a temporary artifact without masking a successful install."""

    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOGGER.warning("Could not remove temporary database artifact %s: %s", path, exc)


def _database_chunks(path: Path) -> list[Path]:
    chunks = sorted(
        (item for item in path.iterdir() if _CHUNK_RE.fullmatch(item.name)),
        key=lambda item: int(_CHUNK_RE.fullmatch(item.name).group(1)),
    )
    if not chunks:
        raise FileNotFoundError(f"No {_DATABASE_NAME}.zip.NNN chunks found in {path}")
    expected = list(range(1, len(chunks) + 1))
    actual = [int(_CHUNK_RE.fullmatch(item.name).group(1)) for item in chunks]
    if actual != expected:
        raise ValueError(f"Database archive chunks are incomplete: found {actual}")
    return chunks


def _validate_extracted_database(path: Path) -> None:
    """Check the minimum runtime contract before publishing the database."""

    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            missing = {"parts", "meta", "categories"} - tables
            if missing:
                raise ValueError(
                    "Extracted parts database is missing: " + ", ".join(sorted(missing))
                )
            if connection.execute("SELECT count(*) FROM meta").fetchone()[0] != 1:
                raise ValueError("Extracted parts database has invalid metadata")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Extracted parts database is invalid: {exc}") from exc


def install_parts_database(
    path: str | Path,
    combine_progress: ProgressCallback = None,
    extract_progress: ProgressCallback = None,
) -> Path:
    """Combine chunks, validate the ZIP/SQLite payload, and replace atomically."""

    directory = Path(path)
    chunks = _database_chunks(directory)
    archive_fd, archive_name = tempfile.mkstemp(
        prefix=f".{_DATABASE_NAME}.", suffix=".zip.tmp", dir=directory
    )
    os.close(archive_fd)
    archive_path = Path(archive_name)
    database_fd, database_name = tempfile.mkstemp(
        prefix=f".{_DATABASE_NAME}.", suffix=".tmp", dir=directory
    )
    os.close(database_fd)
    database_temp = Path(database_name)
    final_database = directory / _DATABASE_NAME

    try:
        with archive_path.open("wb") as archive:
            for index, chunk in enumerate(chunks, 1):
                with chunk.open("rb") as source:
                    while data := source.read(1024 * 1024):
                        archive.write(data)
                if combine_progress:
                    combine_progress(index * 100.0 / len(chunks))

        try:
            with ZipFile(archive_path, "r") as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                if len(files) != 1 or files[0].filename != _DATABASE_NAME:
                    names = [item.filename for item in files]
                    raise ValueError(f"Unexpected database archive contents: {names}")
                file_info = files[0]
                if file_info.file_size <= 0:
                    raise ValueError("Database archive contains an empty payload")
                with archive.open(file_info) as source, database_temp.open("wb") as target:
                    while data := source.read(1024 * 1024):
                        target.write(data)
                        if extract_progress:
                            extract_progress(target.tell() * 100.0 / file_info.file_size)
        except BadZipFile as exc:
            raise ValueError(f"Invalid database ZIP archive: {exc}") from exc

        _validate_extracted_database(database_temp)
        _replace_database(database_temp, final_database)
        for chunk in chunks:
            _safe_unlink(chunk)
        return final_database
    finally:
        _safe_unlink(archive_path)
        _safe_unlink(database_temp)


def unzip_parts(parent, path):
    """wx event adapter around :func:`install_parts_database`."""

    import wx  # pylint: disable=import-error,import-outside-toplevel

    from ..core.events import (  # pylint: disable=import-outside-toplevel
        UnzipCombiningProgressEvent,
        UnzipCombiningStartedEvent,
        UnzipExtractingCompletedEvent,
        UnzipExtractingProgressEvent,
        UnzipExtractingStartedEvent,
    )

    _LOGGER.debug("Combine and atomically install database chunks")
    wx.PostEvent(parent, UnzipCombiningStartedEvent())

    extraction_started = False

    def _combine(value: float) -> None:
        wx.PostEvent(parent, UnzipCombiningProgressEvent(value=value))

    def _extract(value: float) -> None:
        nonlocal extraction_started
        if not extraction_started:
            extraction_started = True
            wx.PostEvent(parent, UnzipExtractingStartedEvent())
        wx.PostEvent(parent, UnzipExtractingProgressEvent(value=value))

    result = install_parts_database(path, _combine, _extract)
    wx.PostEvent(parent, UnzipExtractingCompletedEvent())
    return result
