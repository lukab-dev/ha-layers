"""The resolver: base folded with the live layers, bottom to top (SPEC section 3)."""

from __future__ import annotations

import itertools

from layers_logic.model import (
    OFF_COMMAND,
    Color,
    Command,
    Layer,
    Record,
    Resolution,
)
from layers_logic.resolve import active_layer, live_layers, resolve, state_holder

NOW = 1000.0
_seq = itertools.count(1)

WARM = Color.kelvin(2700)
RED_XY = Color.xy(0.64, 0.33)


def layer(
    layer_id: str,
    priority: int,
    command: Command,
    mode: str = "set",
    *,
    seq: int | None = None,
    expires_at: float | None = None,
) -> Layer:
    return Layer(
        id=layer_id,
        priority=priority,
        mode=mode,
        requested=command,
        command=command,
        seq=next(_seq) if seq is None else seq,
        set_at=0.0,
        expires_at=expires_at,
    )


def record(base: Command | None, *layers: Layer) -> Record:
    rec = Record("light.lamp_a", base=base)
    for lay in layers:
        rec.layers[lay.id] = lay
    return rec


# --------------------------------------------------------------------------- #
# Base only
# --------------------------------------------------------------------------- #


def test_unknown_base_and_no_layers_does_nothing():
    assert resolve(record(None), NOW) == Resolution(None, "none")
    assert active_layer(record(None), NOW) is None


def test_known_base_is_the_effective_command():
    base = Command("on", 102, WARM)
    assert resolve(record(base), NOW) == Resolution(base, "base")
    assert resolve(record(OFF_COMMAND), NOW) == Resolution(OFF_COMMAND, "base")
    assert active_layer(record(base), NOW) is None


def test_unknown_base_with_set_off_turns_off():
    rec = record(None, layer("tv", 40, Command("off")))
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "tv")


def test_unknown_base_with_set_on_turns_on_with_only_its_own_attributes():
    rec = record(None, layer("sig", 70, Command("on", 255, RED_XY)))
    assert resolve(rec, NOW) == Resolution(Command("on", 255, RED_XY), "sig")
    rec = record(None, layer("sig", 70, Command("on")))
    assert resolve(rec, NOW) == Resolution(Command("on"), "sig")


def test_unknown_base_with_only_adjust_does_nothing():
    rec = record(None, layer("dim", 40, Command(None, 64), "adjust"))
    assert resolve(rec, NOW) == Resolution(None, "none")
    assert active_layer(rec, NOW) is None


def test_stateless_base_seeds_attributes_but_decides_nothing():
    # A partial base (e.g. take_back of a brightness-only call on an unknown base).
    rec = record(Command(None, 30))
    assert resolve(rec, NOW) == Resolution(None, "none")
    rec.layers["nl"] = layer("nl", 50, Command("on"))
    assert resolve(rec, NOW) == Resolution(Command("on", 30), "nl")


# --------------------------------------------------------------------------- #
# set layers
# --------------------------------------------------------------------------- #


def test_set_off_over_base_on():
    rec = record(Command("on", 200, WARM), layer("tv", 40, Command("off")))
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "tv")
    assert active_layer(rec, NOW) is rec.layers["tv"]


def test_set_on_replaces_only_the_attributes_it_gives():
    base = Command("on", 200, WARM)
    assert resolve(record(base, layer("a", 10, Command("on", 50))), NOW).command == Command("on", 50, WARM)
    assert resolve(record(base, layer("a", 10, Command("on", None, RED_XY))), NOW).command == Command(
        "on", 200, RED_XY
    )


def test_set_on_without_attributes_inherits_from_base():
    rec = record(Command("on", 102, WARM), layer("nl", 50, Command("on")))
    assert resolve(rec, NOW) == Resolution(Command("on", 102, WARM), "nl")


def test_set_on_inherits_through_a_set_off_below_it():
    rec = record(
        Command("on", 102, WARM),
        layer("tv", 40, Command("off")),
        layer("nl", 60, Command("on")),
    )
    assert resolve(rec, NOW) == Resolution(Command("on", 102, WARM), "nl")


def test_set_on_inherits_from_a_lower_set_on_hidden_by_a_set_off():
    rec = record(
        OFF_COMMAND,
        layer("low", 10, Command("on", 80, RED_XY)),
        layer("tv", 40, Command("off")),
        layer("top", 60, Command("on")),
    )
    assert resolve(rec, NOW) == Resolution(Command("on", 80, RED_XY), "top")


def test_set_on_over_off_base_has_no_attributes_to_inherit():
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on")))
    assert resolve(rec, NOW) == Resolution(Command("on"), "nl")


