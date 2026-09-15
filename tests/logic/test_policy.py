"""Policy: set, clear, external changes and expiry on one lamp's record (SPEC section 5)."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable

import pytest

from layers_logic.model import (
    OFF_COMMAND,
    Caps,
    Color,
    Command,
    External,
    Layer,
    Observed,
    Owed,
    Record,
    Resolution,
    SetRequest,
    Tombstone,
)
from layers_logic.policy import (
    ClearResult,
    ExpireResult,
    ExternalResult,
    PolicyError,
    SetResult,
    apply_clear,
    apply_external,
    apply_replay,
    apply_set,
    expire,
    lift_on_off,
    record_observed_as_base,
)
from layers_logic.resolve import resolve

NOW = 1000.0
LAMP = "light.lamp_a"
WARM = Color.kelvin(2700)
RED = Color.xy(0.64, 0.33)
CAPS = Caps(frozenset({"color_temp", "xy"}), 2000, 6500, True, "test")
STATE_BRIGHTNESS = frozenset({"state", "brightness"})
_seq = itertools.count(1)


def layer(
    layer_id: str,
    priority: int,
    command: Command,
    mode: str = "set",
    *,
    seq: int | None = None,
    requested: Command | None = None,
    expires_at: float | None = None,
    owner: str | None = None,
    resume: bool = False,
    on_expire: str = "safe",
) -> Layer:
    return Layer(
        id=layer_id,
        priority=priority,
        mode=mode,
        requested=command if requested is None else requested,
        command=command,
        seq=next(_seq) if seq is None else seq,
        set_at=0.0,
        expires_at=expires_at,
        owner=owner,
        resume_after_manual=resume,
        on_expire=on_expire,
    )


def record(base: Command | None, *layers: Layer, tombstones: Iterable[Tombstone] = ()) -> Record:
    rec = Record(LAMP, base=base)
    for lay in layers:
        rec.layers[lay.id] = lay
    for tomb in tombstones:
        rec.tombstones[tomb.layer_id] = tomb
    return rec


def seq_from(start: int) -> Callable[[], int]:
    counter = itertools.count(start)
    return lambda: next(counter)


def no_seq() -> int:
    raise AssertionError("next_seq is only called when a layer is created")


# --------------------------------------------------------------------------- #
# apply_set: base
# --------------------------------------------------------------------------- #


def test_set_base_on_an_unknown_base_with_attributes_only_turns_on():
    rec = record(None)
    res = apply_set(rec, SetRequest("base", Command(None, 64)), NOW, no_seq)
    assert res == SetResult("base_set", "base")
    assert rec.base == Command("on", 64)
    assert (rec.base_source, rec.base_at, rec.last_layers_change) == ("service", NOW, NOW)


def test_set_base_on_a_stateless_base_turns_on_as_well():
    # A base with no state (take_back of a partial call on an unknown base) is unknown too.
    rec = record(Command(None, 30, WARM))
    apply_set(rec, SetRequest("base", Command(None, 64)), NOW, no_seq)
    assert rec.base == Command("on", 64, WARM)


def test_set_base_merges_into_a_known_base():
    rec = record(Command("on", 200, WARM))
    apply_set(rec, SetRequest("base", Command(None, 64)), NOW, no_seq)
    assert rec.base == Command("on", 64, WARM)
    apply_set(rec, SetRequest("base", Command(None, None, RED)), NOW, no_seq)
    assert rec.base == Command("on", 64, RED)
    apply_set(rec, SetRequest("base", Command("off")), NOW, no_seq)
    assert rec.base == OFF_COMMAND


def test_set_base_attributes_on_an_off_base_leave_it_off():
    # Only an unknown base is turned on; merge_command keeps a known off base off.
    rec = record(OFF_COMMAND)
    apply_set(rec, SetRequest("base", Command(None, 64)), NOW, no_seq)
    assert rec.base == OFF_COMMAND


def test_set_base_leaves_the_layers_alone():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    apply_set(rec, SetRequest("base", Command("on", 50)), NOW, no_seq)
    assert list(rec.layers) == ["tv"]
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "tv")


# --------------------------------------------------------------------------- #
# apply_set: active
# --------------------------------------------------------------------------- #


def test_set_active_on_a_set_top_edits_command_not_requested():
    tv = layer("tv", 40, Command("on", 100, WARM))
    rec = record(Command("on", 200), layer("low", 10, Command("on", 150)), tv)
    res = apply_set(rec, SetRequest("active", Command(None, 30)), NOW, no_seq)
    assert res == SetResult("active_set", "tv")
    assert tv.command == Command("on", 30, WARM)
    assert tv.requested == Command("on", 100, WARM)
    assert rec.last_layers_change == NOW
    assert rec.base == Command("on", 200)
    assert resolve(rec, NOW) == Resolution(Command("on", 30, WARM), "tv")


def test_set_active_off_on_a_set_top():
    tv = layer("tv", 40, Command("on", 100, WARM))
    rec = record(Command("on", 200), tv)
    apply_set(rec, SetRequest("active", Command("off")), NOW, no_seq)
    assert tv.mode == "set"
    assert tv.command == OFF_COMMAND
    assert tv.requested == Command("on", 100, WARM)


def test_set_active_off_on_an_adjust_top_turns_it_into_set_off():
    dim = layer("dim", 40, Command(None, 64), "adjust")
    rec = record(Command("on", 200, WARM), dim)
    res = apply_set(rec, SetRequest("active", Command("off")), NOW, no_seq)
    assert res == SetResult("active_set", "dim")
    assert (dim.mode, dim.command, dim.requested) == ("set", OFF_COMMAND, Command(None, 64))
    assert rec.base == Command("on", 200, WARM)
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "dim")


def test_set_active_attributes_on_an_adjust_top_go_into_command_only():
    dim = layer("dim", 40, Command(None, 64), "adjust")
    rec = record(Command("on", 200, WARM), dim)
    apply_set(rec, SetRequest("active", Command("on", 20, RED)), NOW, no_seq)
    assert dim.mode == "adjust"
    assert dim.command == Command(None, 20, RED)        # attributes only: no state written in
    assert dim.requested == Command(None, 64)
    assert resolve(rec, NOW) == Resolution(Command("on", 20, RED), "dim")


def test_set_active_on_an_adjust_whose_own_command_is_off_keeps_the_attributes():
    # An adjust layer's own state is ignored; an "off" there must not swallow an edit.
    dim = layer("dim", 40, Command("off"), "adjust")
    rec = record(Command("on", 200), dim)
    apply_set(rec, SetRequest("active", Command(None, 20)), NOW, no_seq)
    assert dim.command == Command(None, 20)
    assert resolve(rec, NOW).command == Command("on", 20)


def test_set_active_edits_the_layer_resolve_names():
    # An adjust above a set-off does nothing, so the set-off is the active layer.
    tv = layer("tv", 40, OFF_COMMAND)
    dim = layer("dim", 60, Command(None, 64), "adjust")
    rec = record(Command("on", 200), tv, dim)
    res = apply_set(rec, SetRequest("active", Command("on")), NOW, no_seq)
    assert res.layer == "tv"
    assert tv.command == Command("on")
    assert dim.command == Command(None, 64)


def test_set_active_with_no_active_layer_sets_the_base():
    rec = record(None)
    assert apply_set(rec, SetRequest("active", Command(None, 64)), NOW, no_seq) == SetResult(
        "base_set", "base"
    )
    assert rec.base == Command("on", 64)

    dim = layer("dim", 40, Command(None, 5), "adjust")   # over an off base: not active
    rec = record(OFF_COMMAND, dim)
    assert apply_set(rec, SetRequest("active", Command("on", 50)), NOW, no_seq).result == "base_set"
    assert rec.base == Command("on", 50)
    assert dim.command == Command(None, 5)


# --------------------------------------------------------------------------- #
# apply_set: named layers
# --------------------------------------------------------------------------- #


def test_create_a_layer():
    rec = record(Command("on", 200))
    req = SetRequest(
        "tv",
        OFF_COMMAND,
        priority=40,
        expires_at=NOW + 60,
        owner="owner_a",
        resume_after_manual=True,
        on_expire="render",
    )
    assert apply_set(rec, req, NOW, seq_from(7)) == SetResult("created", "tv")
    assert rec.layers["tv"] == Layer(
        "tv", 40, "set", OFF_COMMAND, OFF_COMMAND, 7, NOW, NOW + 60, "owner_a", True, "render"
    )
    assert rec.last_layers_change == NOW


def test_an_identical_request_only_renews_the_lease():
    # Only the TTL (and the owner, when given) is refreshed; the options stay as set.
    tv = layer("tv", 40, OFF_COMMAND, seq=3, expires_at=NOW + 10, owner="owner_a")
    rec = record(Command("on", 200), tv)
    rec.last_layers_change = 5.0
    req = SetRequest(
        "tv",
        OFF_COMMAND,
        priority=40,
        expires_at=NOW + 600,
        owner="owner_b",
        resume_after_manual=True,
        on_expire="render",
    )
    assert apply_set(rec, req, NOW, no_seq) == SetResult("refreshed", "tv")
    assert tv.expires_at == NOW + 600                    # the TTL is renewed
    assert (tv.owner, tv.resume_after_manual, tv.on_expire) == ("owner_b", False, "safe")
    assert (tv.seq, tv.set_at, tv.priority) == (3, 0.0, 40)
    assert rec.last_layers_change == 5.0


def test_a_renewal_that_leaves_the_options_out_keeps_them():
    # A nightlight set with resume_after_manual, renewed without it: after a
    # person's take-back its tombstone must still lift when the lamp goes off.
    nl_cmd = Command("on", 13, Color.kelvin(2202))
    rec = record(OFF_COMMAND)
    create = SetRequest("nl", nl_cmd, priority=50, expires_at=NOW + 600, resume_after_manual=True,
                        owner="owner_a")
    apply_set(rec, create, NOW, seq_from(1))
    renew = SetRequest("nl", nl_cmd, only_if_present=True, expires_at=NOW + 900)
    assert apply_set(rec, renew, NOW + 300, no_seq).result == "refreshed"
    assert (rec.layers["nl"].resume_after_manual, rec.layers["nl"].owner) == (True, "owner_a")
    assert rec.layers["nl"].expires_at == NOW + 900
    apply_external(rec, OFF_COMMAND, frozenset({"state"}), "automation", "take_back", NOW + 400)
    assert lift_on_off(rec) == ("nl",)
    again = SetRequest("nl", nl_cmd, priority=50, expires_at=NOW + 1000)
    assert apply_set(rec, again, NOW + 460, seq_from(2)).result == "created"


def test_a_request_that_sets_nothing_is_refused():
    # A bare lease renewal must not turn a set layer into "on at whatever it was".
    nl = layer("nl", 50, Command("on", 13), expires_at=NOW + 600)
    rec = record(OFF_COMMAND, nl)
    before = rec.to_json()
    for req in (
        SetRequest("nl", Command(None), only_if_present=True, expires_at=NOW + 900),
        SetRequest("nl", Command(None), priority=50),
        SetRequest("active", Command(None)),
        SetRequest("base", Command(None)),
    ):
        with pytest.raises(PolicyError) as err:
            apply_set(rec, req, NOW, no_seq)
        assert err.value.code == "invalid_request"
    assert rec.to_json() == before
    assert resolve(rec, NOW).command == Command("on", 13)


def test_an_expiry_that_is_not_in_the_future_is_refused():
    # A renewal whose `until` has passed would otherwise "refresh" the hold away
    # and relight the lamp behind expire()'s safety rule.
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND, expires_at=NOW + 3600))
    before = rec.to_json()
    for req in (
        SetRequest("tv", OFF_COMMAND, priority=40, expires_at=NOW - 5),
        SetRequest("tv", OFF_COMMAND, priority=40, expires_at=NOW),
        SetRequest("sig", Command("on", 255), priority=70, expires_at=NOW - 1),
    ):
        with pytest.raises(PolicyError) as err:
            apply_set(rec, req, NOW, no_seq)
        assert err.value.code == "invalid_request"
    assert rec.to_json() == before
    assert resolve(rec, NOW).command == OFF_COMMAND


def test_an_identical_request_without_a_priority_refreshes():
    tv = layer("tv", 40, OFF_COMMAND, seq=3)
    rec = record(Command("on", 200), tv)
    res = apply_set(rec, SetRequest("tv", OFF_COMMAND, expires_at=NOW + 5), NOW, no_seq)
    assert res.result == "refreshed"
    assert tv.expires_at == NOW + 5


def test_edits_to_command_survive_a_refresh():
    tv = layer("tv", 40, Command("on", 100, WARM))
    rec = record(Command("on", 200), tv)
    apply_set(rec, SetRequest("active", Command(None, 30)), NOW, no_seq)
    res = apply_set(rec, SetRequest("tv", Command("on", 100, WARM), priority=40), NOW + 1, no_seq)
    assert res.result == "refreshed"
    assert tv.command == Command("on", 30, WARM)
    assert tv.requested == Command("on", 100, WARM)


def test_a_different_request_updates_and_drops_the_edits():
    tv = layer("tv", 40, Command("on", 30), requested=Command("on", 100), seq=3, owner="owner_a")
    rec = record(Command("on", 200), tv)
    req = SetRequest("tv", Command("on", 150), expires_at=NOW + 60, owner="owner_b")
    assert apply_set(rec, req, NOW, no_seq) == SetResult("updated", "tv")
    assert tv.requested == tv.command == Command("on", 150)
    assert (tv.seq, tv.priority, tv.set_at) == (3, 40, NOW)
    assert (tv.expires_at, tv.owner) == (NOW + 60, "owner_b")
    assert rec.last_layers_change == NOW


def test_changing_the_mode_is_an_update():
    dim = layer("dim", 40, Command(None, 64))
    rec = record(Command("on", 200), dim)
    res = apply_set(rec, SetRequest("dim", Command(None, 64), mode="adjust"), NOW, no_seq)
    assert res.result == "updated"
    assert dim.mode == "adjust"


def test_changing_the_priority_is_an_update_that_restacks():
    a = layer("a", 40, Command("on", 100), seq=1)
    b = layer("b", 50, OFF_COMMAND, seq=2)
    rec = record(Command("on", 200), a, b)
    assert resolve(rec, NOW).active == "b"
    res = apply_set(rec, SetRequest("a", Command("on", 100), priority=60), NOW, no_seq)
    assert res.result == "updated"
    assert (a.priority, a.seq) == (60, 1)
    assert resolve(rec, NOW) == Resolution(Command("on", 100), "a")


def test_moving_to_a_held_priority_is_a_conflict_and_changes_nothing():
    rec = record(Command("on", 200), layer("a", 40, Command("on", 100)), layer("b", 50, OFF_COMMAND))
    before = rec.to_json()
    with pytest.raises(PolicyError) as err:
        apply_set(rec, SetRequest("a", Command("on", 90), priority=50), NOW, no_seq)
    assert err.value.code == "priority_conflict"
    assert err.value.placeholders["other"] == "b"
    assert rec.to_json() == before


def test_only_if_present_never_creates():
    rec = record(Command("on", 200))
    req = SetRequest("nl", Command("on", 13), priority=50, only_if_present=True)
    assert apply_set(rec, req, NOW, no_seq) == SetResult("skipped_absent", "nl")
    assert rec.layers == {}
    assert rec.last_layers_change is None


def test_only_if_present_renews_a_present_layer():
    nl = layer("nl", 50, Command("on", 13), expires_at=NOW + 10)
    rec = record(OFF_COMMAND, nl)
    req = SetRequest("nl", Command("on", 13), only_if_present=True, expires_at=NOW + 600)
    assert apply_set(rec, req, NOW, no_seq).result == "refreshed"
    assert nl.expires_at == NOW + 600


def test_a_renewal_without_the_mode_keeps_an_adjust_layer_adjust():
    # SetRequest.mode defaults to "set". A renewal that leaves it out must not turn
    # an adjust layer into a set layer that lights a dark lamp.
    dim = layer("dim", 20, Command(None, 64), "adjust", expires_at=NOW + 600)
    rec = record(OFF_COMMAND, dim)
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "base")
    renew = SetRequest("dim", Command(None, 64), only_if_present=True, expires_at=NOW + 900)
    assert renew.mode == "set"
    assert apply_set(rec, renew, NOW, no_seq).result == "refreshed"
    assert (dim.mode, dim.expires_at) == ("adjust", NOW + 900)
    assert resolve(rec, NOW).command == OFF_COMMAND
    # A different command through only_if_present updates it, still as adjust.
    change = SetRequest("dim", Command(None, 30), only_if_present=True, expires_at=NOW + 900)
    assert apply_set(rec, change, NOW + 1, no_seq).result == "updated"
    assert (dim.mode, dim.command) == ("adjust", Command(None, 30))
    assert resolve(rec, NOW + 1).command == OFF_COMMAND


def test_only_if_present_does_not_revive_an_expired_layer():
    # Expired while Home Assistant was down, not yet removed: it goes through expire().
    nl = layer("nl", 50, Command("on", 13), expires_at=NOW - 1)
    rec = record(OFF_COMMAND, nl)
    req = SetRequest("nl", Command("on", 13), only_if_present=True, expires_at=NOW + 600)
    assert apply_set(rec, req, NOW, no_seq).result == "skipped_absent"
    assert rec.layers["nl"].expires_at == NOW - 1
    assert expire(rec, NOW, Observed("on", 13), CAPS) == ExpireResult(("nl",), True)


def test_setting_an_expired_id_again_creates_it_afresh():
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 13), seq=3, expires_at=NOW - 1))
    req = SetRequest("nl", Command("on", 13), priority=50, expires_at=NOW + 600)
    assert apply_set(rec, req, NOW, seq_from(9)).result == "created"
    assert (rec.layers["nl"].seq, rec.layers["nl"].expires_at) == (9, NOW + 600)


def test_a_new_layer_needs_a_priority():
    rec = record(Command("on", 200))
    before = rec.to_json()
    with pytest.raises(PolicyError) as err:
        apply_set(rec, SetRequest("tv", OFF_COMMAND), NOW, no_seq)
    assert err.value.code == "priority_required"
    assert err.value.placeholders == {"entity_id": LAMP, "layer": "tv"}
    assert rec.to_json() == before


def test_a_second_id_at_a_held_priority_is_a_conflict():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    before = rec.to_json()
    with pytest.raises(PolicyError) as err:
        apply_set(rec, SetRequest("movie", Command("on", 10), priority=40), NOW, no_seq)
    assert err.value.code == "priority_conflict"
    assert err.value.placeholders == {
        "entity_id": LAMP,
        "layer": "movie",
        "priority": "40",
        "other": "tv",
    }
    assert "priority_conflict" in str(err.value)
    assert rec.to_json() == before


def test_an_expired_layer_no_longer_holds_its_priority():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND, expires_at=NOW))
    res = apply_set(rec, SetRequest("movie", Command("on", 10), priority=40), NOW, seq_from(1))
    assert res.result == "created"


def test_a_tombstoned_id_is_skipped():
    rec = record(Command("on", 200), tombstones=[Tombstone("tv", 1.0, NOW + 60)])
    for req in (
        SetRequest("tv", OFF_COMMAND, priority=40),
        SetRequest("tv", OFF_COMMAND, only_if_present=True),
        SetRequest("tv", OFF_COMMAND),                  # checked before priority_required
    ):
        assert apply_set(rec, req, NOW, no_seq) == SetResult("skipped_tombstoned", "tv")
    assert rec.layers == {}
    assert rec.last_layers_change is None


def test_an_expired_tombstone_no_longer_blocks():
    rec = record(Command("on", 200), tombstones=[Tombstone("tv", 1.0, NOW)])
    assert apply_set(rec, SetRequest("tv", OFF_COMMAND, priority=40), NOW, seq_from(1)).result == "created"


def test_all_is_not_a_layer_id_and_modes_are_checked():
    rec = record(Command("on", 200))
    for req in (
        SetRequest("all", OFF_COMMAND, priority=40),
        SetRequest("tv", OFF_COMMAND, priority=40, mode="toggle"),
    ):
        with pytest.raises(PolicyError) as err:
            apply_set(rec, req, NOW, no_seq)
        assert err.value.code == "invalid_request"
    assert rec.layers == {}


# --------------------------------------------------------------------------- #
# apply_clear
# --------------------------------------------------------------------------- #


def test_clear_an_id_removes_that_layer_and_that_ids_tombstone():
    rec = record(
        Command("on", 200),
        layer("tv", 40, OFF_COMMAND),
        layer("nl", 50, Command("on", 13)),
        tombstones=[Tombstone("tv", 1.0, NOW - 5), Tombstone("sig", 1.0)],
    )
    assert apply_clear(rec, "tv", NOW) == ClearResult(("tv",), ("tv",))
    assert list(rec.layers) == ["nl"]
    assert list(rec.tombstones) == ["sig"]
    assert rec.last_layers_change == NOW


def test_clearing_an_id_lifts_its_tombstone():
    rec = record(Command("on", 200), tombstones=[Tombstone("tv", 1.0, None)])
    req = SetRequest("tv", OFF_COMMAND, priority=40)
    assert apply_set(rec, req, NOW, no_seq).result == "skipped_tombstoned"
    assert apply_clear(rec, "tv", NOW) == ClearResult((), ("tv",))
    assert rec.last_layers_change is None           # only a tombstone went: no layer changed
    assert apply_set(rec, req, NOW, seq_from(1)).result == "created"


def test_clear_an_unknown_id_changes_nothing():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    before = rec.to_json()
    assert apply_clear(rec, "nope", NOW) == ClearResult()
    assert rec.to_json() == before


def test_clear_active_removes_only_the_active_layer():
    rec = record(
        Command("on", 200),
        layer("low", 10, Command("on", 50)),
        layer("tv", 40, OFF_COMMAND),
        layer("dim", 60, Command(None, 5), "adjust"),       # over the set-off: not active
        tombstones=[Tombstone("x", 1.0)],
    )
    assert apply_clear(rec, "active", NOW) == ClearResult(("tv",), ())
    assert list(rec.layers) == ["low", "dim"]
    assert list(rec.tombstones) == ["x"]
    assert resolve(rec, NOW) == Resolution(Command("on", 5), "dim")


def test_clear_active_with_nothing_active_changes_nothing():
    rec = record(OFF_COMMAND, layer("dim", 60, Command(None, 5), "adjust"))
    assert apply_clear(rec, "active", NOW) == ClearResult()
    assert list(rec.layers) == ["dim"]
    assert rec.last_layers_change is None


def test_clear_all_removes_every_layer_and_tombstone():
    rec = record(
        Command("on", 200),
        layer("sig", 70, Command("on", 255, RED)),
        layer("tv", 40, OFF_COMMAND),
        layer("dim", 60, Command(None, 5), "adjust"),
        tombstones=[Tombstone("nl", 1.0), Tombstone("aa", 1.0, NOW + 5, True)],
    )
    assert apply_clear(rec, "all", NOW) == ClearResult(("tv", "dim", "sig"), ("aa", "nl"))
    assert rec.layers == {} and rec.tombstones == {}
    assert rec.last_layers_change == NOW
    assert resolve(rec, NOW) == Resolution(Command("on", 200), "base")


def test_clear_leaves_an_expired_layer_for_expire_to_render():
    # A signal that ran out while Home Assistant was down still shows on the lamp.
    # Dropping it here would let the startup record it as the base; expire() renders it away.
    rec = record(Command("on", 102, WARM), layer("sig", 70, Command("on", 255, RED), expires_at=NOW - 60))
    assert apply_clear(rec, "sig", NOW) == ClearResult((), (), ("sig",))
    assert apply_clear(rec, "all", NOW) == ClearResult((), (), ("sig",))
    assert list(rec.layers) == ["sig"]
    assert rec.layers["sig"].on_expire == "render"
    assert rec.last_layers_change == NOW
    shows = Observed("on", 255, "xy", xy=(0.64, 0.33))
    assert expire(rec, NOW, shows, CAPS) == ExpireResult(("sig",), True)


def test_an_owners_clear_of_an_expired_hold_is_a_restore_not_a_suppressed_expiry():
    # Home Assistant was down past the hold's TTL. During the startup grace the
    # owner's start trigger clears it (the film is over): that is the restore,
    # and the safety rule must not swallow it when expire() finally runs.
    rec = record(Command("on", 150, WARM), layer("tv", 40, OFF_COMMAND, expires_at=NOW - 50))
    before = resolve(rec, NOW)
    cleared = apply_clear(rec, "tv", NOW)
    assert resolve(rec, NOW) == before              # nothing to compare: expire() renders it
    res = expire(rec, NOW + 30, Observed("off"), CAPS)
    assert res == ExpireResult(("tv",), True)
    assert (rec.base, rec.base_source) == (Command("on", 150, WARM), None)
    assert cleared == ClearResult((), (), ("tv",))

    rec = record(Command("on", 150, WARM), layer("tv", 40, OFF_COMMAND, expires_at=NOW - 50),
                 layer("nl", 50, Command("on", 13)), tombstones=[Tombstone("x", 1.0)])
    cleared = apply_clear(rec, "all", NOW)
    assert expire(rec, NOW + 30, Observed("off"), CAPS).render is True
    assert cleared == ClearResult(("nl",), ("x",), ("tv",))


def test_clear_base_is_refused():
    rec = record(Command("on", 200))
    with pytest.raises(PolicyError) as err:
        apply_clear(rec, "base", NOW)
    assert err.value.code == "invalid_request"


# --------------------------------------------------------------------------- #
# apply_external: take_back
# --------------------------------------------------------------------------- #


def test_take_back_makes_the_change_the_base_and_drops_every_layer():
    tv = layer("tv", 40, OFF_COMMAND, expires_at=NOW + 3600)
    nl = layer("nl", 50, Command("on", 13, WARM), resume=True)
    rec = record(Command("on", 200, WARM), nl, tv)
    rec.owed = Owed(NOW - 1, Command("on", 13, WARM), turns_on=True)
    rec.diverged = "delivery"
    rec.last_layers_change = 5.0
    shown = Command("on", 80, RED)

    res = apply_external(rec, shown, None, "user", "take_back", NOW, user_id="user_1")

    assert res == ExternalResult(("tv", "nl"), None, False)
    assert (rec.base, rec.base_source, rec.base_at) == (shown, "user", NOW)
    assert rec.layers == {}
    assert rec.tombstones == {
        "tv": Tombstone("tv", NOW, NOW + 3600, False, "user"),     # inherits expires_at
        "nl": Tombstone("nl", NOW, None, True, "user"),            # lift_when_off from resume_after_manual
    }
    assert rec.diverged is None
    assert rec.owed is None
    assert rec.last_external == External(NOW, "user", "take_back", "user_1", ("tv", "nl"), None)
    assert rec.last_layers_change == 5.0     # a change Layers did not make: not for the replay rule
    assert resolve(rec, NOW) == Resolution(shown, "base")


def test_take_back_of_part_of_the_groups_keeps_the_rest_of_the_base_and_marks_partial():
    # A call set only brightness while a signal's colour was showing.
    rec = record(Command("on", 200, WARM), layer("sig", 70, Command("on", 255, RED)))
    rec.observed = Observed("on", 60, "xy", xy=(0.64, 0.33))    # the engine updates it first
    shown = Command("on", 60, RED)                  # what the lamp reports now
    res = apply_external(rec, shown, STATE_BRIGHTNESS, "automation", "take_back", NOW, caps=CAPS)
    assert res == ExternalResult(("sig",), None, True)
    assert rec.base == Command("on", 60, WARM)
    assert rec.diverged == "partial"


def test_take_back_of_an_intent_that_misses_a_colour_the_lamp_shows_is_partial():
    # The intent path: a turn_on at the brightness the lamp already shows, while a
    # signal's colour shows. shown is the call's intent, not the lamp: the lamp
    # still shows red, the new base says warm white, so the next set/clear repairs it.
    rec = record(Command("on", 150, WARM), layer("sig", 70, Command("on", 150, RED)))
    rec.observed = Observed("on", 150, "xy", xy=(0.64, 0.33))
    res = apply_external(rec, Command("on", 150), STATE_BRIGHTNESS, "user", "take_back", NOW, caps=CAPS)
    assert res == ExternalResult(("sig",), None, True)
    assert rec.diverged == "partial"
    assert resolve(rec, NOW).command == Command("on", 150, WARM)


def test_take_back_partial_without_caps_uses_the_reported_mode():
    rec = record(Command("on", 150, WARM), layer("sig", 70, Command("on", 150, RED)))
    rec.observed = Observed("on", 150, "xy", xy=(0.64, 0.33))
    assert apply_external(rec, Command("on", 150), STATE_BRIGHTNESS, "user", "take_back", NOW).partial


def test_take_back_is_not_partial_when_the_lamp_shows_the_rest():
    # A dashboard brightness change on a lamp that shows its base's colour: the lamp
    # is in sync, so nothing is flagged and nothing needs to be persisted.
    rec = record(Command("on", 100, WARM))
    rec.observed = Observed("on", 150, "color_temp", kelvin=2700)
    res = apply_external(rec, Command("on", 150, WARM), STATE_BRIGHTNESS, "user", "take_back", NOW,
                         caps=CAPS)
    assert res == ExternalResult((), None, False)
    assert rec.diverged is None
    assert not rec.worth_persisting()

    # A person turns a lamp up under a hold that kept it off: base and lamp agree.
    rec = record(Command("on", 150, WARM), layer("tv", 40, OFF_COMMAND))
    rec.observed = Observed("on", 200, "color_temp", kelvin=2700)
    res = apply_external(rec, Command("on", 200, WARM), STATE_BRIGHTNESS, "user", "take_back", NOW,
                         caps=CAPS)
    assert (res.partial, rec.diverged) == (False, None)


def test_take_back_partial_needs_a_report():
    # Away (the intent path while unavailable): the return handles it, not a repair.
    rec = record(Command("on", 150, WARM), layer("sig", 70, Command("on", 150, RED)))
    rec.observed = Observed("unavailable")
    res = apply_external(rec, Command("on", 150), STATE_BRIGHTNESS, "user", "take_back", NOW, caps=CAPS)
    assert (res.partial, rec.diverged) == (False, None)


def test_take_back_whose_groups_cover_what_the_lamp_shows_is_not_partial():
    # The intent path: a known turn_off on a lamp a layer already holds off.
    rec = record(Command("on", 200, WARM), layer("tv", 40, OFF_COMMAND))
    rec.diverged = "partial"
    res = apply_external(rec, OFF_COMMAND, frozenset({"state"}), "user", "take_back", NOW)
    assert res == ExternalResult(("tv",), None, False)
    assert rec.base == OFF_COMMAND
    assert rec.diverged is None


def test_take_back_on_an_unknown_base_starts_from_nothing():
    rec = record(None, layer("tv", 40, OFF_COMMAND))
    apply_external(rec, Command("on", 60, RED), STATE_BRIGHTNESS, "user", "take_back", NOW)
    assert rec.base == Command("on", 60)

    rec = record(None)
    apply_external(rec, Command("on", 60, RED), None, "device", "take_back", NOW)
    assert rec.base == Command("on", 60, RED)
    assert rec.tombstones == {}


def test_take_back_tombstone_blocks_the_owner_until_it_clears():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    apply_external(rec, Command("on", 150), None, "user", "take_back", NOW)
    req = SetRequest("tv", OFF_COMMAND, priority=40)
    assert apply_set(rec, req, NOW + 1, no_seq).result == "skipped_tombstoned"
    assert resolve(rec, NOW + 1).command == Command("on", 150)
    apply_clear(rec, "tv", NOW + 2)
    assert apply_set(rec, req, NOW + 3, seq_from(1)).result == "created"


def test_take_back_tombstone_runs_out_with_the_dropped_layer():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND, expires_at=NOW + 60))
    apply_external(rec, Command("on", 150), None, "user", "take_back", NOW)
    req = SetRequest("tv", OFF_COMMAND, priority=40)
    assert apply_set(rec, req, NOW + 59, no_seq).result == "skipped_tombstoned"
    assert expire(rec, NOW + 60, Observed("on", 150), CAPS) == ExpireResult((), False, ("tv",))
    assert apply_set(rec, req, NOW + 60, seq_from(1)).result == "created"


def test_take_back_tombstone_with_resume_after_manual_lifts_when_the_lamp_is_off():
    rec = record(
        Command("on", 200),
        layer("tv", 40, OFF_COMMAND),
        layer("nl", 50, Command("on", 13), resume=True),
    )
    apply_external(rec, Command("on", 150), None, "user", "take_back", NOW)
    assert lift_on_off(rec) == ("nl",)
    assert list(rec.tombstones) == ["tv"]
    assert apply_set(rec, SetRequest("nl", Command("on", 13), priority=50), NOW, seq_from(1)).result == "created"
    assert apply_set(rec, SetRequest("tv", OFF_COMMAND, priority=40), NOW, no_seq).result == "skipped_tombstoned"


# --------------------------------------------------------------------------- #
# apply_external: edit_active
# --------------------------------------------------------------------------- #


def test_edit_active_edits_the_active_layers_command_only():
    tv = layer("tv", 40, Command("on", 100, WARM))
    rec = record(Command("on", 200), tv)
    rec.diverged = "delivery"

    res = apply_external(rec, Command("on", 30, RED), None, "user", "edit_active", NOW, user_id="user_1")

    assert res == ExternalResult((), "tv", False)
    assert tv.command == Command("on", 30, RED)
    assert tv.requested == Command("on", 100, WARM)
    assert (rec.base, rec.base_source) == (Command("on", 200), None)
    assert rec.tombstones == {}
    assert rec.diverged == "delivery"                   # left as it is
    assert rec.last_external == External(NOW, "user", "edit_active", "user_1", (), "tv")
    assert resolve(rec, NOW) == Resolution(Command("on", 30, RED), "tv")


def test_edit_active_takes_only_the_groups_a_call_gave():
    tv = layer("tv", 40, Command("on", 100, WARM))
    rec = record(Command("on", 200), tv)
    apply_external(rec, Command("on", 30, RED), frozenset({"brightness"}), "user", "edit_active", NOW)
    assert tv.command == Command("on", 30, WARM)


def test_edit_active_off_turns_an_adjust_top_into_set_off():
    dim = layer("dim", 40, Command(None, 64), "adjust")
    rec = record(Command("on", 200), dim)
    res = apply_external(rec, OFF_COMMAND, frozenset({"state"}), "user", "edit_active", NOW)
    assert res == ExternalResult((), "dim", False)
    assert (dim.mode, dim.command, dim.requested) == ("set", OFF_COMMAND, Command(None, 64))
    # The off also becomes the base: clearing the layer must not relight the lamp.
    assert (rec.base, rec.base_source) == (OFF_COMMAND, "user")
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "dim")


def test_edit_active_on_an_adjust_top_takes_attributes_only():
    dim = layer("dim", 40, Command(None, 64), "adjust")
    rec = record(Command("on", 200), dim)
    apply_external(rec, Command("on", 30, RED), None, "device", "edit_active", NOW)
    assert (dim.mode, dim.command) == ("adjust", Command(None, 30, RED))


def test_edit_active_with_no_active_layer_is_a_take_back():
    dim = layer("dim", 40, Command(None, 5), "adjust")      # over an off base: not active
    rec = record(OFF_COMMAND, dim)
    res = apply_external(rec, Command("on", 80), None, "user", "edit_active", NOW)
    assert res == ExternalResult(("dim",), None, False)
    assert rec.base == Command("on", 80)
    assert rec.layers == {}
    assert list(rec.tombstones) == ["dim"]
    assert rec.last_external.policy == "edit_active"


def test_edit_active_edits_survive_the_owners_refresh():
    tv = layer("tv", 40, Command("on", 100))
    rec = record(Command("on", 200), tv)
    apply_external(rec, Command("on", 30), None, "user", "edit_active", NOW)
    res = apply_set(rec, SetRequest("tv", Command("on", 100), priority=40), NOW + 1, no_seq)
    assert res.result == "refreshed"
    assert resolve(rec, NOW + 1).command == Command("on", 30)


def test_edit_active_takes_state_only_when_the_call_gave_it():
    # A brightness-only call reported with the lamp off does not turn the adjust off.
    dim = layer("dim", 40, Command(None, 64), "adjust")
    rec = record(Command("on", 200), dim)
    apply_external(rec, OFF_COMMAND, frozenset({"brightness"}), "user", "edit_active", NOW)
    assert (dim.mode, dim.command, dim.requested_mode) == ("adjust", Command(None, 64), None)


@pytest.mark.parametrize("how", ["layer_active", "edit_active"])
def test_a_person_turning_an_adjust_layer_off_survives_the_owners_refresh(how):
    # An adjust hold (priority 40) that a person turned off becomes set/off. The
    # owner's byte-identical re-send (e.g. a media player flapping) only refreshes:
    # the edit survives and the lamp stays off.
    dim = layer("dim", 40, Command(None, 64), "adjust", expires_at=NOW + 100)
    rec = record(Command("on", 200), dim)
    if how == "layer_active":
        apply_set(rec, SetRequest("active", OFF_COMMAND), NOW, no_seq)
    else:
        apply_external(rec, OFF_COMMAND, frozenset({"state"}), "user", "edit_active", NOW)
    assert resolve(rec, NOW).command == OFF_COMMAND
    resend = SetRequest("dim", Command(None, 64), priority=40, mode="adjust", expires_at=NOW + 600)
    for i in range(1, 4):
        assert apply_set(rec, resend, NOW + i, no_seq).result == "refreshed"
        assert resolve(rec, NOW + i).command == OFF_COMMAND
    assert (dim.mode, dim.requested_mode, dim.expires_at) == ("set", "adjust", NOW + 600)

    # A different request is an update: the owner's new command, as adjust again.
    change = SetRequest("dim", Command(None, 30), mode="adjust")
    assert apply_set(rec, change, NOW + 5, no_seq).result == "updated"
    assert (dim.mode, dim.requested_mode, dim.command) == ("adjust", None, Command(None, 30))
    if how == "layer_active":       # the owner's own edit: the base is still on
        assert resolve(rec, NOW + 5).command == Command("on", 30)
    else:                           # a person's off is the base now: an adjust never lights it
        assert resolve(rec, NOW + 5).command == OFF_COMMAND


# --------------------------------------------------------------------------- #
# apply_external: base_keep_layers
# --------------------------------------------------------------------------- #


def test_base_keep_layers_with_an_active_layer_keeps_the_change_on_show():
    tv = layer("tv", 40, OFF_COMMAND)
    rec = record(Command("on", 200, WARM), tv)
    rec.observed = Observed("on", 150, "color_temp", kelvin=2700)
    res = apply_external(rec, Command("on", 150, WARM), None, "user", "base_keep_layers", NOW, caps=CAPS)
    assert res == ExternalResult((), None, False)
    assert (rec.base, rec.base_source) == (Command("on", 150, WARM), "user")
    assert rec.layers == {"tv": tv}
    assert rec.tombstones == {}
    assert rec.diverged == "manual_keep"
    assert resolve(rec, NOW).command == OFF_COMMAND      # the model still says off; no snap-back


def test_base_keep_layers_is_not_manual_keep_when_the_lamp_shows_the_effective_command():
    # The rocker's Off on a lamp a hold already keeps off: nothing is on show but
    # what the layers say, so the lamp must not stay flagged (and persisted) forever.
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    rec.observed = Observed("off")
    apply_external(rec, OFF_COMMAND, frozenset({"state"}), "user", "base_keep_layers", NOW, caps=CAPS)
    assert rec.base == OFF_COMMAND
    assert rec.diverged is None
    apply_clear(rec, "tv", NOW + 60)
    assert rec.diverged is None
    assert not rec.worth_persisting()


def test_base_keep_layers_on_a_lamp_that_is_away_is_not_manual_keep():
    # It missed the change, so it does not show it; its return decides.
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    rec.observed = Observed("unavailable")
    apply_external(rec, Command("on", 150), None, "user", "base_keep_layers", NOW, caps=CAPS)
    assert rec.diverged is None


def test_base_keep_layers_with_nothing_active_is_not_manual_keep():
    # Turned off by hand under an adjust layer: the adjust stops applying, the lamp shows its base.
    rec = record(Command("on", 200), layer("dim", 40, Command(None, 64), "adjust"))
    rec.diverged = "manual_keep"
    rec.observed = Observed("off")
    res = apply_external(rec, OFF_COMMAND, frozenset({"state"}), "user", "base_keep_layers", NOW,
                         caps=CAPS)
    assert res == ExternalResult((), None, False)
    assert rec.base == OFF_COMMAND
    assert list(rec.layers) == ["dim"]
    assert rec.diverged is None

    # As take_back would: a brightness-only change while the lamp shows another colour.
    rec = record(Command("on", 200, WARM))
    rec.observed = Observed("on", 60, "xy", xy=(0.64, 0.33))
    res = apply_external(rec, Command("on", 60, RED), STATE_BRIGHTNESS, "user", "base_keep_layers", NOW,
                         caps=CAPS)
    assert res.partial is True and rec.diverged == "partial"


def test_base_keep_layers_an_adjust_the_change_wakes_up_is_manual_keep():
    rec = record(OFF_COMMAND, layer("dim", 40, Command(None, 64), "adjust"))
    rec.observed = Observed("on", 150, "brightness")
    apply_external(rec, Command("on", 150), None, "user", "base_keep_layers", NOW, caps=CAPS)
    assert rec.base == Command("on", 150)
    assert rec.diverged == "manual_keep"
    assert resolve(rec, NOW) == Resolution(Command("on", 64), "dim")


# --------------------------------------------------------------------------- #
# apply_external: every policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("policy", ["take_back", "edit_active", "base_keep_layers"])
def test_every_policy_clears_owed_and_records_last_external(policy):
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND))
    rec.owed = Owed(NOW - 5, OFF_COMMAND, missed=True)
    res = apply_external(rec, Command("on", 90), None, "device", policy, NOW)
    assert rec.owed is None
    assert rec.last_external == External(NOW, "device", policy, None, res.dropped, res.edited)


def test_an_unknown_policy_is_refused():
    rec = record(Command("on", 200))
    with pytest.raises(ValueError):
        apply_external(rec, Command("on", 90), None, "user", "ignore", NOW)
    assert rec.base == Command("on", 200)


# --------------------------------------------------------------------------- #
# apply_replay
# --------------------------------------------------------------------------- #


def test_a_replayed_press_only_writes_the_base():
    # A scene retry loop re-applies a press made before the hold arrived: the base
    # learns it, and the hold, its tombstones, divergence and owed render stay.
    tv = layer("tv", 40, OFF_COMMAND)
    rec = record(Command("on", 150, WARM), tv, tombstones=[Tombstone("nl", 1.0)])
    rec.owed = Owed(NOW - 2, OFF_COMMAND)
    rec.diverged = "delivery"
    rec.last_layers_change = NOW - 16
    res = apply_replay(rec, Command("on", 102, WARM), frozenset({"state", "brightness", "color"}),
                       "automation", NOW, user_id=None)
    assert res == ExternalResult()
    assert (rec.base, rec.base_source, rec.base_at) == (Command("on", 102, WARM), "automation", NOW)
    assert rec.layers == {"tv": tv} and list(rec.tombstones) == ["nl"]
    assert (rec.diverged, rec.owed) == ("delivery", Owed(NOW - 2, OFF_COMMAND))
    assert rec.last_external == External(NOW, "automation", "replay",
                                         groups=frozenset({"state", "brightness", "color"}))
    assert rec.last_layers_change == NOW - 16
    assert resolve(rec, NOW).command == OFF_COMMAND


def test_apply_external_with_the_replay_policy_is_a_replay():
    # So a follow-up can re-apply last_external.policy as it is.
    rec = record(Command("on", 150), layer("tv", 40, OFF_COMMAND))
    apply_external(rec, Command("on", 60), frozenset({"state", "brightness"}), "automation", "replay", NOW)
    assert list(rec.layers) == ["tv"]
    assert rec.base == Command("on", 60)
    assert rec.last_external.policy == "replay"


# --------------------------------------------------------------------------- #
# expire
# --------------------------------------------------------------------------- #


def test_expire_with_nothing_expired_changes_nothing():
    rec = record(Command("on", 200), layer("tv", 40, OFF_COMMAND, expires_at=NOW + 1))
    rec.last_layers_change = 5.0
    before = rec.to_json()
    assert expire(rec, NOW, Observed("off"), CAPS) == ExpireResult((), False, ())
    assert rec.to_json() == before


def test_expire_a_named_layer_still_active_renders():
    # An owner still holds the lamp: render, even though that lights it.
    rec = record(
        OFF_COMMAND,
        layer("low", 10, Command("on", 200)),
        layer("hold", 40, OFF_COMMAND, expires_at=NOW),
    )
    assert expire(rec, NOW, Observed("off"), CAPS) == ExpireResult(("hold",), True)
    assert list(rec.layers) == ["low"]
    assert (rec.base, rec.base_source) == (OFF_COMMAND, None)
    assert rec.last_layers_change == NOW


def test_expire_on_expire_render_renders():
    rec = record(Command("on", 200, WARM), layer("tv", 40, OFF_COMMAND, expires_at=NOW, on_expire="render"))
    assert expire(rec, NOW, Observed("off"), CAPS) == ExpireResult(("tv",), True)
    assert (rec.base, rec.base_source) == (Command("on", 200, WARM), None)


def test_expire_one_on_expire_render_among_several_renders():
    rec = record(
        Command("on", 200),
        layer("tv", 40, OFF_COMMAND, expires_at=NOW),
        layer("sig", 70, OFF_COMMAND, expires_at=NOW - 5, on_expire="render"),
    )
    assert expire(rec, NOW, Observed("off"), CAPS) == ExpireResult(("tv", "sig"), True)


def test_safety_rule_a_hold_that_turned_a_lamp_off_does_not_relight_it():
    rec = record(Command("on", 200, WARM), layer("hold", 40, OFF_COMMAND, expires_at=NOW - 1))
    res = expire(rec, NOW, Observed("off", at=NOW - 30), CAPS)
    assert res == ExpireResult(("hold",), False)
    assert (rec.base, rec.base_source, rec.base_at) == (OFF_COMMAND, "expiry", NOW)
    assert rec.layers == {}
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "base")
    assert rec.last_layers_change == NOW


def test_safety_rule_a_signal_expiring_over_a_dimmer_base_renders():
    rec = record(Command("on", 102, WARM), layer("sig", 70, Command("on", 255, RED), expires_at=NOW))
    res = expire(rec, NOW, Observed("on", 255, "xy", xy=(0.64, 0.33)), CAPS)
    assert res == ExpireResult(("sig",), True)
    assert (rec.base, rec.base_source) == (Command("on", 102, WARM), None)


def test_safety_rule_a_nightlight_expiring_over_an_off_base_renders():
    rec = record(OFF_COMMAND, layer("nl", 50, Command("on", 13, WARM), expires_at=NOW))
    res = expire(rec, NOW, Observed("on", 13, "color_temp", kelvin=2700), CAPS)
    assert res == ExpireResult(("nl",), True)
    assert rec.base == OFF_COMMAND


def test_safety_rule_an_expiry_that_would_brighten_records_the_lamp_instead():
    rec = record(Command("on", 200, WARM), layer("dim", 40, Command("on", 60), expires_at=NOW))
    res = expire(rec, NOW, Observed("on", 60, "color_temp", kelvin=2700), CAPS)
    assert res == ExpireResult(("dim",), False)
    assert rec.base == Command("on", 60, Color.kelvin(2700))
    assert rec.base_source == "expiry"


def test_safety_rule_a_rise_within_tolerance_renders():
    rec = record(Command("on", 104), layer("dim", 40, Command("on", 100), expires_at=NOW))
    assert expire(rec, NOW, Observed("on", 100, "brightness"), CAPS).render is True


def test_safety_rule_uses_p_at_drop_when_the_lamp_is_unavailable():
    rec = record(Command("on", 200, WARM), layer("hold", 40, OFF_COMMAND, expires_at=NOW))
    rec.available = False
    rec.p_at_drop = Observed("off", at=NOW - 100)
    assert expire(rec, NOW, Observed("unavailable", at=NOW - 50), CAPS) == ExpireResult(("hold",), False)
    assert (rec.base, rec.base_source) == (OFF_COMMAND, "expiry")

    rec = record(Command("on", 102, WARM), layer("sig", 70, Command("on", 255, RED), expires_at=NOW))
    rec.available = False
    rec.p_at_drop = Observed("on", 255, "xy", xy=(0.64, 0.33))
    res = expire(rec, NOW, Observed("unavailable"), CAPS)
    assert res == ExpireResult(("sig",), True)          # the caller records it as owed
    assert rec.base == Command("on", 102, WARM)


def test_safety_rule_prefers_a_live_report_over_p_at_drop():
    rec = record(Command("on", 200), layer("hold", 40, OFF_COMMAND, expires_at=NOW))
    rec.p_at_drop = Observed("on", 255)                 # left from an earlier outage
    assert expire(rec, NOW, Observed("off"), CAPS).render is False
    assert rec.base == OFF_COMMAND


def test_safety_rule_with_nothing_known_about_the_lamp_does_not_render():
    rec = record(Command("on", 200), layer("hold", 40, OFF_COMMAND, expires_at=NOW))
    assert expire(rec, NOW, None, CAPS) == ExpireResult(("hold",), False)
    assert rec.base is None                             # what the lamp shows is unknown
    assert resolve(rec, NOW) == Resolution(None, "none")


def test_expire_never_renders_a_command_that_is_none():
    rec = record(None, layer("sig", 70, Command("on", 255, RED), expires_at=NOW, on_expire="render"))
    assert expire(rec, NOW, Observed("on", 255), CAPS) == ExpireResult(("sig",), False)


def test_expire_removes_tombstones_that_ran_out_without_touching_the_stack():
    rec = record(
        Command("on", 200),
        tombstones=[Tombstone("tv", 1.0, NOW), Tombstone("nl", 1.0, None, True), Tombstone("sig", 1.0, NOW + 1)],
    )
    rec.last_layers_change = 5.0
    assert expire(rec, NOW, Observed("on", 200), CAPS) == ExpireResult((), False, ("tv",))
    assert sorted(rec.tombstones) == ["nl", "sig"]
    assert rec.last_layers_change == 5.0


def test_an_expiry_never_lights_a_lamp_through_an_adjust_layer():
    # An adjust layer left active is not an owner holding the lamp: its "on" is the
    # base's. A hold over a dark lamp expiring must not light it through it.
    rec = record(
        Command("on", 200, WARM),
        layer("dim", 5, Command(None, 80), "adjust"),
        layer("hold", 40, OFF_COMMAND, expires_at=NOW),
    )
    assert expire(rec, NOW, Observed("off"), CAPS) == ExpireResult(("hold",), False)
    assert (rec.base, rec.base_source) == (OFF_COMMAND, "expiry")
    assert resolve(rec, NOW) == Resolution(OFF_COMMAND, "base")
    assert list(rec.layers) == ["dim"]


def test_an_expiry_that_leaves_a_set_layer_under_an_adjust_renders():
    rec = record(
        OFF_COMMAND,
        layer("low", 10, Command("on", 200)),
        layer("hold", 40, OFF_COMMAND, expires_at=NOW),
        layer("dim", 60, Command(None, 64), "adjust"),
    )
    assert expire(rec, NOW, Observed("off"), CAPS) == ExpireResult(("hold",), True)
    assert resolve(rec, NOW) == Resolution(Command("on", 64), "dim")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def test_lift_on_off_removes_only_tombstones_that_lift_when_off():
    rec = record(
        OFF_COMMAND,
        tombstones=[
            Tombstone("a", 1.0, None, True),
            Tombstone("b", 1.0, None, False),
            Tombstone("c", 1.0, NOW + 5, True),
        ],
    )
    assert lift_on_off(rec) == ("a", "c")
    assert list(rec.tombstones) == ["b"]
    assert lift_on_off(rec) == ()


def test_record_observed_as_base():
    rec = record(Command("on", 200, WARM))
    obs = Observed("on", 90, "color_temp", kelvin=3000, at=NOW)
    assert record_observed_as_base(rec, obs, CAPS, NOW, "startup") == Command("on", 90, Color.kelvin(3000))
    assert (rec.base_source, rec.base_at) == ("startup", NOW)
    assert record_observed_as_base(rec, Observed("off"), CAPS, NOW + 1, "return") == OFF_COMMAND
    assert (rec.base, rec.base_source, rec.base_at) == (OFF_COMMAND, "return", NOW + 1)
    assert record_observed_as_base(rec, Observed("unavailable"), CAPS, NOW + 2, "startup") is None
    assert rec.base is None


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def test_a_flapping_owner_costs_nothing():
    rec = record(Command("on", 200, WARM))
    req = SetRequest("tv", OFF_COMMAND, priority=40, expires_at=NOW + 3600, owner="owner_a")
    assert apply_set(rec, req, NOW, seq_from(1)).result == "created"
    for i in range(1, 9):
        again = SetRequest("tv", OFF_COMMAND, priority=40, expires_at=NOW + 3600 + i, owner="owner_a")
        assert apply_set(rec, again, NOW + i, no_seq).result == "refreshed"
    assert rec.last_layers_change == NOW
    assert rec.layers["tv"].expires_at == NOW + 3608
    assert apply_clear(rec, "tv", NOW + 10) == ClearResult(("tv",))
    assert resolve(rec, NOW + 10) == Resolution(Command("on", 200, WARM), "base")


def test_expired_during_downtime_goes_through_the_safety_rule_after_the_grace():
    # A hold ran out while Home Assistant was down. During the startup grace the
    # owner's renewal leaves it alone; expire() then keeps the lamp dark.
    rec = record(Command("on", 200, WARM), layer("hold", 40, OFF_COMMAND, expires_at=NOW - 600))
    renew = SetRequest("hold", OFF_COMMAND, only_if_present=True, expires_at=NOW + 600)
    assert apply_set(rec, renew, NOW, no_seq).result == "skipped_absent"
    assert expire(rec, NOW + 180, Observed("off"), CAPS) == ExpireResult(("hold",), False)
    assert rec.base == OFF_COMMAND


def test_expired_during_downtime_and_cleared_by_its_owner_renders_after_the_grace():
    # The same, but the owner's start trigger clears it: an explicit clear restores.
    rec = record(Command("on", 200, WARM), layer("hold", 40, OFF_COMMAND, expires_at=NOW - 600))
    cleared = apply_clear(rec, "hold", NOW)
    assert expire(rec, NOW + 180, Observed("off"), CAPS) == ExpireResult(("hold",), True)
    assert rec.base == Command("on", 200, WARM)
    assert cleared == ClearResult((), (), ("hold",))


# --------------------------------------------------------------------------- #
# Review fixes
# --------------------------------------------------------------------------- #


def test_an_only_if_present_renewal_reads_its_command_in_the_layers_mode():
    # The service leaves the state out of a renewal that leaves the mode out. On an
    # adjust layer that is the owner's request unchanged: a refresh, options kept.
    dim = layer("dim", 40, Command(None, 64), "adjust", expires_at=NOW + 600, resume=True,
                on_expire="render")
    rec = record(Command("on", 102), dim)
    rec.last_layers_change = 5.0
    renew = SetRequest("dim", Command(None, 64), only_if_present=True, expires_at=NOW + 900)
    assert apply_set(rec, renew, NOW, no_seq).result == "refreshed"
    assert (dim.mode, dim.requested, dim.resume_after_manual, dim.on_expire) == (
        "adjust", Command(None, 64), True, "render")
    assert rec.last_layers_change == 5.0
    # Even with an explicit "on" (an adjust layer takes attributes only).
    renew_on = SetRequest("dim", Command("on", 64), only_if_present=True, expires_at=NOW + 900)
    assert apply_set(rec, renew_on, NOW, no_seq).result == "refreshed"
    # On a set layer, attributes without a state mean "on like this", as at creation.
    nl = layer("nl", 50, Command("on", 13), expires_at=NOW + 600)
    rec = record(OFF_COMMAND, nl)
    renew = SetRequest("nl", Command(None, 13), only_if_present=True, expires_at=NOW + 900)
    assert apply_set(rec, renew, NOW, no_seq).result == "refreshed"
    assert nl.requested == Command("on", 13)
    change = SetRequest("nl", Command(None, 20), only_if_present=True, expires_at=NOW + 900)
    assert apply_set(rec, change, NOW, no_seq).result == "updated"
    assert (nl.mode, nl.requested) == ("set", Command("on", 20))


def test_edit_active_an_off_becomes_the_base_so_a_clear_never_relights():
    # A house-wide Off while the tv layer holds an edit_active lamp.
    tv = layer("tv", 40, Command("on", 30))
    rec = record(Command("on", 200), tv)
    res = apply_external(rec, OFF_COMMAND, frozenset({"state"}), "automation", "edit_active", NOW)
    assert res.edited == "tv"
    assert tv.command == OFF_COMMAND and rec.base == OFF_COMMAND
    apply_clear(rec, "tv", NOW + 1)
    assert resolve(rec, NOW + 1).command == OFF_COMMAND


def test_edit_active_an_on_over_an_off_base_reaches_the_base():
    # The lamp was off before the hold; a person turns it on mid-hold. Clearing the
    # hold must not turn it off again.
    tv = layer("tv", 40, OFF_COMMAND)
    rec = record(OFF_COMMAND, tv)
    apply_external(rec, Command("on", 150, WARM), None, "user", "edit_active", NOW)
    assert tv.command == Command("on", 150, WARM)
    assert rec.base == Command("on", 150, WARM)
    # An on over a base that is on only edits the layer: the base keeps its attributes.
    tv2 = layer("tv", 40, Command("on", 30))
    rec = record(Command("on", 200), tv2)
    apply_external(rec, Command("on", 90), frozenset({"state", "brightness"}), "user",
                   "edit_active", NOW)
    assert (tv2.command, rec.base) == (Command("on", 90), Command("on", 200))


def test_external_records_the_groups_it_took():
    rec = record(Command("on", 102, WARM), layer("sig", 70, Command("on", 255, RED)))
    apply_external(rec, Command("on", 128, RED), STATE_BRIGHTNESS, "user", "take_back", NOW)
    assert rec.last_external.groups == STATE_BRIGHTNESS
    apply_external(rec, Command("on", 90), None, "device", "take_back", NOW)
    assert rec.last_external.groups is None


# --------------------------------------------------------------------------- #
# Renewals and the lease
# --------------------------------------------------------------------------- #


def test_a_renewal_without_an_expiry_keeps_the_lease():
    # An identical set "only extends the time" (README): one that gives no ttl must not
    # silently make a leased layer permanent.
    nl = layer("nl", 50, Command("on", 13), expires_at=NOW + 600)
    rec = record(OFF_COMMAND, nl)
    assert apply_set(rec, SetRequest("nl", Command("on", 13), priority=50), NOW + 1, no_seq).result == "refreshed"
    assert nl.expires_at == NOW + 600
    assert apply_set(rec, SetRequest("nl", Command("on", 13), priority=50, expires_at=NOW + 900),
                     NOW + 2, no_seq).result == "refreshed"
    assert nl.expires_at == NOW + 900
    # An update (a different command) replaces the layer, expiry included.
    assert apply_set(rec, SetRequest("nl", Command("on", 30), priority=50), NOW + 3, no_seq).result == "updated"
    assert nl.expires_at is None
