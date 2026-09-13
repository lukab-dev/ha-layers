"""Tests the completeness review asked for (review 4, items 4-15): behaviour that was
implemented but that no test would have noticed breaking.

Ticking clock and helpers from test_behaviour.py and test_lifecycle.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from freezegun.api import TickingDateTimeFactory
from homeassistant.components.light import ATTR_TRANSITION, ATTR_XY_COLOR
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from custom_components.layers import render as render_module
from custom_components.layers.const import DOMAIN, EVENT_EXTERNAL, EVENT_RENDER, EVENT_RENDER_FAILED
from custom_components.layers.logic.model import DIV_COLOUR, OFF_COMMAND, Owed, Record

from . import test_lifecycle as lc
from .conftest import FakeLamp, settle, setup_layers
from .test_behaviour import (
    A,
    C,
    advance,
    automation,
    brightness,
    kinds,
    layers,
    light,
    sent_by_layers,
    start,
    state,
)
from .test_config_services import FakeGroupLight

pytestmark = pytest.mark.freeze_time(tick=True)

ROOM = "light.room_x"
STATUS = "sensor.layers_status"


@pytest.fixture
def lamps(lamps: dict[str, FakeLamp]) -> dict[str, FakeLamp]:
    return {**lamps, "room": FakeGroupLight("room_x", {"entity_id": [A, C], "is_hue_group": True})}


@pytest.fixture
def renders(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_RENDER)


@pytest.fixture
def externals(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_EXTERNAL)


@pytest.fixture
def person(hass_admin_user: MockUser) -> Context:
    return Context(user_id=hass_admin_user.id)


# --------------------------------------------------------------------------- 4


async def test_a_persons_change_on_the_state_path_cancels_a_pending_replay_rerender(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #4: with base_keep_layers (the take_back version of this lands in sync
    anyway) a person's change after a replay must not be snapped back by the re-render."""
    engine = await start(hass, [A], base_keep=[A])
    rec = engine.records[A]
    press = automation()
    await light(hass, "turn_on", A, press, brightness=180)
    await advance(hass, freezer, 1)
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    await advance(hass, freezer, 2)
    await light(hass, "turn_on", A, press, brightness=180)     # replayed
    await settle(hass)
    assert rec.last_external.policy == "replay" and state(hass, A).state == "on"
    await light(hass, "turn_on", A, person, brightness=90)
    await settle(hass)
    assert rec.diverged == "manual_keep" and set(rec.layers) == {"tv"}
    assert not engine._pending("replay", A)          # noqa: SLF001 — superseded by the person
    await advance(hass, freezer, 25)
    assert state(hass, A).state == "on" and brightness(hass, A) == 90
    assert len(sent_by_layers(lights["a"], renders)) == 1


# --------------------------------------------------------------------------- 5


async def test_a_persons_off_on_the_intent_path_cancels_our_render_in_flight(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #5: the lamp is slow to take our ON; a person's Off (it still shows off:
    the intent path) takes it back, and our render must not turn it on afterwards."""
    engine = await start(hass, [C])
    lamp = lights["c"]
    lamp.ignore_commands = True
    await layers(hass, "set", entity_id=C, layer="hold", priority=40, brightness=150, transition=5)
    await asyncio.sleep(0.05)
    assert engine.renderer.alive(C)
    await light(hass, "turn_off", C, person)
    assert "external_intent" in kinds(engine, C)
    assert not engine.renderer.alive(C)
    assert engine.records[C].layers == {} and "hold" in engine.records[C].tombstones
    lamp.ignore_commands = False
    await advance(hass, freezer, 60)
    assert state(hass, C).state == "off"
    assert len(sent_by_layers(lamp, renders)) == 1


# --------------------------------------------------------------------------- 6


async def test_a_set_inside_a_device_debounce_judges_the_change_first(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #6: a dimmer change still debouncing when the owner sets another layer:
    the person's change is judged first (take-back), so the later clear restores it."""
    engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    await advance(hass, freezer, 20)
    lights["a"].push(brightness=180)
    await hass.async_block_till_done()
    assert engine._pending("debounce", A)            # noqa: SLF001
    await layers(hass, "set", entity_id=A, layer="signal", priority=70, brightness=255)
    await settle(hass)
    assert "hold" in rec.tombstones and rec.base.brightness == 180
    await layers(hass, "clear", entity_id=A, layer="signal")
    await settle(hass)
    assert brightness(hass, A) == 180


