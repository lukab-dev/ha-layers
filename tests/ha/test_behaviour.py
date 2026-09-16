"""Behaviour scenarios: what Layers does when people, automations, devices and
vendor bridges act on the lamps it manages (docs/SPEC.md sections 5 to 7).

Every test runs on a ticking frozen clock: time passes normally (renders sleep
for real, briefly), and ``advance`` jumps it forward so the engine's timers
(debounce, return settle, replay quiet, late windows) fire as they would.

Who acted is told apart the way Home Assistant tells it apart:

- a person: a service call whose context carries a real ``user_id``;
- an automation: a service call whose context carries a ``parent_id``;
- a device: a state written with a fresh context and neither (``FakeLamp.push``).

What Layers itself sent is found through its ``layers_render`` events: each
render attempt fires one with the context it then calls the lamp with.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from freezegun.api import TickingDateTimeFactory
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    LightEntityFeature,
)
from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from custom_components.layers import logbook as layers_logbook
from custom_components.layers.const import (
    DOMAIN,
    EVENT_EXTERNAL,
    EVENT_RENDER,
    EVENT_RENDER_FAILED,
)
from custom_components.layers.engine import Engine
from custom_components.layers.logic.model import (
    DIV_DELIVERY,
    DIV_MANUAL_KEEP,
    DIV_UNSYNCED,
    OFF_COMMAND,
    Color,
    Command,
)

from custom_components.layers import render as render_module
from custom_components.layers.logic import classify as classify_module

from .conftest import FakeLamp, settle, setup_layers

pytestmark = pytest.mark.freeze_time(tick=True)

A, B, C = "light.lamp_a", "light.lamp_b", "light.lamp_c"
GROUP_AB = "light.group_ab"
STATUS = "sensor.layers_status"
WARM = Color.kelvin(2700)       # what the fake colour-temperature lamps start in
SIGNAL_XY = [0.679, 0.318]      # a saturated red that only survives as xy on some lamps


# --------------------------------------------------------------------------- helpers


async def start(hass: HomeAssistant, entities: list[str], **options: Any) -> Engine:
    """Set Layers up on ``entities`` with the startup grace already over.

    ``setup_layers`` ends the grace by hand but leaves the real grace timer
    running, which would end it a second time 5 s later and re-record every
    lamp; in production it ends once. Cancel the leftover timer.
    """
    entry = await setup_layers(hass, entities, **options)
    engine: Engine = entry.runtime_data.engine
    engine._cancel("grace", "")  # noqa: SLF001
    return engine


async def advance(hass: HomeAssistant, freezer: TickingDateTimeFactory, seconds: float) -> None:
    """Move the clock forward; timers that come due run, and renders they start finish."""
    freezer.tick(timedelta(seconds=seconds))
    for _ in range(3):
        await asyncio.sleep(0)
    await settle(hass, 0.05)


async def layers(
    hass: HomeAssistant, service: str, context: Context | None = None, **data: Any
) -> dict[str, str]:
    """Call a layers.* service and return its per-lamp result."""
    response = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, context=context, return_response=True
    )
    await hass.async_block_till_done()
    return response["entities"]


async def light(hass: HomeAssistant, service: str, entity_id: str, context: Context,
                **data: Any) -> None:
    """Someone other than Layers calls light.<service>."""
    await hass.services.async_call(
        "light", service, {"entity_id": entity_id, **data}, blocking=True, context=context
    )
    await hass.async_block_till_done()


def automation() -> Context:
    """The context an automation's or script's service call carries."""
    return Context(parent_id=Context().id)


def state(hass: HomeAssistant, entity_id: str) -> State:
    st = hass.states.get(entity_id)
    assert st is not None
    return st


def brightness(hass: HomeAssistant, entity_id: str) -> int | None:
    return state(hass, entity_id).attributes.get(ATTR_BRIGHTNESS)


def sent_by_layers(lamp: FakeLamp, renders: list[Event]) -> list[tuple[str, dict[str, Any]]]:
    """The commands the lamp received under a context one of Layers' renders used."""
    ours = {event.context.id for event in renders}
    return [(service, kwargs) for service, kwargs, ctx in lamp.calls
            if ctx is not None and ctx.id in ours]


def kinds(engine: Engine, entity_id: str) -> list[str]:
    """The classifier decisions recorded for one lamp, oldest first."""
    return [d["kind"] for d in engine.decisions if d["entity_id"] == entity_id]


def describers(hass: HomeAssistant) -> dict[str, Any]:
    """The logbook describe callbacks Layers registers, by event type."""
    found: dict[str, Any] = {}

    @callback
    def _register(domain: str, event_type: str, describe: Any) -> None:
        assert domain == DOMAIN
        found[event_type] = describe

    layers_logbook.async_describe_events(hass, _register)
    return found


@pytest.fixture
def renders(hass: HomeAssistant) -> list[Event]:
    """Every layers_render event: one per render attempt, fired with its context."""
    return async_capture_events(hass, EVENT_RENDER)


@pytest.fixture
def externals(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_EXTERNAL)


