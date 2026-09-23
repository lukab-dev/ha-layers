"""Follow layers: brightness and colour read from another entity (Adaptive Lighting, a
template sensor), stacked like any layer, never dropped by a person's change.

What a person changes by hand stops following, only that, until the lamp goes off or
the layer's ``manual_timeout`` runs out.
"""

from __future__ import annotations

import pytest

from layers_logic.capability import follow_command
from layers_logic.model import (
    OFF_COMMAND,
    Color,
    Command,
    Layer,
    Record,
    SetRequest,
)
from layers_logic.policy import (
    PolicyError,
    apply_clear,
    apply_external,
    apply_set,
    expire,
    release_manual,
)
from layers_logic.resolve import active_layer, editable_layer, resolve, state_holder

NOW = 1000.0
LAMP = "light.lamp_a"
SOURCE = "switch.adaptive_lighting_living"
WARM = Color.kelvin(2700)
COOL = Color.kelvin(4000)
RED = Color.xy(0.64, 0.33)
ATTRS = frozenset({"brightness", "color"})


def seq():
    n = iter(range(1, 1000))
    return lambda: next(n)


def follow(rec: Record, *, layer="ambient", priority=10, command=Command(None, 120, WARM),
           **kw) -> Layer:
    """A follow layer as the engine leaves it after reading the source."""
    apply_set(rec, SetRequest(layer, Command(None), priority, mode="follow", source=SOURCE, **kw),
              NOW, seq())
    rec.layers[layer].command = command
    return rec.layers[layer]


def lamp(base: Command | None = Command("on", 255, COOL)) -> Record:
    return Record(LAMP, base=base)


# --------------------------------------------------------------------------- source


def test_follow_command_reads_adaptive_lightings_switch():
    attrs = {"brightness_pct": 40, "color_temp_kelvin": 2680, "xy_color": [0.5, 0.4],
             "rgb_color": [255, 180, 100]}
    assert follow_command("on", attrs) == Command(None, 102, Color.kelvin(2680))


def test_follow_command_is_empty_while_the_source_is_off_or_away():
    for state in ("off", "unavailable", "unknown", None):
        assert follow_command(state, {"brightness": 100}) == Command(None)


def test_follow_command_takes_any_state_of_a_template_sensor():
    assert follow_command("21.5", {"brightness": 300, "hs_color": [30, 60]}) == Command(
        None, 255, Color("hs_color", (30.0, 60.0)))


# --------------------------------------------------------------------------- resolver


def test_it_colours_and_dims_a_lamp_that_is_on():
    rec = lamp()
    follow(rec)
    assert resolve(rec, NOW).command == Command("on", 120, WARM)
    assert resolve(rec, NOW).active == "ambient"


def test_it_never_turns_a_lamp_on_and_never_holds_it():
    rec = lamp(OFF_COMMAND)
    follow(rec)
    assert resolve(rec, NOW).command == OFF_COMMAND
    assert resolve(rec, NOW).active == "base"
    assert state_holder(rec, NOW) is None


def test_a_source_with_nothing_to_give_is_not_active():
    rec = lamp()
    follow(rec, command=Command(None))
    assert resolve(rec, NOW) .command == Command("on", 255, COOL)
    assert resolve(rec, NOW).active == "base"


def test_what_a_person_took_over_is_skipped():
    rec = lamp(Command("on", 60, COOL))
    layer = follow(rec)
    layer.manual = frozenset({"brightness"})
    assert resolve(rec, NOW).command == Command("on", 60, WARM)
    layer.manual = ATTRS
    assert resolve(rec, NOW).command == Command("on", 60, COOL)
    assert resolve(rec, NOW).active == "base"


def test_a_layer_above_wins_and_one_below_decides_on_off():
    rec = lamp(OFF_COMMAND)
    follow(rec)
    apply_set(rec, SetRequest("evening", Command("on", 200), 5), NOW, seq())
    assert resolve(rec, NOW).command == Command("on", 120, WARM)       # set below, follow above
    apply_set(rec, SetRequest("night", Command("on", 20, RED), 50), NOW, seq())
    assert resolve(rec, NOW).command == Command("on", 20, RED)          # a nightlight wins
    assert resolve(rec, NOW).active == "night"