# --------------------------------------------------------------------------- 7


async def test_resync_blips_on_an_unsynced_lamp_keep_its_layers(
    hass: HomeAssistant, lights: dict[str, FakeLamp], externals: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #7: only the "close to observed_prev" rule keeps these: the lamp does not
    show its effective command (observe-only). Rollout phase 1 runs exactly like this."""
    engine = await start(hass, [A], apply=False)
    rec = engine.records[A]
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        state="off") == {A: "shadow"}
    lights["a"].push(is_on=False)                    # the 7 ms Hue resync
    await hass.async_block_till_done()
    lights["a"].push(is_on=True)
    await advance(hass, freezer, 3.5)
    assert set(rec.layers) == {"hold"} and rec.tombstones == {} and externals == []
    lights["a"].push(brightness=180)                 # a brightness blip that comes back
    await advance(hass, freezer, 1)
    lights["a"].push(brightness=102)
    await advance(hass, freezer, 3.5)
    assert set(rec.layers) == {"hold"} and rec.tombstones == {} and externals == []
    assert kinds(engine, A)[-1] == "rereport"


# --------------------------------------------------------------------------- 8


async def test_the_startup_drop_snapshot_does_not_outlive_the_grace(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: Any, hass_storage: dict[str, Any],
) -> None:
    """Review 4 #8: p_at_drop seeded at startup is cleared at the grace end, so a later
    absence is judged against what the lamp showed when it went away then."""
    now = lc.now_ts()
    lc.seed(hass_storage, {
        A: Record(A, base=lc.BASE_A,
                  layers={"hold": lc.make_layer("hold", 40, OFF_COMMAND, seq=1, set_at=now - 900)},
                  observed=lc.shows(hass, A), owed=Owed(now - 30, OFF_COMMAND, turns_on=False)),
    })
    entry = await setup_layers(hass, [A], apply=False, end_grace=False)
    engine = lc.engine_of(entry)
    await lc.advance(hass, freezer, 5.5)             # the grace ends: the owed off is delivered
    assert not engine.in_grace and hass.states.get(A).state == "off"
    assert engine.records[A].p_at_drop is None
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    assert (await lc.layers(hass, "clear", {"entity_id": A, "layer": "hold"}))["entities"] == {
        A: "pending"}
    lights["a"].set_available(True)                  # back, untouched: still off
    await hass.async_block_till_done()
    await lc.advance(hass, freezer, 5.5)
    lc.assert_shows_base_a(hass)


# --------------------------------------------------------------------------- 9


async def test_an_off_by_area_on_a_lamp_held_off_becomes_its_base(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    """Review 4 #9: foreign calls by area reach the intent path (a house-wide or wall-switch
    Off on lamps the layer already holds off)."""
    room = ar.async_get(hass).async_create("Test room")
    er.async_get(hass).async_update_entity(A, area_id=room.id)
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    await hass.services.async_call("light", "turn_off", {"area_id": room.id}, blocking=True,
                                   context=automation())
    await settle(hass)
    rec = engine.records[A]
    assert rec.base == OFF_COMMAND and "tv" in rec.tombstones
    assert await layers(hass, "clear", entity_id=A, layer="tv") == {A: "unchanged"}
    await settle(hass)
    assert state(hass, A).state == "off" and len(sent_by_layers(lights["a"], renders)) == 1


# --------------------------------------------------------------------------- 10


async def test_our_own_off_lifts_a_resume_after_manual_tombstone(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
) -> None:
    """Review 4 #10: lift_when_off lifts when the lamp is seen off, our own off included."""
    engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", entity_id=A, layer="night", priority=50, brightness=13,
                 resume_after_manual=True)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=200)
    await settle(hass)
    assert rec.tombstones["night"].lift_when_off
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, state="off")
    await settle(hass)
    assert state(hass, A).state == "off" and kinds(engine, A)[-1] == "ours"
    assert rec.tombstones == {}