@pytest.fixture
def person(hass_admin_user: MockUser) -> Context:
    """A fresh context per call is what the app gives; tests call ``Context(user_id=...)``."""
    return Context(user_id=hass_admin_user.id)


# --------------------------------------------------------------------------- take-back


async def test_take_back_by_a_person_from_the_app(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], hass_admin_user: MockUser, freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30,
                        owner="movie") == {A: "queued"}
    await settle(hass)
    assert brightness(hass, A) == 30

    await light(hass, "turn_on", A, Context(user_id=hass_admin_user.id), brightness=230)
    await settle(hass)

    rec = engine.records[A]
    assert rec.layers == {}
    assert set(rec.tombstones) == {"hold"}
    assert rec.base == Command("on", 230, WARM)   # only what the call named; the colour stays
    assert rec.base_source == "user"
    assert rec.last_external.source == "user"
    assert rec.last_external.user_id == hass_admin_user.id
    assert rec.diverged is None
    assert [e.data for e in externals] == [
        {"entity_id": A, "source": "user", "policy": "take_back", "dropped": ["hold"],
         "edited": None}
    ]
    # Layers never answers a person: nothing more is sent, now or later.
    await advance(hass, freezer, 60)
    assert brightness(hass, A) == 230
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_take_back_by_an_automation_turning_the_lamp_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event],
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)

    await light(hass, "turn_off", A, automation())
    await settle(hass)

    rec = engine.records[A]
    assert state(hass, A).state == "off"
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert rec.base == OFF_COMMAND
    assert rec.base_source == "automation"
    assert [e.data["source"] for e in externals] == ["automation"]
    # The owner's clear lifts the tombstone and restores the base, which is now off.
    assert await layers(hass, "clear", entity_id=A, layer="hold") == {A: "unchanged"}
    await settle(hass)
    assert state(hass, A).state == "off"
    assert rec.tombstones == {}
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_take_back_through_a_light_group_reaches_every_member(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    engine = await start(hass, [A, B])
    await layers(hass, "set", entity_id=[A, B], layer="hold", priority=40, brightness=40)
    await settle(hass)

    await light(hass, "turn_on", GROUP_AB, automation(), brightness=220)
    await settle(hass)

    for eid in (A, B):
        rec = engine.records[eid]
        assert rec.layers == {} and set(rec.tombstones) == {"hold"}
        assert rec.base.brightness == 220
        assert brightness(hass, eid) == 220
    assert len(sent_by_layers(lights["a"], renders)) == 1
    assert len(sent_by_layers(lights["b"], renders)) == 1


async def test_take_back_by_a_device_is_judged_only_after_the_debounce(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    rec = engine.records[A]

    lights["a"].push(brightness=180)        # a dimmer bound to the lamp: no context
    await hass.async_block_till_done()
    assert "hold" in rec.layers and rec.tombstones == {}
    await advance(hass, freezer, 2.5)
    assert "hold" in rec.layers, "judged before the 3 s debounce ran out"

    await advance(hass, freezer, 1)
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert rec.base == Command("on", 180, WARM)
    assert rec.base_source == "device"
    assert [e.data["source"] for e in externals] == ["device"]
    assert brightness(hass, A) == 180
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_a_device_turning_the_lamp_off_after_the_late_window_is_taken_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    await advance(hass, freezer, 20)        # past the 15 s late window of our command

    lights["a"].push(is_on=False)           # the wall switch
    await hass.async_block_till_done()
    assert kinds(engine, A)[-1] == "debounce"
    await advance(hass, freezer, 3.5)

    rec = engine.records[A]
    assert rec.base == OFF_COMMAND
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert state(hass, A).state == "off"
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_a_no_context_blip_that_comes_back_within_the_debounce_is_ignored(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    rec = engine.records[A]
    base = rec.base

    lights["a"].push(brightness=180)
    await advance(hass, freezer, 1)
    lights["a"].push(brightness=30)         # back where it was
    await advance(hass, freezer, 3.5)

    assert "hold" in rec.layers and rec.tombstones == {}
    assert rec.base == base
    assert externals == []
    assert kinds(engine, A)[-1] == "rereport"

    # An on/off resync blip (off and straight back on), well after our command.
    await advance(hass, freezer, 20)
    lights["a"].push(is_on=False)
    await hass.async_block_till_done()
    lights["a"].push(is_on=True)
    await advance(hass, freezer, 3.5)
    assert "hold" in rec.layers and rec.tombstones == {}
    assert externals == []
    assert len(sent_by_layers(lights["a"], renders)) == 1


# --------------------------------------------------------------------------- other policies


async def test_edit_active_puts_a_persons_change_into_the_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], person: Context,
) -> None:
    engine = await start(hass, [A], edit_active=[A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)

    await light(hass, "turn_on", A, person, brightness=230)
    await settle(hass)

    rec = engine.records[A]
    layer = rec.layers["hold"]
    assert layer.command == Command("on", 230)
    assert layer.requested == Command("on", 30)
    assert rec.tombstones == {}
    assert rec.base == Command("on", 102, WARM)
    assert [e.data for e in externals] == [
        {"entity_id": A, "source": "user", "policy": "edit_active", "dropped": [],
         "edited": "hold"}
    ]
    # The owner re-sending what it asked for only refreshes: the person's edit stays.
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        brightness=30) == {A: "unchanged"}
    await settle(hass)
    assert brightness(hass, A) == 230
    # Clearing falls through to the base, as for any layer.
    assert await layers(hass, "clear", entity_id=A, layer="hold") == {A: "queued"}
    await settle(hass)
    assert brightness(hass, A) == 102
    assert len(sent_by_layers(lights["a"], renders)) == 2


async def test_base_keep_layers_keeps_the_persons_change_until_sync(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    person: Context, freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A], base_keep=[A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    assert state(hass, A).state == "off"

    await light(hass, "turn_on", A, person, brightness=230)
    await settle(hass)

    rec = engine.records[A]
    assert set(rec.layers) == {"tv"} and rec.tombstones == {}
    assert rec.base == Command("on", 230, WARM)
    assert rec.diverged == DIV_MANUAL_KEEP
    # No snap-back: the lamp keeps showing the person's change.
    await advance(hass, freezer, 60)
    assert state(hass, A).state == "on" and brightness(hass, A) == 230
    assert len(sent_by_layers(lights["a"], renders)) == 1

    # sync puts the layers back on the lamp...
    assert await layers(hass, "sync", entity_id=A) == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "off"
    assert rec.diverged is None
    # ...and the clear falls through to the base the person set.
    assert await layers(hass, "clear", entity_id=A, layer="tv") == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "on" and brightness(hass, A) == 230


async def test_base_keep_lamp_showing_its_command_after_a_clear_is_not_diverged(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
) -> None:
    engine = await start(hass, [A], base_keep=[A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=230)
    await settle(hass)
    rec = engine.records[A]
    assert rec.diverged == DIV_MANUAL_KEEP

    # The owner clears without a sync: the base is what the lamp already shows.
    assert await layers(hass, "clear", entity_id=A, layer="tv") == {A: "in_sync"}
    await settle(hass)
    assert rec.layers == {}
    assert state(hass, A).state == "on" and brightness(hass, A) == 230
    # SPEC 2: diverged says why the lamp does not show its effective command. It does.
    assert rec.diverged is None


# --------------------------------------------------------------------------- tombstones


async def test_a_tombstone_skips_the_owners_set_on_that_lamp_until_it_clears(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
) -> None:
    engine = await start(hass, [A, B])
    await layers(hass, "set", entity_id=[A, B], layer="hold", priority=40, brightness=30)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=230)
    await settle(hass)

    # Only the lamp the person took back skips the owner.
    assert await layers(hass, "set", entity_id=[A, B], layer="hold", priority=40,
                        brightness=30) == {A: "skipped_tombstoned", B: "unchanged"}
    await settle(hass)
    assert brightness(hass, A) == 230
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        brightness=10) == {A: "skipped_tombstoned"}
    await settle(hass)
    assert brightness(hass, A) == 230
    assert len(sent_by_layers(lights["a"], renders)) == 1

    assert await layers(hass, "clear", entity_id=A, layer="hold") == {A: "unchanged"}
    assert engine.records[A].tombstones == {}
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        brightness=30) == {A: "queued"}
    await settle(hass)
    assert brightness(hass, A) == 30


