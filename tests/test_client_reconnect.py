# SPDX-FileCopyrightText: 2026 paintress-team
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test the daemon client's lazy TCP auto-reconnect.

paintressd_client is plain Python (no Klipper deps), so it can be tested with a
tiny fake daemon: after the link drops, the next command transparently
reconnects and succeeds, without a manual reconnect. Runs under pytest or as a
plain script.
"""

import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extras"))
from paintressd_client import PaintressdClient  # noqa: E402


class FakeDaemon:
    """Minimal NDJSON server: greets each client and acks every command."""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self.connections = 0
        self._run = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self._run:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.sendall((json.dumps({"type": "welcome", "version": "test"}) + "\n").encode())
        buf = b""
        while self._run:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                resp = {"type": "response", "cmd": msg.get("cmd"),
                        "id": msg.get("id"), "success": True}
                try:
                    conn.sendall((json.dumps(resp) + "\n").encode())
                except OSError:
                    return

    def stop(self):
        self._run = False
        try:
            self._srv.close()
        except OSError:
            pass


def test_lazy_reconnect_after_drop():
    daemon = FakeDaemon()
    try:
        client = PaintressdClient(host="127.0.0.1", port=daemon.port, timeout=3.0)
        assert client.connect()
        assert client.send_command("status").get("success")
        assert daemon.connections == 1

        # Simulate a detected link drop (e.g. daemon restarted).
        client._disconnect_internal()
        assert not client.connected

        # The next command must auto-reconnect and succeed, no manual connect().
        resp = client.send_command("status")
        assert resp.get("success"), resp
        assert daemon.connections == 2  # a fresh connection was opened

        client.disconnect()
    finally:
        daemon.stop()


def test_reconnect_fails_fast_when_daemon_down():
    # Point at a closed port; auto-reconnect must fail fast, not hang.
    client = PaintressdClient(host="127.0.0.1", port=1, timeout=3.0)
    start = time.monotonic()
    resp = client.send_command("status")
    assert resp.get("success") is False
    assert time.monotonic() - start < 5.0  # bounded by the short connect timeout


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
