"""Switches are lamps that only do on/off: relays behind lights, smart plugs.

The case that motivated this (2026-09-15): a fan on a smart plug that must stay off
while someone sleeps. The plug drops off the network for minutes at a time and comes
back `on` by itself; an edge-triggered automation turned it off once and never
re-asserted, and could not blindly, because that would also fight a person's press.
A layer holding the plug off is verified, survives the dropout as an owed command,
and a person's press takes it back.
"""

from __future__ import annotations

import pytest
from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.layers.config_flow import not_a_lamp
from custom_components.layers.const import DOMAIN

from .conftest import FakeLamp, FakeSwitch, settle, setup_layers
from .test_behaviour import advance, start

pytestmark = pytest.mark.freeze_time(tick=True)


async def test_hold_a_switch_off_then_give_it_back(
    hass: HomeAssistant, switches: dict[str, FakeSwitch]
) -> None:
    entry = await setup_layers(hass, ["switch.relay"])
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "switch.relay", "layer": "sleep", "priority": 50, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("switch.relay").state == "off"
    assert switches["relay"].calls[-1][0] == "turn_off"
    rec = entry.runtime_data.engine.records["switch.relay"]
    assert rec.diverged is None and "sleep" in rec.layers

    await hass.services.async_call(DOMAIN, "clear", {"layer": "sleep"}, blocking=True)
    await settle(hass)
    assert hass.states.get("switch.relay").state == "on"
    assert switches["relay"].calls[-1][0] == "turn_on"
    # A switch gets a bare turn_on: no brightness, no transition.
    assert switches["relay"].calls[-1][1] == {}


async def test_brightness_and_colour_are_projected_away_on_a_switch(
    hass: HomeAssistant, switches: dict[str, FakeSwitch]
) -> None:
    await setup_layers(hass, ["switch.plug"])
    await hass.services.async_call(
        DOMAIN, "set",
        {"entity_id": "switch.plug", "layer": "signal", "priority": 70,
         "brightness": 200, "xy_color": [0.68, 0.31], "transition": 5},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("switch.plug").state == "on"
    assert switches["plug"].calls[-1] == ("turn_on", {}, switches["plug"].calls[-1][2])


async def test_a_person_takes_the_switch_back(
    hass: HomeAssistant, switches: dict[str, FakeSwitch], hass_admin_user: MockUser
) -> None:
    entry = await setup_layers(hass, ["switch.relay"])
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "switch.relay", "layer": "sleep", "priority": 50, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.relay"},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await settle(hass)
    rec = entry.runtime_data.engine.records["switch.relay"]
    assert not rec.layers and "sleep" in rec.tombstones
    assert rec.base.state == "on"
    assert hass.states.get("switch.relay").state == "on"


async def test_a_wall_press_with_no_context_takes_the_switch_back(
    hass: HomeAssistant, switches: dict[str, FakeSwitch], freezer: TickingDateTimeFactory,
) -> None:
    """A relay's wall switch toggles it locally: the state changes with no context.

    Inside the 15 s late window after our command a no-context flip back is read
    as the device reverting the command and retried once (the Hue-bridge trap);
    a press later than that is a person's.
    """
    engine = await start(hass, ["switch.relay"])
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "switch.relay", "layer": "sleep", "priority": 50, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    await advance(hass, freezer, 20)         # past the late window
    switches["relay"].push(is_on=True)
    await advance(hass, freezer, 3.5)        # the no-context debounce
    rec = engine.records["switch.relay"]
    assert not rec.layers and "sleep" in rec.tombstones
    assert rec.base.state == "on"
    assert hass.states.get("switch.relay").state == "on"


async def test_a_switch_that_returns_untouched_gets_its_owed_off(
    hass: HomeAssistant, switches: dict[str, FakeSwitch], freezer: TickingDateTimeFactory,
) -> None:
    """Held off, drops off the network while a person's automation asks for off, comes
    back showing what it showed: the owed off is delivered (SPEC 6.4)."""
    engine = await start(hass, ["switch.relay"])
    plug = switches["relay"]
    plug.set_available(False)
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "switch.relay", "layer": "sleep", "priority": 50, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    rec = engine.records["switch.relay"]
    assert rec.owed is not None and plug.calls == []

    plug.set_available(True)                 # back, still on as it was
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)        # the return settle
    assert hass.states.get("switch.relay").state == "off"
    assert plug.calls[-1][0] == "turn_off"
    assert "sleep" in rec.layers and rec.owed is None


async def test_a_switch_that_returns_changed_is_taken_back(
    hass: HomeAssistant, switches: dict[str, FakeSwitch], freezer: TickingDateTimeFactory,
) -> None:
    """The smart-plug trap, as the model stands: held off, drops off the network,
    comes back ON. Nobody can tell a power-on default from a press while Layers
    was blind, so it is read as a device change and taken back (SPEC 6.4) - the
    plug stays on and the layer is tombstoned. A policy that re-asserts on such
    a return would be a separate decision; this test pins the current one."""
    engine = await start(hass, ["switch.relay"])
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "switch.relay", "layer": "sleep", "priority": 50, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("switch.relay").state == "off"
    plug = switches["relay"]
    plug.set_available(False)
    await hass.async_block_till_done()
    calls_before = len(plug.calls)

    plug._attr_is_on = True                  # the plug's own power-on default
    plug.set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    rec = engine.records["switch.relay"]
    assert hass.states.get("switch.relay").state == "on"
    assert len(plug.calls) == calls_before
    assert not rec.layers and "sleep" in rec.tombstones and rec.base.state == "on"


async def test_a_light_call_does_not_reach_a_switch_and_vice_versa(
    hass: HomeAssistant, lights: dict[str, FakeLamp], switches: dict[str, FakeSwitch],
    hass_admin_user: MockUser,
) -> None:
    entry = await setup_layers(hass, ["light.lamp_a", "switch.relay"])
    for eid in ("light.lamp_a", "switch.relay"):
        await hass.services.async_call(
            DOMAIN, "set", {"entity_id": eid, "layer": "hold", "priority": 40, "state": "off"},
            blocking=True,
        )
    await settle(hass)
    engine = entry.runtime_data.engine
    # A person's light.turn_on naming the switch is refused by HA and must not be
    # read as a take-back of the switch.
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_a"},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await settle(hass)
    assert not engine.records["light.lamp_a"].layers
    assert engine.records["switch.relay"].layers


async def test_a_switch_group_cannot_be_enrolled(hass: HomeAssistant, switches: dict[str, FakeSwitch]) -> None:
    hass.states.async_set("switch.all_relays", "on", {"entity_id": ["switch.relay", "switch.plug"]})
    assert not_a_lamp(hass, ["switch.relay", "switch.all_relays"]) == ["switch.all_relays"]


async def test_get_describes_a_switch(hass: HomeAssistant, switches: dict[str, FakeSwitch]) -> None:
    await setup_layers(hass, ["switch.relay"])
    response = await hass.services.async_call(
        DOMAIN, "get", {"entity_id": "switch.relay"}, blocking=True, return_response=True
    )
    ent = response["entities"]["switch.relay"]
    assert ent["observed"]["state"] == "on"
    assert ent["observed"]["brightness"] is None
