"""End-to-end smoke tests: set, clear, take back, persistence, shadow mode."""

from __future__ import annotations

from homeassistant.core import Context, HomeAssistant
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.layers.const import DOMAIN

from .conftest import FakeLamp, settle, setup_layers


async def test_set_then_clear_restores_what_is_below(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await setup_layers(hass, ["light.lamp_a", "light.lamp_b"])
    await hass.services.async_call(
        DOMAIN, "set",
        {"entity_id": ["light.lamp_a", "light.lamp_b"], "layer": "hold", "priority": 40, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("light.lamp_a").state == "off"
    assert hass.states.get("light.lamp_b").state == "off"

    await hass.services.async_call(DOMAIN, "clear", {"layer": "hold"}, blocking=True)
    await settle(hass)
    a, b = hass.states.get("light.lamp_a"), hass.states.get("light.lamp_b")
    assert a.state == "on" and a.attributes["brightness"] == 102
    assert b.state == "on" and b.attributes["brightness"] == 200


async def test_our_render_carries_the_callers_context(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await setup_layers(hass, ["light.lamp_a"])
    caller = Context()
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "light.lamp_a", "layer": "hold", "priority": 40, "state": "off"},
        blocking=True, context=caller,
    )
    await settle(hass)
    assert hass.states.get("light.lamp_a").context.parent_id == caller.id


async def test_a_person_takes_the_lamp_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    entry = await setup_layers(hass, ["light.lamp_a"])
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "light.lamp_a", "layer": "hold", "priority": 40, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    # A person turns it up from the app mid-hold.
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_a", "brightness": 230},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await settle(hass)
    engine = entry.runtime_data.engine
    rec = engine.records["light.lamp_a"]
    assert not rec.layers and "hold" in rec.tombstones
    assert rec.base.brightness == 230
    # The owner's refresh is skipped, and clearing leaves the person's setting alone.
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "light.lamp_a", "layer": "hold", "priority": 40, "state": "off"},
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, "clear", {"layer": "hold", "entity_id": "light.lamp_a"}, blocking=True)
    await settle(hass)
    state = hass.states.get("light.lamp_a")
    assert state.state == "on" and state.attributes["brightness"] == 230
    assert "hold" not in rec.tombstones


async def test_shadow_mode_sends_nothing(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await setup_layers(hass, ["light.lamp_a"], apply=False)
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "light.lamp_a", "layer": "hold", "priority": 40, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("light.lamp_a").state == "on"
    assert lights["a"].calls == []
    assert hass.states.get("sensor.layers_status").state == "shadow"


async def test_layers_survive_a_reload(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    entry = await setup_layers(hass, ["light.lamp_a"])
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "light.lamp_a", "layer": "hold", "priority": 40, "state": "off"},
        blocking=True,
    )
    await settle(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    assert "hold" in engine.records["light.lamp_a"].layers
    assert engine.apply is True


async def test_get_reports_the_model(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await setup_layers(hass, ["light.lamp_a"])
    await hass.services.async_call(
        DOMAIN, "set",
        {"entity_id": "light.lamp_a", "layer": "dim", "priority": 40, "mode": "adjust", "brightness_pct": 25},
        blocking=True,
    )
    await settle(hass)
    result = await hass.services.async_call(DOMAIN, "get", {"entity_id": "light.lamp_a"},
                                            blocking=True, return_response=True)
    lamp = result["entities"]["light.lamp_a"]
    assert lamp["active"] == "dim"
    assert lamp["effective"]["brightness"] == 64
    assert hass.states.get("light.lamp_a").attributes["brightness"] == 64
