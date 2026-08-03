"""Track the KiCad process that owns an external IPC action."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import threading
from typing import Callable, Mapping, Optional
from urllib.parse import unquote


def local_ipc_path(endpoint: str) -> Optional[Path]:
    """Return the filesystem path for a KiCad IPC endpoint, when it has one."""

    value = str(endpoint or "").strip()
    for prefix in ("ipc://", "unix://"):
        if value.startswith(prefix):
            raw_path = unquote(value[len(prefix) :])
            if not raw_path or raw_path.startswith("@"):
                return None
            return Path(raw_path)
    return None


def process_is_alive(pid: int) -> bool:
    """Check a process without adding a psutil dependency."""

    if pid <= 1 or pid == os.getpid():
        return False

    if sys.platform == "win32":
        try:
            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


class KicadLifecycleMonitor:
    """Notify the UI when the KiCad instance behind this action exits."""

    def __init__(
        self,
        callback: Callable[[], None],
        environ: Optional[Mapping[str, str]] = None,
        parent_pid: Optional[int] = None,
        poll_interval: float = 0.5,
    ):
        env = os.environ if environ is None else environ
        endpoint_path = local_ipc_path(str(env.get("KICAD_API_SOCKET", "")))
        self._endpoint_path = (
            endpoint_path if endpoint_path is not None and endpoint_path.exists() else None
        )
        self._parent_pid = os.getppid() if parent_pid is None else parent_pid
        self._watch_parent = (
            self._endpoint_path is None and process_is_alive(self._parent_pid)
        )
        self._callback = callback
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start monitoring when a useful host signal is available."""

        if self._thread is not None:
            return
        if self._endpoint_path is None and not self._watch_parent:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="jlcpcb-kicad-lifecycle",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            if self._endpoint_path is not None:
                alive = self._endpoint_path.exists()
            else:
                alive = process_is_alive(self._parent_pid)
            if alive:
                continue
            if not self._stop.is_set():
                self._callback()
            return

    def close(self) -> None:
        """Stop monitoring without invoking the callback."""

        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._poll_interval * 2))
