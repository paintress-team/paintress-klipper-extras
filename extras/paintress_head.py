# paintress_head.py: mechanical description of one fitted printhead.
#
# SPDX-FileCopyrightText: 2026 paintress-team
# SPDX-License-Identifier: GPL-3.0-or-later
"""A `[paintress_head <name>]` section.

It carries the two things a print payload cannot state: where nozzle 0 of
the leading column sits relative to the toolhead reference point (the
3D-printer nozzle), and which purge channels the head physically has.
`[paintress]` selects one of them with `head:`.

This is a config section in its own right, rather than a block
[paintress] parses inline, because Klipper rejects any section no module
claims. A machine that owns two heads should be able to declare both and
switch by editing one line, but the unselected section is, by
definition, never read, so an inline parse would make it "not a valid
config section". Claiming the prefix here makes every declared head
legal, and validates each one whether or not it is the one fitted.
"""

from .paintress import color_channels


def load_config_prefix(config):
    """Klipper entry point for a `[paintress_head <name>]` section."""
    return PaintressHead(config)


class PaintressHead:
    """One head's mechanical offsets and channel inventory.

    Every attribute is None when the section does not state it, which
    means "keep what `[paintress]` already had" rather than "zero". A
    section may therefore override just the one value that differs.
    """

    def __init__(self, config):
        self.name = config.get_name().split(None, 1)[-1]

        # Displacement from nozzle 0 of the leading column to the toolhead
        # reference point. Purely mechanical: unbolting the head moves it,
        # which is why it belongs with the head and not with the machine.
        self.x_offset = config.getfloat("x_offset", None)
        self.y_offset = config.getfloat("y_offset", None)
        self.z_offset = config.getfloat("z_offset", None)

        self.channels = self._parse_channels(config)

    def _parse_channels(self, config):
        """The channel names this head carries, or None if unstated.

        The INDEX of a name is the firmware's and never varies; the
        section only says which of them exist. A head that lacks a
        channel must refuse it rather than drive an all-zero mask, which
        powers the head up, fires nothing and powers it down. That
        looks like a purge that silently did nothing.
        """
        raw = config.get("channels", None)
        if raw is None:
            return None

        names = [n.strip().lower().replace("-", "_").replace(" ", "_")
                 for n in raw.split(",") if n.strip()]
        unknown = [n for n in names if n not in color_channels]
        if unknown:
            raise config.error(
                f"[{config.get_name()}] channels: unknown name(s) "
                f"{', '.join(unknown)}. Valid: {', '.join(color_channels)}"
            )
        if not names:
            raise config.error(
                f"[{config.get_name()}] channels: must list at least one name")
        return set(names)
