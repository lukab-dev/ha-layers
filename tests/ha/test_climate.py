"""Climate entities through Home Assistant's real climate domain (SPEC section 8).

A ``FakeThermostat`` rounds setpoints to 0.5 without saying so, can report late, and
can ignore commands. Who acted is told apart as for lamps: a person's call carries a
``user_id``, a device report (``push``) carries a fresh context with neither.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.layers.const import DOMAIN, EVENT_EXTERNAL, EVENT_RENDER_FAILED
from custom_components.layers.engine import Engine
from custom_components.layers.logic.model import RETURN_SETTLE_S

from .conftest import FakeLamp, FakeThermostat, settle, setup_layers

pytestmark = pytest.mark.freeze_time(tick=True)

LIVING, OFFICE = "climate.zone_a", "climate.zone_b"
LAMP = "light.lamp_a"
STATUS = "sensor.layers_status"
WINDOW_S = 1.0          # the report-back window in these tests (seconds)
OPTIONS = {"climate_report_window": WINDOW_S, "climate_manual_priority": 80,
           "climate_manual_timeout": 180}


async def advance(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await settle(hass)


async def start(hass: HomeAssistant, freezer: Any, entities: list[str], *, apply: bool = True,
                **extra: Any) -> tuple[MockConfigEntry, Engine]:
    entry = await setup_layers(hass, entities, end_grace=False, apply=apply,
                               extra={**OPTIONS, **extra})
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    engine: Engine = entry.runtime_data.engine
    assert not engine.in_grace
    return entry, engine


async def layers(hass: HomeAssistant, service: str, context: Context | None = None,
                 **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                              return_response=True, context=context)
    await hass.async_block_till_done()
    return response


def setpoint(hass: HomeAssistant, eid: str = LIVING) -> tuple[str, float | None]:
    state = hass.states.get(eid)
    return state.state, state.attributes.get("temperature")


def sent(zone: FakeThermostat) -> list[tuple[str, dict[str, Any]]]:
    return [(service, data) for service, data, _ctx in zone.calls]


# --------------------------------------------------------------------------- set, clear, get


async def test_a_layer_on_a_thermostat_and_back(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    result = await layers(hass, "set", entity_id=LIVING, layer="night", priority=30,
                          temperature=18)
    assert result["entities"] == {LIVING: "queued"}
    await settle(hass)
    # Already heating: only the setpoint is sent.
    assert sent(zone) == [("set_temperature", {"temperature": 18.0})]
    assert setpoint(hass) == ("heat", 18.0)
    rec = engine.climate.records[LIVING]
    assert rec.owed is None and rec.diverged is None

    got = (await layers(hass, "get", entity_id=LIVING))["entities"][LIVING]
    assert got["domain"] == "climate" and got["active"] == "night"
    assert got["effective"] == {"hvac_mode": "heat", "temperature": 18.0}
    assert got["base"] == {"hvac_mode": "heat", "temperature": 21.0}

    result = await layers(hass, "clear", layer="night")
    assert result["entities"] == {LIVING: "queued"}
    await settle(hass)
    assert setpoint(hass) == ("heat", 21.0)


async def test_mode_preset_and_offset(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, _engine = await start(hass, freezer, [OFFICE])
    zone = thermostats["b"]
    await layers(hass, "set", entity_id=OFFICE, layer="base", hvac_mode="heat", temperature=20)
    await settle(hass)
    assert [s for s, _ in sent(zone)] == ["set_hvac_mode", "set_temperature"]
    assert setpoint(hass, OFFICE) == ("heat", 20.0)

    await layers(hass, "set", entity_id=OFFICE, layer="away", priority=40, mode="adjust",
                 offset=-2.5)
    await settle(hass)
    assert setpoint(hass, OFFICE) == ("heat", 17.5)

    await layers(hass, "set", entity_id=OFFICE, layer="eco", priority=50, preset_mode="eco")
    await settle(hass)
    assert hass.states.get(OFFICE).attributes["preset_mode"] == "eco"
    assert sent(zone)[-1] == ("set_preset_mode", {"preset_mode": "eco"})


async def test_a_setpoint_the_thermostat_rounds_counts_as_delivered(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    await layers(hass, "set", entity_id=LIVING, layer="boost", priority=60, temperature=21.3)
    await settle(hass)
    assert setpoint(hass) == ("heat", 21.5)
    assert len(thermostats["a"].calls) == 1, "a rounded readback was retried"
    rec = engine.climate.records[LIVING]
    assert rec.diverged is None and rec.owed is None
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"render_failed_{LIVING}")


async def test_off_wins_and_never_carries_a_setpoint(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    await start(hass, freezer, [LIVING])
    await layers(hass, "set", entity_id=LIVING, layer="window", priority=90, hvac_mode="off",
                 strength="locked")
    await settle(hass)
    assert sent(thermostats["a"]) == [("set_hvac_mode", {"hvac_mode": "off"})]
    assert hass.states.get(LIVING).state == "off"


# --------------------------------------------------------------------------- shadow


async def test_apply_off_records_the_commands_it_would_send(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [OFFICE], apply=False)
    result = await layers(hass, "set", entity_id=OFFICE, layer="morning", priority=20,
                          hvac_mode="heat", temperature=21)
    await settle(hass)
    assert result["entities"] == {OFFICE: "shadow"}
    assert thermostats["b"].calls == []
    assert hass.states.get(STATUS).state == "shadow"
    got = (await layers(hass, "get", entity_id=OFFICE))["entities"][OFFICE]
    assert got["diverged"] == "unsynced"
    assert got["would_send"] == [
        {"service": "climate.set_hvac_mode", "hvac_mode": "heat"},
        {"service": "climate.set_temperature", "temperature": 21.0},
    ]
    shadow = [d for d in engine.decisions if d["entity_id"] == OFFICE and d["kind"] == "shadow"]
    assert shadow and shadow[-1]["domain"] == "climate" and shadow[-1]["layer"] == "morning"
    assert shadow[-1]["calls"] == got["would_send"]

    # Apply on, then sync: now it is sent.
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.layers_apply"},
                                   blocking=True)
    await layers(hass, "sync", entity_id=OFFICE)
    await settle(hass)
    assert setpoint(hass, OFFICE) == ("heat", 21.0)


# --------------------------------------------------------------------------- person or slow device


async def test_a_late_report_with_no_context_is_the_device_not_a_person(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    """The command lands on the device, which reports it only after Home Assistant
    has stopped stamping writes with our context."""
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    zone.ignore_commands = True             # takes it, says nothing yet
    await layers(hass, "set", entity_id=LIVING, layer="night", priority=30, temperature=18)
    await asyncio.sleep(0.05)
    zone.push(target_temperature=18.0)      # the late report, no context
    await settle(hass)
    rec = engine.climate.records[LIVING]
    assert "manual" not in rec.layers
    assert rec.diverged is None and rec.owed is None
    assert len(zone.calls) == 1


async def test_a_change_after_the_window_is_a_persons_and_runs_out(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    events = async_capture_events(hass, EVENT_EXTERNAL)
    await layers(hass, "set", entity_id=LIVING, layer="holiday", priority=60, temperature=16)
    await settle(hass)
    await advance(hass, freezer, 10)        # past the window, and the device has arrived
    n = len(zone.calls)

    zone.push(target_temperature=22.5)      # someone at the dial
    await advance(hass, freezer, 3.5)       # the no-context debounce
    rec = engine.climate.records[LIVING]
    manual = rec.layers["manual"]
    assert manual.priority == 80 and manual.command.temperature == 22.5
    assert "holiday" in rec.layers, "a person's change outranks holiday; it does not end it"
    assert len(zone.calls) == n, "Layers sent something over the person"
    assert events[-1].data["policy"] == "manual_layer" and events[-1].data["edited"] == "manual"

    await advance(hass, freezer, 3 * 3600)  # three hours later
    assert "manual" not in rec.layers
    assert setpoint(hass) == ("heat", 16.0)


async def test_a_persons_call_is_recognised_at_once(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any,
    hass_admin_user: MockUser,
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    await layers(hass, "set", entity_id=LIVING, layer="away", priority=50, temperature=17)
    await settle(hass)
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": LIVING, "temperature": 23}, blocking=True,
                                   context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    rec = engine.climate.records[LIVING]
    assert rec.layers["manual"].command.temperature == 23
    assert rec.layers["manual"].owner == "user"
    assert setpoint(hass) == ("heat", 23.0)


async def test_an_open_window_holds_and_the_persons_setting_comes_back(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any,
    hass_admin_user: MockUser,
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    await layers(hass, "set", entity_id=LIVING, layer="window", priority=90, hvac_mode="off",
                 strength="locked")
    await settle(hass)
    assert hass.states.get(LIVING).state == "off"

    # Someone turns the heating up while the window is still open.
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": LIVING, "temperature": 23, "hvac_mode": "heat"},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    await advance(hass, freezer, 2.5)       # the hold re-shows the window layer
    rec = engine.climate.records[LIVING]
    assert hass.states.get(LIVING).state == "off"
    assert rec.layers["manual"].command.temperature == 23
    assert zone.calls[-1][0] == "set_hvac_mode"

    await layers(hass, "clear", layer="window")
    await settle(hass)
    assert setpoint(hass) == ("heat", 23.0)


# --------------------------------------------------------------------------- expiry


async def test_an_expiry_never_switches_the_heating_on(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    await layers(hass, "set", entity_id=LIVING, layer="window", priority=90, hvac_mode="off",
                 ttl=60)
    await settle(hass)
    n = len(zone.calls)
    await advance(hass, freezer, 61)
    rec = engine.climate.records[LIVING]
    assert rec.layers == {}
    assert rec.base.hvac_mode == "off" and rec.base_source == "expiry"
    assert hass.states.get(LIVING).state == "off"
    assert len(zone.calls) == n, "the expiry sent something"
    assert any(d["kind"] == "expiry" and d["domain"] == "climate" for d in engine.decisions)


async def test_an_expiry_brings_the_setpoint_back(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    await start(hass, freezer, [LIVING])
    await layers(hass, "set", entity_id=LIVING, layer="boost", priority=60, temperature=24,
                 ttl=60)
    await settle(hass)
    assert setpoint(hass) == ("heat", 24.0)
    await advance(hass, freezer, 61)
    assert setpoint(hass) == ("heat", 21.0)


# --------------------------------------------------------------------------- delivery


async def test_a_thermostat_that_never_reports_fails_loudly(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    zone.ignore_commands = True
    failed = async_capture_events(hass, EVENT_RENDER_FAILED)
    await layers(hass, "set", entity_id=LIVING, layer="night", priority=30, temperature=18)
    for _ in range(4):
        await settle(hass, 1.5)             # each attempt waits out the 1 s window
    assert len(zone.calls) == 3             # one try and two retries
    assert failed and failed[-1].data["entity_id"] == LIVING
    assert engine.climate.records[LIVING].diverged == "delivery"
    assert hass.states.get(STATUS).state == "failed"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"render_failed_{LIVING}")


async def test_an_unavailable_thermostat_is_owed_and_gets_it_on_return(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    zone.set_available(False)
    await hass.async_block_till_done()
    result = await layers(hass, "set", entity_id=LIVING, layer="night", priority=30,
                          temperature=18)
    assert result["entities"] == {LIVING: "pending"}
    assert engine.climate.records[LIVING].owed is not None
    zone.set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, RETURN_SETTLE_S + 0.5)
    assert setpoint(hass) == ("heat", 18.0)
    assert engine.climate.records[LIVING].owed is None


# --------------------------------------------------------------------------- services and lifecycle


async def test_lamps_and_thermostats_in_one_call(
    hass: HomeAssistant, lights: dict[str, FakeLamp], thermostats: dict[str, FakeThermostat],
    freezer: Any,
) -> None:
    _entry, engine = await start(hass, freezer, [LAMP, LIVING])
    result = await layers(hass, "set", entity_id=[LAMP, LIVING], layer="away", priority=40,
                          state="off", temperature=17)
    await settle(hass)
    assert result["entities"] == {LAMP: "queued", LIVING: "queued"}
    assert hass.states.get(LAMP).state == "off"
    assert setpoint(hass) == ("heat", 17.0)
    assert set(engine.describe()) == {LAMP, LIVING}
    status = hass.states.get(STATUS).attributes
    assert status["lamps"] == 1 and status["climate"] == 1
    assert status["layered"] == [LIVING, LAMP]


@pytest.mark.parametrize(("data", "reason"), [
    ({"state": "off"}, "climate entities"),
    ({"hvac_mode": "cool"}, "no hvac_mode 'cool'"),
    ({"preset_mode": "party"}, "no preset 'party'"),
    ({"temperature": 18, "priority": 80}, "manual priority"),
    ({"layer": "manual", "temperature": 18}, "'manual'"),
])
async def test_set_refuses_what_a_thermostat_cannot_take(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any,
    data: dict[str, Any], reason: str,
) -> None:
    await start(hass, freezer, [LIVING])
    call = {"entity_id": LIVING, "layer": "x", "priority": 40, **data}
    with pytest.raises(Exception) as err:
        await layers(hass, "set", **call)
    assert reason in str(err.value.translation_placeholders.get("reason"))


async def test_layers_survive_a_reload(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    entry, _engine = await start(hass, freezer, [LIVING])
    await layers(hass, "set", entity_id=LIVING, layer="night", priority=30, temperature=18)
    await settle(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    engine: Engine = entry.runtime_data.engine
    rec = engine.climate.records[LIVING]
    assert "night" in rec.layers and rec.layers["night"].command.temperature == 18


async def test_a_move_inside_the_window_is_retried_once(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    """Inside the report-back window, before the device has held the command for
    ARRIVED_HOLD_S, a no-context move is still the device: when the window ends and
    it does not show the command, the command is sent once more. (The price: a
    person this quick is sent over once; their next change is theirs.)"""
    _entry, engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    await layers(hass, "set", entity_id=LIVING, layer="night", priority=30, temperature=18)
    await settle(hass)
    assert len(zone.calls) == 1
    zone.push(target_temperature=21.0)      # the device flips back, no context
    await hass.async_block_till_done()
    rec = engine.climate.records[LIVING]
    assert "manual" not in rec.layers
    await advance(hass, freezer, WINDOW_S + 0.5)
    assert len(zone.calls) == 2
    assert setpoint(hass) == ("heat", 18.0)
    kinds = [d["kind"] for d in engine.decisions if d["entity_id"] == LIVING]
    assert "late" in kinds and "window_failed" in kinds
    assert "manual" not in rec.layers


async def test_a_reload_mid_command_does_not_mistake_its_report_for_a_person(
    hass: HomeAssistant, thermostats: dict[str, FakeThermostat], freezer: Any
) -> None:
    """The thermostat reports after the entry reloaded, under the context of a call
    the old engine made (which carries the caller's id as its parent)."""
    entry, _engine = await start(hass, freezer, [LIVING])
    zone = thermostats["a"]
    zone.report_after = 0.2
    await layers(hass, "set", entity_id=LIVING, layer="night", priority=30, temperature=18)
    await asyncio.sleep(0.05)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass, 0.5)
    rec = entry.runtime_data.engine.climate.records[LIVING]
    assert "manual" not in rec.layers
    assert "night" in rec.layers and setpoint(hass) == ("heat", 18.0)
