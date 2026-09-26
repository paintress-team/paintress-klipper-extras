<p align="center">
  <img src="docs/img/paintress.svg" alt="Paintress" width="160">
</p>

# paintress-klipper-extras

A Klipper add-on that prints with an inkjet head on a Klipper machine. It
handles the motion: it moves the head across the material while the
controller board fires the nozzles.

Part of [Paintress](https://paintress.dev), an open-source controller for
piezo inkjet printheads. The other published parts are
[paintress-rip-encoder](https://github.com/paintress-team/paintress-rip-encoder),
[paintress-protocol](https://github.com/paintress-team/paintress-protocol) and
[paintress-daemon](https://github.com/paintress-team/paintress-daemon).
The firmware is not published yet.

> **Status: experimental.** Paintress is not ready for general use yet, and
> things can change without notice.

This is where it sits in the chain:

```
  rip.py → RIP payload → encoder.py → job (.json+.bin) → paintress-daemon → firmware
                                          ▲
                                          │ TCP (NDJSON)
                              [ paintress-klipper-extras ]  ← this repository
                                          │
                                     Klipper motion
```

The add-on never talks to the firmware. Everything goes through the Paintress
daemon ([paintress-daemon](https://github.com/paintress-team/paintress-daemon))
over TCP.

## Contents

- [The files](#the-files)
- [How a print runs](#how-a-print-runs)
- [Installation](#installation)
- [Configuration](#configuration)
- [G-code commands](#g-code-commands)
- [An example session](#an-example-session)
- [Known issues](#known-issues)
- [Use of AI](#use-of-ai)

## The files

There are three, all in `extras/`:

- `paintress.py` is the Klipper add-on itself. Klipper loads any `.py` file
  in `klippy/extras/` and uses the file name as the config section, so you
  turn it on with a `[paintress]` section. It adds the `PAINTRESS_*` G-code
  commands and runs the print.
- `paintress_head.py` reads the optional `[paintress_head <name>]` sections,
  one for each head the machine can use. See
  [Head sections](#head-sections-optional).
- `paintressd_client.py` is a small TCP client for the daemon. `paintress.py`
  uses it to load jobs, start passes and send commands. You can also run it
  on its own to check that the daemon is reachable (connect, open the serial
  port, load a job). It can't run a print on its own, since that needs
  Klipper to move the head.

`paintress.py` is the only file that knows about Klipper.
`paintressd_client.py` is plain Python and only knows the daemon's protocol.

## How a print runs

A print is split into **passes** (also called swaths): strips of the image,
each printed in one sweep of the head along X. The job file, made by
[paintress-rip-encoder](https://github.com/paintress-team/paintress-rip-encoder),
holds the data for every pass and the Y position of each one. The add-on only
reads the job header that the daemon sends back. The daemon keeps the data.

For each pass the add-on:

1. moves the head to the start of the pass;
2. tells the daemon to start the pass (the board already has its data);
3. sweeps the head along X and raises the start trigger at a set distance,
   so the board starts firing;
4. the board fires one column after another while the head moves, so the
   gantry's speed sets the spacing.

The daemon, not the add-on, sends the pass data to the board. It holds two
passes on the board at a time, and sends pass N+1 while pass N prints. This
keeps big transfers out of Klipper, where they used to cause "Timer too
close" errors. Starting a pass just waits, on the daemon, until its data is
on the board.

### The print loop

`PAINTRESS_PRINT` runs these steps:

```
store_position()              save where the toolhead is
move_to_print_origin()        go up to z_offset, then to the start of pass 1
  for each pass:
    execute_swath_pass()      start the pass and sweep X at print_speed
    _await_swath_completion() check that the pass really fired (see below)
    prepare_next_swath()      move to the start of the next pass
return_to_stored_position()   go back to the saved position
```

After each sweep the add-on waits up to about 3 s for the daemon's
`PRINT_COMPLETE` event for that pass. That event proves the trigger reached
the board and the pass fired. If the trigger got lost, the print stops right
at that pass with a clear error. Without this check you would only find out at
the next pass, or, on the last pass, not at all, because the board's own
`TRIGGER_TIMEOUT` only comes after 10 s. The same wait also catches the error
events (trigger timeout, print error, pipeline error, lost link). It checks
the event stream in short steps so Klipper never stalls.

If anything fails during a print, the add-on tries to move back to the saved
position and then reports the error.

### Where the print goes

Three rectangles matter here, and they are different:

| | What it is | Where it comes from |
|---|---|---|
| `print_origin` | where the bottom-left corner of the image lands | you (`ORIGIN_X`/`ORIGIN_Y`) or `exclude_object` |
| `print_anchor` | the machine position that a pass offset of 0 maps to | `print_origin` + `x_offset`/`y_offset` |
| `print_bounds` | the area the toolhead actually travels | `print_anchor` + the job's passes and the overscan |

### The offsets, and which way they point

Klipper moves the **toolhead reference point**, which is the tip of the 3D
printing nozzle. The inkjet head sits somewhere else on the carriage, so the
offsets tell the add-on how far to shift the toolhead for the inkjet head to
land in the right place.

This makes the sign the opposite of what most people expect:

> `x_offset`/`y_offset` is the distance **from the inkjet head's reference
> point to the nozzle**, not from the nozzle to the inkjet head.

If the inkjet head sits 30 mm in **+X** from the nozzle, set `x_offset: -30`.
The add-on computes `print_anchor = print_origin + offset` and sends the
toolhead there. With the sign the wrong way round the image moves by **twice**
the offset, which looks like a scaling problem but isn't one.

The inkjet head's reference point is **nozzle 0 of its leading column**: the
column that reaches a spot first, at the bottom end of that column. On
`c4n180` that is cyan's first nozzle, on `c6n90` it is column 5's. Those are
different places on different heads, which is why the offsets can go in a
`[paintress_head <name>]` section.

Don't put the head's lead-in into the offsets. Some heads have to start
below the image, and the job already says so with negative Y positions in its
pass list (the add-on reads them from `travel.y_min_mm`). Adding it to
`y_offset` as well counts it twice.

The easy way to calibrate avoids the sign question: print with the offsets
you have, measure how far the print landed from where you wanted it, and
**subtract** that from the current values.

`PAINTRESS_GET_BOUNDS` works out all three rectangles, and
`PAINTRESS_PRINT` runs it too. The origin comes from one of two places:

- `ORIGIN_X` / `ORIGIN_Y`, if you give them.
- Otherwise `exclude_object`: the lower corner of the object's outline, minus
  `extra_margin`. This is how you print an image on top of a 3D-printed part.

The travel area always comes from the job. In X the head needs room to reach
full speed before the first column, so X covers the job's padded width plus
`start_overscan` and `end_overscan`. In Y the add-on uses the job's own pass
positions. It can't guess them: on a head whose inks are stacked along Y, the
first passes are **negative**, because the head has to start below the image
before its top nozzles can reach the first row. On such a head the travel area
starts below the origin and ends short of the top of the image. It also means
the add-on needs nothing head-specific to place a print.

Before printing, the add-on checks the travel area against the machine's axis
limits. Two things to know:

- Below an object you need `extra_margin` **plus** the job's lead-in. If the
  check fails for that reason, the message says so. **Don't raise
  `extra_margin` to make room**: it moves the origin, so it moves where the
  ink lands. Move the origin up instead.
- With `exclude_object`, a job smaller than the object only gives a warning.
  Printing a small image on part of a big object is fine. The image starts at
  the origin and stops where it ends.

### Column timing

When a job is opened, the add-on works out the time between two column
firings from the job's DPI and `print_speed`:

```
column_fire_interval_us = round(25_400_000 / (dpi * print_speed))
```

This value is sent with every pass, so a board reset can't quietly lose it.
It is the only timing value. The board always waits for the hardware trigger
to start, so there is no acceleration delay to set.

## Installation

Klipper loads add-ons from `klippy/extras/`. Copy (or link) the three files
there on the Klipper host:

```sh
cp extras/paintress.py         ~/klipper/klippy/extras/
cp extras/paintress_head.py    ~/klipper/klippy/extras/
cp extras/paintressd_client.py ~/klipper/klippy/extras/
```

`paintressd_client.py` only uses the Python standard library, so there is
nothing else to install. Restart Klipper after copying.

The Paintress daemon has to be running and reachable over TCP, by default on
`localhost:9000`.

## Configuration

Add a `[paintress]` section to the printer config:

```ini
[paintress]
socket_port: 9000
serial_port: /dev/serial/by-id/usb-XXXXXXXX-if00
base_path: /home/pi/printer_data/paintress/
x_offset: 0.0
y_offset: 0.0
z_offset: 1.0
z_lift_speed: 5.0
move_speed: 300
move_accel: 5000
print_speed: 200
print_accel: 5000
start_overscan: 10.0
end_overscan: 10.0
extra_margin: 2
trigger_output_pin: paintress_trigger
#head: c4n180
#trigger_distance: 10.0
#verbose: false
#auto_connect: true
```

### The trigger pin

Each pass starts on a hardware signal. The add-on drives it through a normal
`[output_pin]` that you declare yourself; it doesn't own the pin. Wire that
pin (on the motion board) to the trigger input of the controller board, and
put the section name in `trigger_output_pin`, which is required:

```ini
[output_pin paintress_trigger]
pin: PE6
```

### Head sections (optional)

`head:` names a `[paintress_head <name>]` section. It holds the two things a
job can't tell the add-on: where nozzle 0 sits relative to the toolhead, and
which purge channels the head really has. Changing heads is then a one-line
change.

```ini
[paintress]
head: c4n180

[paintress_head c4n180]
# These offsets are EXAMPLES, not measurements: every carriage is different.
# Calibrate them (see the note on the sign above). They are zero until you do.
x_offset: 1.5
y_offset: -2.25
z_offset: 3.0
channels: all, yellow, black, magenta, cyan

[paintress_head c6n90]
x_offset: 0.0
y_offset: 0.0
channels: all, yellow, black, light_cyan, light_magenta, magenta, cyan
```

You can declare heads you aren't using right now; the example above is a
machine with both. The sections are read by `paintress_head.py`, so install
that file next to `paintress.py`, or Klipper stops with
`Section 'paintress_head c4n180' is not a valid config section`.

A value the section leaves out is not set to zero. It keeps the value from
`[paintress]`, so a section can change just one offset.

Without `head:` nothing changes: the `[paintress]` offsets are used and every
channel name is accepted.

`channels` lists the channel names this head has. The number behind each name
comes from the firmware and never changes (see the table below); the section
only says which ones exist. A purge on a channel the head doesn't have is
refused. Otherwise the board would power the head up, fire nothing and power
it down again, which looks like a purge that silently did nothing.

A head section doesn't change where the head travels. That comes from the
job, and the daemon already refuses a job made for a different head.

### Options

Every option has a default, so you only set the ones that differ on your
machine.

| Option            | Default                       | What it does |
|-------------------|-------------------------------|--------------|
| `socket_port`     | `9000`                        | TCP port of the daemon (on `localhost`). |
| `auto_connect`    | `true`                        | Connect to the daemon **and** open the board's serial port as soon as Klipper is ready, so you don't need the `PAINTRESS_CONNECT_*` commands. If the daemon isn't up yet, Klipper still starts: you get a warning and can connect by hand later. Set `false` to always connect by hand. |
| `serial_port`     | a `/dev/serial/by-id/...` path | The serial device the daemon opens to reach the board. |
| `base_path`       | `/home/pi/printer_data/paintress/` | Folder for job files. Files outside it can't be opened (see `PAINTRESS_SET_DIRECTORY`). |
| `x_offset`        | `0.0`                         | X distance **from** the inkjet head's reference point **to** the toolhead reference point (mm). See the note on the sign above. |
| `y_offset`        | `0.0`                         | Y distance **from** the inkjet head's reference point **to** the toolhead reference point (mm). See the note on the sign above. |
| `z_offset`        | `1.0`                         | How far the toolhead goes up to bring the inkjet head to printing height (mm). It covers both the fixed height difference between the 3D nozzle and the ink outlet, and the small gap to the material. The outlet is always above the nozzle. |
| `z_lift_speed`    | `5.0`                         | Speed of the Z move into and out of printing height (mm/s). |
| `move_speed`      | `300`                         | Speed of moves that don't print (mm/s). |
| `move_accel`      | `5000`                        | Acceleration of moves that don't print (mm/s²). |
| `print_speed`     | `200`                         | Speed of the printing sweep (mm/s). It sets the firing rate: the add-on sends `25_400_000 / (dpi * print_speed)` µs between columns with every pass. |
| `print_accel`     | `5000`                        | Acceleration of the printing sweep (mm/s²). |
| `start_overscan`  | `10.0`                        | Room before the print so the head is at full speed (mm). |
| `end_overscan`    | `10.0`                        | Room after the print (mm). It has to be enough to slow down from `print_speed`: 4 mm at 200 mm/s and 5000 mm/s². |
| `extra_margin`    | `2`                           | How far the image runs past the object when printing over it with `exclude_object` (mm). It moves the **origin**, so don't raise it to make room for a head's lead-in: the image would move off the object. |
| `head`            | none                          | Name of a `[paintress_head <name>]` section with this head's offsets and purge channels. Optional. Without it the `[paintress]` offsets are used and every channel name is accepted. It doesn't change where the head travels. |
| `trigger_output_pin` | required                   | Name of the `[output_pin]` wired to the board's trigger input. The add-on sets it with `SET_PIN`. The sweep is split at `trigger_distance` and the pin goes high there, so the motion board raises it at an exact position and the TCP/USB delays don't shift the passes. The pin is set low before each pass and after every sweep, even a failed one, so it can't stay high. |
| `trigger_distance`| `start_overscan`              | How far X travels from the start of the pass before the trigger goes high (mm). It must be at least the acceleration distance, so the head is at full speed, and shorter than the pass. |
| `verbose`         | `false`                       | If `true`, prints an `OK G1 ...` line for every move. Errors and important status are always shown. |

Use a `/dev/serial/by-id/...` path for `serial_port`. It stays the same after
a reboot or when you plug the board back in.

## G-code commands

### Connection

| Command                      | Arguments | What it does |
|-------------------------------|-----------|--------------|
| `PAINTRESS_CONNECT_SOCKET`    | none      | Connect to the daemon. |
| `PAINTRESS_DISCONNECT_SOCKET` | none      | Disconnect from the daemon. |
| `PAINTRESS_CONNECT_SERIAL`    | none      | Ask the daemon to open the board's serial port. |
| `PAINTRESS_DISCONNECT_SERIAL` | none      | Ask the daemon to close the board's serial port. |

With `auto_connect: true` (the default) both connections open by themselves
when Klipper is ready. You only need these commands after a disconnect, or if
`auto_connect` is off.

### Status

| Command            | Arguments | What it does |
|--------------------|-----------|--------------|
| `PAINTRESS_STATUS` | none      | Show the daemon and add-on status in the console. |

### Jobs

| Command                    | Arguments   | What it does |
|----------------------------|-------------|--------------|
| `PAINTRESS_SET_DIRECTORY`  | `DIRECTORY=` | Pick the folder to load jobs from (inside `base_path`). |
| `PAINTRESS_OPEN_PAYLOAD`   | `FILENAME=`  | Load a job `.json` into the daemon and set up the timing. |
| `PAINTRESS_CLOSE_PAYLOAD`  | none        | Unload the job from the daemon. |

### Printing

| Command                | Arguments                            | What it does |
|------------------------|--------------------------------------|--------------|
| `PAINTRESS_GET_BOUNDS` | `FROM_MESH=` (true or false, default `true`) | Work out where the print goes. `true` uses `exclude_object`; `false` uses the job at the current origin. |
| `PAINTRESS_PRINT`      | `ORIGIN_X=` `ORIGIN_Y=` (optional)   | Run the whole print. With an origin, the job is placed there; without one, it goes over the `exclude_object` area. Give `ORIGIN_X` and `ORIGIN_Y` together or not at all. |

### Maintenance and calibration

| Command                          | Arguments | What it does |
|----------------------------------|-----------|--------------|
| `PAINTRESS_PURGE`                | `CHANNEL=` `PULSES=` (default `10`) | Fire a channel's nozzles `PULSES` times to clear or prime them. |
| `PAINTRESS_TEST_TRIGGER`         | `PIN=` `MOVE=` `DISTANCE=` `TRIGGER_DISTANCE=` `SPEED=` `ACCEL=` `PULSE_MS=` (all optional) | Pulse the trigger pin with `SET_PIN` so you can check it on a scope. Uses neither the daemon nor the board. `MOVE=1` raises it partway through an X move, like a real pass; `MOVE=0` pulses it without moving. |
| `PAINTRESS_SET_TRIGGER_DISTANCE` | `VALUE=` (mm, optional) | Show the trigger distance (without `VALUE`) or change it. The change lasts until Klipper restarts. |

#### Channel names

`CHANNEL` takes a number (`0` to `6`) or a name:

| Name            | Number |
|-----------------|--------|
| `all`           | `0`    |
| `yellow`        | `1`    |
| `black`         | `2`    |
| `light_cyan`    | `3`    |
| `light_magenta` | `4`    |
| `magenta`       | `5`    |
| `cyan`          | `6`    |

The numbers follow the firmware's `channel_mask_id_t` list
(`engine/channels.h` in the firmware), and the two are kept the same.

Every command answers `OK <command>` or `ERROR <command>: ...` in the Klipper
console. A failed command never stops Klipper.

## An example session

```gcode
; 1. Connect (the daemon must already be running)
PAINTRESS_CONNECT_SOCKET
PAINTRESS_CONNECT_SERIAL

; 2. (optional) pick a folder inside base_path
PAINTRESS_SET_DIRECTORY DIRECTORY=my_job

; 3. Load the job (this also sets the timing)
PAINTRESS_OPEN_PAYLOAD FILENAME=job.json

; 4. Home and place the object as usual, then print.
;    Without ORIGIN_*, the print goes over the exclude_object area:
PAINTRESS_PRINT
;    ...or at a given origin:
PAINTRESS_PRINT ORIGIN_X=20 ORIGIN_Y=30

; 5. Clean up
PAINTRESS_CLOSE_PAYLOAD
PAINTRESS_DISCONNECT_SERIAL
PAINTRESS_DISCONNECT_SOCKET
```

`PAINTRESS_PRINT` saves the toolhead position, works out and checks where the
print goes, prints every pass, and goes back to the saved position.

The tests are in `tests/`: `test_positioning.py` (travel area, both origin
modes, head sections), `test_client_reconnect.py` (the client reconnecting)
and `test_event_inbox.py` (reading the event stream and reporting errors).

## Known issues

Things we know are wrong and haven't fixed yet.

- **There is no `klippy:disconnect` handler.** `paintress.py` only listens to
  `klippy:shutdown`, `klippy:connect` and `klippy:ready`. A `RESTART`, a
  `FIRMWARE_RESTART` or a normal Klipper stop can close the host without
  telling the daemon to stop the head. The fix is a disconnect handler that
  does the same as `on_shutdown`.
- **The add-on's own moves depend on the G-code state.** `_g1()` sends a
  plain `G1 X.. Y.. F..`, without `SAVE_GCODE_STATE` or `G90`. If a `G91`, a
  `G92` or a `SET_GCODE_OFFSET` is still active from you or from a macro,
  every move of the print is shifted. The fix is to save the state and switch
  to absolute moves around them, or to move the toolhead directly.
- **`M220` breaks the column spacing.** The firing interval comes from the
  configured `print_speed`, and the speed factor set by `M220` is never read.
  With `M220 S50` the head moves at half speed while the board still fires at
  the full-speed rate, so the columns end up half as far apart as they
  should. The fix is to apply the speed factor to the interval, or to refuse
  to print when it isn't 100 %.

## Use of AI

The architecture of Paintress was planned by people, and so was the reverse
engineering behind it: probing the original controller with an oscilloscope
and a logic analyser, and working out from those captures how the head is
driven. The first tests and the first printed lines were also done by
people, on the bench.

AI tools were used in a limited way: to fix bugs, to help keep the stages of
the pipeline consistent with each other, to write and edit the
documentation, and most of all on the communication between the daemon and
the firmware.

## License

Copyright (C) 2026 paintress-team.

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. The full text is in [`LICENSE`](LICENSE).

In short: if you share a changed version of this code, or a product that
includes it, you have to share its source under the same license. Klipper is
GPLv3 too, and these files run inside it.

Contributions are welcome. Sign off your commits as explained in
[`CONTRIBUTING.md`](CONTRIBUTING.md).
