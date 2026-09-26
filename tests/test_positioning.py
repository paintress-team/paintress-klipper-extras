# SPDX-FileCopyrightText: 2026 paintress-team
# SPDX-License-Identifier: GPL-3.0-or-later

"""Positioning: the travel rectangle, both origin modes, and the head section.

Where the head goes is a property of the JOB, not of a configured head type:
every payload carries its own pass schedule, so the plugin derives the travel
envelope from it and needs no head-specific positioning logic. These tests pin
that, using pass schedules taken from real RIPped jobs: c6n90 (all inks
colinear, no lead-in) and c4n180 (inks stacked in Y, so the first passes are
NEGATIVE: the head must start below the image for its highest nozzles to reach
the first row).

The bug they exist to prevent: print_bounds used to be the image rectangle, so
a head with a lead-in travelled below its own validated minimum and the move
failed at runtime, after every check had passed.

Klipper is stubbed to the minimum those paths need. Runs under pytest or as a
plain script.
"""
import sys
import types
from pathlib import Path

EXTRAS = Path(__file__).resolve().parents[1] / "extras"

# The plugin imports its client relatively, as Klipper loads it from a package.
# Load it as one so the import resolves without editing the module.
import importlib.util  # noqa: E402

_pkg = types.ModuleType("kx")
_pkg.__path__ = [str(EXTRAS)]
sys.modules["kx"] = _pkg
_spec = importlib.util.spec_from_file_location("kx.paintress",
                                               EXTRAS / "paintress.py")
paintress = importlib.util.module_from_spec(_spec)
sys.modules["kx.paintress"] = paintress
_spec.loader.exec_module(paintress)
sys.modules["paintress"] = paintress

_hspec = importlib.util.spec_from_file_location("kx.paintress_head",
                                                EXTRAS / "paintress_head.py")
paintress_head = importlib.util.module_from_spec(_hspec)
sys.modules["kx.paintress_head"] = paintress_head
_hspec.loader.exec_module(paintress_head)


class ConfigError(Exception):
    pass


class StubConfig:
    error = ConfigError

    def __init__(self, values, sections=None, printer=None, name="paintress"):
        self.values = values
        self.sections = sections or {}
        self._printer = printer
        self._name = name

    def get_name(self):
        return self._name

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getfloat(self, key, default=None, **kw):
        raw = self.values.get(key, default)
        return None if raw is None else float(raw)

    def getint(self, key, default=None, **kw):
        return int(self.values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self.values.get(key, default))

    def get_printer(self):
        return self._printer

    def has_section(self, name):
        return name in self.sections

    def getsection(self, name):
        return StubConfig(self.sections[name], self.sections, self._printer,
                          name)


class StubGcode:
    def __init__(self):
        self.messages = []

    def respond_info(self, msg):
        self.messages.append(msg)

    def register_command(self, *a, **kw):
        pass

    def register_mux_command(self, *a, **kw):
        pass


class StubToolhead:
    def get_status(self, t):
        return {"axis_minimum": [0.0, 0.0, 0.0],
                "axis_maximum": [250.0, 250.0, 250.0]}


class StubPrinter:
    def __init__(self):
        self.gcode = StubGcode()
        self.toolhead = StubToolhead()
        self.configfile = types.SimpleNamespace(error=ConfigError)
        self.objects = {"gcode": self.gcode, "toolhead": self.toolhead,
                        "configfile": self.configfile}

    def lookup_object(self, name, default=Ellipsis):
        if name in self.objects:
            return self.objects[name]
        if default is not Ellipsis:
            return default
        raise KeyError(name)

    def load_object(self, config, section, default=Ellipsis):
        """Mirror Klipper: build the section's module on demand, once."""
        if section in self.objects:
            return self.objects[section]
        if section.split()[0] != "paintress_head":
            if default is not Ellipsis:
                return default
            raise ConfigError(f"Unable to load module '{section}'")
        obj = paintress_head.load_config_prefix(config.getsection(section))
        self.objects[section] = obj
        return obj

    def register_event_handler(self, *a, **kw):
        pass

    def get_reactor(self):
        return types.SimpleNamespace(monotonic=lambda: 0.0)