async def test_resume_after_manual_lifts_the_tombstone_once_the_lamp_is_seen_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    rec = engine.records[A]
    night = {"entity_id": A, "layer": "night", "priority": 50, "brightness": 13,
             "color_temp_kelvin": 2202, "resume_after_manual": True}
    await layers(hass, "set", **night)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=200)
    await settle(hass)
    assert rec.tombstones["night"].lift_when_off
    assert await layers(hass, "set", **night) == {A: "skipped_tombstoned"}

    await advance(hass, freezer, 20)        # past the late window of the person's call
    lights["a"].push(is_on=False)           # turned off at the wall
    await advance(hass, freezer, 3.5)
    assert rec.tombstones == {}
    assert rec.base == OFF_COMMAND

    assert await layers(hass, "set", **night) == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "on" and brightness(hass, A) == 13


async def test_resume_after_manual_lifts_when_a_person_turns_the_lamp_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
) -> None:
    engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", entity_id=A, layer="night", priority=50, brightness=13,
                 resume_after_manual=True)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=200)
    await settle(hass)
    assert set(rec.tombstones) == {"night"}

    await light(hass, "turn_off", A, person)
    await settle(hass)
    assert rec.tombstones == {}


async def test_a_plain_tombstone_does_not_lift_when_the_lamp_goes_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
) -> None:
    engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=13)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=200)
    await light(hass, "turn_off", A, person)
    await settle(hass)
    assert set(rec.tombstones) == {"hold"}
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        brightness=13) == {A: "skipped_tombstoned"}


# --------------------------------------------------------------------------- intent path