def test_editable_layer_passes_over_follow():
    rec = lamp()
    follow(rec)
    assert active_layer(rec, NOW).id == "ambient"
    assert editable_layer(rec, NOW) is None
    apply_set(rec, SetRequest("tv", Command("on", 40), 5), NOW, seq())
    assert editable_layer(rec, NOW).id == "tv"


# --------------------------------------------------------------------------- layers.set


def test_set_needs_a_source_and_nothing_else():
    rec = lamp()
    with pytest.raises(PolicyError):
        apply_set(rec, SetRequest("a", Command(None), 10, mode="follow"), NOW, seq())
    with pytest.raises(PolicyError):
        apply_set(rec, SetRequest("a", Command(None, 100), 10, mode="follow", source=SOURCE),
                  NOW, seq())
    with pytest.raises(PolicyError):
        apply_set(rec, SetRequest("a", Command(None), 10, mode="follow", source=LAMP), NOW, seq())
    with pytest.raises(PolicyError):
        apply_set(rec, SetRequest("base", Command(None), mode="follow", source=SOURCE), NOW, seq())


def test_the_same_source_again_only_refreshes_and_keeps_the_take_over():
    rec = lamp()
    layer = follow(rec, manual_timeout=600)
    layer.manual = frozenset({"brightness"})
    result = apply_set(rec, SetRequest("ambient", Command(None), mode="follow", source=SOURCE,
                                       expires_at=NOW + 60), NOW, seq())
    assert result.result == "refreshed"
    assert layer.manual == frozenset({"brightness"})
    assert layer.command == Command(None, 120, WARM)
    assert layer.expires_at == NOW + 60 and layer.manual_timeout == 600


def test_a_renewal_without_mode_refreshes_a_follow_layer():
    rec = lamp()
    follow(rec)
    result = apply_set(rec, SetRequest("ambient", Command(None), only_if_present=True,
                                       expires_at=NOW + 60), NOW, seq())
    assert result.result == "refreshed"
    assert rec.layers["ambient"].mode == "follow"


def test_a_new_source_is_an_update_and_starts_over():
    rec = lamp()
    layer = follow(rec)
    layer.manual = ATTRS
    result = apply_set(rec, SetRequest("ambient", Command(None), mode="follow",
                                       source="sensor.circadian"), NOW, seq())
    assert result.result == "updated"
    assert layer.source == "sensor.circadian"
    assert layer.manual == frozenset() and layer.command == Command(None)


def test_turning_a_follow_layer_into_a_set_layer_forgets_the_source():
    rec = lamp()
    layer = follow(rec)
    apply_set(rec, SetRequest("ambient", Command("on", 30)), NOW, seq())
    assert layer.mode == "set" and layer.source is None
    assert resolve(rec, NOW).command == Command("on", 30, COOL)


def test_layer_active_edits_the_layer_below_follow():
    rec = lamp()
    follow(rec)
    result = apply_set(rec, SetRequest("active", Command(None, 50)), NOW, seq())
    assert result.result == "base_set"
    assert rec.base == Command("on", 50, COOL)
    assert rec.layers["ambient"].command == Command(None, 120, WARM)


def test_clear_removes_it():
    rec = lamp()
    follow(rec)
    apply_clear(rec, "ambient", NOW)
    assert resolve(rec, NOW).command == Command("on", 255, COOL)


# --------------------------------------------------------------------------- a person's change


@pytest.mark.parametrize("policy", ["take_back", "edit_active", "base_keep_layers", "reassert"])
def test_it_survives_every_policy(policy):
    rec = lamp()
    follow(rec)
    apply_set(rec, SetRequest("tv", Command("on", 40), 40), NOW, seq())
    apply_external(rec, Command("on", 90), frozenset({"state", "brightness"}), "user", policy, NOW,
                   chosen=frozenset({"brightness"}))
    assert "ambient" in rec.layers
    assert "ambient" not in rec.tombstones


def test_take_back_drops_the_others_and_takes_over_only_what_was_chosen():
    rec = lamp()
    follow(rec)
    apply_set(rec, SetRequest("tv", Command("on", 40), 40), NOW, seq())
    result = apply_external(rec, Command("on", 90), frozenset({"state", "brightness"}), "user",
                            "take_back", NOW, chosen=frozenset({"brightness"}))
    assert result.dropped == ("tv",)
    assert rec.layers["ambient"].manual == frozenset({"brightness"})
    assert resolve(rec, NOW).command == Command("on", 90, WARM)     # colour still follows