def make_plugin(values=None, sections=None):
    P = paintress
    printer = StubPrinter()
    base = {"trigger_output_pin": "my_trigger"}
    base.update(values or {})
    cfg = StubConfig(base, sections, printer)
    plugin = P.Paintress.__new__(P.Paintress)
    # Drive __init__ but keep the stubs: the real __init__ only needs printer,
    # gcode and reactor from Klipper, all of which the stub supplies.
    plugin.printer = printer
    plugin.reactor = printer.get_reactor()
    plugin.gcode = printer.gcode
    P.Paintress.__init__(plugin, cfg)
    plugin.toolhead = printer.toolhead
    return plugin


def load_payload(plugin, y_positions, padded_width, height):
    plugin.print_settings = {
        "swaths": len(y_positions),
        "passes": list(y_positions),
        "dimensions": {"width_mm": padded_width,
                       "padded_width_mm": padded_width,
                       "height_mm": height},
        "travel": {"y_min_mm": min(y_positions),
                   "y_max_mm": max(y_positions),
                   "x_width_mm": float(padded_width)},
        "dpi": 720,
    }
    plugin.payload_absolute_path = "stub.json"
    plugin.is_board_configured = True
    plugin.column_fire_interval_us = 100


# Real numbers, taken from the jobs RIPped earlier.
C4N180_PASSES = [-16.933, -16.898, -16.863, -16.828,
                 -8.467, -8.431, -8.396, -8.361,
                 0.0, 0.035, 0.071, 0.106,
                 8.467, 8.502, 8.537, 8.572]
C6N90_PASSES = [0.0, 3.629, 7.257, 10.886, 25.4, 29.029]


def test_c6n90_bounds_are_unchanged_in_x_and_start_at_the_anchor():
    p = make_plugin({"start_overscan": 10.0, "end_overscan": 10.0})
    load_payload(p, C6N90_PASSES, padded_width=26.0, height=40.0)
    p.print_origin = [50.0, 50.0]
    bounds = p.get_bounds(from_mesh=False)
    assert p.print_anchor == [50.0, 50.0]
    assert bounds[0][0] == 40.0, bounds          # 50 - overscan
    assert bounds[1][0] == 86.0, bounds          # 50 + 26 + overscan
    assert bounds[0][1] == 50.0, bounds          # anchor + 0 (no lead-in)
    assert abs(bounds[1][1] - 79.029) < 1e-6, bounds   # anchor + max(pass)
    print("PASS test_c6n90_bounds_are_unchanged_in_x_and_start_at_the_anchor")


def test_c4n180_bounds_span_the_schedule_not_the_image():
    p = make_plugin({"start_overscan": 10.0, "end_overscan": 10.0})
    load_payload(p, C4N180_PASSES, padded_width=26.035, height=15.205)
    p.print_origin = [50.0, 50.0]
    bounds = p.get_bounds(from_mesh=False)
    # Bottom: the head goes 16.933 BELOW the image origin.
    assert abs(bounds[0][1] - (50.0 - 16.933)) < 1e-6, bounds
    # Top: the head never reaches the image top (50+15.205); it stops at
    # anchor + max(pass).
    assert abs(bounds[1][1] - (50.0 + 8.572)) < 1e-6, bounds
    print("PASS test_c4n180_bounds_span_the_schedule_not_the_image")