async def test_an_off_on_a_lamp_a_layer_holds_off_becomes_the_base(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event],
) -> None:
    """The rocker's Off while the TV layer holds the lamp off must not relight it later."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    assert state(hass, A).state == "off"

    await light(hass, "turn_off", A, automation())   # no state change: it is already off
    await settle(hass)

    rec = engine.records[A]
    assert "external_intent" in kinds(engine, A)
    assert rec.base == OFF_COMMAND
    assert rec.layers == {} and set(rec.tombstones) == {"tv"}
    assert [e.data["dropped"] for e in externals] == [["tv"]]

    assert await layers(hass, "clear", entity_id=A, layer="tv") == {A: "unchanged"}
    await settle(hass)
    assert state(hass, A).state == "off"
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_a_persons_off_through_a_group_on_lamps_held_off_is_kept(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
) -> None:
    engine = await start(hass, [A, B])
    await layers(hass, "set", entity_id=[A, B], layer="tv", priority=40, state="off")
    await settle(hass)

    await light(hass, "turn_off", GROUP_AB, person)
    await settle(hass)
    for eid in (A, B):
        assert engine.records[eid].base == OFF_COMMAND

    assert await layers(hass, "clear", layer="tv") == {A: "unchanged", B: "unchanged"}
    await settle(hass)
    assert state(hass, A).state == "off" and state(hass, B).state == "off"
    assert len(sent_by_layers(lights["a"], renders)) == 1
    assert len(sent_by_layers(lights["b"], renders)) == 1


async def test_a_turn_on_to_what_the_lamp_already_shows_is_a_take_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)

    await light(hass, "turn_on", A, person, brightness=30)
    await settle(hass)

    rec = engine.records[A]
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert rec.base == Command("on", 30, WARM)


# --------------------------------------------------------------------------- unavailable lamps


async def test_an_off_aimed_at_an_unavailable_lamp_is_delivered_when_it_returns_untouched(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    lamp = lights["a"]
    lamp.set_available(False)
    await hass.async_block_till_done()

    calls_before = len(lamp.calls)
    await light(hass, "turn_off", A, automation())
    await settle(hass)
    assert len(lamp.calls) == calls_before, "Home Assistant should drop the unavailable lamp"

    rec = engine.records[A]
    assert rec.base == OFF_COMMAND
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert rec.owed is not None and rec.owed.missed and rec.owed.target == OFF_COMMAND
    assert state(hass, STATUS).state == "pending"

    lamp.set_available(True)                 # back, still showing what it showed
    await hass.async_block_till_done()
    assert state(hass, A).state == "on"
    await advance(hass, freezer, 5.5)

    assert state(hass, A).state == "off"
    assert sent_by_layers(lamp, renders)[-1][0] == "turn_off"
    assert rec.owed is None
    assert state(hass, STATUS).state == "ok"


async def test_an_off_aimed_at_an_unavailable_lamp_is_dropped_if_it_returns_changed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    lamp = lights["a"]
    lamp.set_available(False)
    await light(hass, "turn_off", A, automation())
    await settle(hass)

    lamp._attr_brightness = 255              # power-cycled at the wall: back at full
    lamp.set_available(True)
    await advance(hass, freezer, 5.5)

    rec = engine.records[A]
    assert state(hass, A).state == "on" and brightness(hass, A) == 255
    assert rec.base == Command("on", 255, WARM)
    assert rec.owed is None
    assert len(sent_by_layers(lamp, renders)) == 1


async def test_a_set_on_an_unavailable_lamp_is_delivered_when_it_returns_untouched(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [C])
    lamp = lights["c"]
    lamp.set_available(False)
    await hass.async_block_till_done()

    assert await layers(hass, "set", entity_id=C, layer="hold", priority=40,
                        brightness=150) == {C: "pending"}
    await settle(hass)
    assert lamp.calls == []
    status = state(hass, STATUS)
    assert status.state == "pending" and C in status.attributes["pending_since"]

    lamp.set_available(True)                 # back off, as it went away
    await hass.async_block_till_done()
    assert state(hass, C).state == "off", "delivered before the return settled"
    await advance(hass, freezer, 5.5)

    assert state(hass, C).state == "on" and brightness(hass, C) == 150
    assert engine.records[C].owed is None
    assert "hold" in engine.records[C].layers
    assert state(hass, STATUS).state == "ok"


async def test_a_set_on_an_unavailable_lamp_is_not_delivered_if_it_returns_changed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [C])
    lamp = lights["c"]
    lamp.set_available(False)
    await hass.async_block_till_done()
    assert await layers(hass, "set", entity_id=C, layer="hold", priority=40,
                        brightness=150) == {C: "pending"}

    # Someone used it at the switch while it was away: it comes back on, at full.
    lamp._attr_is_on = True
    lamp._attr_brightness = 255
    lamp.set_available(True)
    await advance(hass, freezer, 5.5)

    rec = engine.records[C]
    assert state(hass, C).state == "on" and brightness(hass, C) == 255
    assert lamp.calls == []
    assert rec.owed is None
    assert rec.base == Command("on", 255, WARM)
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert [e.data["source"] for e in externals] == ["device"]
    assert state(hass, STATUS).state == "ok"


# --------------------------------------------------------------------------- late reversals


async def test_a_bridge_reverting_our_on_is_retried_never_taken_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [C])
    engine._platforms[C] = "hue"            # noqa: SLF001 — a 60 s late window
    lamp = lights["c"]
    lamp.revert_after = 25                  # measured: 18-38 s after an ON, no context

    await layers(hass, "set", entity_id=C, layer="hold", priority=40, brightness=150)
    await settle(hass)
    assert state(hass, C).state == "on"

    await advance(hass, freezer, 25.5)      # the bridge flips it back off
    await settle(hass)

    rec = engine.records[C]
    assert "failed_delivery" in kinds(engine, C)
    assert state(hass, C).state == "on" and brightness(hass, C) == 150
    assert "hold" in rec.layers and rec.tombstones == {}
    assert rec.last_external is None and externals == []
    assert [s for s, _ in sent_by_layers(lamp, renders)] == ["turn_on", "turn_on"]


async def test_the_same_reversal_on_a_lamp_without_a_long_late_window_is_a_device_change(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [C])
    lamp = lights["c"]
    lamp.revert_after = 25                  # past the 15 s default window

    await layers(hass, "set", entity_id=C, layer="hold", priority=40, brightness=150)
    await settle(hass)
    await advance(hass, freezer, 25.5)
    await advance(hass, freezer, 3.5)

    rec = engine.records[C]
    assert state(hass, C).state == "off"
    assert rec.base == OFF_COMMAND
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert len(sent_by_layers(lamp, renders)) == 1


async def test_a_no_context_flip_soon_after_our_command_is_retried_on_any_platform(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [C])
    await layers(hass, "set", entity_id=C, layer="hold", priority=40, brightness=150)
    await settle(hass)
    await advance(hass, freezer, 8)         # past HA's 5 s context reuse, inside 15 s

    lights["c"].push(is_on=False)
    await settle(hass)

    # The retry's own report ("ours") is recorded after the failed delivery.
    assert "failed_delivery" in kinds(engine, C)
    assert state(hass, C).state == "on"
    assert "hold" in engine.records[C].layers
    assert len(sent_by_layers(lights["c"], renders)) == 2


async def test_a_bridge_reverting_a_foreign_off_marks_delivery_and_the_next_clear_repairs(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    engine._platforms[A] = "hue"            # noqa: SLF001
    lamp = lights["a"]
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, mode="adjust",
                 brightness=50)
    await settle(hass)
    assert brightness(hass, A) == 50

    lamp.revert_after = 10                  # measured: ~10 s after an OFF
    await light(hass, "turn_off", A, automation())
    await settle(hass)
    rec = engine.records[A]
    assert rec.base == OFF_COMMAND and set(rec.tombstones) == {"tv"}

    await advance(hass, freezer, 10.5)      # the bridge turns it back on, no context
    await settle(hass)
    assert "failed_delivery" in kinds(engine, A)
    assert state(hass, A).state == "on"
    assert rec.diverged == DIV_DELIVERY
    assert rec.base == OFF_COMMAND          # the base keeps what was asked
    assert len(sent_by_layers(lamp, renders)) == 1, "a foreign command is never re-sent"

    # The film ends: the owner's clear targets the lamp and repairs it.
    assert await layers(hass, "clear", entity_id=A, layer="tv") == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "off"
    assert rec.diverged is None
    assert rec.tombstones == {}


# --------------------------------------------------------------------------- failed renders


async def test_a_lamp_that_ignores_commands_fails_loudly_then_recovers(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    engine = await start(hass, [A])
    failures = async_capture_events(hass, EVENT_RENDER_FAILED)
    issues = ir.async_get(hass)
    lamp = lights["a"]
    lamp.ignore_commands = True

    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        state="off") == {A: "queued"}
    await settle(hass, 0.5)

    rec = engine.records[A]
    assert [s for s, _, _ in lamp.calls] == ["turn_off"] * 7      # the first try and 6 retries
    assert [e.data["attempt"] for e in renders] == [1, 2, 3, 4, 5, 6, 7]
    assert rec.diverged == DIV_DELIVERY
    assert rec.owed is None
    assert [e.data for e in failures] == [{"entity_id": A, "layer": "hold", "attempts": 7}]
    assert issues.async_get_issue(DOMAIN, f"render_failed_{A}") is not None
    status = state(hass, STATUS)
    assert status.state == "failed" and status.attributes["failed"] == [A]
    assert describers(hass)[EVENT_RENDER_FAILED](failures[0]) == {
        LOGBOOK_ENTRY_NAME: "Layers",
        LOGBOOK_ENTRY_MESSAGE: "could not set it after 7 attempts",
        LOGBOOK_ENTRY_ENTITY_ID: A,
    }

    # The lamp works again; the owner's next set on it repairs the delivery.
    lamp.ignore_commands = False
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        state="off") == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "off"
    assert rec.diverged is None
    assert issues.async_get_issue(DOMAIN, f"render_failed_{A}") is None
    assert state(hass, STATUS).state == "ok"
    assert len(failures) == 1


# --------------------------------------------------------------------------- what is sent


async def test_xy_goes_to_an_xy_lamp_exactly_as_written(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    engine = await start(hass, [A])
    lamp = lights["a"]
    lamp.desaturate_hs = True               # this lamp would ruin an hs colour

    await layers(hass, "set", entity_id=A, layer="signal", priority=70,
                 xy_color=SIGNAL_XY, brightness=255)
    await settle(hass)

    ((service, kwargs),) = sent_by_layers(lamp, renders)
    assert service == "turn_on"
    assert tuple(kwargs[ATTR_XY_COLOR]) == tuple(SIGNAL_XY)
    assert ATTR_HS_COLOR not in kwargs
    st = state(hass, A)
    assert st.attributes["color_mode"] == "xy"
    assert tuple(st.attributes[ATTR_XY_COLOR]) == tuple(SIGNAL_XY)
    assert engine.records[A].diverged is None


async def test_an_adjust_layer_over_an_off_lamp_leaves_it_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    engine = await start(hass, [C])
    assert await layers(hass, "set", entity_id=C, layer="dim", priority=20, mode="adjust",
                        brightness_pct=25) == {C: "unchanged"}
    await settle(hass)
    assert state(hass, C).state == "off"
    assert lights["c"].calls == []
    assert engine.describe([C])[C]["active"] == "base"

    # Once something below turns the lamp on, the adjust layer applies.
    assert await layers(hass, "set", entity_id=C, layer="base", state="on") == {C: "queued"}
    await settle(hass)
    assert state(hass, C).state == "on" and brightness(hass, C) == 64
    assert engine.describe([C])[C]["active"] == "dim"


async def test_transition_is_sent_only_to_lamps_that_support_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    lights["b"].push(supported_features=LightEntityFeature(0))    # lamp_b: no transition
    engine = await start(hass, [A, B])
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)

    await layers(hass, "set", entity_id=[A, B], layer="dim", priority=30, brightness=20,
                 transition=0.05)
    await settle(hass)

    sent = {e.data["service_data"]["entity_id"]: e.data["service_data"]
            for e in calls if e.data["domain"] == "light"}
    assert sent[A][ATTR_TRANSITION] == 0.05
    assert ATTR_TRANSITION not in sent[B]
    assert brightness(hass, A) == 20 and brightness(hass, B) == 20

    # A Matter lamp's turn_off ignores transition: it is not sent there either.
    engine._platforms[A] = "matter"         # noqa: SLF001
    calls.clear()
    await layers(hass, "set", entity_id=A, layer="off", priority=60, state="off",
                 transition=0.05)
    await settle(hass)
    (off,) = [e.data["service_data"] for e in calls if e.data["domain"] == "light"]
    assert e_service(calls) == ["turn_off"]
    assert ATTR_TRANSITION not in off


def e_service(calls: list[Event]) -> list[str]:
    return [e.data["service"] for e in calls if e.data["domain"] == "light"]


# --------------------------------------------------------------------------- replays


async def test_a_retry_loop_replaying_an_old_press_updates_the_base_and_keeps_the_layers(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    rec = engine.records[A]
    press = automation()                    # one press, re-sent under the same context

    await light(hass, "turn_on", A, press, brightness=180)
    await settle(hass)
    assert rec.base == Command("on", 180, WARM)
    await advance(hass, freezer, 1)

    assert await layers(hass, "set", entity_id=A, layer="tv", priority=40,
                        state="off") == {A: "queued"}
    await settle(hass)
    assert state(hass, A).state == "off"

    await advance(hass, freezer, 2)         # the loop re-sends its press (+3 s)
    await light(hass, "turn_on", A, press, brightness=180)
    await settle(hass)
    assert state(hass, A).state == "on"
    assert set(rec.layers) == {"tv"} and rec.tombstones == {}
    assert rec.base == Command("on", 180, WARM)
    assert rec.last_external.policy == "replay"
    assert externals == []

    await advance(hass, freezer, 4)         # and again (+7 s): the lamp already shows it
    await light(hass, "turn_on", A, press, brightness=180)
    await settle(hass)
    assert set(rec.layers) == {"tv"}

    await advance(hass, freezer, 18)        # quiet for 18 s: not yet
    assert state(hass, A).state == "on"
    await advance(hass, freezer, 3)         # 21 s after the last replay: layers back
    await settle(hass)
    assert state(hass, A).state == "off"
    assert set(rec.layers) == {"tv"}
    assert [s for s, _ in sent_by_layers(lights["a"], renders)] == ["turn_off", "turn_off"]
    assert renders[-1].data["reason"] == "replay"


async def test_a_persons_change_during_the_replay_quiet_period_cancels_the_rerender(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    press = automation()
    await light(hass, "turn_on", A, press, brightness=180)
    await advance(hass, freezer, 1)
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    await advance(hass, freezer, 2)
    await light(hass, "turn_on", A, press, brightness=180)      # replayed
    await settle(hass)

    await light(hass, "turn_on", A, person, brightness=90)
    await settle(hass)
    await advance(hass, freezer, 30)

    assert brightness(hass, A) == 90
    assert engine.records[A].layers == {}
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_a_persons_off_on_the_intent_path_cancels_a_pending_replay_rerender(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A], base_keep=[A])
    rec = engine.records[A]
    press = automation()
    await light(hass, "turn_off", A, press)                     # a loop's Off
    await advance(hass, freezer, 1)
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=200)
    await settle(hass)
    assert state(hass, A).state == "on"
    await advance(hass, freezer, 2)
    await light(hass, "turn_off", A, press)                     # the loop re-sends it
    await settle(hass)
    assert state(hass, A).state == "off" and rec.last_external.policy == "replay"

    # A person presses Off on the lamp that is already off: the intent path.
    await light(hass, "turn_off", A, person)
    await settle(hass)
    assert rec.diverged == DIV_MANUAL_KEEP

    await advance(hass, freezer, 25)
    # base_keep_layers: the lamp keeps showing the person's change until layers.sync.
    assert state(hass, A).state == "off"
    assert len(sent_by_layers(lights["a"], renders)) == 1


# --------------------------------------------------------------------------- ours vs theirs


async def test_a_persons_change_while_our_render_is_in_flight_cancels_it_and_wins(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    lamp = lights["a"]
    # A 5 s transition keeps the render waiting before it verifies.
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30,
                 transition=5)
    await asyncio.sleep(0.05)
    assert engine.renderer.alive(A)
    assert lamp.calls[-1][1][ATTR_TRANSITION] == 5

    await light(hass, "turn_on", A, person, brightness=230)
    assert not engine.renderer.alive(A)

    await advance(hass, freezer, 40)
    assert brightness(hass, A) == 230
    assert engine.records[A].layers == {}
    assert len(sent_by_layers(lamp, renders)) == 1


async def test_our_own_writes_and_their_reused_context_are_never_taken_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    externals: list[Event], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    lamp = lights["a"]
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    ours = state(hass, A).context
    assert ours.id == renders[-1].context.id
    assert kinds(engine, A) == ["ours"]

    # The lamp re-reports a moment later; Home Assistant stamps it with our context.
    lamp._attr_brightness = 32
    lamp.async_write_ha_state()
    await hass.async_block_till_done()
    assert state(hass, A).context.id == ours.id
    assert kinds(engine, A) == ["ours", "ours"]

    await advance(hass, freezer, 60)
    rec = engine.records[A]
    assert "hold" in rec.layers and rec.tombstones == {}
    assert externals == []
    assert len(sent_by_layers(lamp, renders)) == 1


async def test_a_strangers_write_inside_the_context_reuse_is_judged_as_no_context(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """HA stamps any write within 5 s of our call with our context. One that contradicts
    our command is someone else's: debounced like a device change, never retried."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    ours = state(hass, A).context

    lamp._attr_is_on = False                # the wall switch, reported inside the 5 s
    lamp.async_write_ha_state()
    await hass.async_block_till_done()
    assert state(hass, A).context.id == ours.id
    assert kinds(engine, A)[-1] == "debounce"

    await advance(hass, freezer, 3.5)
    rec = engine.records[A]
    assert rec.base == OFF_COMMAND
    assert rec.layers == {} and set(rec.tombstones) == {"hold"}
    assert state(hass, A).state == "off"
    assert len(sent_by_layers(lamp, renders)) == 1


