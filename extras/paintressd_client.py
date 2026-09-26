# SPDX-FileCopyrightText: 2026 paintress-team
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Synchronous TCP client for the Paintress daemon.

This module is the transport half of `paintress-klipper-extras`: the
Klipper plugin in `paintress.py` uses PaintressdClient to reach the
Paintress daemon over TCP, and through it the controller board firmware.

Pipeline context:

    paintress.py  -->  PaintressdClient  -->  paintress-daemon  -->  firmware
    (Klipper plugin)   (this module)         (TCP, NDJSON)          (USB serial)

Protocol:
    The daemon speaks NDJSON: one JSON object per line. This client is
    deliberately blocking and simple: send_command() writes a command
    and blocks until the matching `response` arrives, transparently
    dispatching any asynchronous `event` / `firmware_log` / `keepalive`
    messages that arrive in between. High-level wrappers raise
    PaintressdClientError on failure; the low-level send_command()
    returns the raw response dict instead.

The module can also be run directly as a connection / print self-test:

    python paintressd_client.py --host localhost --port 9000 [--job FILE]
"""

import collections
import json
import logging
import socket
import threading
import time
from typing import Dict, Optional, Sequence


logger = logging.getLogger(__name__)


class PaintressdClientError(RuntimeError):
    """Raised when a high-level client operation fails."""
    pass


class PaintressdClient:
    """
    Synchronous TCP client for the Paintress daemon.

    Design:
    - Blocking, single-threaded-friendly API.
    - A lock serialises access to the shared socket.
    - Low-level send_command() returns the raw response dict.
    - High-level methods raise PaintressdClientError on failure.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9000,
        timeout: float = 30.0,
        buffer_size: int = 1024 * 1024,
    ):
        """Store connection parameters; no socket is opened until connect()."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self.buffer_size = buffer_size

        self._socket: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._msg_id = 0
        self._connected = False
        self._recv_buffer = b""

        # Asynchronous `event` messages that arrive while no one is polling
        # for them (dispatched by send_command's wait loop, or read ahead by
        # poll_event) are parked here in arrival order until poll_event()
        # consumes them. Bounded so a burst of unmatched events cannot grow
        # without limit; the oldest are dropped.
        self._event_inbox = collections.deque(maxlen=256)

        # Connection info, populated from the daemon welcome message.
        self.daemon_version: Optional[str] = None
        self.device_connected: bool = False
        self.job_loaded: bool = False

    # -------------------------------------------------------------------------
    # Basic state
    # -------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True while the socket is open and the connection is live."""
        return self._connected and self._socket is not None

    # -------------------------------------------------------------------------
    # Connection management
    # -------------------------------------------------------------------------

    def connect(self) -> bool:
        """Open the TCP connection and consume the daemon welcome message.

        Returns True on success (or if already connected). Raises
        PaintressdClientError if the daemon does not send a valid
        welcome message.
        """
        with self._lock:
            return self._connect_internal()

    def _connect_internal(self, connect_timeout: Optional[float] = None) -> bool:
        """connect() body; the caller must hold the lock.

        `connect_timeout` bounds the TCP connect (used by the auto-reconnect path
        so a dead daemon fails fast); the socket then uses the normal timeout.
        """
        if self._connected:
            return True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(connect_timeout or self.timeout)
            self._socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_RCVBUF,
                self.buffer_size,
            )
            self._socket.connect((self.host, self.port))
            self._socket.settimeout(self.timeout)

            self._connected = True
            self._recv_buffer = b""

            # The daemon greets every new client with a welcome message.
            welcome = self._recv_message(timeout=5.0)
            if not welcome:
                self._disconnect_internal()
                raise PaintressdClientError(
                    f"No welcome message from daemon at {self.host}:{self.port}"
                )

            if welcome.get("type") != "welcome":
                self._disconnect_internal()
                raise PaintressdClientError(
                    f"Invalid welcome message from daemon: {welcome}"
                )

            self.daemon_version = welcome.get("version")
            self.device_connected = bool(welcome.get("device_connected", False))
            self.job_loaded = bool(welcome.get("job_loaded", False))

            logger.info(
                "Connected to daemon v%s, device=%s, job=%s",
                self.daemon_version,
                "connected" if self.device_connected else "disconnected",
                "loaded" if self.job_loaded else "none",
            )
            return True

        except Exception:
            self._disconnect_internal()
            raise

    def disconnect(self):
        """Close the connection (safe to call when already disconnected)."""
        with self._lock:
            self._disconnect_internal()

    def _disconnect_internal(self):
        """Tear down the socket and reset state (caller must hold the lock)."""
        self._connected = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        self._recv_buffer = b""

    def _require_connected(self):
        """Raise PaintressdClientError if the socket is not connected."""
        if not self.connected:
            raise PaintressdClientError("Daemon socket is not connected")

    # -------------------------------------------------------------------------
    # Low-level socket helpers
    # -------------------------------------------------------------------------

    def _send_raw(self, data: bytes) -> bool:
        """Write raw bytes to the socket; drop the connection on error."""
        if not self._socket:
            return False

        try:
            self._socket.sendall(data)
            return True
        except Exception as e:
            logger.error("Send error: %s", e)
            self._disconnect_internal()
            return False

    def _recv_message(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Read and decode one NDJSON object from the socket.

        Returns the decoded dict, or None on timeout / disconnection.
        Any bytes read past the first newline are kept buffered for the
        next call, so messages are never split or lost.
        """
        if not self._socket:
            return None

        self._socket.settimeout(self.timeout if timeout is None else timeout)

        try:
            while True:
                # Emit a buffered line as soon as one is complete.
                newline_idx = self._recv_buffer.find(b"\n")
                if newline_idx >= 0:
                    line = self._recv_buffer[:newline_idx]
                    self._recv_buffer = self._recv_buffer[newline_idx + 1 :]

                    if not line:
                        continue

                    try:
                        return json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON received: %r", line[:200])
                        continue

                chunk = self._socket.recv(65536)
                if not chunk:
                    # Empty read means the peer closed the connection.
                    self._disconnect_internal()
                    return None

                self._recv_buffer += chunk

        except socket.timeout:
            return None
        except Exception as e:
            logger.error("Receive error: %s", e)
            self._disconnect_internal()
            return None

    # -------------------------------------------------------------------------
    # Command transport
    # -------------------------------------------------------------------------

    def send_command(self, cmd: str, timeout: Optional[float] = None, **kwargs) -> Dict:
        """Send a command and block until its `response` arrives.

        Extra keyword arguments become command parameters. Asynchronous
        messages (event / firmware_log / keepalive) received while
        waiting are dispatched and skipped. When no timeout is given,
        per-command defaults that mirror the daemon's own timeouts are
        used. Always returns a dict; failures are reported as
        {"success": False, "error": ...} rather than raised.
        """
        with self._lock:
            if not self._connected:
                # Auto-reconnect: heal a dropped link so new commands work
                # without a manual reconnect. The command below is only sent
                # once the (fresh) socket is up; a mid-command drop is not
                # silently retried (a half-executed state-changer must not run
                # twice). A short connect timeout fails fast if the daemon is
                # down.
                try:
                    if not self._connect_internal(connect_timeout=2.0):
                        return {"success": False, "error": "not_connected"}
                except Exception as exc:
                    return {"success": False, "error": "not_connected",
                            "message": str(exc)}

            # Per-command timeout defaults, matching the daemon side.
            # connect/reconnect deliberately keep the full default: with a
            # job loaded they can ride a firmware reboot (stale-slot
            # pre-clear) inside the daemon before answering.
            if timeout is None:
                if cmd in ("status", "get_status"):
                    timeout = 10.0
                elif cmd in ("reset", "abort"):
                    # These ride a firmware reboot + reconnect:
                    # ~1-2 s typical, 8 s bounded wait inside the daemon, which
                    # itself watchdogs them at 30 s; stay above that.
                    timeout = 35.0
                elif cmd == "load_job":
                    timeout = 300.0
                else:
                    timeout = self.timeout

            # Each command carries a monotonically increasing id so the
            # matching response can be identified.
            self._msg_id += 1
            msg_id = self._msg_id
            msg = {"cmd": cmd, "id": msg_id, **kwargs}

            try:
                line = json.dumps(msg) + "\n"
                if not self._send_raw(line.encode("utf-8")):
                    return {"success": False, "error": "send_failed"}
            except Exception as e:
                return {"success": False, "error": f"send_error: {e}"}

            start_time = time.monotonic()

            # Read until the response with the matching id arrives, or
            # the overall timeout elapses.
            while True:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    return {"success": False, "error": "timeout"}

                resp = self._recv_message(timeout=min(remaining, 5.0))

                if resp is None:
                    if not self._connected:
                        return {"success": False, "error": "connection_closed"}
                    if time.monotonic() - start_time >= timeout:
                        return {"success": False, "error": "timeout"}
                    continue

                msg_type = resp.get("type")

                if msg_type == "response" and resp.get("id") == msg_id:
                    return resp
                # Asynchronous messages may interleave with the response.
                if msg_type == "event":
                    self._handle_event(resp)
                    continue
                if msg_type == "firmware_log":
                    self._handle_firmware_log(resp)
                    continue
                if msg_type == "keepalive":
                    continue

                logger.debug("Ignoring unexpected message while waiting response: %r", resp)

    def _command_or_raise(self, cmd: str, timeout: Optional[float] = None, **kwargs) -> Dict:
        """Like send_command(), but raise PaintressdClientError on failure."""
        resp = self.send_command(cmd, timeout=timeout, **kwargs)
        if not resp.get("success", False):
            error = resp.get("error", "unknown_error")
            message = resp.get("message")
            # Include the daemon's human-readable detail (which swath, why the
            # arm did not happen: pipeline_timeout vs a firmware arm-reject code)
            # so the failure is diagnosable from the Klipper console.
            detail = f"{error}: {message}" if message else str(error)
            raise PaintressdClientError(f"Command '{cmd}' failed: {detail}")
        return resp

    def _extract_data(self, resp: Dict):
        """Return resp["data"] when present, otherwise the response itself."""
        if not isinstance(resp, dict):
            raise PaintressdClientError(f"Invalid daemon response type: {type(resp)!r}")
        data = resp.get("data")
        return data if data is not None else resp

    def _extract_payload_info(self, resp: Dict) -> Dict:
        """Pull the payload metadata block out of a load_job response.

        The daemon may place the payload info either under "data" or at
        the top level, so both are inspected.
        """
        candidates = []

        data = resp.get("data")
        if isinstance(data, dict):
            candidates.append(data)

        if isinstance(resp, dict):
            candidates.append(resp)

        for candidate in candidates:
            if isinstance(candidate, dict) and (
                "metadata" in candidate or "passes" in candidate
            ):
                return candidate

        raise PaintressdClientError(
            f"load_job response does not contain payload info: {resp}"
        )

    # -------------------------------------------------------------------------
    # Asynchronous messages
    # -------------------------------------------------------------------------

    def _handle_event(self, msg: dict):
        """Queue an async `event` message for poll_event(), and log it.

        Events dispatched here by send_command's wait loop (they interleave
        with a command's response) go into the inbox in arrival order, so a
        completion/fault emitted during another round-trip is not lost; the
        next poll_event() finds it. keepalive / firmware_log are not events
        and never reach this path.
        """
        logger.debug("Event: %s - %s", msg.get("event", "unknown"), msg.get("data", {}))
        self._event_inbox.append(msg)

    @staticmethod
    def _event_matches(msg: dict, events: Sequence[str],
                       swath_id: Optional[int]) -> bool:
        """True if `msg` is one of `events` and (if asked) the right swath.

        A `swath_id` filter matches events that carry that id; events with no
        swath_id (daemon-level faults such as pipeline_error / serial_lost)
        match by name alone, so a whole-pipeline failure is never missed while
        waiting on a specific swath.
        """
        if msg.get("event") not in events:
            return False
        if swath_id is not None:
            msg_swath = msg.get("swath_id")
            if msg_swath is not None and msg_swath != swath_id:
                return False
        return True

    def _match_from_inbox(self, events: Sequence[str],
                          swath_id: Optional[int]) -> Optional[dict]:
        """Pop and return the first inbox event matching the filter, else None.

        Non-matching events (e.g. a completion for another swath) stay queued
        in order for a later poll. Caller must hold self._lock.
        """
        for i, msg in enumerate(self._event_inbox):
            if self._event_matches(msg, events, swath_id):
                del self._event_inbox[i]
                return msg
        return None

    def clear_events(self) -> int:
        """Drop every parked async event; returns how many were dropped.

        Call at a print boundary (start of a print, or right after an abort):
        events parked during earlier command round-trips (a stale
        print_complete or trigger_timeout from a previous run) must never
        satisfy or fail a wait that belongs to the NEXT print, since swath
        ids repeat on every job.
        """
        with self._lock:
            dropped = len(self._event_inbox)
            self._event_inbox.clear()
            return dropped

    def poll_event(self, events: Sequence[str], swath_id: Optional[int] = None,
                   max_block: float = 0.05) -> Optional[dict]:
        """Return the next matching async event, or None if none is ready.

        A short, reactor-friendly poll (never blocks longer than `max_block`),
        so the Klipper plugin can wait on print_complete / trigger_timeout /
        print_error by event instead of polling status and counting
        completions. `events` is the set of event names to accept; `swath_id`,
        when given, additionally requires that id (events without one match by
        name; see _event_matches).

        First drains anything already queued in the inbox, then reads at most
        one message off the socket: an event is matched or parked, a
        firmware_log / keepalive is handled and skipped, an orphan response
        (its waiter already gave up) is ignored, and a timeout returns None.
        """
        with self._lock:
            # An event received earlier (during a command round-trip, or read
            # ahead by a previous poll) satisfies the wait immediately.
            hit = self._match_from_inbox(events, swath_id)
            if hit is not None:
                return hit

            if not self._connected or not self._socket:
                return None

            msg = self._recv_message(timeout=max_block)
            if msg is None:
                return None

            msg_type = msg.get("type")
            if msg_type == "event":
                if self._event_matches(msg, events, swath_id):
                    return msg
                # Some other event: park it in order for a later poll.
                self._event_inbox.append(msg)
                return None
            if msg_type == "firmware_log":
                self._handle_firmware_log(msg)
                return None
            if msg_type == "keepalive":
                return None

            # An orphan `response` (its send_command already returned/timed out)
            # or anything unexpected: drop it, same as the response wait loop.
            logger.debug("poll_event ignoring message: %r", msg)
            return None

    def _handle_firmware_log(self, msg: dict):
        """Log an async `firmware_log` message."""
        logger.debug("Firmware log: %s", msg.get("text", ""))

    # -------------------------------------------------------------------------
    # Status / daemon info
    # -------------------------------------------------------------------------

    def get_daemon_status(self) -> Dict:
        """Return the daemon-level status (version, connection, statistics)."""
        resp = self._command_or_raise("status")
        data = self._extract_data(resp)
        if not isinstance(data, dict):
            raise PaintressdClientError(f"Invalid daemon status payload: {data}")
        return data

    def status_socket(self) -> Dict:
        """Alias of get_daemon_status() expected by the Klipper plugin."""
        return self.get_daemon_status()

    def reset(self) -> bool:
        """Reset the firmware (= chip reboot).

        The daemon rides the board's reboot + USB re-enumeration (~1-2 s) and
        returns once it re-identified the firmware, hence the longer timeout.
        """
        self._command_or_raise("reset")
        return True

    def abort(self) -> bool:
        """Emergency stop, ready for retry.

        The daemon sends the firmware ABORT (electrical kill-switch: DAC off
        and latched, ink stops immediately) followed by a RESET (a chip
        reboot whose clean boot clears the latch and the slots), and it stops
        its streaming pipeline. Returns once the board is back (~1-2 s).
        """
        self._command_or_raise("abort")
        return True

    # -------------------------------------------------------------------------
    # Job commands
    # -------------------------------------------------------------------------

    def load_job(self, filepath: str) -> Dict:
        """Load an encoded job file into the daemon; return its payload info."""
        resp = self._command_or_raise("load_job", filepath=filepath)
        self.job_loaded = True
        return self._extract_payload_info(resp)

    def unload_job(self) -> bool:
        """Unload the job currently held by the daemon."""
        self._command_or_raise("unload_job")
        self.job_loaded = False
        return True

    # -------------------------------------------------------------------------
    # Swath / print commands
    # -------------------------------------------------------------------------

    def print_swath(self, swath_id: int, line_delay_us: int) -> bool:
        """Arm a swath for printing (fires on the hardware start trigger).

        line_delay_us is the column firing interval for this swath: the
        timing travels with each print; there is no separate
        set_timing step or device-side timing state.
        """
        self._command_or_raise("print", swath_id=swath_id,
                               line_delay_us=line_delay_us)
        return True

    def purge(
        self,
        channel: int = 0,
        pulses: int = 10,
    ) -> bool:
        """Run a purge cycle to clear/prime nozzles on a channel."""
        self._command_or_raise(
            "purge",
            channel=channel,
            pulses=pulses,
        )
        return True

    # -------------------------------------------------------------------------
    # Serial link
    # -------------------------------------------------------------------------

    def connect_serial(self, port: str) -> bool:
        """Ask the daemon to open the serial link to the controller board."""
        self._command_or_raise("connect", port=port)
        self.device_connected = True
        return True

    def disconnect_serial(self) -> bool:
        """Ask the daemon to close the serial link to the controller board.

        The daemon command is `disconnect`. Best-effort: a failure is logged,
        not raised, and the link is considered closed either way.
        """
        resp = self.send_command("disconnect", timeout=10.0)
        if not resp.get("success", False):
            logger.warning(
                "Daemon did not confirm serial disconnect; error=%r",
                resp.get("error"),
            )
        self.device_connected = False
        return True

    # Note: set_timing is not used: the column firing
    # interval travels inside each print_swath() call instead of being
    # device state that a reset could silently revert.

    # -------------------------------------------------------------------------
    # Context manager
    # -------------------------------------------------------------------------

    def __enter__(self):
        """Context-manager entry: connect and return the client."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context-manager exit: disconnect."""
        self.disconnect()
        return False


if __name__ == "__main__":
    # Standalone connectivity smoke test: connect to a running daemon, show its
    # status, and optionally open the serial link and load a job (which starts
    # the daemon's streaming pipeline). It does NOT run a print; a real print
    # needs Klipper gantry motion and the hardware start trigger, so that path
    # lives in the Klipper plugin, not here.
    import argparse

    parser = argparse.ArgumentParser(
        description="Paintress daemon connectivity smoke test"
    )
    parser.add_argument("--host", default="localhost", help="Daemon host")
    parser.add_argument("--port", "-p", type=int, default=9000, help="Daemon port")
    parser.add_argument("--serial", "-s", default=None,
                        help="Serial port for the daemon to open")
    parser.add_argument("--job", "-j", default=None,
                        help="Job file to load (starts the daemon pipeline)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        with PaintressdClient(host=args.host, port=args.port) as client:
            print(f"Connected to daemon v{client.daemon_version}")
            print(
                f"Device: {'connected' if client.device_connected else 'disconnected'}"
            )

            if args.serial:
                client.connect_serial(args.serial)
                print(f"Serial opened: {args.serial}")

            if args.job:
                info = client.load_job(args.job)
                meta = info.get("metadata", info)
                print(
                    f"Job loaded ({meta.get('total_passes')} passes); "
                    "the daemon is now streaming it"
                )

            status = client.get_daemon_status()
            print(f"Daemon status: {json.dumps(status, indent=2, ensure_ascii=False)}")
            print("\nDone.")

    except Exception as e:
        print(f"Fatal error: {e}")
        raise
