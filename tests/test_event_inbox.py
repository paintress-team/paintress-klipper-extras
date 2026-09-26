# SPDX-FileCopyrightText: 2026 paintress-team
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test the daemon client's event inbox and poll_event() primitive.

paintressd_client is plain Python (no Klipper deps), so it can be tested with a
tiny fake daemon that both acks commands and pushes asynchronous `event`
messages. poll_event() is the reactor-friendly primitive the plugin uses to wait
on a swath's PRINT_COMPLETE (or a fault) off the event stream, instead of the
old prints_completed status polling. Runs under pytest or as a plain script.
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
    """NDJSON server that greets, acks commands, and can push events.

    A command named ``emit_then_ack`` first sends the event object carried in
    its ``event_obj`` param and *then* the response, reproducing an event that
    interleaves with a command's response (dispatched into the client's inbox by
    send_command's wait loop). ``push()`` sends an arbitrary object to the
    connected client at any time (used to inject events around a poll).
    """

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._run = True
        self._conn = None
        self._conn_ready = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        self._conn = conn
        self._send(conn, {"type": "welcome", "version": "test"})
        self._conn_ready.set()
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
                self._on_command(conn, msg)

    def _on_command(self, conn, msg):
        if msg.get("cmd") == "emit_then_ack":
            self._send(conn, msg.get("event_obj"))
        self._send(conn, {"type": "response", "cmd": msg.get("cmd"),
                          "id": msg.get("id"), "success": True})

    def _send(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    def push(self, obj):
        """Send an object to the connected client (waits for the connection)."""
        self._conn_ready.wait(2.0)
        self._send(self._conn, obj)

    def stop(self):
        self._run = False
        try:
            self._srv.close()
        except OSError:
            pass


def _poll_until(client, events, swath_id=None, timeout=2.0):
    """Loop poll_event() until it yields a match or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        evt = client.poll_event(events, swath_id=swath_id, max_block=0.05)
        if evt is not None:
            return evt
    return None


def _connect(daemon):
    client = PaintressdClient(host="127.0.0.1", port=daemon.port, timeout=3.0)
    assert client.connect()
    return client


def test_clear_events_drops_parked_events():
    # A stale event from a previous print (parked during a command
    # round-trip) must not survive a print boundary: clear_events() drops it
    # and a later wait for the same swath id finds nothing.
    daemon = FakeDaemon()
    try:
        client = _connect(daemon)
        evt = {"type": "event", "event": "print_complete", "swath_id": 7}
        resp = client.send_command("emit_then_ack", event_obj=evt)
        assert resp.get("success"), resp

        assert client.clear_events() == 1
        assert client.clear_events() == 0  # idempotent

        found = client.poll_event(("print_complete",), swath_id=7,
                                  max_block=0.05)
        assert found is None, f"stale event leaked past clear_events: {found}"
        client.disconnect()
    finally:
        daemon.stop()


def test_event_arriving_during_send_command_lands_in_inbox():
    # An event that interleaves with a command's response is dispatched into
    # the inbox, so a later poll finds it (no socket read needed).
    daemon = FakeDaemon()
    try:
        client = _connect(daemon)
        evt = {"type": "event", "event": "print_complete", "swath_id": 1}
        resp = client.send_command("emit_then_ack", event_obj=evt)
        assert resp.get("success"), resp
        # The event was consumed off the wire during the round-trip; it must
        # now be waiting in the inbox for poll_event.
        found = client.poll_event(("print_complete",), swath_id=1, max_block=0.0)
        assert found is not None and found["event"] == "print_complete"
        client.disconnect()
    finally:
        daemon.stop()


def test_event_arriving_during_poll_is_found():
    # Nothing queued yet: poll_event reads the event straight off the socket.
    daemon = FakeDaemon()
    try:
        client = _connect(daemon)
        daemon.push({"type": "event", "event": "print_complete", "swath_id": 2})
        found = _poll_until(client, ("print_complete",), swath_id=2)
        assert found is not None and found["swath_id"] == 2
        client.disconnect()
    finally:
        daemon.stop()


def test_wrong_swath_is_parked_not_consumed():
    # An event for another swath must not satisfy the wait; it stays queued so
    # the poll for its own swath still finds it.
    daemon = FakeDaemon()
    try:
        client = _connect(daemon)
        daemon.push({"type": "event", "event": "print_complete", "swath_id": 5})
        # Poll for swath 3: never matches, so we time out with None.
        assert _poll_until(client, ("print_complete",), swath_id=3,
                           timeout=0.4) is None
        # The swath-5 event was parked, not dropped.
        found = client.poll_event(("print_complete",), swath_id=5, max_block=0.0)
        assert found is not None and found["swath_id"] == 5
        client.disconnect()
    finally:
        daemon.stop()


def test_daemon_fault_without_swath_id_matches_by_name():
    # pipeline_error carries no swath_id: it must satisfy a swath-specific wait
    # by name alone, so a whole-pipeline failure is never missed.
    daemon = FakeDaemon()
    try:
        client = _connect(daemon)
        daemon.push({"type": "event", "event": "pipeline_error",
                     "message": "swath 4: stream failed"})
        found = _poll_until(client, ("print_complete", "pipeline_error"),
                            swath_id=4)
        assert found is not None and found["event"] == "pipeline_error"
        client.disconnect()
    finally:
        daemon.stop()


def test_inbox_is_bounded_under_a_burst():
    # A burst of unmatched events must not grow the inbox without limit.
    daemon = FakeDaemon()
    try:
        client = _connect(daemon)
        # Feed well past maxlen (256) events the poll will never match, and
        # drain them off the socket into the inbox.
        for i in range(400):
            daemon.push({"type": "event", "event": "print_started", "swath_id": i})
        # Drain them off the socket into the inbox (one per poll; never a
        # match, so each is parked). A few extra polls cover in-flight lines.
        for _ in range(450):
            client.poll_event(("print_complete",), max_block=0.02)
        assert len(client._event_inbox) <= 256
        client.disconnect()
    finally:
        daemon.stop()


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
