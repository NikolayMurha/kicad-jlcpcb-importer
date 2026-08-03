"""Cross-platform single-instance activation for the external IPC action."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import threading
from typing import Callable, Mapping, Optional


_HOST = "127.0.0.1"
_PORT_BASE = 24000
_PORT_COUNT = 16


def _candidate_ports(key: str) -> tuple[int, ...]:
    """Return stable private ports for one KiCad/plugin instance."""

    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).digest()
    first = int.from_bytes(digest[:2], "big") % 8000
    step = (int.from_bytes(digest[2:4], "big") % 7999) | 1
    return tuple(
        _PORT_BASE + ((first + index * step) % 8000)
        for index in range(_PORT_COUNT)
    )


class SingleInstanceCoordinator:
    """Keep one process per KiCad IPC socket and activate its existing window."""

    def __init__(self, key: str):
        self._key = key
        key_hash = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
        self._request = f"JLCPCB-IPC-ACTIVATE:{key_hash}".encode("ascii")
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._callback_lock = threading.Lock()
        self._callback: Optional[Callable[[], None]] = None
        self._activation_pending = False

    @classmethod
    def for_kicad_environment(
        cls,
        identifier: str,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "SingleInstanceCoordinator":
        """Build a key scoped to one plugin action in one KiCad process."""

        env = os.environ if environ is None else environ
        socket_path = str(env.get("KICAD_API_SOCKET", ""))
        return cls(f"{identifier}\0{socket_path}")

    def acquire(self) -> bool:
        """Become the primary process, or activate the existing process."""

        if self._server is not None:
            return True

        for port in _candidate_ports(self._key):
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                server.bind((_HOST, port))
                server.listen(4)
                server.settimeout(0.2)
            except OSError:
                server.close()
                if self._signal_existing(port):
                    return False
                continue

            self._server = server
            self._thread = threading.Thread(
                target=self._serve,
                name="jlcpcb-single-instance",
                daemon=True,
            )
            self._thread.start()
            return True

        raise RuntimeError("Unable to reserve a local activation port for JLCPCB Importer")

    def set_activation_callback(self, callback: Callable[[], None]) -> None:
        """Install the UI activation callback and deliver any queued activation."""

        with self._callback_lock:
            self._callback = callback
            pending = self._activation_pending
            self._activation_pending = False
        if pending:
            callback()

    def _signal_existing(self, port: int) -> bool:
        try:
            with socket.create_connection((_HOST, port), timeout=0.15) as connection:
                connection.settimeout(0.3)
                connection.sendall(self._request + b"\n")
                return hmac.compare_digest(connection.recv(32).strip(), b"OK")
        except OSError:
            return False

    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop.is_set():
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(0.3)
                    request = connection.recv(256).strip()
                    if not hmac.compare_digest(request, self._request):
                        continue
                    connection.sendall(b"OK\n")
                    self._notify_activation()
                except OSError:
                    continue

    def _notify_activation(self) -> None:
        with self._callback_lock:
            callback = self._callback
            if callback is None:
                self._activation_pending = True
                return
        callback()

    def close(self) -> None:
        """Release the activation port and stop the listener."""

        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