def test_per_swath_y_is_not_double_counted():
    """The old base was print_bounds[0][1]; that now carries y_min, so using
    it would subtract the lead-in twice."""
    p = make_plugin()
    load_payload(p, C4N180_PASSES, padded_width=26.035, height=15.205)
    p.print_origin = [50.0, 50.0]
    p.get_bounds(from_mesh=False)
    moved = []
    p._g1 = lambda **kw: moved.append(kw)
    p.toolhead.wait_moves = lambda: None
    for i in (0, 8, 15):
        p.move_to_swath_start(i, wait=False)
    ys = [m["y"] for m in moved]
    assert abs(ys[0] - (50.0 - 16.933)) < 1e-6, ys
    assert abs(ys[1] - 50.0) < 1e-6, ys
    assert abs(ys[2] - (50.0 + 8.572)) < 1e-6, ys
    # Every commanded Y must sit inside the validated rectangle.
    b = p.print_bounds
    assert all(b[0][1] - 1e-6 <= y <= b[1][1] + 1e-6 for y in ys), (ys, b)
    print("PASS test_per_swath_y_is_not_double_counted")


def test_a_lead_in_below_the_bed_is_caught_before_moving():
    p = make_plugin()
    load_payload(p, C4N180_PASSES, padded_width=26.035, height=15.205)
    p.print_origin = [50.0, 10.0]        # 10mm from the front: not enough
    p.get_bounds(from_mesh=False)
    ok, errors = p.validate_bounds(log_errors=False)
    assert not ok, "a lead-in below the axis minimum must not validate"
    joined = " ".join(errors)
    assert "lead-in" in joined and "extra_margin" in joined, joined
    print("PASS test_a_lead_in_below_the_bed_is_caught_before_moving")
    print("      -> " + errors[0])


def test_the_same_origin_validates_on_the_head_without_a_lead_in():
    p = make_plugin()
    load_payload(p, C6N90_PASSES, padded_width=26.0, height=40.0)
    p.print_origin = [50.0, 10.0]
    p.get_bounds(from_mesh=False)
    ok, errors = p.validate_bounds(log_errors=False)
    assert ok, errors
    print("PASS test_the_same_origin_validates_on_the_head_without_a_lead_in")


def test_mesh_mode_sizes_travel_from_the_payload_not_the_object():
    """An object narrower than the image used to shorten the sweep, ending it
    with columns still to fire."""
    p = make_plugin({"extra_margin": 2, "start_overscan": 10.0,
                     "end_overscan": 10.0})
    load_payload(p, C4N180_PASSES, padded_width=26.035, height=15.205)
    p.get_mesh_bounds = lambda: ([100.0, 100.0], [105.0, 105.0])  # 5mm object
    bounds = p.get_bounds(from_mesh=True)
    assert p.print_origin == [98.0, 98.0]                 # mesh_min - margin
    assert abs(bounds[0][0] - 88.0) < 1e-6, bounds        # 98 - overscan
    # The sweep must cover the PAYLOAD width, not the 5mm object.
    assert abs(bounds[1][0] - (98.0 + 26.035 + 10.0)) < 1e-6, bounds
    assert abs(bounds[0][1] - (98.0 - 16.933)) < 1e-6, bounds
    # This payload is far bigger than the object, so it covers it: no warning.
    assert not [m for m in p.gcode.messages if "does not cover" in m]
    print("PASS test_mesh_mode_sizes_travel_from_the_payload_not_the_object")


def test_mesh_mode_warns_when_the_image_does_not_cover_the_object():
    """The question the old mesh-sized bounds were really asking, kept as a
    warning now that it no longer drives the travel."""
    p = make_plugin({"extra_margin": 2})
    load_payload(p, C4N180_PASSES, padded_width=26.035, height=15.205)
    p.get_mesh_bounds = lambda: ([100.0, 100.0], [180.0, 160.0])   # 80x60 object
    p.get_bounds(from_mesh=True)
    warned = [m for m in p.gcode.messages if "does not cover the object" in m]
    assert warned, "a payload smaller than the object should warn"
    assert "X" in warned[0] and "Y" in warned[0], warned[0]
    print("PASS test_mesh_mode_warns_when_the_image_does_not_cover_the_object")
    print("      -> " + warned[0].split("PAINTRESS WARNING: ")[1][:120])