def test_set_off_ignores_attributes_of_its_own_command():
    # An off layer never contributes attributes, even if one slipped into its command.
    rec = record(Command("on", 102, WARM), layer("tv", 40, Command("off", 7)), layer("nl", 60, Command("on")))
    assert resolve(rec, NOW).command == Command("on", 102, WARM)


def test_set_layer_without_state_turns_on():
    rec = record(OFF_COMMAND, layer("nl", 50, Command(None, 13, WARM)))
    assert resolve(rec, NOW) == Resolution(Command("on", 13, WARM), "nl")


# --------------------------------------------------------------------------- #
# adjust layers
# --------------------------------------------------------------------------- #


def test_adjust_over_on_changes_attributes_and_is_active():
    rec = record(Command("on", 200, WARM), layer("dim", 40, Command(None, 64), "adjust"))
    assert resolve(rec, NOW) == Resolution(Command("on", 64, WARM), "dim")
    assert active_layer(rec, NOW) is rec.layers["dim"]


def test_adjust_over_off_base_has_no_effect_and_is_not_active():
    rec = record(OFF_COMMAND, layer("dim", 40, Command(None, 64, RED_XY), "adjust"))
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "base")
    assert active_layer(rec, NOW) is None


def test_adjust_over_set_off_leaves_the_set_layer_active():
    rec = record(
        Command("on", 200, WARM),
        layer("tv", 40, Command("off")),
        layer("dim", 60, Command(None, 64), "adjust"),
    )
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "tv")
    assert active_layer(rec, NOW) is rec.layers["tv"]


def test_adjust_over_a_set_on_layer():
    rec = record(
        OFF_COMMAND,
        layer("sig", 40, Command("on", 200, RED_XY)),
        layer("dim", 60, Command(None, 20), "adjust"),
    )
    assert resolve(rec, NOW) == Resolution(Command("on", 20, RED_XY), "dim")


def test_adjust_never_turns_a_lamp_on_or_off():
    # Its state is ignored: it neither lights an off lamp nor turns an on lamp off.
    rec = record(OFF_COMMAND, layer("dim", 40, Command("on", 64), "adjust"))
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "base")
    rec = record(Command("on", 200), layer("dim", 40, Command("off"), "adjust"))
    assert resolve(rec, NOW) == Resolution(Command("on", 200), "dim")


def test_adjust_applies_in_place_not_to_a_set_on_above_it():
    # Below the set-on the fold was off, so the adjust did nothing; the set-on
    # then inherits from the base, not from the adjust.
    rec = record(
        OFF_COMMAND,
        layer("dim", 10, Command(None, 64), "adjust"),
        layer("nl", 50, Command("on")),
    )
    assert resolve(rec, NOW) == Resolution(Command("on"), "nl")


def test_set_on_above_an_adjust_replaces_what_it_gives():
    rec = record(
        Command("on", 200, WARM),
        layer("dim", 10, Command(None, 64, RED_XY), "adjust"),
        layer("nl", 50, Command("on", 5)),
    )
    assert resolve(rec, NOW) == Resolution(Command("on", 5, RED_XY), "nl")


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def test_colour_is_replaced_atomically_never_merged():
    hs = Color("hs_color", (14.0, 100.0))
    rec = record(Command("on", 200, RED_XY), layer("warm", 40, Command("on", None, WARM)))
    assert resolve(rec, NOW).command.color == WARM
    rec = record(Command("on", 200, WARM), layer("hue", 40, Command(None, None, hs), "adjust"))
    assert resolve(rec, NOW).command == Command("on", 200, hs)


def test_colours_are_carried_exactly_as_written():
    odd = Color.xy(0.6412345, 0.3298765)
    rgb = Color("rgb_color", (255.0, 0.0, 0.0))
    assert resolve(record(Command("on", 1, odd)), NOW).command.color.value == (0.6412345, 0.3298765)
    rec = record(Command("on", 1, odd), layer("sig", 70, Command("on", 255, rgb)))
    assert resolve(rec, NOW).command.color == Color("rgb_color", (255.0, 0.0, 0.0))


# --------------------------------------------------------------------------- #
# Ordering and expiry
# --------------------------------------------------------------------------- #


def test_priority_orders_the_stack_regardless_of_seq_or_insertion():
    high = layer("high", 70, Command("on", 255, RED_XY), seq=1)
    low = layer("low", 40, Command("off"), seq=9)
    for rec in (record(Command("on", 100), high, low), record(Command("on", 100), low, high)):
        assert resolve(rec, NOW) == Resolution(Command("on", 255, RED_XY), "high")
        assert [lay.id for lay in live_layers(rec, NOW)] == ["low", "high"]


