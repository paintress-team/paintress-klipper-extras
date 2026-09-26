# SPDX-FileCopyrightText: 2026 paintress-team
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Paintress Klipper extra: inkjet printing on a Klipper-driven gantry.

This module is stage 4 of the Paintress pipeline. Klipper loads it
because it lives in `klippy/extras/`; the file name becomes the config
section name, so a printer enables it with a `[paintress]` section.

It registers a set of PAINTRESS_* G-code commands that:

  * connect to the Paintress daemon over TCP and, through it, to the
    controller board firmware over serial;
  * load an encoded print job ("payload") into the daemon;
  * derive the print area, either from the loaded payload or from the
    Klipper `exclude_object` polygons;
  * drive the swath-by-swath print loop, moving the gantry while the
    firmware fires the printhead nozzles in sync with the X position.

Pipeline context:

    encoder --> .json job --> paintress-daemon <-- this module --> Klipper motion

The module never talks to the firmware directly: all firmware traffic
goes through the daemon via PaintressdClient (see paintressd_client.py).
"""

import json
import logging
from pathlib import Path
from .paintressd_client import PaintressdClient

# Maps human-readable channel names accepted by PAINTRESS_* commands
# that take a CHANNEL argument onto the numeric channel index used by
# the firmware. "all" (0) addresses every channel at once.
#
# The order MUST match the firmware's channel_mask_id_t enum
# (paintress-firmware/engine/channels.h): all, yellow, black, light_cyan,
# light_magenta, magenta, cyan. Keep the two in lockstep.
#
# The INDEX of a name is the firmware's and never varies. Which names a given
# head actually has does vary: a four-ink head has no light cyan, and purging
# it drives an all-zero mask: the head powers up, fires nothing and powers
# down, silently. A [paintress_head] section lists the names that head really
# carries, and anything outside that list is refused instead of no-opped.
color_channels = {
    "all": 0,
    "yellow": 1,
    "black": 2,
    "light_cyan": 3,
    "light_magenta": 4,
    "magenta": 5,
    "cyan": 6,
}

# The firmware's line delay is a 16-bit value on the wire (microseconds).
FIRMWARE_MAX_LINE_DELAY_US = 65535

# Approximate lower bound of the firmware's per-line data path (fire the
# column + DMA the next one; see https://paintress.dev/concepts/firing-and-timing/):
# below this the fixed firing grid cannot keep up. Empirical, so it only
# triggers a warning, not an error.
FIRMWARE_MIN_LINE_DELAY_US = 46


def load_config(config):
    """Klipper entry point: build the module for a [paintress] section."""
    return Paintress(config)


class Paintress:
    """Klipper extra that orchestrates inkjet printing.

    A single instance is created per `[paintress]` config section. It
    holds the configuration, the connection state, the currently loaded
    payload, and the runtime state of an in-progress print.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.toolhead = None  # resolved at klippy:connect

        # --- CONFIGURATION (read from the [paintress] section) --- #

        # TCP port of the Paintress daemon (assumed to run on localhost).
        self.socket_port = config.getint("socket_port", 9000)

        # When True (default), open the daemon socket and the serial link to
        # the board automatically once Klipper is ready, so neither
        # PAINTRESS_CONNECT_SOCKET nor PAINTRESS_CONNECT_SERIAL has to be run
        # by hand (or from PRINT_START). Best-effort: if the daemon is not up
        # yet, startup is not aborted; a warning is printed and the manual
        # commands still work.
        self.auto_connect = config.getboolean("auto_connect", True)

        # Serial device the daemon should open to reach the controller
        # board. Passed through to the daemon on connect.
        self.serial_port = config.get(
            "serial_port",
            "/dev/serial/by-id/usb-Raspberry_Pi_Pico_9EACBDA2824791EA-if00",
        )

        # Printhead offset relative to the toolhead reference point.
        self.x_offset = config.getfloat("x_offset", 0.0)
        self.y_offset = config.getfloat("y_offset", 0.0)

        # Which printhead is fitted, naming an optional [paintress_head <name>]
        # section. It carries only what a payload cannot state: where nozzle 0
        # sits relative to the toolhead (mechanical, changes when the head is
        # swapped) and which purge channels the head really has.
        #
        # It deliberately does NOT drive positioning. Where the head must
        # travel is a property of the job, and every job already carries its
        # own pass schedule: deriving travel from a config field would be a
        # third declaration of a fact the RIP and the daemon already agree on,
        # and three declarations of one fact is one that drifts. The daemon
        # already refuses a job packed for the wrong head.
        self.head_name = config.get("head", None)
        self.head_channels = set(color_channels)

        # Root directory under which PAINTRESS_SET_DIRECTORY may select a
        # payload search directory. Acts as a sandbox: payloads cannot be
        # opened from outside this tree.
        self.base_path = config.get("base_path", "/home/pi/printer_data/paintress/")

        # z_offset is how far the toolhead is raised to bring the 2D
        # printhead to its working height. The ink outlet always sits
        # above the 3D-printer nozzle (the toolhead reference point), so
        # z_offset bundles the fixed nozzle-to-outlet distance with the
        # small gap the outlet keeps above the material. Because that gap
        # clears the material, the same raise doubles as travel
        # clearance, so no separate lift move is needed.
        self.z_offset = config.getfloat("z_offset", 1.0)
        self.z_lift_speed = config.getfloat("z_lift_speed", 5.0)

        # Applied last, so a head section overrides the offsets read above.
        self._load_head_section(config)

        # Travel (non-printing) motion limits.
        self.move_speed = config.getint("move_speed", 300)
        self.move_accel = config.getfloat("move_accel", 5000)

        # Printing-pass motion limits (the speed used while firing).
        self.print_speed = config.getint("print_speed", 200)
        self.print_accel = config.getfloat("print_accel", 5000)

        # Verbose console output. When False (default), routine per-move
        # "OK G1 ..." chatter is suppressed; errors and key status still show.
        self.verbose = config.getboolean("verbose", False)

        # Distance travelled before the print area so the head reaches a
        # constant speed before the first column is fired.
        self.start_overscan = config.getfloat("start_overscan", 10.0)

        # Safety margin past the print area for any trailing ink drop.
        self.end_overscan = config.getfloat("end_overscan", 10.0)

        # Extra margin added around the exclude_object bounds when
        # printing over an existing 3D-printed layer.
        self.extra_margin = config.getint("extra_margin", 2)

        # --- START TRIGGER --- #
        #
        # The firmware has a single firing method: it always arms and starts on
        # a hardware rising edge. The plugin splits the printing sweep at the
        # trigger position and raises the line into the controller board there
        # with SET_PIN, so the firmware fires on that edge, emitted by the
        # motion MCU at the exact print_time the X axis reaches the split,
        # carrying none of the TCP / USB jitter that otherwise shifts each pass.

        # Name of the [output_pin] section wired to the controller board
        # trigger input. The plugin does NOT own a pin; it drives this
        # existing output_pin via SET_PIN. Required.
        self.trigger_output_pin = config.get("trigger_output_pin", None)

        # Distance (mm) the X axis travels from the pass start before the
        # trigger fires. Defaults to start_overscan so firing begins right
        # at the print area. Must be >= the acceleration distance so the
        # head is already at constant speed when the edge is emitted.
        self.trigger_distance = config.getfloat(
            "trigger_distance", self.start_overscan, minval=0.0
        )

        if not self.trigger_output_pin:
            raise config.error(
                "[paintress] trigger_output_pin must be set "
                "(the [output_pin] wired to the controller board trigger)"
            )

        # --- RUNTIME STATE --- #

        # Resolved base_path; set at klippy:connect (pathlib.Path).
        self.base_directory = None

        # Current payload search directory; set at klippy:connect and
        # changed by PAINTRESS_SET_DIRECTORY (pathlib.Path).
        self.search_directory = None

        # Absolute path of the open payload; set by PAINTRESS_OPEN_PAYLOAD.
        self.payload_absolute_path = None

        # Interval between nozzle column firings, derived from the
        # payload DPI and print_speed (microseconds).
        self.column_fire_interval_us = 0

        # Print geometry and pass table extracted from the payload.
        self.print_settings = self._empty_print_settings()

        # Index of the swath currently being printed (0-based).
        self.current_swath_index = 0

        # First point where ink should be jetted (image bottom-left:
        # swath 0 carries the bottom of the image, since Y steps up each
        # pass), in machine coordinates.
        self.print_origin = [0.0, 0.0]

        # print_origin plus the head's mechanical offsets: the machine
        # position at which a pass offset of 0 falls. Every per-swath Y is
        # this plus that swath's scheduled offset, which is the only place
        # those offsets are added; print_bounds must not be used as a base
        # for them, because it already contains the schedule's extremes.
        self.print_anchor = None

        # Travel rectangle of the print: the rectangle the toolhead actually
        # covers, X including the overscan and Y spanning the payload's pass
        # schedule. None until get_bounds() computes it. A dummy rectangle
        # here made status() validate [[0,0],[0,0]] and report spurious
        # bounds errors.
        self.print_bounds = None

        # Toolhead position saved before a print, restored afterwards.
        self.stored_position = None
        self.is_printing = False

        # Connection / configuration flags.
        self.is_serial_connected = False
        self.is_socket_connected = False
        self.is_board_configured = False

        # Result of the most recent bounds validation.
        self.last_bounds_valid = None
        self.last_bounds_errors = []

        # PaintressdClient instance; created at klippy:connect.
        self.client = None

        self.register_commands()
        self.printer.register_event_handler("klippy:shutdown", self.on_shutdown)
        self.printer.register_event_handler("klippy:connect", self.on_connect)
        self.printer.register_event_handler("klippy:ready", self.on_ready)

    # --- COMMAND REGISTRATION --- #

    def _load_head_section(self, config):
        """Apply the [paintress_head <name>] section named by `head`, if any.

        Without `head:` nothing changes: the global offsets stand and every
        channel name is accepted, which is exactly how the plugin behaved
        before head sections existed. An existing config keeps working
        untouched.

        The section is parsed by the paintress_head module, not here, so
        that a config may declare every head the machine owns and select
        one; see that module for why an inline parse cannot.
        """
        if not self.head_name:
            return

        section = f"paintress_head {self.head_name}"
        if not config.has_section(section):
            raise config.error(
                f"[paintress] head: {self.head_name} has no matching "
                f"[{section}] section"
            )
        head = self.printer.load_object(config, section)

        # None means the section is silent about that value, so what the
        # [paintress] section said stands.
        if head.x_offset is not None:
            self.x_offset = head.x_offset
        if head.y_offset is not None:
            self.y_offset = head.y_offset
        if head.z_offset is not None:
            self.z_offset = head.z_offset
        if head.channels is not None:
            self.head_channels = head.channels

    def register_commands(self):
        """Register every PAINTRESS_* G-code command with Klipper."""
        reg = self.gcode.register_command
        reg("PAINTRESS_CONNECT_SERIAL", self.cmd_connect_serial,
            desc="Ask the daemon to open the serial link to the controller board")
        reg("PAINTRESS_DISCONNECT_SERIAL", self.cmd_disconnect_serial,
            desc="Ask the daemon to close the serial link to the controller board")
        reg("PAINTRESS_CONNECT_SOCKET", self.cmd_connect_socket,
            desc="Open the TCP connection to the Paintress daemon")
        reg("PAINTRESS_DISCONNECT_SOCKET", self.cmd_disconnect_socket,
            desc="Close the TCP connection to the Paintress daemon")
        reg("PAINTRESS_STATUS", self.cmd_status,
            desc="Report the combined daemon and plugin status")
        reg("PAINTRESS_SET_DIRECTORY", self.cmd_set_directory,
            desc="Select the payload search directory (under base_path)")
        reg("PAINTRESS_OPEN_PAYLOAD", self.cmd_open_payload,
            desc="Load an encoded print job into the daemon")
        reg("PAINTRESS_CLOSE_PAYLOAD", self.cmd_close_payload,
            desc="Unload the current print job from the daemon")
        reg("PAINTRESS_PURGE", self.cmd_purge,
            desc="Fire nozzles to clear or prime ink")
        reg("PAINTRESS_TEST_TRIGGER", self.cmd_test_trigger,
            desc="Pulse the trigger output pin via SET_PIN (scope test)")
        reg("PAINTRESS_SET_TRIGGER_DISTANCE", self.cmd_set_trigger_distance,
            desc="Get/set the hardware-trigger distance at runtime (mm)")
        reg("PAINTRESS_GET_BOUNDS", self.cmd_get_bounds,
            desc="Compute the print bounds from the payload or exclude_object")
        reg("PAINTRESS_PRINT", self.cmd_print,
            desc="Run the full swath-by-swath print")

    # --- LIFECYCLE --- #

    def on_connect(self):
        """klippy:connect handler: resolve the toolhead and base
        directory, and create the daemon client."""
        self.toolhead = self.printer.lookup_object("toolhead")

        try:
            self.base_directory = Path(self.base_path).resolve()
            self.search_directory = self.base_directory

            if not self.base_directory.exists():
                raise self.printer.command_error(
                    f"Configured base_path does not exist: {self.base_directory}"
                )
            if not self.base_directory.is_dir():
                raise self.printer.command_error(
                    f"Configured base_path is not a directory: {self.base_directory}"
                )

            self.client = PaintressdClient(host="localhost", port=self.socket_port)
        except Exception as e:
            self._fail("Failed during klippy:connect", exc=e)

    def on_ready(self):
        """klippy:ready handler: open the daemon socket and the board serial
        link automatically, so a print needs no manual connect step.

        Best-effort: this runs at startup, when the daemon may not be up yet
        (it starts independently of Klipper). A failure is reported to the
        console and logged, never raised: aborting klippy:ready would take
        the whole printer down just because the 2D side is offline. The manual
        PAINTRESS_CONNECT_SOCKET / PAINTRESS_CONNECT_SERIAL commands remain the
        recovery path. Disable with `auto_connect: false`.
        """
        if not self.auto_connect:
            return

        try:
            self.connect_socket()
            self.connect_serial()
            self.gcode.respond_info(
                "PAINTRESS: auto-connected (daemon socket + board serial)"
            )
        except Exception as e:
            logging.exception("PAINTRESS auto-connect failed")
            self.gcode.respond_info(
                "WARNING PAINTRESS: auto-connect failed "
                f"({e}); run PAINTRESS_CONNECT_SOCKET / "
                "PAINTRESS_CONNECT_SERIAL once the daemon is up"
            )

    def on_shutdown(self):
        """klippy:shutdown handler: stop printing and release every
        connection, collecting (rather than raising) any errors."""
        errors = []

        for action_name, action in (
            ("emergency_stop", self.emergency_stop),
            ("disconnect_serial", self.disconnect_serial),
            ("disconnect_socket", self.disconnect_socket),
        ):
            try:
                action()
            except Exception as e:
                errors.append(f"{action_name}: {e}")
                logging.exception("Shutdown action failed: %s", action_name)

        self.is_printing = False

        if errors:
            self.gcode.respond_info("PAINTRESS shutdown warnings: " + " | ".join(errors))

    def emergency_stop(self):
        """Bring the board to a safe, de-energized state (best-effort).

        Called from klippy:shutdown, so it must never raise. RESET is a chip
        reboot: the DAC pins drop before the reboot, and the
        clean boot clears the DAC latch, the slots and all other state, so a
        shutdown leaves nothing energized and the board recoverable. The
        daemon rides the expected USB drop and answers once the board is back.

        The timeout is deliberately shorter than the client's 35 s default
        for reset: the goal here is de-energizing, and a shutdown must not
        hang half a minute on a board that is not coming back (the daemon's
        own bounded wait is 8 s).
        """
        self.is_printing = False
        if self.client is None:
            return
        try:
            resp = self.client.send_command("reset", timeout=15.0)
            if not resp.get("success", False):
                logging.error("emergency_stop: reset failed: %r",
                              resp.get("error"))
        except Exception:
            logging.exception("emergency_stop: reset failed")

    # --- MOVEMENT --- #

    def _get_position(self):
        """Return the current toolhead position."""
        return self._require_toolhead().get_position()

    def _require_stored_position(self):
        """Return the saved toolhead position, or fail if there is none."""
        self._ensure(
            self.stored_position is not None,
            "No stored position available",
        )
        self._ensure(
            len(self.stored_position) >= 3,
            f"Invalid stored position: {self.stored_position}",
        )
        return self.stored_position

    def _g1(self, x=None, y=None, z=None, speed=None, accel=None):
        """Issue a G1 move.

        When `accel` is given the acceleration limit is set for this
        move only and restored to its previous value immediately after.
        """
        toolhead = self._require_toolhead()
        st = toolhead.get_status(self._eventtime())
        prev_accel = st["max_accel"]

        parts = []
        if x is not None:
            parts.append(f"X{float(x):.3f}")
        if y is not None:
            parts.append(f"Y{float(y):.3f}")
        if z is not None:
            parts.append(f"Z{float(z):.3f}")

        if speed is not None:
            self._ensure(speed > 0, f"Invalid move speed: {speed}")
            # Klipper feedrate F is in mm/min; config speeds are mm/s.
            parts.append(f"F{float(speed) * 60:.0f}")

        if not parts:
            self._fail("_g1 called without any axis or speed change")

        cmds = []
        if accel is not None:
            self._ensure(accel > 0, f"Invalid accel: {accel}")
            cmds.append(f"SET_VELOCITY_LIMIT ACCEL={float(accel):.0f}")

        cmds.append("G1 " + " ".join(parts))

        if accel is not None:
            cmds.append(f"SET_VELOCITY_LIMIT ACCEL={float(prev_accel):.0f}")

        self.gcode.run_script_from_command("\n".join(cmds))
        if self.verbose:
            self.gcode.respond_info(f"OK G1 {' '.join(parts)}")

    def _warn_if_trigger_too_early(self):
        """Warn (but proceed) if the trigger fires before the head reaches
        constant speed. Placement is only uniform once cruising, since the
        firmware fires on a time grid."""
        accel_dist = (self.print_speed ** 2) / (2.0 * self.print_accel)
        if self.trigger_distance < accel_dist:
            self.gcode.respond_info(
                f"WARNING PAINTRESS: trigger_distance {self.trigger_distance:.3f}mm "
                f"is below the acceleration distance {accel_dist:.3f}mm; the first "
                f"columns fire before constant speed is reached"
            )

    def cmd_set_trigger_distance(self, gcmd):
        """Get or set the hardware-trigger distance (mm) at runtime.

        With no VALUE, reports the current value. The override lasts until the
        next restart (Klipper config is not self-editing); persist it from a
        macro / PRINT_START or a [save_variables] entry if you need it to
        survive a restart.
        """
        def _do():
            value = gcmd.get_float("VALUE", None, minval=0.0)
            if value is not None:
                self.trigger_distance = value
                self._warn_if_trigger_too_early()
            return f"trigger_distance = {self.trigger_distance:.3f}mm"

        self._run_cmd(gcmd, "PAINTRESS_SET_TRIGGER_DISTANCE", _do)

    # --- UTILS --- #

    def _screen_msg(self, gcmd, msg):
        """Echo a message back to the Klipper console."""
        gcmd.respond_info(msg)

    def _run_cmd(self, gcmd, cmd_name, fn, success_details=None):
        """Run a command body, reporting OK/ERROR to the console.

        Exceptions from `fn` are caught, logged, and turned into an
        "ERROR ..." console message so a failing command never aborts
        the Klipper session.
        """
        try:
            result = fn()

            msg = f"OK {cmd_name}"
            if success_details:
                msg += f": {success_details}"
            elif isinstance(result, str) and result.strip():
                msg += f": {result}"

            self._screen_msg(gcmd, msg)
            return result

        except Exception as e:
            logging.exception("Command %s failed", cmd_name)
            self._screen_msg(gcmd, f"ERROR {cmd_name}: {e}")
            return None

    def _empty_print_settings(self):
        """Return a blank print-settings structure (no payload loaded)."""
        return {
            "swaths": 0,
            "passes": [],
            "dimensions": {
                "width_mm": 0,
                "padded_width_mm": 0,
                "height_mm": 0,
            },
            # Where the head must actually go, relative to the print anchor.
            # Derived from the pass schedule, not from the image: a head whose
            # inks are stacked along Y has to start BELOW the image so its
            # highest nozzles reach the first row, so y_min is negative there
            # and the head never reaches the top of the image itself.
            "travel": {"y_min_mm": 0.0, "y_max_mm": 0.0, "x_width_mm": 0.0},
            "dpi": 0,
        }

    def _reset_payload_state(self):
        """Clear all state tied to an open payload."""
        self.payload_absolute_path = None
        self.column_fire_interval_us = 0
        self.current_swath_index = 0
        self.print_settings = self._empty_print_settings()
        self.print_anchor = None
        self.print_bounds = None
        self.is_board_configured = False
        self.last_bounds_valid = None
        self.last_bounds_errors = []

    def _reset_runtime_state(self):
        """Clear per-print runtime state once a print ends."""
        self.current_swath_index = 0
        self.is_printing = False
        self.stored_position = None

    def _require_print_bounds(self):
        """Validate that print_bounds is a well-formed numeric rectangle."""
        self._ensure(
            self.print_bounds is not None,
            "No print bounds computed yet (run PAINTRESS_GET_BOUNDS, or "
            "PAINTRESS_PRINT which derives them)",
        )
        self._ensure(
            isinstance(self.print_bounds, list) and len(self.print_bounds) == 2,
            f"Invalid print_bounds structure: {self.print_bounds}",
        )

        for idx, point in enumerate(self.print_bounds):
            self._ensure(
                isinstance(point, (list, tuple)) and len(point) >= 2,
                f"Invalid print_bounds[{idx}]: {point}",
            )
            try:
                float(point[0])
                float(point[1])
            except Exception:
                self._fail(
                    f"print_bounds[{idx}] contains non-numeric values: {point}"
                )

        return self.print_bounds

    def _get_bool_arg(self, gcmd, name, default=False):
        """Parse a boolean G-code argument (accepts 1/0, true/false, ...)."""
        value = gcmd.get(name, None)
        if value is None:
            return default

        value = value.strip().lower()
        if value in ("1", "true", "on", "yes"):
            return True
        if value in ("0", "false", "off", "no"):
            return False

        raise gcmd.error(
            f"{name} must be one of: 1, 0, true, false, on, off, yes, no"
        )

    def _get_channel_arg(self, gcmd, name="CHANNEL", default=0):
        """Parse a CHANNEL G-code argument.

        Accepts either a numeric index (0..6) or one of the colour names
        in `color_channels`, restricted to the names the configured head
        actually has.

        A name this head does not carry is refused rather than passed through:
        the firmware would drive an all-zero mask for it, so the head would
        power up, fire nothing and power down, and the operator would be left
        watching a purge that silently did nothing.
        """
        raw = gcmd.get(name, None)
        if raw is None:
            return default

        raw = raw.strip().lower()
        available = {n: color_channels[n] for n in sorted(
            self.head_channels, key=lambda n: color_channels[n])}

        try:
            value = int(raw)
        except ValueError:
            # Not a number: try to resolve it as a colour name.
            normalized = raw.replace("-", "_").replace(" ", "_")
            if normalized in available:
                value = available[normalized]
            elif normalized in color_channels:
                self._fail(
                    f"{name}='{raw}' is not a channel of the configured head"
                    + (f" ({self.head_name})" if self.head_name else "")
                    + f". This head has: {', '.join(available)}",
                    gcmd=gcmd,
                )
            else:
                self._fail(
                    f"Invalid {name}='{raw}'. Use a number or one of: "
                    f"{', '.join(available)}",
                    gcmd=gcmd,
                )

        if value not in available.values():
            self._fail(
                f"{name} out of range: {value}. This head's channels are "
                + ", ".join(f"{n}={i}" for n, i in available.items()),
                gcmd=gcmd,
            )

        return value

    def _eventtime(self):
        """Return the current reactor (monotonic) time."""
        return self.reactor.monotonic()

    def _fail(self, msg, gcmd=None, exc=None):
        """Log an error and raise it as a Klipper command error."""
        if exc is not None:
            msg = f"{msg}: {exc}"
            logging.exception(msg)
        else:
            logging.error(msg)

        if gcmd is not None:
            raise gcmd.error(msg)
        raise self.printer.command_error(msg)

    def _ensure(self, condition, msg, gcmd=None):
        """Raise via _fail() unless `condition` holds."""
        if not condition:
            self._fail(msg, gcmd=gcmd)

    def _require_toolhead(self):
        """Return the toolhead, or fail if it is not initialised yet."""
        self._ensure(self.toolhead is not None, "Toolhead is not initialized")
        return self.toolhead

    def _require_client(self):
        """Return the daemon client, or fail if it is not initialised."""
        self._ensure(self.client is not None, "PaintressdClient is not initialized")
        return self.client

    def _require_base_directory(self):
        """Return the base directory, or fail if it is not initialised."""
        self._ensure(
            self.base_directory is not None,
            "Base directory is not initialized",
        )
        return self.base_directory

    def _require_search_directory(self):
        """Return the payload search directory, validating it still exists."""
        self._ensure(
            self.search_directory is not None,
            "Search directory is not initialized",
        )
        self._ensure(
            self.search_directory.exists(),
            f"Search directory does not exist: {self.search_directory}",
        )
        self._ensure(
            self.search_directory.is_dir(),
            f"Search directory is not a directory: {self.search_directory}",
        )
        return self.search_directory

    def _require_payload_loaded(self):
        """Validate that a well-formed payload is currently open."""
        self._ensure(
            self.payload_absolute_path is not None,
            "No payload is currently open",
        )
        self._ensure(
            isinstance(self.print_settings, dict),
            "print_settings is invalid",
        )

        swaths = self.print_settings["swaths"]
        passes = self.print_settings["passes"]
        dims = self.print_settings["dimensions"]
        dpi = self.print_settings["dpi"]

        self._ensure(swaths > 0, "Invalid payload: swaths=0")
        self._ensure(isinstance(passes, list), "Invalid payload: passes must be a list")
        self._ensure(len(passes) > 0, "Invalid payload: passes list is empty")
        self._ensure(
            len(passes) == swaths,
            f"Invalid payload: total_passes={swaths} but passes_len={len(passes)}",
        )
        self._ensure(dpi > 0, "Invalid payload: dpi must be > 0")

        self._ensure("width_mm" in dims, "Invalid payload: missing dimensions.width_mm")
        self._ensure(
            "padded_width_mm" in dims,
            "Invalid payload: missing dimensions.padded_width_mm",
        )
        self._ensure(
            "height_mm" in dims,
            "Invalid payload: missing dimensions.height_mm",
        )

        self._ensure(
            float(dims["width_mm"]) >= 0,
            f"Invalid payload width_mm: {dims['width_mm']}",
        )
        self._ensure(
            float(dims["padded_width_mm"]) >= 0,
            f"Invalid payload padded_width_mm: {dims['padded_width_mm']}",
        )
        self._ensure(
            float(dims["height_mm"]) >= 0,
            f"Invalid payload height_mm: {dims['height_mm']}",
        )

    def _require_not_printing(self):
        """Fail if a print is already running."""
        self._ensure(
            not self.is_printing,
            "Printer is already running an inkjet print",
        )

    def _require_print_ready(self):
        """Validate everything that must be in place before a print."""
        self._require_toolhead()
        self._require_client()
        self._require_search_directory()
        self._require_payload_loaded()

    def _resolve_under_base(self, user_path):
        """Resolve a user-supplied path, refusing to escape base_path."""
        base = self._require_base_directory().resolve()
        candidate = (base / user_path).resolve()

        try:
            candidate.relative_to(base)
        except ValueError:
            self._fail(f"Path escapes base_path: {user_path}")

        return candidate

    def _validate_swath_index(self, swath_index):
        """Validate that a swath index lies within the loaded payload."""
        self._require_payload_loaded()
        self._ensure(swath_index >= 0, f"Invalid swath index: {swath_index}")
        self._ensure(
            swath_index < self.print_settings["swaths"],
            f"Swath index out of range: {swath_index} "
            f"(max {self.print_settings['swaths'] - 1})",
        )
        self._ensure(
            swath_index < len(self.print_settings["passes"]),
            f"Swath index {swath_index} missing in passes list "
            f"(len={len(self.print_settings['passes'])})",
        )

    # --- CHECKS --- #

    def validate_bounds(
        self,
        print_origin=None,
        print_dimensions=None,
        log_errors=True,
    ):
        """Check the head's travel rectangle against the machine axis limits.

        print_bounds is what gets checked, and since get_bounds() now builds it
        from the payload's pass schedule it is genuinely the rectangle the
        toolhead will cover. Before that it was the image rectangle, which on a
        head with stacked inks both understated the travel at the bottom (the
        lead-in passes are negative, so moves failed at runtime after passing
        this check) and overstated it at the top (rejecting prints that fit).

        print_dimensions is accepted for callers that want to sanity-check a
        hypothetical image size; it does not participate in the axis check,
        because the image is not what the axes have to accommodate.

        Returns (is_valid, errors) and caches the result in
        last_bounds_valid / last_bounds_errors.
        """
        toolhead = self._require_toolhead()
        self._require_print_bounds()

        if print_origin is None:
            print_origin = self.print_origin

        self._ensure(
            print_origin is not None and len(print_origin) >= 2,
            f"Invalid print_origin: {print_origin}",
        )

        if print_dimensions is not None:
            self._ensure(
                len(print_dimensions) >= 2,
                f"Invalid print_dimensions: {print_dimensions}",
            )
            self._ensure(
                float(print_dimensions[0]) >= 0,
                f"Print width must be non-negative: {print_dimensions[0]}",
            )
            self._ensure(
                float(print_dimensions[1]) >= 0,
                f"Print height must be non-negative: {print_dimensions[1]}",
            )

        status = toolhead.get_status(self._eventtime())

        # Use the reported axis limits when available; otherwise fall
        # back to a conservative 300 x 300 mm volume.
        if "axis_minimum" in status and "axis_maximum" in status:
            axis_min = status["axis_minimum"]
            axis_max = status["axis_maximum"]
            xmin, ymin = float(axis_min[0]), float(axis_min[1])
            xmax, ymax = float(axis_max[0]), float(axis_max[1])
        else:
            xmin, ymin = 0.0, 0.0
            xmax, ymax = 300.0, 300.0

        bound_min_x = float(self.print_bounds[0][0])
        bound_min_y = float(self.print_bounds[0][1])
        bound_max_x = float(self.print_bounds[1][0])
        bound_max_y = float(self.print_bounds[1][1])

        errors = []

        if bound_min_x < xmin:
            errors.append(
                f"X minimum {bound_min_x:.3f} is below axis minimum {xmin:.3f}"
            )
        if bound_max_x > xmax:
            errors.append(
                f"X maximum {bound_max_x:.3f} is above axis maximum {xmax:.3f}"
            )
        if bound_min_y < ymin:
            errors.append(
                f"Y minimum {bound_min_y:.3f} is below axis minimum {ymin:.3f}"
                + self._lead_in_hint(ymin, bound_min_y)
            )
        if bound_max_y > ymax:
            errors.append(
                f"Y maximum {bound_max_y:.3f} is above axis maximum {ymax:.3f}"
            )

        is_valid = len(errors) == 0
        self.last_bounds_valid = is_valid
        self.last_bounds_errors = errors

        if errors and log_errors:
            logging.error("validate_bounds failed: %s", " | ".join(errors))

        return is_valid, errors

    def _lead_in_hint(self, ymin, bound_min_y):
        """Explain a low-Y failure caused by the head's lead-in.

        Without this the operator reads "Y minimum below axis minimum" and
        moves the origin up by the shortfall, which is right. What they must
        NOT do is grow extra_margin to absorb it, because that shifts where the
        ink lands. Saying where the travel comes from makes the difference
        visible at the moment it matters.
        """
        lead_in = -float(self.print_settings["travel"]["y_min_mm"])
        if lead_in <= 1e-6:
            return ""
        return (
            f". {lead_in:.3f}mm of that is this job's lead-in: its highest "
            f"nozzles must reach the first row, so the head starts below the "
            f"image. Move the print origin up by at least "
            f"{ymin - bound_min_y:.3f}mm: do NOT raise extra_margin, which "
            f"moves the image itself"
        )

    def preflight_check(self, print_origin=None, print_dimensions=None):
        """Run every safety check required before starting a print."""
        self.health_check()
        self._require_not_printing()
        self._require_print_ready()

        if print_origin is not None:
            self._ensure(
                len(print_origin) >= 2,
                f"Invalid print_origin: {print_origin}",
            )

        if print_dimensions is not None:
            self._ensure(
                len(print_dimensions) >= 2,
                f"Invalid print_dimensions: {print_dimensions}",
            )

        is_valid, errors = self.validate_bounds(
            print_origin=print_origin,
            print_dimensions=print_dimensions,
            log_errors=True,
        )

        if not is_valid:
            self._fail(
                "Preflight failed: print is out of bounds | " + " | ".join(errors)
            )

        return True

    def health_check(self):
        """Validate connections, directories, and motion configuration."""
        self._require_toolhead()

        problems = []

        if self.client is None:
            problems.append("PaintressdClient is not initialized")
        if self.search_directory is None:
            problems.append("Search directory is not initialized")
        elif not self.search_directory.exists():
            problems.append(f"Search directory does not exist: {self.search_directory}")
        elif not self.search_directory.is_dir():
            problems.append(
                f"Search directory is not a directory: {self.search_directory}"
            )

        if self.move_speed <= 0:
            problems.append(f"move_speed invalid: {self.move_speed}")
        if self.print_speed <= 0:
            problems.append(f"print_speed invalid: {self.print_speed}")
        if self.z_lift_speed <= 0:
            problems.append(f"z_lift_speed invalid: {self.z_lift_speed}")
        if self.move_accel <= 0:
            problems.append(f"move_accel invalid: {self.move_accel}")
        if self.print_accel <= 0:
            problems.append(f"print_accel invalid: {self.print_accel}")
        if self.start_overscan < 0:
            problems.append(f"start_overscan invalid: {self.start_overscan}")
        if self.end_overscan < 0:
            problems.append(f"end_overscan invalid: {self.end_overscan}")
        if self.extra_margin < 0:
            problems.append(f"extra_margin invalid: {self.extra_margin}")

        if problems:
            self._fail("Health check failed: " + " | ".join(problems))

        return True

    # --- PAINTRESS_CONNECT_SOCKET --- #

    def connect_socket(self):
        """Open the TCP connection to the daemon."""
        self._require_client()
        self.client.connect()
        self.is_socket_connected = True

    def cmd_connect_socket(self, gcmd):
        self._run_cmd(gcmd, "PAINTRESS_CONNECT_SOCKET", self.connect_socket)

    # --- PAINTRESS_DISCONNECT_SOCKET --- #

    def disconnect_socket(self):
        """Close the TCP connection to the daemon."""
        self._require_client()

        if hasattr(self.client, "disconnect"):
            self.client.disconnect()

        self.is_socket_connected = False

    def cmd_disconnect_socket(self, gcmd):
        self._run_cmd(gcmd, "PAINTRESS_DISCONNECT_SOCKET", self.disconnect_socket)

    # --- PAINTRESS_STATUS --- #

    def status(self):
        """Collect the combined daemon and plugin status as a dict."""
        remote = None
        remote_error = None

        # Query the daemon only when a socket is open.
        if self.client is not None and self.is_socket_connected:
            try:
                remote = self.client.status_socket()
                self._ensure(
                    isinstance(remote, dict),
                    f"Invalid status response: {remote}",
                )
            except Exception as e:
                remote_error = str(e)
                logging.exception("Failed to query remote status")

        bounds_valid = self.last_bounds_valid
        bounds_errors = list(self.last_bounds_errors)

        # Re-validate the current bounds, but never let that fail status.
        try:
            if self.print_bounds is not None:
                bounds_valid, bounds_errors = self.validate_bounds(log_errors=False)
        except Exception as e:
            bounds_valid = None
            bounds_errors = [str(e)]

        return {
            "client": remote,
            "client_error": remote_error,
            "is_socket_connected": self.is_socket_connected,
            "is_serial_connected": self.is_serial_connected,
            "is_board_configured": self.is_board_configured,
            "is_printing": self.is_printing,
            "payload_loaded": self.payload_absolute_path is not None,
            "search_directory": (
                str(self.search_directory) if self.search_directory else None
            ),
            "payload_absolute_path": (
                str(self.payload_absolute_path)
                if self.payload_absolute_path
                else None
            ),
            "column_fire_interval_us": self.column_fire_interval_us,
            "current_swath_index": self.current_swath_index,
            "print_origin": self.print_origin,
            "print_bounds": self.print_bounds,
            "bounds_valid": bounds_valid,
            "bounds_errors": bounds_errors,
            "print_settings": self.print_settings,
        }

    def cmd_status(self, gcmd):
        def _do():
            status = self.status()
            self._screen_msg(
                gcmd,
                "PAINTRESS_STATUS: " + json.dumps(status, ensure_ascii=False),
            )

        self._run_cmd(gcmd, "PAINTRESS_STATUS", _do)

    # --- PAINTRESS_CONNECT_SERIAL --- #

    def connect_serial(self):
        """Ask the daemon to open the serial link to the controller board."""
        self._require_client()
        self._ensure(
            self.serial_port is not None and self.serial_port.strip() != "",
            "serial_port is empty",
        )
        self.client.connect_serial(self.serial_port)
        self.is_serial_connected = True

    def cmd_connect_serial(self, gcmd):
        self._run_cmd(gcmd, "PAINTRESS_CONNECT_SERIAL", self.connect_serial)

    # --- PAINTRESS_DISCONNECT_SERIAL --- #

    def disconnect_serial(self):
        """Ask the daemon to close the serial link to the controller board."""
        self._require_client()

        if hasattr(self.client, "disconnect_serial"):
            self.client.disconnect_serial()

        self.is_serial_connected = False

    def cmd_disconnect_serial(self, gcmd):
        self._run_cmd(gcmd, "PAINTRESS_DISCONNECT_SERIAL", self.disconnect_serial)

    # --- PAINTRESS_SET_DIRECTORY --- #

    def set_directory(self, directory):
        """Select a new payload search directory, sandboxed under base_path."""
        self._ensure(
            directory is not None and directory.strip() != "",
            "DIRECTORY must be informed",
        )

        resolved = self._resolve_under_base(directory)

        self._ensure(resolved.exists(), f"Directory does not exist: {resolved}")
        self._ensure(resolved.is_dir(), f"Path is not a directory: {resolved}")

        self.search_directory = resolved

    def cmd_set_directory(self, gcmd):
        def _do():
            directory = gcmd.get("DIRECTORY", None)
            if directory is None:
                self._fail("Missing required argument DIRECTORY", gcmd=gcmd)

            self.set_directory(directory)
            return f"directory={self.search_directory}"

        self._run_cmd(gcmd, "PAINTRESS_SET_DIRECTORY", _do)

    # --- PAINTRESS_OPEN_PAYLOAD --- #

    def open_payload(self, filename):
        """Load an encoded job file into the daemon and configure the board.

        On any failure the daemon job is unloaded and all payload state
        is reset, so the plugin is never left half-configured.
        """
        self._require_client()
        self._require_search_directory()

        self._ensure(
            filename is not None and filename.strip() != "",
            "FILENAME must be informed",
        )

        payload_path = (self.search_directory / filename).resolve()

        # Reject any filename that escapes the search directory.
        try:
            payload_path.relative_to(self.search_directory.resolve())
        except ValueError:
            self._fail(f"Payload path escapes current search_directory: {filename}")

        self._ensure(
            payload_path.exists(),
            f"Payload file does not exist: {payload_path}",
        )
        self._ensure(
            payload_path.is_file(),
            f"Payload path is not a file: {payload_path}",
        )

        try:
            # Hand the file to the daemon and validate the metadata it
            # reports back.
            info = self.client.load_job(str(payload_path))
            self._ensure(
                isinstance(info, dict),
                "load_job() returned invalid response",
            )
            self._ensure("metadata" in info, "Payload response missing 'metadata'")

            metadata = info["metadata"]
            required_keys = [
                "total_passes",
                "print_width_mm",
                "padded_width_mm",
                "print_height_mm",
                "dpi",
                "passes",
            ]
            for key in required_keys:
                self._ensure(
                    key in metadata,
                    f"Payload metadata missing '{key}'",
                )

            # passes is a list of per-pass objects: {y_position_mm, y_delta_mm,
            # line_count}. The plugin only needs the Y positions.
            passes = metadata["passes"]
            self._ensure(
                isinstance(passes, list),
                "Payload 'passes' must be a list",
            )
            self._ensure(
                metadata["total_passes"] > 0,
                "Payload total_passes must be > 0",
            )
            self._ensure(metadata["dpi"] > 0, "Payload dpi must be > 0")
            self._ensure(
                len(passes) == metadata["total_passes"],
                f"Payload passes length mismatch: "
                f"total_passes={metadata['total_passes']} "
                f"passes_len={len(passes)}",
            )

            y_positions_mm = [p["y_position_mm"] for p in passes]

            new_print_settings = {
                "swaths": metadata["total_passes"],
                "passes": y_positions_mm,
                "dimensions": {
                    "width_mm": metadata["print_width_mm"],
                    "padded_width_mm": metadata["padded_width_mm"],
                    "height_mm": metadata["print_height_mm"],
                },
                # The travel envelope, taken from the schedule this very job
                # will be printed with. This is the whole reason the plugin
                # needs no head-specific positioning logic: whatever the head,
                # the job states where its own passes are.
                "travel": {
                    "y_min_mm": min(y_positions_mm),
                    "y_max_mm": max(y_positions_mm),
                    "x_width_mm": float(metadata["padded_width_mm"]),
                },
                "dpi": metadata["dpi"],
            }

            # Fire one nozzle column every 1/DPI inch at print_speed mm/s.
            # column pitch = 25.4/dpi mm; time = pitch / print_speed; the
            # constant 25_400_000 = 25.4 mm/inch x 1e6 us/s folds the mm->us
            # and per-second conversions together.
            dpi = new_print_settings["dpi"]
            self._ensure(
                self.print_speed > 0,
                f"Invalid print_speed: {self.print_speed}",
            )
            new_column_fire_interval_us = round(25_400_000 / (dpi * self.print_speed))
            self._ensure(
                new_column_fire_interval_us > 0,
                f"Calculated invalid column_fire_interval_us: "
                f"{new_column_fire_interval_us}",
            )
            # The firmware line delay is a u16 on the wire; a very low
            # dpi x print_speed product overflows it (e.g. 90 dpi at 1 mm/s
            # -> 282222 us). Fail with the knobs to turn, not a pack error.
            self._ensure(
                new_column_fire_interval_us <= FIRMWARE_MAX_LINE_DELAY_US,
                f"Derived column interval {new_column_fire_interval_us}us "
                f"exceeds the firmware maximum ({FIRMWARE_MAX_LINE_DELAY_US}us); "
                "increase print_speed and/or use a higher-DPI payload",
            )
            if new_column_fire_interval_us < FIRMWARE_MIN_LINE_DELAY_US:
                self.gcode.respond_info(
                    f"WARNING PAINTRESS: derived column interval "
                    f"{new_column_fire_interval_us}us is below the firmware's "
                    f"~{FIRMWARE_MIN_LINE_DELAY_US}us per-line floor; the "
                    "firing grid will not keep up; reduce print_speed or "
                    "use a lower-DPI payload"
                )

            # No set_timing step: the derived interval travels
            # with each print (trigger_swath passes it), so a firmware reset
            # can never silently revert it. The plugin's "column fire
            # interval" and the firmware's "line delay" are the same value.

            # Commit the new payload state only once everything succeeded.
            self.payload_absolute_path = payload_path
            self.print_settings = new_print_settings
            self.column_fire_interval_us = new_column_fire_interval_us
            # Stale geometry dies with the old payload. The anchor goes too:
            # it is only ever set alongside the bounds, and a live anchor with
            # dead bounds would let move_to_swath_start compute a Y from the
            # previous job's offsets.
            self.print_bounds = None
            self.print_anchor = None
            self.current_swath_index = 0
            self.is_board_configured = True
            self.last_bounds_valid = None
            self.last_bounds_errors = []

            self.gcode.respond_info(
                f"PAYLOAD INFO: {json.dumps(info, ensure_ascii=False)}"
            )

        except Exception:
            # Roll back: unload the daemon job and clear payload state.
            try:
                if self.client is not None and hasattr(self.client, "unload_job"):
                    self.client.unload_job()
            except Exception:
                logging.exception("Failed to unload job after open_payload error")

            self._reset_payload_state()
            raise

    def cmd_open_payload(self, gcmd):
        def _do():
            filename = gcmd.get("FILENAME", None)
            if filename is None:
                self._fail("Missing required argument FILENAME", gcmd=gcmd)

            self.open_payload(filename)
            return f"file={self.payload_absolute_path}"

        self._run_cmd(gcmd, "PAINTRESS_OPEN_PAYLOAD", _do)

    # --- PAINTRESS_CLOSE_PAYLOAD --- #

    def close_payload(self):
        """Unload the current payload from the daemon and reset state."""
        self._require_client()
        self._require_payload_loaded()

        stats = self.client.unload_job()

        self.stored_position = None
        self._reset_payload_state()

        return stats

    def cmd_close_payload(self, gcmd):
        self._run_cmd(gcmd, "PAINTRESS_CLOSE_PAYLOAD", self.close_payload)

    # --- PAINTRESS_PURGE --- #

    def purge(self, channel=0, pulses=10):
        """Fire nozzles on a channel to clear or prime ink."""
        self._require_client()
        self._ensure(0 <= channel <= 6, f"Invalid channel: {channel}")
        self._ensure(pulses > 0, f"Invalid pulses: {pulses}")

        self.client.purge(channel, pulses)

    def cmd_purge(self, gcmd):
        def _do():
            purge_channel = self._get_channel_arg(gcmd, "CHANNEL", 0)
            purge_pulses = gcmd.get_int("PULSES", 10, minval=1)

            self.purge(purge_channel, purge_pulses)
            return (
                f"channel={purge_channel}, "
                f"pulses={purge_pulses}"
            )

        self._run_cmd(gcmd, "PAINTRESS_PURGE", _do)

    # --- PAINTRESS_TEST_TRIGGER --- #

    def cmd_test_trigger(self, gcmd):
        """Pulse the trigger output pin, for scoping. No daemon, no firmware.

        Drives an existing [output_pin] via SET_PIN (the proven Klipper path);
        the plugin does not own the pin. In MOVE mode the rising edge is fired
        at a colinear split in the sweep, so it lands at an exact X position
        while the head is cruising and without stopping it.

        Params (all optional):
          PIN=trigger_print_pin   name of the [output_pin] to drive.
          MOVE=1            1 = fire the edge at TRIGGER_DISTANCE into an X
                            move (mirrors a pass); 0 = pulse in place.
          DISTANCE=50       total X move length (mm) when MOVE=1.
          TRIGGER_DISTANCE  X travelled before the rising edge (mm),
                            defaults to the configured trigger_distance.
          SPEED / ACCEL     move speed/accel, default print_speed/accel.
          PULSE_MS=200      high time when MOVE=0.
        """
        def _do():
            pin_name = gcmd.get("PIN", "trigger_print_pin")
            self._ensure(
                self.printer.lookup_object("output_pin %s" % pin_name, None)
                is not None,
                f"[output_pin {pin_name}] is not configured",
            )

            move = gcmd.get_int("MOVE", 1, minval=0, maxval=1)
            distance = gcmd.get_float("DISTANCE", 50.0, above=0.0)
            trigger_distance = gcmd.get_float(
                "TRIGGER_DISTANCE", self.trigger_distance, minval=0.0
            )
            speed = gcmd.get_float("SPEED", self.print_speed, above=0.0)
            accel = gcmd.get_float("ACCEL", self.print_accel, above=0.0)
            pulse_ms = gcmd.get_float("PULSE_MS", 200.0, above=0.0)

            return self.test_trigger(
                pin_name=pin_name,
                move=bool(move),
                distance=distance,
                trigger_distance=trigger_distance,
                speed=speed,
                accel=accel,
                pulse_s=pulse_ms / 1e3,
            )

        self._run_cmd(gcmd, "PAINTRESS_TEST_TRIGGER", _do)

    def _set_trigger_pin(self, pin_name, value):
        """Set an [output_pin] high/low via SET_PIN (synced to the move queue
        through the standard output_pin lookahead callback)."""
        self.gcode.run_script_from_command(
            "SET_PIN PIN=%s VALUE=%d" % (pin_name, int(value))
        )

    def test_trigger(self, pin_name, move, distance, trigger_distance, speed,
                     accel, pulse_s):
        """Pulse an output pin (see cmd_test_trigger)."""
        toolhead = self._require_toolhead()

        if move:
            self._ensure(
                trigger_distance < distance,
                f"TRIGGER_DISTANCE {trigger_distance} must be < DISTANCE {distance}",
            )
            start_x = self._get_position()[0]

            # Split the sweep at the trigger position. SET_PIN registers a
            # lookahead callback on move A, so the rising edge fires exactly
            # at A's end (start + trigger_distance). A and B are colinear, so
            # the lookahead cruises through the junction: the head does not
            # stop. The line is dropped after the whole sweep (the firmware
            # only needs the rising edge), and is forced low first so a line
            # left HIGH by an earlier failure still yields a clean edge.
            self._set_trigger_pin(pin_name, 0)
            self._g1(x=start_x + trigger_distance, y=None, z=None,
                     speed=speed, accel=accel)        # move A
            self._set_trigger_pin(pin_name, 1)         # rising edge at A end
            try:
                self._g1(x=start_x + distance, y=None, z=None,
                         speed=speed, accel=accel)     # move B (colinear)
                toolhead.wait_moves()
            finally:
                # Drop after the sweep, even if the move failed mid-way.
                try:
                    self._set_trigger_pin(pin_name, 0)
                except Exception:
                    logging.exception("failed to lower the trigger pin after the test sweep")

            self._g1(x=start_x, y=None, z=None, speed=speed, accel=accel)
            toolhead.wait_moves()
            return (f"rising edge {trigger_distance:.2f}mm in, "
                    f"moved {distance:.1f}mm X via {pin_name}")

        # Stationary pulse.
        self._set_trigger_pin(pin_name, 1)
        toolhead.dwell(pulse_s)
        self._set_trigger_pin(pin_name, 0)
        toolhead.wait_moves()
        return f"{pulse_s * 1e3:.0f}ms pulse in place via {pin_name}"

    # --- PAINTRESS_GET_BOUNDS --- #

    def get_mesh_bounds(self):
        """Return (min, max) XY corners enclosing all exclude_object polygons."""
        exclude_objects = self.printer.lookup_object("exclude_object", None)
        self._ensure(exclude_objects is not None, "exclude_object is not available")

        try:
            objects = exclude_objects.get_status(self._eventtime()).get("objects", [])
        except TypeError:
            objects = exclude_objects.get_status().get("objects", [])

        self._ensure(objects, "No exclude_object polygons found")

        xs, ys = [], []
        for obj in objects:
            polygon = obj.get("polygon", [])
            self._ensure(polygon, f"Object without polygon: {obj}")
            for point in polygon:
                self._ensure(len(point) >= 2, f"Invalid polygon point: {point}")
                xs.append(point[0])
                ys.append(point[1])

        self._ensure(xs and ys, "No valid polygon points found")
        return [min(xs), min(ys)], [max(xs), max(ys)]

    def get_bounds(self, from_mesh=False):
        """Compute print_origin, print_anchor and print_bounds.

        Three rectangles are involved and they are not the same one; conflating
        them is what let a head travel outside its own validated bounds:

        * print_origin: where the image's bottom-left corner lands. Set by
          the operator (ORIGIN_X/Y) or, with from_mesh, derived from the
          exclude_object polygons so the image sits over the object.
        * print_anchor: print_origin plus the head's mechanical offsets:
          the machine position at which pass offset 0 falls.
        * print_bounds: the rectangle the toolhead actually travels, and the
          only one worth validating against the axis limits.

        The travel envelope ALWAYS comes from the payload, in both modes. The
        mesh positions the print; it does not size it. Sizing travel from the
        mesh was wrong in both directions: in Y the head went below the
        validated minimum on any head whose inks are stacked (its first passes
        are negative), and in X the sweep length was set by the object while
        the column count came from the payload, so an object narrower than the
        image ended the sweep with columns still to fire.
        """
        self._require_toolhead()
        # Needed in BOTH modes now: without a payload there is no schedule, and
        # without a schedule there is no honest travel rectangle to report.
        self._require_payload_loaded()

        travel = self.print_settings["travel"]
        width = float(travel["x_width_mm"])
        self._ensure(width >= 0, f"Invalid payload padded_width_mm: {width}")

        mesh_max = None
        if from_mesh:
            mesh_min, mesh_max = self.get_mesh_bounds()

            self._ensure(
                mesh_max[0] >= mesh_min[0],
                f"Invalid mesh X bounds: min={mesh_min[0]} max={mesh_max[0]}",
            )
            self._ensure(
                mesh_max[1] >= mesh_min[1],
                f"Invalid mesh Y bounds: min={mesh_min[1]} max={mesh_max[1]}",
            )

            # extra_margin is BLEED: how far the image should overrun the
            # object. It moves where the ink lands, so it must never be used to
            # buy clearance for the travel envelope: growing it to absorb a
            # head's lead-in would shift the image off the object.
            self.print_origin = [
                mesh_min[0] - self.extra_margin,
                mesh_min[1] - self.extra_margin,
            ]

        # Where pass offset 0 falls, in machine coordinates.
        self.print_anchor = [
            float(self.print_origin[0]) + self.x_offset,
            float(self.print_origin[1]) + self.y_offset,
        ]

        # The travel rectangle: X spans the payload's padded width plus the
        # overscan that brings the head up to speed; Y spans the pass schedule.
        self.print_bounds = [
            [
                self.print_anchor[0] - self.start_overscan,
                self.print_anchor[1] + float(travel["y_min_mm"]),
            ],
            [
                self.print_anchor[0] + width + self.end_overscan,
                self.print_anchor[1] + float(travel["y_max_mm"]),
            ],
        ]

        self._require_print_bounds()

        if from_mesh:
            self._warn_if_image_misses_object(mesh_max, width)

        self.gcode.respond_info(
            "PAINTRESS_BOUNDS: "
            + json.dumps(
                {
                    "print_origin": self.print_origin,
                    "print_anchor": self.print_anchor,
                    "print_bounds": self.print_bounds,
                    "travel": travel,
                    "from_mesh": from_mesh,
                },
                ensure_ascii=False,
            )
        )

        return self.print_bounds

    def _warn_if_image_misses_object(self, mesh_max, width):
        """Warn when the payload does not cover the object plus its margin.

        This is what the old code was reaching for when it sized the bounds
        from the mesh. Sizing travel that way was wrong, but the question
        underneath it was right: does this image actually cover this object?
        It is a warning, not an error: deliberately printing a small image
        onto part of a large object is legitimate.
        """
        height = float(self.print_settings["dimensions"]["height_mm"])
        want_x = float(mesh_max[0]) + self.extra_margin
        want_y = float(mesh_max[1]) + self.extra_margin
        short_x = want_x - (float(self.print_origin[0]) + width)
        short_y = want_y - (float(self.print_origin[1]) + height)

        missing = []
        if short_x > 1e-6:
            missing.append(f"{short_x:.2f}mm in X")
        if short_y > 1e-6:
            missing.append(f"{short_y:.2f}mm in Y")
        if missing:
            self.gcode.respond_info(
                "PAINTRESS WARNING: the payload does not cover the object "
                f"plus its {self.extra_margin}mm margin, short by "
                f"{' and '.join(missing)}. The image will be printed from the "
                "origin and simply stop short."
            )

    def cmd_get_bounds(self, gcmd):
        def _do():
            from_mesh = self._get_bool_arg(gcmd, "FROM_MESH", True)
            bounds = self.get_bounds(from_mesh=from_mesh)
            return f"from_mesh={from_mesh}, bounds={bounds}"

        self._run_cmd(gcmd, "PAINTRESS_GET_BOUNDS", _do)

    # --- PAINTRESS_PRINT --- #

    def move_to_swath_start(self, swath_index, wait=True):
        """Move the head to the X/Y start of a swath."""
        self._validate_swath_index(swath_index)
        self._require_print_bounds()
        self._ensure(
            self.print_anchor is not None,
            "print_anchor is not set; run get_bounds() first",
        )

        # X starts at the overscan edge so the head is cruising by the trigger
        # split. Y is the anchor plus this swath's scheduled offset: the
        # anchor, NOT print_bounds[0], which already carries the schedule's
        # lowest offset and would double-count it.
        x_position = self.print_bounds[0][0]
        y_position = self.print_anchor[1] + self.print_settings["passes"][swath_index]

        self.gcode.respond_info(
            f"MOVING TO SWATH {swath_index + 1} START POSITION: "
            f"{[x_position, y_position]}"
        )
        self._g1(
            x=x_position,
            y=y_position,
            z=None,
            speed=self.move_speed,
            accel=self.move_accel,
        )

        if wait:
            self.toolhead.wait_moves()

    def move_to_print_origin(self):
        """Raise the toolhead to the printhead working height (z_offset),
        then move to the start of the first swath."""
        self._require_payload_loaded()
        pos = self._get_position()

        self.gcode.respond_info(
            f"MOVING TO PRINT ORIGIN: {self.print_origin} FROM {pos}"
        )

        self._g1(z=pos[2] + self.z_offset, speed=self.z_lift_speed)
        self.toolhead.wait_moves()

        self.move_to_swath_start(0)

    def store_position(self):
        """Save the current toolhead position for later restoration."""
        pos = self._get_position()
        self._ensure(len(pos) >= 3, f"Invalid toolhead position: {pos}")
        self.stored_position = pos
        self.gcode.respond_info(f"STORED POSITION: {self.stored_position}")

    def return_to_stored_position(self):
        """Return the head to the position saved by store_position()."""
        pos = self._require_stored_position()

        self._g1(
            x=pos[0],
            y=pos[1],
            z=None,
            speed=self.move_speed,
            accel=self.move_accel,
        )
        self.toolhead.wait_moves()

        self._g1(z=pos[2], speed=self.z_lift_speed)
        self.toolhead.wait_moves()

        self.gcode.respond_info(f"RETURNED TO STORED POSITION: {pos}")

    def _await_swath_completion(self, swath_number, budget=3.0):
        """Block after a sweep until this swath's PRINT_COMPLETE event arrives.
        That event is the proof that the start edge reached the board and the swath
        actually fired.

        Without this, a lost trigger surfaced only at the NEXT arm (as a
        confusing print_in_progress reject, since the firmware was still armed
        for the previous swath) and never for the LAST swath: the firmware only
        emits TRIGGER_TIMEOUT after its own 10 s guard, long after the sweep,
        so the print would finish "successfully" with the last swath blank and
        a stale fault latched for the next print. Waiting on the completion
        event catches the failure right here, for every swath, within `budget`
        seconds.

        The wait is event-driven: poll_event() returns this swath's outcome
        directly off the daemon's event stream (the daemon broadcasts it):
        no status round-trip, no completion arithmetic. print_complete succeeds;
        any of the fault events (trigger_timeout, print_error, pipeline_error,
        serial_lost) fails with its cause. Each poll blocks at most 50 ms and
        yields to the reactor between tries, so the reactor never stalls. If the
        budget elapses with no event, a single status read is the backstop
        (the fault, if any, should already have arrived on the event stream).
        """
        self._require_client()
        outcome_events = ("print_complete", "print_error", "trigger_timeout",
                          "pipeline_error", "serial_lost")
        deadline = self._eventtime() + budget
        while True:
            evt = self.client.poll_event(outcome_events, swath_id=swath_number,
                                         max_block=0.05)
            if evt is not None:
                name = evt.get("event")
                if name == "print_complete":
                    return
                detail = evt.get("error") or evt.get("message") or ""
                self._fail(
                    f"Print fault after swath {swath_number}: {name} ({detail})"
                )

            # No outcome yet: yield to the reactor, then retry until the budget
            # is spent.
            self.reactor.pause(self._eventtime() + 0.05)
            if self._eventtime() >= deadline:
                # Backstop: the fault event should already have surfaced above,
                # but check the daemon's latched fault once before giving up.
                status = self.client.get_daemon_status()
                fault = status.get("print_fault") if isinstance(status, dict) else None
                if fault:
                    self._fail(
                        f"Print fault after swath {swath_number}: "
                        f"{fault.get('error')} ({fault.get('message')})"
                    )
                self._fail(
                    f"Swath {swath_number} was swept but the firmware never "
                    f"reported PRINT_COMPLETE within {budget:.1f}s: either "
                    "the start trigger never reached the board (check the "
                    "trigger_output_pin wiring and trigger_distance) or the "
                    "print was interrupted (abort/reset)"
                )

    def execute_swath_pass(self):
        """Print the current swath: arm the firmware, then sweep X.

        The firmware fires nozzle columns as the head travels, so the
        printing pass is paced by the gantry motion itself.

        trigger_swath() only arms the firmware (it does its setup and waits for
        the start edge); the sweep is then split at trigger_distance and the
        board trigger line is raised there with SET_PIN, so the firmware fires
        on that rising edge, emitted by the motion MCU at the exact print_time
        the head reaches the split, while cruising and without stopping (the two
        halves are colinear).
        """
        self._validate_swath_index(self.current_swath_index)
        self._require_print_bounds()

        self.trigger_swath()

        self._warn_if_trigger_too_early()

        split_x = self.print_bounds[0][0] + self.trigger_distance
        self._ensure(
            split_x < self.print_bounds[1][0],
            f"trigger_distance {self.trigger_distance}mm reaches past the "
            "swath end; reduce it or widen the overscan",
        )

        # Force the line low before the sweep. Normally a no-op (it is dropped
        # after every sweep), but it self-heals a line left HIGH by an earlier
        # mid-sweep failure: the firmware is already armed at this point, so
        # its known-low baseline wait gets its low here and this pass still
        # gets a clean rising edge, instead of burning the whole trigger
        # window and dying with a confusing trigger_timeout.
        self._set_trigger_pin(self.trigger_output_pin, 0)

        # Move A: accelerate up to the trigger position. SET_PIN registers
        # a lookahead callback on this move, so the rising edge fires at
        # its end (the split). Move B continues colinearly, so the head
        # cruises through the split without stopping. The line is dropped
        # after the sweep: the firmware only needs the rising edge, and
        # it must be low again before the next swath arms.
        self._g1(x=split_x, y=None, z=None,
                 speed=self.print_speed, accel=self.print_accel)
        self._set_trigger_pin(self.trigger_output_pin, 1)
        try:
            self._g1(x=self.print_bounds[1][0], y=None, z=None,
                     speed=self.print_speed, accel=self.print_accel)
            self.toolhead.wait_moves()
        finally:
            # Drop the line even when the sweep fails mid-way (motion error,
            # shutdown): leaving it HIGH would stall the next pass's baseline
            # wait. Best-effort: never mask the sweep's own error.
            try:
                self._set_trigger_pin(self.trigger_output_pin, 0)
            except Exception:
                logging.exception("failed to lower the trigger pin after the sweep")

    def prepare_next_swath(self):
        """Advance to the next swath by moving the head to its start.

        No data is streamed here: the daemon owns the double-buffer pipeline and
        keeps both firmware slots fed (it streams the next swath as soon as a
        slot frees, while the current one prints), so the plugin only moves and
        arms. This is why the reactor never blocks on a swath-sized TCP transfer
        and the old "Timer too close" risk is gone.
        """
        self.current_swath_index += 1
        self._validate_swath_index(self.current_swath_index)

        self.move_to_swath_start(self.current_swath_index, wait=True)

    def print(self):
        """Run the full swath-by-swath print.

        The toolhead position is saved first and restored afterwards,
        including on failure, with a best-effort recovery move.
        """
        self._require_print_ready()
        self._require_print_bounds()

        total_swaths = self.print_settings["swaths"]
        self._ensure(total_swaths > 0, "Cannot print: total_swaths must be > 0")

        # Print boundary: drop any event parked during earlier round-trips
        # (e.g. a stale completion/timeout from a previous run): swath ids
        # repeat every job, so a leftover could satisfy or fail this print's
        # completion waits.
        dropped = self.client.clear_events()
        if dropped:
            logging.info("paintress: dropped %d stale daemon events", dropped)

        self.current_swath_index = 0
        self.store_position()

        try:
            self.move_to_print_origin()

            for swath_id in range(total_swaths):
                self.gcode.respond_info(
                    f"STARTING SWATH {swath_id + 1}/{total_swaths}"
                )
                self.execute_swath_pass()

                # Close the window between "swept" and "the swath actually
                # fired": wait for this swath's PRINT_COMPLETE event (and catch
                # faults) before moving on, including the last swath, which
                # has no following arm to surface a failure.
                self._await_swath_completion(swath_id + 1)

                if swath_id < total_swaths - 1:
                    self.prepare_next_swath()

        except Exception as print_exc:
            # Fail-safe: stop the firmware first; it may be armed or mid-fire.
            # The daemon's abort is ABORT (electrical kill: DAC off + latched,
            # ink stops immediately) followed by RESET (engine interrupted,
            # slots emptied, latch cleared), and it also stops the streaming
            # pipeline and clears the latched fault, a clean state to retry from.
            try:
                if self.client is not None:
                    self.client.abort()
                    # The abort/reboot round-trip is another producer of
                    # orphan events; drop them so they cannot leak into the
                    # next print's waits.
                    self.client.clear_events()
            except Exception:
                logging.exception("print: firmware abort on failure failed")

            # Best-effort: return the head to where the print started.
            if self.stored_position is not None:
                try:
                    self.gcode.respond_info(
                        "PRINT FAILED - ATTEMPTING TO RETURN TO STORED POSITION"
                    )
                    self.return_to_stored_position()
                except Exception as return_exc:
                    logging.exception(
                        "Failed to return to stored position after print error"
                    )
                    self.gcode.respond_info(
                        "WARNING: failed to return to stored position after error: "
                        f"{return_exc}"
                    )
            raise print_exc

        self.return_to_stored_position()

    def cmd_print(self, gcmd):
        def _do():
            received_quick_origin_argument = False
            quick_origin_x = None
            quick_origin_y = None

            origin_x_raw = gcmd.get("ORIGIN_X", None)
            origin_y_raw = gcmd.get("ORIGIN_Y", None)

            # ORIGIN_X and ORIGIN_Y must be supplied together.
            if (origin_x_raw is None) != (origin_y_raw is None):
                self._fail(
                    "ORIGIN_X and ORIGIN_Y must be informed together",
                    gcmd=gcmd,
                )

            if origin_x_raw is not None and origin_y_raw is not None:
                quick_origin_x = gcmd.get_float("ORIGIN_X")
                quick_origin_y = gcmd.get_float("ORIGIN_Y")
                received_quick_origin_argument = True
                self._screen_msg(gcmd, f"QUICK ORIGIN: {[quick_origin_x, quick_origin_y]}")

            self._require_not_printing()
            self._require_print_ready()

            # With an explicit origin, bound the print to the payload
            # size; otherwise derive it from the exclude_object mesh.
            if received_quick_origin_argument:
                self.print_origin = [quick_origin_x, quick_origin_y]
                self.get_bounds(from_mesh=False)
            else:
                self.get_bounds(from_mesh=True)

            self.preflight_check()

            try:
                self.is_printing = True
                self.print()
                return "print finished successfully"
            finally:
                self._reset_runtime_state()

        self._run_cmd(gcmd, "PAINTRESS_PRINT", _do)

    # --- CONTROLLER BOARD RELATED --- #

    def trigger_swath(self):
        """Arm the current swath (1-based id) for printing.

        Calls the daemon's print command, which blocks until the swath has been
        streamed into a firmware slot (the daemon pre-streams on load and keeps
        the slots fed) and then sends ARM; the firmware fires on the hardware
        start trigger. The column fire interval derived at open_payload rides
        along (the timing travels with each print). The plugin
        no longer streams swath data itself.
        """
        self._require_client()
        self._validate_swath_index(self.current_swath_index)
        self._ensure(
            self.column_fire_interval_us > 0,
            "No column fire interval (open a payload first)",
        )
        self.client.print_swath(self.current_swath_index + 1,
                                self.column_fire_interval_us)
        self.gcode.respond_info(f"OK TRIGGER_SWATH {self.current_swath_index + 1}")
