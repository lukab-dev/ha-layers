"""Regression tests: startup, reload, persistence and renames (SPEC 2, 7.2, 7.4).

Each test names the review finding it pins down. Clock and Store helpers from
test_lifecycle.py (``advance`` moves the clock and fires what fell due).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.layers.const import CONF_EDIT_ACTIVE, CONF_ENTITIES, STORAGE_KEY
from custom_components.layers.logic.model import OFF_COMMAND, Owed, Record

from .conftest import FakeLamp, settle, setup_layers
from .test_lifecycle import (
    BASE_A,
    SIGNAL_XY,
    A,
    C,
    advance,
    assert_shows_base_a,
    brightness,
    engine_of,
    layers,
    now_ts,
    seed,
    services,
    shows,
    start,
    watch_light_calls,
)

pytestmark = pytest.mark.freeze_time(tick=True)


def on_disk(hass_storage: dict[str, Any], eid: str) -> dict[str, Any]:
    return hass_storage.get(STORAGE_KEY, {}).get("data", {}).get("entities", {}).get(eid, {})


# --------------------------------------------------------------------------- R2-2


@pytest.mark.parametrize("how", ["clear", "ttl"])
async def test_a_layer_released_during_the_grace_restores_what_the_lamp_showed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, how: str,
) -> None:
    """Review 2 #2: during the startup grace a lamp with no layer has no base yet. A
    doorbell flash set and released in that window must not strand the lamp on the
    flash, nor make the flash its base at the grace end."""
    entry = await setup_layers(hass, [A], end_grace=False)
    engine = engine_of(entry)
    assert engine.in_grace and engine.records[A].base is None
    flash = {"entity_id": A, "layer": "doorbell", "priority": 80, "brightness": 255,
             "xy_color": list(SIGNAL_XY)}
    if how == "ttl":
        flash["ttl"] = 2
    assert (await layers(hass, "set", flash))["entities"] == {A: "queued"}
    await settle(hass)
    assert brightness(hass, A) == 255
    assert engine.records[A].base == BASE_A          # learned before the layer went on
    if how == "clear":
        assert (await layers(hass, "clear", {"entity_id": A, "layer": "doorbell"}))[
            "entities"] == {A: "queued"}
        await settle(hass)
        assert_shows_base_a(hass)
    await advance(hass, freezer, 5.5)                # the grace ends (a ttl expires with it)
    assert not engine.in_grace
    rec = engine.records[A]
    assert rec.layers == {} and rec.base == BASE_A
    assert_shows_base_a(hass)


async def test_after_a_reload_a_set_learns_the_base_first(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any,
) -> None:
    """Review 2 #2 C: a reload forgets the bases of lamps without layers (they are not
    stored); an options change must not open that window after every save."""
    entry = await start(hass, freezer, [A])
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    engine = engine_of(entry)
    assert engine.records[A].base is None
    await layers(hass, "set", {"entity_id": A, "layer": "doorbell", "priority": 80,
                               "brightness": 255, "xy_color": list(SIGNAL_XY)})
    await settle(hass)
    await layers(hass, "clear", {"entity_id": A, "layer": "doorbell"})
    await settle(hass)
    assert_shows_base_a(hass)
    await advance(hass, freezer, 5.5)
    assert engine.records[A].base == BASE_A


# --------------------------------------------------------------------------- R2-5 / R4-1


async def test_owed_is_on_disk_as_soon_as_a_render_starts(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_storage: dict[str, Any], freezer: Any,
) -> None:
    """Review 2 #5 / review 4 #1: SPEC 7.2: owed is persisted immediately. A lazy save
    of an observation must not push that write out."""
    await start(hass, freezer, [A])
    lights["a"].ignore_commands = True
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "transition": 30})
    for _ in range(3):
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    got = on_disk(hass_storage, A).get("owed")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.layers_apply"},
                                   blocking=True)
    assert got and got["target"] == {"state": "off"}


async def test_a_new_layer_is_on_disk_within_seconds_even_when_the_lamp_reports_meanwhile(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_storage: dict[str, Any], freezer: Any,
) -> None:
    """Review 4 #1: a transition step reported under our context (a lazy, 60 s save)
    right after the set must not postpone the set's own 2 s save: a crash then would
    lose the layer, and the next startup would take its dim output as the base."""
    entry = await start(hass, freezer, [A])
    engine = engine_of(entry)
    lamp = lights["a"]
    await layers(hass, "set", {"entity_id": A, "layer": "tv", "priority": 40, "brightness": 64,
                               "transition": 10})
    await asyncio.sleep(0.05)
    lamp._attr_brightness = 66                  # a step of the transition, inside HA's reuse
    lamp.async_write_ha_state()
    await hass.async_block_till_done()
    assert [d["kind"] for d in engine.decisions if d["entity_id"] == A][-2:] == ["ours", "ours"]
    engine.renderer.cancel(A)                   # (no need to wait out the transition)
    await advance(hass, freezer, 3)
    assert "tv" in on_disk(hass_storage, A).get("layers", {}), "the write was postponed"


async def test_a_save_after_the_apply_switch_is_not_lost(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_storage: dict[str, Any], freezer: Any,
) -> None:
    """The earliest-write bookkeeping resets when the switch saves directly."""
    await start(hass, freezer, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.layers_apply"},
                                   blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.layers_apply"},
                                   blocking=True)
    await layers(hass, "set", {"entity_id": A, "layer": "sig", "priority": 70, "brightness": 20})
    await advance(hass, freezer, 3)
    assert set(on_disk(hass_storage, A)["layers"]) == {"hold", "sig"}


# --------------------------------------------------------------------------- R2-10


async def test_a_renamed_lamp_keeps_its_layers_and_options(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any,
) -> None:
    """Review 2 #10: renaming an enrolled lamp's entity id must not strand its layers
    (the options flow would then refuse both to keep and to drop the old id)."""
    entry = await start(hass, freezer, [A, C], edit_active=[A])
    await layers(hass, "set", {"entity_id": A, "layer": "tv", "priority": 40, "brightness": 20})
    await settle(hass)
    new = "light.lamp_x"
    er.async_get(hass).async_update_entity(A, new_entity_id=new)
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    engine = engine_of(entry)
    assert entry.options[CONF_ENTITIES] == [new, C]
    assert entry.options[CONF_EDIT_ACTIVE] == [new]
    assert A not in engine.records and "tv" in engine.records[new].layers
    assert engine.policy_for(new) == "edit_active"
    assert (await layers(hass, "clear", {"entity_id": new, "layer": "tv"}))["entities"] == {
        new: "queued"}
    await settle(hass)
    assert brightness(hass, new) == 102


# --------------------------------------------------------------------------- R3-8


async def test_a_change_made_while_layers_was_down_is_not_undone_by_the_owners_clear(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any,
) -> None:
    """Review 3 #8: Home Assistant's downtime counts as time away (SPEC 2). A layered
    lamp that came back different from when Layers stopped was changed by somebody:
    taken back at the grace end, so the film's end does not relight it."""
    entry = await start(hass, freezer, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "tv", "priority": 40, "mode": "adjust",
                               "brightness": 64})
    await settle(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    lights["a"].push(is_on=False)                    # while Layers is not loaded
    await hass.async_block_till_done()
    calls = watch_light_calls(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine = engine_of(entry)
    await advance(hass, freezer, 5.5)
    rec = engine.records[A]
    assert rec.layers == {} and "tv" in rec.tombstones and rec.base == OFF_COMMAND
    assert (await layers(hass, "clear", {"entity_id": A, "layer": "tv"}))["entities"] == {
        A: "unchanged"}
    await settle(hass)
    assert hass.states.get(A).state == "off" and calls == []


# --------------------------------------------------------------------------- R4-3


async def test_an_owed_off_older_than_a_day_is_still_delivered(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
) -> None:
    """Review 4 #3: SPEC 6.4: an owed off is delivered however old (only an owed ON ages
    out, in decide_return)."""
    now = now_ts()
    lights["c"].push(is_on=True, brightness=13)
    await hass.async_block_till_done()
    seed(hass_storage, {
        C: Record(C, base=OFF_COMMAND, observed=shows(hass, C),
                  owed=Owed(now - 25 * 3600, OFF_COMMAND, turns_on=False)),
    })
    calls = watch_light_calls(hass)
    await setup_layers(hass, [C], apply=False, end_grace=False)
    await advance(hass, freezer, 5.5)
    assert services(calls) == ["turn_off"] and hass.states.get(C).state == "off"


# --------------------------------------------------------------------------- removed entities


async def test_a_lamp_whose_entity_is_removed_and_re_added_is_judged_as_a_return(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any,
) -> None:
    """Review 2 #10: a removed entity (its integration reloading) is away, not a lamp
    still showing its last report; when it is back, its return is judged."""
    entry = await start(hass, freezer, [C])
    engine = engine_of(entry)
    lamp = lights["c"]
    await layers(hass, "set", {"entity_id": C, "layer": "night", "priority": 50,
                               "brightness": 13})
    await settle(hass)
    hass.states.async_remove(C)                      # as an integration reload does
    await hass.async_block_till_done()
    rec = engine.records[C]
    assert not rec.available and rec.p_at_drop is not None
    assert engine.describe([C])[C]["available"] is False
    lamp._attr_is_on = False                          # it comes back at its power-on default
    lamp.async_write_ha_state()
    await hass.async_block_till_done()
    assert engine._pending("return", C)               # noqa: SLF001
    await advance(hass, freezer, 5.5)
    assert [d["kind"] for d in engine.decisions if d["entity_id"] == C][-1] == "return:external"
    assert rec.layers == {} and rec.base == OFF_COMMAND