async def test_a_lamp_back_off_from_an_absence_lifts_a_resume_after_manual_tombstone(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #10: seen off on its return (here it already shows the Off it was owed
    while away, so nothing is sent and nothing else would see it off)."""
    engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", entity_id=A, layer="night", priority=50, brightness=13,
                 resume_after_manual=True)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=200)
    await settle(hass)
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    await light(hass, "turn_off", A, automation())   # while it is away: owed
    assert rec.owed is not None and "night" in rec.tombstones
    lights["a"]._attr_is_on = False                  # it comes back off
    lights["a"].set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    assert [d["kind"] for d in engine.decisions if d["entity_id"] == A][-1] == "return:nothing"
    assert rec.tombstones == {}


# --------------------------------------------------------------------------- 11


async def test_a_lamp_that_drops_mid_render_is_owed_not_failed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #11: no failure alarm for a lamp that went away; its return delivers."""
    engine = await start(hass, [A])
    failures = async_capture_events(hass, EVENT_RENDER_FAILED)
    lamp = lights["a"]
    lamp.ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, state="off", transition=2)
    await asyncio.sleep(0.05)
    assert engine.renderer.alive(A)
    lamp.set_available(False)                        # it drops while we wait to verify
    await hass.async_block_till_done()
    await advance(hass, freezer, 3)
    rec = engine.records[A]
    assert not engine.renderer.alive(A)
    assert failures == [] and ir.async_get(hass).async_get_issue(DOMAIN, f"render_failed_{A}") is None
    assert rec.owed is not None and rec.diverged is None
    assert state(hass, STATUS).state == "pending"
    lamp.ignore_commands = False
    lamp.set_available(True)                         # back, untouched
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    assert state(hass, A).state == "off" and rec.owed is None


# --------------------------------------------------------------------------- 12


async def test_a_light_call_that_raises_is_verified_and_retried(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 4 #12: HomeAssistantError from the light call is swallowed; verification decides."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    real = lamp.async_turn_off
    seen = {"n": 0}

    async def flaky(**kwargs: Any) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            raise HomeAssistantError("bridge busy")
        await real(**kwargs)

    monkeypatch.setattr(lamp, "async_turn_off", flaky)
    assert await layers(hass, "set", entity_id=A, layer="hold", priority=40,
                        state="off") == {A: "queued"}
    await settle(hass, 0.3)
    assert state(hass, A).state == "off"
    assert engine.records[A].owed is None and engine.records[A].diverged is None
    assert [e.data["attempt"] for e in renders] == [1, 2]


# --------------------------------------------------------------------------- 13


async def test_a_brightness_step_is_taken_as_the_lamp_shows_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
) -> None:
    """Review 4 #13: an intent Layers cannot know: the state path takes what it shows."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=100)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness_step=50)
    await settle(hass)
    rec = engine.records[A]
    shown = brightness(hass, A)
    assert shown > 140
    assert rec.base.brightness == shown and "hold" in rec.tombstones and rec.diverged is None


# --------------------------------------------------------------------------- 14


async def test_an_attribute_tail_after_a_persons_change_is_folded_in(
    hass: HomeAssistant, lights: dict[str, FakeLamp], externals: list[Event], person: Context,
) -> None:
    """Review 4 #14: FOLLOW_UP re-applies the last external change to its tails at once."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=230)
    await settle(hass)
    lights["a"].push(brightness=240)                 # the lamp settles a little further
    await hass.async_block_till_done()
    rec = engine.records[A]
    assert kinds(engine, A)[-1] == "follow_up" and not engine._pending("debounce", A)  # noqa: SLF001
    assert rec.base.brightness == 240 and rec.last_external.source == "user"
    assert len(externals) == 1                       # the tail is no new take-back