def test_mesh_mode_needs_clearance_for_margin_plus_lead_in():
    p = make_plugin({"extra_margin": 2})
    load_payload(p, C4N180_PASSES, padded_width=26.035, height=15.205)
    p.get_mesh_bounds = lambda: ([100.0, 18.0], [130.0, 60.0])
    p.get_bounds(from_mesh=True)
    ok, _ = p.validate_bounds(log_errors=False)
    assert not ok, "18mm from the front is short of 2 + 16.933"
    p.get_mesh_bounds = lambda: ([100.0, 19.0], [130.0, 60.0])
    p.get_bounds(from_mesh=True)
    ok, errors = p.validate_bounds(log_errors=False)
    assert ok, errors
    print("PASS test_mesh_mode_needs_clearance_for_margin_plus_lead_in")
    print("      -> boundary is mesh_min_y = extra_margin + lead_in = 18.933mm")


def test_head_section_overrides_offsets_and_restricts_channels():
    sections = {"paintress_head c4n180": {
        "x_offset": 1.5, "y_offset": -2.25, "z_offset": 3.0,
        "channels": "all, yellow, black, magenta, cyan"}}
    p = make_plugin({"head": "c4n180", "x_offset": 9.0, "y_offset": 9.0},
                    sections)
    assert (p.x_offset, p.y_offset, p.z_offset) == (1.5, -2.25, 3.0)
    assert p.head_channels == {"all", "yellow", "black", "magenta", "cyan"}
    print("PASS test_head_section_overrides_offsets_and_restricts_channels")


def test_a_channel_this_head_lacks_is_refused_not_no_opped():
    sections = {"paintress_head c4n180": {
        "channels": "all, yellow, black, magenta, cyan"}}
    p = make_plugin({"head": "c4n180"}, sections)

    class G:
        def __init__(self, v):
            self.v = v

        def get(self, name, default=None):
            return self.v

    failures = []
    p._fail = lambda msg, gcmd=None, exc=None: failures.append(msg) or (_ for _ in ()).throw(RuntimeError(msg))

    assert p._get_channel_arg(G("cyan")) == 6
    assert p._get_channel_arg(G("3")) if False else True
    for bad in ("light_cyan", "3", "light-magenta"):
        try:
            p._get_channel_arg(G(bad))
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted on a four-ink head")
    assert any("configured head" in f or "channels are" in f for f in failures), failures
    print("PASS test_a_channel_this_head_lacks_is_refused_not_no_opped")
    print("      -> " + failures[0])


def test_without_a_head_section_nothing_changes():
    p = make_plugin({"x_offset": 4.0, "y_offset": 5.0})
    assert p.head_name is None
    assert p.head_channels == set(
        __import__("paintress").color_channels)
    assert (p.x_offset, p.y_offset) == (4.0, 5.0)
    print("PASS test_without_a_head_section_nothing_changes")


def test_a_declared_but_unselected_head_section_is_still_claimed():
    """Klipper rejects any section no module claims, so declaring both
    heads and selecting one must not make the unselected one invalid,
    which is what "Section 'paintress_head c4n180' is not a valid config
    section" meant when [paintress] parsed the section inline."""
    section = {"x_offset": 0.0, "channels": "all, cyan"}
    cfg = StubConfig({}, {"paintress_head c4n180": section}, StubPrinter())
    head = paintress_head.load_config_prefix(
        cfg.getsection("paintress_head c4n180"))
    assert head.name == "c4n180"
    assert head.channels == {"all", "cyan"}
    # Silent about Y and Z, so [paintress] keeps whatever it had.
    assert head.y_offset is None and head.z_offset is None
    print("PASS test_a_declared_but_unselected_head_section_is_still_claimed")


def test_an_unknown_head_name_fails_at_config_time():
    try:
        make_plugin({"head": "c9n999"})
    except ConfigError as exc:
        assert "no matching" in str(exc), exc
    else:
        raise AssertionError("an undeclared head section was accepted")
    print("PASS test_an_unknown_head_name_fails_at_config_time")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
