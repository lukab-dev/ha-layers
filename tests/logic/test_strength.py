"""Strength: sticky and locked layers against changes Layers did not make (SPEC 5.3, 6.5)."""

from __future__ import annotations

import itertools

import pytest
from layers_logic.classify import CallInfo, change_scope
from layers_logic.model import (
    OFF_COMMAND,
    Color,
    Command,
    Layer,
    Observed,
    Record,
    SetRequest,
    Tombstone,
)
from layers_logic.policy import PolicyError, apply_clear, apply_external, apply_set
from layers_logic.resolve import resolve

NOW = 1000.0
LAMP = "light.lamp_a"
OTHER = "light.lamp_b"
WARM = Color.kelvin(2700)
ORANGE = Color.xy(0.6, 0.38)
SIGNAL = Command("on", 255, ORANGE)
RELAX = Command("on", 90, WARM)
_seq = itertools.count(1)


def layer(layer_id: str, priority: int, command: Command, *, strength: str = "soft",
          mode: str = "set", expires_at: float | None = None, resume: bool = False) -> Layer:
    return Layer(id=layer_id, priority=priority, mode=mode, requested=command, command=command,
                 seq=next(_seq), set_at=0.0, expires_at=expires_at, resume_after_manual=resume,
                 strength=strength)


def record(base: Command | None, *layers: Layer, shows: Observed | None = None) -> Record:
    rec = Record(LAMP, base=base)
    for lay in layers:
        rec.layers[lay.id] = lay
    rec.observed = shows
    return rec


def call(*, user: str | None = None, lamps: frozenset[str] = frozenset({LAMP}),
         via: frozenset[str] = frozenset(), scene: bool = False) -> CallInfo:
    return CallInfo("ctx", "user" if user else "automation", user, "turn_on", RELAX,
                    RELAX.groups(), lamps, via, NOW, scene=scene)


# --------------------------------------------------------------------------- scope (6.5)


@pytest.mark.parametrize(("source", "info", "scope"), [
    ("device", None, "lamp"),                               # its own switch or dimmer
    ("user", call(user="u1"), "lamp"),                      # its tile in the app
    ("user", None, "lamp"),                                 # a person, no call known
    ("user", call(user="u1", scene=True), "room"),          # a person running a scene
    ("user", call(user="u1", lamps=frozenset({LAMP, OTHER})), "room"),
    ("user", call(user="u1", via=frozenset({LAMP})), "room"),   # a vendor room group
    ("automation", call(), "room"),                         # a wall button, a motion sensor
    ("automation", call(scene=True), "room"),
    ("automation", None, "room"),
])
def test_scope(source: str, info: CallInfo | None, scope: str) -> None:
    assert change_scope(LAMP, source, info) == scope


# --------------------------------------------------------------------------- sticky


def test_a_room_change_goes_under_a_sticky_layer():
    tv = layer("tv", 40, Command("on", 40))
    trash = layer("trash", 70, SIGNAL, strength="sticky", expires_at=NOW + 3600)
    rec = record(Command("on", 200, WARM), tv, trash, shows=Observed("on", 90, "color_temp",
                                                                        kelvin=2700))

    res = apply_external(rec, RELAX, None, "automation", "take_back", NOW, scope="room")

    assert res.dropped == ("tv",)                   # the soft layer: taken back as ever
    assert res.held == ("trash",)
    assert set(rec.layers) == {"trash"}
    assert rec.layers["trash"] is trash             # untouched, lease and all
    assert rec.base == RELAX                        # the scene is what is underneath now
    assert set(rec.tombstones) == {"tv"}
    assert rec.diverged == "delivery"               # it shows the scene, not the signal
    assert resolve(rec, NOW).command == SIGNAL
    assert rec.last_external.scope == "room"
    assert rec.last_external.held == ("trash",)


def test_a_room_off_leaves_a_sticky_signal_lit():
    """No exception for off: one rule is easier to predict. The lease bounds it."""
    trash = layer("trash", 70, SIGNAL, strength="sticky", expires_at=NOW + 3600)
    rec = record(Command("on", 200, WARM), trash, shows=Observed("off"))

    res = apply_external(rec, OFF_COMMAND, None, "automation", "take_back", NOW, scope="room")

    assert res.held == ("trash",)
    assert rec.base == OFF_COMMAND
    assert resolve(rec, NOW).command == SIGNAL
    # Cleared later, the lamp falls to what the room is now: off.
    apply_clear(rec, "trash", NOW + 60)
    assert resolve(rec, NOW + 60).command == OFF_COMMAND


def test_a_hand_on_the_lamp_dismisses_a_sticky_layer_and_tombstones_it():
    trash = layer("trash", 70, SIGNAL, strength="sticky", expires_at=NOW + 3600, resume=True)
    rec = record(Command("on", 200, WARM), trash, shows=Observed("on", 40, "color_temp",
                                                                   kelvin=2700))
    shown = Command("on", 40, WARM)

    res = apply_external(rec, shown, None, "device", "take_back", NOW, scope="lamp")

    assert res.dropped == ("trash",)
    assert res.held == ()
    assert rec.layers == {}
    assert rec.base == shown
    assert rec.tombstones["trash"] == Tombstone("trash", NOW, NOW + 3600, True, "device", "sticky")
    # The owner's next set of it (a reminder) is skipped on this lamp: only the lamp is silenced.
    again = apply_set(rec, SetRequest("trash", SIGNAL, priority=70, strength="sticky"), NOW + 5,
                      lambda: 99)
    assert again.result == "skipped_tombstoned"


