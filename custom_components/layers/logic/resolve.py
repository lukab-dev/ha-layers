"""Resolver: fold a lamp's base and its live layers into the effective command.

Pure: nothing here mutates the record, and time enters only through ``now``.
Expired layers are skipped, not removed; removing them (and deciding whether
that may send anything) is ``policy.expire``'s job.

Fold, bottom to top (SPEC section 3):

- start from the base's state, brightness and colour (all ``None`` when the
  base is unknown);
- a ``set`` layer decides on/off and is active either way. Off keeps the
  attributes below in the fold, so a higher ``set`` on without attributes
  inherits them; on (or no state at all) replaces the brightness and colour
  below with its own, where given;
- an ``adjust`` layer replaces brightness and colour, where given, and only
  while the fold is on. Over an off or unknown result it does nothing and is
  not active;
- colour is one atomic group: a layer's colour replaces the one below whole.
"""

from __future__ import annotations

from .model import (
    LAYER_BASE,
    MODE_ADJUST,
    MODE_SET,
    OFF,
    OFF_COMMAND,
    ON,
    Color,
    Command,
    Layer,
    Record,
    Resolution,
)

ACTIVE_NONE = "none"    # Resolution.active when nothing decides: no known base state and no layer on top


def _stack_key(layer: Layer) -> tuple[int, int]:
    """Stacking order: priority first, then the newer seq on top."""
    return (layer.priority, layer.seq)


def live_layers(rec: Record, now: float) -> list[Layer]:
    """The layers still in force at ``now`` (``expires_at`` None or later), bottom first."""
    return sorted((layer for layer in rec.layers.values() if layer.live(now)), key=_stack_key)


def _fold(rec: Record, now: float) -> tuple[Command | None, Layer | None]:
    """The effective command and the layer that decided it (None: the base, or nothing)."""
    base = rec.base
    state: str | None = base.state if base else None
    brightness: int | None = base.brightness if base else None
    color: Color | None = base.color if base else None
    top: Layer | None = None

    for layer in live_layers(rec, now):
        cmd = layer.command
        if layer.mode == MODE_SET:
            if cmd.state == OFF:
                state = OFF                     # attributes below stay in the fold
            else:                               # on (a set layer without a state turns on)
                state = ON
                if cmd.brightness is not None:
                    brightness = cmd.brightness
                if cmd.color is not None:
                    color = cmd.color
            top = layer
        elif layer.mode == MODE_ADJUST and state == ON:
            if cmd.brightness is not None:
                brightness = cmd.brightness
            if cmd.color is not None:
                color = cmd.color
            top = layer
        # An adjust over an off/unknown result, or a layer with an unknown mode, does nothing.

    if state is None:
        return None, top
    if state == OFF:
        return OFF_COMMAND, top
    return Command(ON, brightness, color), top


def resolve(rec: Record, now: float) -> Resolution:
    """The lamp's effective command at ``now`` and which layer decided it.

    ``command`` is ``None`` when the base state is unknown and no ``set`` layer
    is live ("do nothing"). ``active`` is the deciding layer's id, ``"base"``
    when the base decides, or ``"none"`` when nothing does.
    """
    command, top = _fold(rec, now)
    if top is not None:
        return Resolution(command, top.id)
    base_known = rec.base is not None and rec.base.state is not None
    return Resolution(command, LAYER_BASE if base_known else ACTIVE_NONE)


def active_layer(rec: Record, now: float) -> Layer | None:
    """The layer ``resolve()`` names as active, or ``None`` when the base (or nothing) decides."""
    return _fold(rec, now)[1]


def state_holder(rec: Record, now: float) -> Layer | None:
    """The top live ``set`` layer: the one that decides on/off, or ``None`` when the base does.

    An ``adjust`` layer can be active without holding the lamp: its "on" comes
    from below. The expiry and return rules ask this, not ``active_layer``,
    whether an owner still holds the lamp, so an adjust layer never lights it.
    """
    holders = [layer for layer in live_layers(rec, now) if layer.mode == MODE_SET]
    return holders[-1] if holders else None