def test_same_priority_newer_seq_is_on_top():
    older = layer("older", 50, Command("on", 10), seq=3)
    newer = layer("newer", 50, Command("off"), seq=4)
    rec = record(Command("on", 100), newer, older)
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "newer")
    assert [lay.id for lay in live_layers(rec, NOW)] == ["older", "newer"]


def test_expired_layers_are_ignored():
    rec = record(
        Command("on", 100, WARM),
        layer("gone", 70, Command("on", 255, RED_XY), expires_at=NOW - 1),
        layer("edge", 60, Command("off"), expires_at=NOW),          # expires_at <= now: expired
        layer("live", 40, Command(None, 30), "adjust", expires_at=NOW + 0.001),
    )
    assert resolve(rec, NOW) == Resolution(Command("on", 30, WARM), "live")
    assert [lay.id for lay in live_layers(rec, NOW)] == ["live"]
    assert active_layer(rec, NOW) is rec.layers["live"]


def test_everything_expired_falls_through_to_the_base_now():
    rec = record(Command("on", 100, WARM), layer("tv", 40, Command("off"), expires_at=NOW))
    assert resolve(rec, NOW - 1) == Resolution(OFF_COMMAND, "tv")
    assert resolve(rec, NOW) == Resolution(Command("on", 100, WARM), "base")
    rec.base = Command("on", 20)        # a live command, not a snapshot: the new base shows at once
    assert resolve(rec, NOW) == Resolution(Command("on", 20), "base")


def test_resolve_does_not_mutate_the_record():
    rec = record(Command("on", 100), layer("gone", 70, Command("off"), expires_at=NOW - 1))
    before = rec.to_json()
    resolve(rec, NOW)
    active_layer(rec, NOW)
    live_layers(rec, NOW)
    assert rec.to_json() == before
    assert "gone" in rec.layers


def test_removing_a_layer_falls_through_to_what_is_below_now():
    rec = record(
        Command("on", 100, WARM),
        layer("low", 30, Command("on", 50)),
        layer("tv", 40, Command("off")),
    )
    assert resolve(rec, NOW).command == OFF_COMMAND
    del rec.layers["tv"]
    assert resolve(rec, NOW) == Resolution(Command("on", 50, WARM), "low")


# --------------------------------------------------------------------------- #
# active_layer
# --------------------------------------------------------------------------- #


def test_active_layer_is_the_top_deciding_layer():
    rec = record(
        Command("on", 100),
        layer("low", 10, Command("on", 50)),
        layer("mid", 40, Command("off")),
        layer("top", 60, Command(None, 5), "adjust"),       # over off: not active
    )
    assert active_layer(rec, NOW) is rec.layers["mid"]
    assert resolve(rec, NOW).active == "mid"


def test_active_layer_agrees_with_resolve():
    cases = [
        record(None),
        record(Command("on", 100)),
        record(OFF_COMMAND, layer("a", 10, Command(None, 5), "adjust")),
        record(Command("on", 100), layer("a", 10, Command(None, 5), "adjust")),
        record(None, layer("b", 20, Command("off")), layer("c", 30, Command(None, 5), "adjust")),
    ]
    for rec in cases:
        res = resolve(rec, NOW)
        top = active_layer(rec, NOW)
        if top is None:
            assert res.active in ("base", "none")
        else:
            assert res.active == top.id


# --------------------------------------------------------------------------- #
# state_holder
# --------------------------------------------------------------------------- #


def test_state_holder_is_the_top_live_set_layer():
    rec = record(
        Command("on", 100),
        layer("low", 10, Command("on", 50)),
        layer("mid", 40, Command("off")),
        layer("top", 60, Command(None, 5), "adjust"),
    )
    assert state_holder(rec, NOW) is rec.layers["mid"]


def test_an_active_adjust_layer_does_not_hold_the_lamp():
    # Its "on" is the base's: the expiry and return rules must not light a lamp through it.
    rec = record(Command("on", 200), layer("dim", 5, Command(None, 80), "adjust"))
    assert active_layer(rec, NOW) is rec.layers["dim"]
    assert state_holder(rec, NOW) is None
    rec = record(Command("on", 200), layer("low", 5, Command("on", 90)), layer("dim", 30, Command(None, 8), "adjust"))
    assert active_layer(rec, NOW) is rec.layers["dim"]
    assert state_holder(rec, NOW) is rec.layers["low"]


def test_state_holder_ignores_expired_layers():
    rec = record(Command("on", 200), layer("tv", 40, Command("off"), expires_at=NOW))
    assert state_holder(rec, NOW - 1) is rec.layers["tv"]
    assert state_holder(rec, NOW) is None