@pytest.mark.parametrize("policy", ["base_keep_layers", "edit_active"])
def test_a_hand_on_the_lamp_dismisses_a_sticky_layer_under_any_policy(policy: str):
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    rec = record(Command("on", 200, WARM), trash, shows=Observed("on", 40))

    res = apply_external(rec, Command("on", 40), None, "device", policy, NOW, scope="lamp")

    assert res.dropped == ("trash",)
    assert "trash" in rec.tombstones


def test_dropped_ids_keep_the_stack_order():
    tv = layer("tv", 40, Command("on", 40))
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    top = layer("test", 99, Command("on", 10))
    rec = record(Command("on", 200, WARM), top, trash, tv, shows=Observed("on", 40))

    res = apply_external(rec, Command("on", 40), None, "device", "take_back", NOW, scope="lamp")

    assert res.dropped == ("tv", "trash", "test")


# --------------------------------------------------------------------------- locked


@pytest.mark.parametrize(("source", "scope"), [("device", "lamp"), ("user", "lamp"),
                                               ("automation", "room")])
def test_every_change_goes_under_a_locked_layer(source: str, scope: str):
    red = Command("on", 255, Color.xy(0.68, 0.31))
    leak = layer("leak", 95, red, strength="locked")
    rec = record(Command("on", 200, WARM), leak, shows=Observed("off"))

    res = apply_external(rec, OFF_COMMAND, None, source, "take_back", NOW, scope=scope)

    assert res.dropped == ()
    assert res.held == ("leak",)
    assert rec.base == OFF_COMMAND
    assert resolve(rec, NOW).command == red
    assert rec.diverged == "delivery"


def test_a_held_layer_the_lamp_already_shows_is_not_marked():
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    rec = record(Command("on", 200, WARM), trash,
                 shows=Observed("on", 255, "xy", xy=(0.6, 0.38)))

    res = apply_external(rec, Command("on", None, WARM), frozenset({"color"}), "automation",
                         "take_back", NOW, scope="room")

    assert res.held == ("trash",)
    assert rec.diverged is None


def test_a_reassert_keeps_every_layer_as_before():
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    rec = record(Command("on", 200, WARM), trash, shows=Observed("off"))

    res = apply_external(rec, OFF_COMMAND, None, "device", "reassert", NOW, scope="lamp")

    assert res.reassert
    assert res.dropped == () and res.held == ()
    assert "trash" in rec.layers


# --------------------------------------------------------------------------- set


def test_set_creates_with_a_strength_and_a_refresh_without_one_keeps_it():
    rec = record(Command("on", 200, WARM))
    apply_set(rec, SetRequest("trash", SIGNAL, priority=70, strength="sticky"), NOW, lambda: 1)
    assert rec.layers["trash"].strength == "sticky"

    res = apply_set(rec, SetRequest("trash", SIGNAL, priority=70), NOW + 1, lambda: 2)
    assert res.result == "refreshed"
    assert rec.layers["trash"].strength == "sticky"

    apply_set(rec, SetRequest("trash", SIGNAL, priority=70, strength="locked"), NOW + 2, lambda: 3)
    assert rec.layers["trash"].strength == "locked"


def test_a_new_layer_without_a_strength_is_soft():
    rec = record(None)
    apply_set(rec, SetRequest("tv", Command("on", 40), priority=40), NOW, lambda: 1)
    assert rec.layers["tv"].strength == "soft"


@pytest.mark.parametrize("req", [
    SetRequest("base", Command("on", 40), strength="sticky"),
    SetRequest("active", Command("on", 40), strength="locked"),
    SetRequest("circ", Command(None), priority=10, mode="follow", source="sensor.c",
               strength="sticky"),
    SetRequest("tv", Command("on", 40), priority=40, strength="loud"),
])
def test_set_refuses_a_strength_where_it_means_nothing(req: SetRequest):
    rec = record(Command("on", 200, WARM))
    with pytest.raises(PolicyError) as err:
        apply_set(rec, req, NOW, lambda: 1)
    assert err.value.code == "invalid_request"


# --------------------------------------------------------------------------- clear


def test_clear_all_leaves_sticky_and_locked_layers_and_their_tombstones():
    tv = layer("tv", 40, Command("on", 40))
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    leak = layer("leak", 95, SIGNAL, strength="locked")
    rec = record(Command("on", 200, WARM), tv, trash, leak)
    rec.tombstones["nl"] = Tombstone("nl", NOW - 5)
    rec.tombstones["door"] = Tombstone("door", NOW - 5, strength="sticky")

    res = apply_clear(rec, "all", NOW)

    assert res.removed == ("tv",)
    assert set(res.kept) == {"trash", "leak"}
    assert res.lifted == ("nl",)
    assert set(rec.layers) == {"trash", "leak"}
    assert set(rec.tombstones) == {"door"}


def test_clear_active_leaves_a_strong_top_and_its_id_clears_it():
    tv = layer("tv", 40, Command("on", 40))
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    rec = record(Command("on", 200, WARM), tv, trash)

    res = apply_clear(rec, "active", NOW)
    assert res.removed == () and res.kept == ("trash",)

    res = apply_clear(rec, "trash", NOW)
    assert res.removed == ("trash",)
    assert set(rec.layers) == {"tv"}


# --------------------------------------------------------------------------- store


def test_strength_round_trips_and_defaults_to_soft():
    trash = layer("trash", 70, SIGNAL, strength="sticky")
    assert Layer.from_json(trash.to_json()) == trash
    old = trash.to_json()
    del old["strength"]                         # written by 0.2.0
    assert Layer.from_json(old).strength == "soft"
    tomb = Tombstone("trash", NOW, strength="locked")
    assert Tombstone.from_json(tomb.to_json()) == tomb
    old = tomb.to_json()
    del old["strength"]
    assert Tombstone.from_json(old).strength == "soft"
