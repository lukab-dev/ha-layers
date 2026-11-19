"""The sensor each managed lamp and thermostat gets: the layer on top, and the stack
in its attributes."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import setup_test_component_platform

from custom_components.layers.const import CONF_ENTITIES
from custom_components.layers.sensor import StackSensor

from . import test_climate as tc
from .conftest import FakeLamp, FakeSwitch, FakeThermostat, settle
from .test_behaviour import A, B, layers, start, state

pytestmark = pytest.mark.freeze_time(tick=True)

SENSOR_A = "sensor.layers_lamp_a"
SENSOR_B = "sensor.layers_lamp_b"


async def test_every_lamp_gets_a_sensor_named_after_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A, B])
    for sensor, lamp in ((SENSOR_A, A), (SENSOR_B, B)):
        st = state(hass, sensor)
        assert st.state == "base"
        assert st.attributes["entity_id"] == lamp
        assert st.attributes["layers"] == []
        assert st.attributes["friendly_name"] == f"Layers {state(hass, lamp).name}"
    reg_entry = er.async_get(hass).async_get(SENSOR_A)
    assert reg_entry is not None
    assert reg_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert state(hass, "sensor.layers_status").state == "ok"


async def test_the_state_is_the_top_layer_and_the_stack_is_listed_top_first(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A, B])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off", owner="tv_dim")
    await settle(hass)
    assert state(hass, SENSOR_A).state == "tv"
    assert state(hass, SENSOR_B).state == "base"

    await layers(hass, "set", entity_id=A, layer="washer", priority=70, state="on",
                 brightness_pct=100, ttl="04:00:00", strength="sticky")
    await settle(hass)
    st = state(hass, SENSOR_A)
    assert st.state == "washer"
    top, below = st.attributes["layers"]
    assert (top["id"], top["priority"], top["strength"]) == ("washer", 70, "sticky")
    assert top["command"]["state"] == "on" and top["command"]["brightness"] == 255
    assert isinstance(top["expires_at"], str)
    assert (below["id"], below["priority"], below["owner"]) == ("tv", 40, "tv_dim")
    assert below["expires_at"] is None
    assert st.attributes["effective"]["state"] == "on"
    assert st.attributes["base"]["state"] == "on"
    assert st.attributes["pending"] is False

    await layers(hass, "clear", layer="washer")
    await settle(hass)
    assert state(hass, SENSOR_A).state == "tv"
    await layers(hass, "clear", layer="tv")
    await settle(hass)
    st = state(hass, SENSOR_A)
    assert st.state == "base"
    assert st.attributes["layers"] == []


async def test_only_the_state_is_recorded(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    ours = set(state(hass, SENSOR_A).attributes) - {"friendly_name", "icon"}
    # entity_id is small and never changes; everything else stays out of the recorder.
    assert ours - StackSensor._unrecorded_attributes == {"entity_id"}


async def test_a_switch_gets_one_too(hass: HomeAssistant, switches: dict[str, FakeSwitch]) -> None:
    await start(hass, ["switch.relay"])
    await layers(hass, "set", entity_id="switch.relay", layer="away", priority=30, state="off")
    await settle(hass)
    st = state(hass, "sensor.layers_relay")
    assert st.state == "away"
    assert st.attributes["entity_id"] == "switch.relay"


async def test_a_lamp_taken_out_of_layers_loses_its_sensor(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    engine = await start(hass, [A, B])
    entry = engine.entry
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_ENTITIES: [A]})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get(SENSOR_B) is None
    assert hass.states.get(SENSOR_B) is None
    assert registry.async_get(SENSOR_A) is not None
    # Until the startup grace ends, the reloaded engine has not read the lamp's base yet.
    assert state(hass, SENSOR_A).state == "none"
    entry.runtime_data.engine._end_grace()
    await hass.async_block_till_done()
    assert state(hass, SENSOR_A).state == "base"


async def test_the_sensor_follows_a_renamed_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    registry = er.async_get(hass)
    before = registry.async_get(SENSOR_A)
    assert before is not None

    registry.async_update_entity(A, new_entity_id="light.lamp_renamed")
    await settle(hass)

    sensors = [e for e in er.async_entries_for_config_entry(registry, before.config_entry_id)
               if e.unique_id == before.unique_id]
    assert [e.entity_id for e in sensors] == [SENSOR_A]    # the same sensor, not a second one
    st = state(hass, SENSOR_A)
    assert st.attributes["entity_id"] == "light.lamp_renamed"
    assert st.state == "tv"


# --------------------------------------------------------------------------- thermostats

ZONE = "climate.zone_a"
SENSOR_ZONE = "sensor.layers_climate_zone_a"


async def test_a_thermostat_gets_one_with_its_layers(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    await tc.start(hass, freezer, [ZONE])
    st = state(hass, SENSOR_ZONE)
    assert st.state == "base"
    assert st.attributes["entity_id"] == ZONE
    assert st.attributes["base"]["temperature"] == 21.0

    await tc.layers(hass, "set", entity_id=ZONE, layer="away", priority=50, temperature=17,
                    owner="presence")
    await tc.layers(hass, "set", entity_id=ZONE, layer="window", priority=90, hvac_mode="off",
                    strength="locked")
    await settle(hass)
    st = state(hass, SENSOR_ZONE)
    assert st.state == "window"
    top, below = st.attributes["layers"]
    assert (top["id"], top["priority"], top["strength"]) == ("window", 90, "locked")
    assert (below["id"], below["owner"], below["command"]["temperature"]) == ("away", "presence", 17)
    assert st.attributes["effective"]["hvac_mode"] == "off"

    await tc.layers(hass, "clear", layer="window")
    await settle(hass)
    assert state(hass, SENSOR_ZONE).state == "away"
    ours = set(state(hass, SENSOR_ZONE).attributes) - {"friendly_name", "icon"}
    assert ours - StackSensor._unrecorded_attributes == {"entity_id"}


async def test_watch_only_shows_what_a_thermostat_would_be_sent(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    await tc.start(hass, freezer, [ZONE], apply_climate=False)
    await tc.layers(hass, "set", entity_id=ZONE, layer="away", priority=50, temperature=17)
    await settle(hass)
    st = state(hass, SENSOR_ZONE)
    assert st.state == "away"
    assert st.attributes["would_send"]
    assert tc.setpoint(hass, ZONE) == ("heat", 21.0)      # nothing was sent


async def test_a_lamp_and_a_thermostat_with_one_name_get_separate_sensors(
    hass: HomeAssistant, lights: dict[str, FakeLamp], zones: dict[str, FakeThermostat],
    freezer: Any,
) -> None:
    zones["same"] = FakeThermostat("lamp_a", temperature=20.0)
    setup_test_component_platform(hass, "climate", list(zones.values()))
    assert await async_setup_component(hass, "climate", {"climate": [{"platform": "test"}]})
    await hass.async_block_till_done()
    await tc.start(hass, freezer, [A, "climate.lamp_a"])
    assert state(hass, SENSOR_A).attributes["entity_id"] == A
    assert state(hass, "sensor.layers_climate_lamp_a").attributes["entity_id"] == "climate.lamp_a"