# --------------------------------------------------------------------------- events and logbook


async def test_layers_render_carries_the_render_context_and_reads_well_in_the_logbook(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    await start(hass, [A])
    order: list[tuple[str, str]] = []

    @callback
    def _seen(event: Event) -> None:
        order.append((event.event_type, event.context.id))

    hass.bus.async_listen(EVENT_RENDER, _seen)
    hass.bus.async_listen(EVENT_CALL_SERVICE, _seen)

    caller = Context()
    await layers(hass, "set", caller, entity_id=A, layer="hold", priority=40, brightness=30,
                 owner="movie")
    await settle(hass)

    (render,) = renders
    assert render.data == {"entity_id": A, "layer": "hold", "owner": "movie", "reason": "set",
                           "service": "turn_on", "attempt": 1}
    assert render.context.parent_id == caller.id
    assert state(hass, A).context.id == render.context.id
    # Fired before the light call under the same context, so the logbook attributes the
    # lamp's change to it.
    assert [kind for kind, ctx in order if ctx == render.context.id] == [
        EVENT_RENDER, EVENT_CALL_SERVICE
    ]
    assert describers(hass)[EVENT_RENDER](render) == {
        LOGBOOK_ENTRY_NAME: "Layers",
        LOGBOOK_ENTRY_MESSAGE: "hold (movie): set",
        LOGBOOK_ENTRY_ENTITY_ID: A,
    }


async def test_layers_external_describes_who_took_the_lamp_and_what_was_dropped(
    hass: HomeAssistant, lights: dict[str, FakeLamp], externals: list[Event], person: Context,
) -> None:
    await start(hass, [A, B], edit_active=[B])
    await layers(hass, "set", entity_id=[A, B], layer="hold", priority=40, brightness=30)
    await settle(hass)

    await light(hass, "turn_on", A, person, brightness=230)
    await light(hass, "turn_on", B, person, brightness=230)
    await settle(hass)

    describe = describers(hass)[EVENT_EXTERNAL]
    assert [describe(e) for e in externals] == [
        {LOGBOOK_ENTRY_NAME: "Layers", LOGBOOK_ENTRY_MESSAGE: "changed by user: dropped hold",
         LOGBOOK_ENTRY_ENTITY_ID: A},
        {LOGBOOK_ENTRY_NAME: "Layers",
         LOGBOOK_ENTRY_MESSAGE: "changed by user: dropped nothing, edited hold",
         LOGBOOK_ENTRY_ENTITY_ID: B},
    ]


# --------------------------------------------------------------------------- review 2026-09-15


async def test_a_bare_turn_on_learns_the_lamps_brightness_so_a_later_clear_restores_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    """Apple Home "on" / Assist "turn on X" send a bare turn_on. The base must learn what
    the lamp came on at, or a later adjust + clear leaves the lamp at 25 %."""
    engine = await start(hass, [C])                     # C starts off
    await light(hass, "turn_on", C, automation())       # bare: comes on at its last 128
    await settle(hass)
    assert engine.records[C].base == Command("on", 128, Color.kelvin(2700))

    await layers(hass, "set", entity_id=C, layer="tv", priority=40, mode="adjust", brightness=32)
    await settle(hass)
    assert brightness(hass, C) == 32
    await layers(hass, "clear", layer="tv")
    await settle(hass)
    assert brightness(hass, C) == 128
    assert sent_by_layers(lights["c"], renders)[-1] == ("turn_on", {"brightness": 128,
                                                                     "color_temp_kelvin": 2700})


async def test_a_dimmer_during_our_render_cancels_it_instead_of_being_fought(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person at a Hue dimmer while our render is still verifying: their change is
    taken, the render is cancelled, and nothing is re-sent over them."""
    monkeypatch.setattr(render_module, "SETTLE_S", 0.3)
    # The ramp grace (a lamp echoing its old level inside RAMP_GRACE_S of our command
    # is noise) is tested in tests/logic; here the person moves the dimmer "later".
    monkeypatch.setattr(classify_module, "RAMP_GRACE_S", 0.0)
    engine = await start(hass, [A])
    lamp = lights["a"]
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, mode="adjust", brightness=64)
    await hass.async_block_till_done()
    await asyncio.sleep(0.05)                           # sent, now inside the settle wait
    assert engine.renderer.alive(A)
    lamp.push(brightness=200, context=Context())        # no user, no parent: a dimmer
    await settle(hass, 0.6)
    rec = engine.records[A]
    assert kinds(engine, A)[-1] == "external"
    assert rec.layers == {} and set(rec.tombstones) == {"tv"}
    assert rec.base == Command("on", 200, Color.kelvin(2700))
    assert brightness(hass, A) == 200
    assert len(sent_by_layers(lamp, renders)) == 1     # no retry over the person


async def test_a_missed_command_in_shadow_mode_is_not_owed_forever(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Apply off: a foreign off aimed at an away lamp is recorded and owed; on its
    return the deferred render is skipped, and the debt must go with it (7.5), or the
    status sensor reads pending until a sync."""
    engine = await start(hass, [A], apply=False)
    lamp = lights["a"]
    lamp.set_available(False)
    await hass.async_block_till_done()
    await light(hass, "turn_off", A, automation())
    await settle(hass)
    rec = engine.records[A]
    assert rec.owed is not None and rec.owed.missed
    lamp.set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    assert rec.owed is None
    assert rec.diverged == DIV_UNSYNCED
    assert state(hass, STATUS).attributes["pending_since"] == {}
    assert lamp.calls == []


async def test_an_entity_removed_mid_render_keeps_the_command_owed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lamp's integration reloads between a failed verification and the retry: the
    state is gone. Its caps read as empty then; the render must not misread its own
    brightness dropping out of the projection as a changed command and drop the debt."""
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (0.4, 0.4, 0.4, 0.4, 0.4, 0.4))
    engine = await start(hass, [A])
    lamp = lights["a"]
    lamp.ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, brightness=64)
    await asyncio.sleep(0.15)                           # first attempt verified: not taken
    assert engine.renderer.alive(A)
    hass.states.async_remove(A)                         # gone, inside the backoff
    await hass.async_block_till_done()
    await settle(hass, 0.6)
    rec = engine.records[A]
    assert not engine.renderer.alive(A)
    assert rec.owed is not None and rec.owed.target == Command("on", 64, WARM)
    assert rec.available is False