def test_a_bare_turn_on_takes_over_nothing():
    rec = lamp(OFF_COMMAND)
    follow(rec)
    apply_external(rec, Command("on", 254, COOL), None, "device", "take_back", NOW,
                   chosen=frozenset())
    assert rec.layers["ambient"].manual == frozenset()
    assert resolve(rec, NOW).command == Command("on", 120, WARM)


def test_without_chosen_the_groups_taken_are_taken_over():
    rec = lamp()
    follow(rec)
    apply_external(rec, Command("on", 90, RED), None, "device", "take_back", NOW)
    assert rec.layers["ambient"].manual == ATTRS


def test_a_reassert_takes_over_nothing():
    rec = lamp()
    follow(rec)
    apply_set(rec, SetRequest("hold", Command("on", 200), 30), NOW, seq())
    result = apply_external(rec, Command("on", 90), None, "device", "reassert", NOW)
    assert result.reassert
    assert rec.layers["ambient"].manual == frozenset()


def test_reassert_with_only_a_follow_layer_is_a_take_back():
    rec = lamp()
    follow(rec)
    result = apply_external(rec, Command("on", 90), None, "device", "reassert", NOW)
    assert not result.reassert
    assert rec.layers["ambient"].manual == frozenset({"brightness"})   # the change had no colour


def test_edit_active_edits_the_layer_below_not_follow():
    rec = lamp()
    follow(rec)
    apply_set(rec, SetRequest("tv", Command("on", 40), 5), NOW, seq())
    result = apply_external(rec, Command("on", 90), frozenset({"state", "brightness"}), "user",
                            "edit_active", NOW, chosen=frozenset({"brightness"}))
    assert result.edited == "tv"
    assert rec.layers["tv"].command == Command("on", 90)
    assert rec.layers["ambient"].command == Command(None, 120, WARM)


def test_a_timeout_gives_it_back():
    rec = lamp()
    layer = follow(rec, manual_timeout=600)
    apply_external(rec, Command("on", 90), frozenset({"state", "brightness"}), "user",
                   "take_back", NOW, chosen=frozenset({"brightness"}))
    assert layer.manual_until == NOW + 600
    assert release_manual(rec, NOW + 599) == ()
    assert release_manual(rec, NOW + 600) == ("ambient",)
    assert layer.manual == frozenset() and layer.manual_until is None
    assert resolve(rec, NOW + 600).command == Command("on", 120, WARM)


def test_off_gives_it_back_without_a_timeout():
    rec = lamp()
    layer = follow(rec)
    layer.manual = ATTRS
    assert release_manual(rec, NOW + 10**6) == ()
    assert release_manual(rec, NOW, off=True) == ("ambient",)
    assert layer.manual == frozenset()


# --------------------------------------------------------------------------- expiry and storage


def test_an_expiry_never_brightens():
    rec = lamp(Command("on", 255, COOL))
    follow(rec, expires_at=NOW + 10, command=Command(None, 60, WARM))
    from layers_logic.model import Caps, Observed
    shown = Observed("on", 60, "color_temp", None, None, 2700, NOW)
    rec.observed = shown
    caps = Caps(frozenset({"color_temp"}), 2000, 6500, True, "test")
    result = expire(rec, NOW + 10, shown, caps)
    assert result.expired == ("ambient",) and not result.render
    assert rec.base == Command("on", 60, WARM)


def test_the_new_fields_survive_the_store_and_an_old_store_loads():
    layer = Layer("ambient", 10, "follow", Command(None), Command(None, 120, WARM), 1, NOW,
                  source=SOURCE, manual=frozenset({"color"}), manual_until=NOW + 5,
                  manual_timeout=5.0, transition=2.0)
    assert Layer.from_json(layer.to_json()) == layer
    old = Layer("tv", 40, "set", Command("on", 40), Command("on", 40), 2, NOW).to_json()
    for key in ("source", "manual", "manual_until", "manual_timeout", "transition"):
        del old[key]
    loaded = Layer.from_json(old)
    assert loaded.source is None and loaded.manual == frozenset()