async def test_a_room_call_attributes_member_changes_to_its_caller(
    hass: HomeAssistant, lights: dict[str, FakeLamp], externals: list[Event], person: Context,
) -> None:
    """Review 4 #14: members of a vendor room report with no context; the room call
    attributes them to its caller at once (no debounce)."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, brightness=30)
    await settle(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": ROOM, "brightness": 200},
                                   blocking=True, context=person)
    lights["a"].push(brightness=200)
    await hass.async_block_till_done()
    rec = engine.records[A]
    assert kinds(engine, A)[-1] == "external" and not engine._pending("debounce", A)  # noqa: SLF001
    assert rec.layers == {} and "hold" in rec.tombstones and rec.base.brightness == 200
    assert [e.data["source"] for e in externals] == ["user"]


# --------------------------------------------------------------------------- 15


async def test_the_late_recheck_resends_once_a_hue_lamp_reverted_silently(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 4 #15: a Hue render is re-checked at LATE_RECHECK_S; a lamp that shows
    something else by then (with no report to classify) gets the command once more."""
    engine = await start(hass, [A])
    engine._platforms[A] = "hue"                     # noqa: SLF001
    monkeypatch.setattr(render_module, "LATE_RECHECK_S", 5.0)
    await layers(hass, "set", entity_id=A, layer="dim", priority=40, brightness=30)
    await settle(hass)
    # The bridge's own state went back without a state_changed reaching Layers.
    engine.records[A].observed = lc.observed_from_state("on", {"brightness": 102}, lc.now_ts())
    lights["a"]._attr_brightness = 102
    await advance(hass, freezer, 6)
    assert "late_recheck_failed" in kinds(engine, A)
    assert brightness(hass, A) == 30
    assert [e.data["reason"] for e in renders] == ["set", "late_recheck"]


async def test_a_colour_the_lamp_will_not_take_is_accepted_after_one_retry_and_marked(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 4 #15: diverged = colour; the next targeted call repairs it."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    real = lamp.async_turn_on

    async def off_by_a_bit(**kwargs: Any) -> None:
        if ATTR_XY_COLOR in kwargs:
            x, y = kwargs[ATTR_XY_COLOR]
            kwargs = {**kwargs, ATTR_XY_COLOR: (x - 0.1, y)}
        await real(**kwargs)

    monkeypatch.setattr(lamp, "async_turn_on", off_by_a_bit)
    await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=255,
                 xy_color=[0.6, 0.35])
    await settle(hass)
    rec = engine.records[A]
    assert rec.diverged == DIV_COLOUR and rec.owed is None
    assert [e.data["attempt"] for e in renders] == [1, 2]
    monkeypatch.setattr(lamp, "async_turn_on", real)
    assert await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=255,
                        xy_color=[0.6, 0.35]) == {A: "queued"}   # unchanged, but repaired
    await settle(hass)
    assert rec.diverged is None


async def test_a_hue_off_is_verified_only_after_the_bridge_had_time_to_correct_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 4 #15: SLOW_OFF_S: Hue reports an optimistic off, corrected to on later."""
    engine = await start(hass, [A])
    engine._platforms[A] = "hue"                     # noqa: SLF001
    monkeypatch.setattr(render_module, "SLOW_OFF_S", 0.2)
    lamp = lights["a"]
    real = lamp.async_turn_off
    seen = {"n": 0}

    async def optimistic(**kwargs: Any) -> None:
        seen["n"] += 1
        await real(**kwargs)
        if seen["n"] == 1:                           # the bridge corrects it 0.1 s later
            def _back_on() -> None:
                lamp.push(is_on=True)
            lamp.hass.loop.call_later(0.1, _back_on)

    monkeypatch.setattr(lamp, "async_turn_off", optimistic)
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, state="off")
    await settle(hass, 1.0)
    assert [e.data["attempt"] for e in renders] == [1, 2]
    assert state(hass, A).state == "off"


async def test_a_retry_goes_straight_there_without_the_transition(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    await start(hass, [A])
    lamp = lights["a"]
    lamp.ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="dim", priority=40, brightness=30,
                 transition=0.02)
    await asyncio.sleep(0.1)
    lamp.ignore_commands = False
    await settle(hass, 0.3)
    assert lamp.calls[0][1][ATTR_TRANSITION] == 0.02
    assert all(ATTR_TRANSITION not in kwargs for _s, kwargs, _c in lamp.calls[1:])
    assert brightness(hass, A) == 30


async def test_status_failed_needs_the_lamp_still_diverged(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #15: a failed render whose lamp later shows its command is no failure."""
    engine = await start(hass, [A])
    lamp = lights["a"]
    lamp.ignore_commands = True
    await layers(hass, "set", entity_id=A, layer="hold", priority=40, state="off")
    await settle(hass, 0.5)
    assert state(hass, STATUS).state == "failed"
    await advance(hass, freezer, 30)
    lamp.push(is_on=False)                           # it catches up on its own
    await advance(hass, freezer, 3.5)
    assert kinds(engine, A)[-1] == "rereport"
    assert await layers(hass, "sync", entity_id=A) == {A: "in_sync"}
    assert engine.records[A].diverged is None
    assert state(hass, STATUS).state == "ok"
