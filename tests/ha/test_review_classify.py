"""Regression tests: what a change on a lamp is taken to mean (SPEC 5.3, 6, 7.1).

Each test names the review finding it pins down. Ticking clock and helpers from
test_behaviour.py. ``room_x`` is a vendor room light (members lamp_a and lamp_c,
flagged ``is_hue_group``): the bridge fans a room call out itself, and the members
then report with no context.
"""

from __future__ import annotations


import pytest

from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from custom_components.layers.const import EVENT_EXTERNAL, EVENT_RENDER
from custom_components.layers.logic.model import DIV_PARTIAL, OFF_COMMAND, Command

from .conftest import FakeLamp, settle
from .test_behaviour import (
    A,
    B,
    C,
    WARM,
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

D = "light.lamp_d"
ROOM = "light.room_x"
RED = [0.6, 0.35]


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


def xy(hass: HomeAssistant, entity_id: str) -> tuple[float, float] | None:
    value = state(hass, entity_id).attributes.get("xy_color")
    return tuple(value) if value else None


# --------------------------------------------------------------------------- R1-2


async def test_a_persons_on_switched_off_at_the_wall_is_never_relit_by_a_later_call(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 1 #2: a person turns a dark lamp on from the app, then off at the wall 2 s
    later (no context, inside the late window). That is not a failed delivery to repair:
    the next layers.* call on the lamp would re-send their ON and light a dark room."""
    engine = await start(hass, [C])
    await light(hass, "turn_on", C, person, brightness=200)
    await settle(hass)
    await advance(hass, freezer, 2)
    lights["c"].push(is_on=False)
    await hass.async_block_till_done()
    assert "failed_delivery" not in kinds(engine, C)
    await advance(hass, freezer, 3.5)
    rec = engine.records[C]
    assert rec.base == OFF_COMMAND and rec.diverged is None

    assert await layers(hass, "set", entity_id=C, layer="tv", priority=40, mode="adjust",
                        brightness_pct=25) == {C: "unchanged"}
    assert await layers(hass, "clear", entity_id=C, layer="nightlight") == {C: "unchanged"}
    await settle(hass)
    assert state(hass, C).state == "off"
    assert sent_by_layers(lights["c"], renders) == []


# --------------------------------------------------------------------------- R1-5 / R3-1


@pytest.mark.parametrize("policy", ["take_back", "base_keep"])
async def test_our_render_after_a_persons_change_is_not_a_tail_of_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], person: Context,
    freezer: TickingDateTimeFactory, policy: str,
) -> None:
    """Review 1 #5: a person sets 200; the owner then sets a signal (rendered at 50). A
    no-context report of our 50 within 10 s of the person is ours landing, not a
    follow-up that re-applies their take-back to the signal (and tombstones it)."""
    engine = await start(hass, [A], **({"base_keep": [A]} if policy == "base_keep" else {}))
    await light(hass, "turn_on", A, person, brightness=200)
    await settle(hass)
    await advance(hass, freezer, 1)
    await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=50,
                 xy_color=RED)
    await settle(hass)
    await advance(hass, freezer, 5.5)                # past HA's context reuse
    lights["a"].push(brightness=52)                  # the bridge's own report: no context
    await hass.async_block_till_done()
    assert kinds(engine, A)[-1] == "debounce"
    await advance(hass, freezer, 3.5)
    rec = engine.records[A]
    assert "sig" in rec.layers and rec.tombstones == {}
    assert rec.base.brightness == 200 and rec.base.color == WARM
    assert await layers(hass, "clear", entity_id=A, layer="sig") == {A: "queued"}
    await settle(hass)
    assert brightness(hass, A) == 200 and xy(hass, A) != tuple(RED)


async def test_a_nightlight_renewed_after_a_house_wide_off_still_goes_off_at_lease_end(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #1 A: an Off to all lights takes the nightlight back (its tombstone lifts:
    resume_after_manual, and the lamp is off). The nightlight sets again 2 s later. The
    lamp's no-context re-report of our 5 % is not a tail of the Off: if it were, the
    nightlight would become the base and never go off."""
    engine = await start(hass, [D])
    night = {"entity_id": D, "layer": "night", "priority": 50, "brightness": 13,
             "color_temp_kelvin": 2202, "ttl": 600, "resume_after_manual": True}
    await layers(hass, "set", **night)
    await settle(hass)
    await hass.services.async_call("light", "turn_off", {"entity_id": "all"}, blocking=True,
                                   context=automation())
    await settle(hass)
    rec = engine.records[D]
    assert rec.layers == {} and rec.tombstones == {}
    await advance(hass, freezer, 2)
    assert await layers(hass, "set", **night) == {D: "queued"}
    await settle(hass)
    await advance(hass, freezer, 4)
    lights["d"].push(brightness=14)                  # no context, 6 s after the Off
    await hass.async_block_till_done()
    assert "follow_up" not in kinds(engine, D)
    await advance(hass, freezer, 3.5)
    assert "night" in rec.layers and rec.base == OFF_COMMAND
    await advance(hass, freezer, 600)
    assert state(hass, D).state == "off"


async def test_a_room_off_is_not_blamed_for_our_nightlight_coming_back_on(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #1 B: the same through a vendor room: the room's Off, then our renewed
    nightlight on the member, then the bridge's no-context report of our "on" 7 s after
    the room call. Our render supersedes the room call, and an "on" never is its Off."""
    engine = await start(hass, [C])
    night = {"entity_id": C, "layer": "night", "priority": 50, "brightness": 13, "ttl": 600,
             "resume_after_manual": True}
    await layers(hass, "set", **night)
    await settle(hass)
    await hass.services.async_call("light", "turn_off", {"entity_id": ROOM}, blocking=True,
                                   context=automation())
    lights["c"].push(is_on=False)                    # the bridge fans the room out
    await hass.async_block_till_done()
    rec = engine.records[C]
    assert rec.layers == {} and rec.base == OFF_COMMAND
    await advance(hass, freezer, 2)
    assert await layers(hass, "set", **night) == {C: "queued"}
    await settle(hass)
    await advance(hass, freezer, 5)
    lights["c"].push(brightness=14)
    await hass.async_block_till_done()
    await advance(hass, freezer, 3.5)
    assert "night" in rec.layers and rec.base == OFF_COMMAND
    await advance(hass, freezer, 600)
    assert state(hass, C).state == "off"


async def test_a_recent_room_call_is_not_blamed_for_our_own_renders_report(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 4 #2: a person sets the room to 200; half a second later the TV dims the
    member. The bridge's no-context report of OUR 65 (after HA's 5 s context reuse, still
    within the room call's 10 s memory) must not become the member's base."""
    import asyncio
    from datetime import timedelta

    engine = await start(hass, [A])
    rec = engine.records[A]
    await hass.services.async_call("light", "turn_on", {"entity_id": ROOM, "brightness": 200},
                                   blocking=True, context=person)
    lights["a"].push(brightness=200)
    await hass.async_block_till_done()
    assert rec.base.brightness == 200
    await advance(hass, freezer, 0.5)
    assert await layers(hass, "set", entity_id=A, layer="tv", priority=40, brightness=64,
                        transition=10) == {A: "queued"}
    freezer.tick(timedelta(seconds=5.5))
    await asyncio.sleep(0)
    await hass.async_block_till_done()
    lights["a"].push(brightness=65)
    await hass.async_block_till_done()
    assert rec.base.brightness == 200 and "tv" in rec.layers
    await advance(hass, freezer, 30)
    assert await layers(hass, "clear", entity_id=A, layer="tv") == {A: "queued"}
    await settle(hass)
    assert brightness(hass, A) == 200


# --------------------------------------------------------------------------- R2-6


async def test_a_call_home_assistant_refuses_changes_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    hass_read_only_user: MockUser, freezer: TickingDateTimeFactory,
) -> None:
    """Review 2 #6: the call event fires before the light service checks permissions.
    A read-only user's Off to an away lamp must neither drop its layer nor be owed."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=255)
    await settle(hass)
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    with pytest.raises(Unauthorized):
        await hass.services.async_call("light", "turn_off", {"entity_id": A}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    await settle(hass)
    rec = engine.records[A]
    assert "sig" in rec.layers and rec.tombstones == {} and rec.owed is None
    lights["a"].set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    assert state(hass, A).state == "on" and brightness(hass, A) == 255
    assert [s for s, _ in sent_by_layers(lights["a"], renders)] == ["turn_on"]


# --------------------------------------------------------------------------- R2-7


async def test_an_off_through_an_old_style_group_is_seen(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    """Review 2 #7: the light service expands group.* entities; so does Layers now."""
    await hass.services.async_call("group", "set", {"object_id": "den", "entities": [A]},
                                   blocking=True)
    await hass.async_block_till_done()
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="sig", priority=70, brightness=255)
    await settle(hass)
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    await hass.services.async_call("light", "turn_off", {"entity_id": "group.den"},
                                   blocking=True, context=automation())
    await settle(hass)
    rec = engine.records[A]
    assert "sig" in rec.tombstones and rec.base == OFF_COMMAND
    assert rec.owed is not None and rec.owed.missed and rec.owed.target == OFF_COMMAND


async def test_an_off_to_all_lights_in_capitals_is_seen(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    """Review 2 #7: HA reads entity_id "ALL" as all; lamp_c is in no light group that
    would re-send the call per member and hide the difference."""
    engine = await start(hass, [C])
    await layers(hass, "set", entity_id=C, layer="tv", priority=40, state="off")
    await hass.services.async_call("light", "turn_off", {"entity_id": "ALL"}, blocking=True,
                                   context=automation())
    await settle(hass)
    rec = engine.records[C]
    assert rec.base == OFF_COMMAND and "tv" in rec.tombstones
    assert await layers(hass, "clear", entity_id=C, layer="tv") == {C: "unchanged"}


# --------------------------------------------------------------------------- R2-8


async def test_a_scripts_next_step_under_the_same_context_is_not_a_replay(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 2 #8: every action of one script run shares a context. A later step with
    other data is a new change, not a retry loop replaying the first one."""
    engine = await start(hass, [A])
    run = automation()
    await light(hass, "turn_on", A, run, brightness=60)
    await advance(hass, freezer, 1)
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, brightness=20)
    await settle(hass)
    await advance(hass, freezer, 15)
    await light(hass, "turn_on", A, run, brightness=200)      # the script's next step
    await settle(hass)
    rec = engine.records[A]
    assert rec.last_external.policy == "take_back" and "tv" in rec.tombstones
    await advance(hass, freezer, 25)
    assert brightness(hass, A) == 200
    assert len(sent_by_layers(lights["a"], renders)) == 1


# --------------------------------------------------------------------------- R3-2


async def test_turning_off_a_lamp_our_command_only_dimmed_is_not_a_reversal(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #2 A: our dim of a lamp that was on (on -> on at 64). An Off at a Hue
    dimmer 20 s later is a person's: the bridge undoing our dim would leave it on."""
    engine = await start(hass, [A])
    engine._platforms[A] = "hue"                     # noqa: SLF001
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, mode="adjust",
                 brightness_pct=25)
    await settle(hass)
    await advance(hass, freezer, 20)
    lights["a"].push(is_on=False)
    await hass.async_block_till_done()
    assert kinds(engine, A)[-1] == "debounce"
    await advance(hass, freezer, 3.5)
    rec = engine.records[A]
    assert rec.base == OFF_COMMAND and "tv" in rec.tombstones
    await advance(hass, freezer, 60)
    assert state(hass, A).state == "off"
    assert len(sent_by_layers(lights["a"], renders)) == 1


async def test_turning_on_a_lamp_a_foreign_off_did_not_turn_off_is_a_change(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #2 B: a house-wide Off reaches a Hue lamp the TV already holds off (it
    changes nothing). A person turning it on 20 s later is no failed delivery of that
    Off, and the film's end must not turn it off again."""
    engine = await start(hass, [B])
    engine._platforms[B] = "hue"                     # noqa: SLF001
    await layers(hass, "set", entity_id=B, layer="tv", priority=40, state="off")
    await settle(hass)
    await advance(hass, freezer, 120)                # mid-film: our Off is long verified
    await light(hass, "turn_off", B, automation())
    await settle(hass)
    await advance(hass, freezer, 20)
    lights["b"].push(is_on=True, brightness=150)
    await hass.async_block_till_done()
    assert "failed_delivery" not in kinds(engine, B)
    await advance(hass, freezer, 3.5)
    rec = engine.records[B]
    assert rec.base == Command("on", 150) and rec.diverged is None
    assert await layers(hass, "clear", entity_id=B, layer="tv") == {B: "unchanged"}
    await settle(hass)
    assert state(hass, B).state == "on" and brightness(hass, B) == 150


# --------------------------------------------------------------------------- R3-3


async def test_a_change_during_a_return_settle_does_not_leave_a_stale_drop_snapshot(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #3: a lamp comes back and an automation turns it off within the 5 s
    settle. The snapshot of its first absence must go with the cancelled return, or a
    later absence is judged against it and an owed Off is dropped."""
    engine = await start(hass, [D])
    lamp = lights["d"]
    lamp.push(is_on=True, brightness=200)
    await advance(hass, freezer, 3.5)                # a device change: base on 200
    lamp.set_available(False)
    lamp.set_available(True)
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    await light(hass, "turn_off", D, automation())   # inside the settle
    await settle(hass)
    rec = engine.records[D]
    assert rec.p_at_drop is None
    await advance(hass, freezer, 600)

    await layers(hass, "set", entity_id=D, layer="night", priority=50, brightness=13)
    await settle(hass)
    lamp.set_available(False)
    await hass.async_block_till_done()
    assert await layers(hass, "clear", entity_id=D, layer="night") == {D: "pending"}
    lamp.set_available(True)                         # back untouched, at 13
    await hass.async_block_till_done()
    await advance(hass, freezer, 5.5)
    assert state(hass, D).state == "off" and rec.owed is None


# --------------------------------------------------------------------------- R3-5 / R3-9


async def test_a_tail_of_a_brightness_only_change_keeps_the_signal_colour_out_of_the_base(
    hass: HomeAssistant, lights: dict[str, FakeLamp], person: Context,
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #5: a person sets only the brightness of the red trash lamp (a partial
    take-back). A no-context tail must take the same groups, not the red."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="trash", priority=70, brightness=255,
                 xy_color=RED)
    await settle(hass)
    await light(hass, "turn_on", A, person, brightness=128)
    await settle(hass)
    rec = engine.records[A]
    assert rec.base == Command("on", 128, WARM) and rec.diverged == DIV_PARTIAL
    await advance(hass, freezer, 6)
    lights["a"].push(brightness=127)                 # the lamp settles: no context
    await hass.async_block_till_done()
    assert kinds(engine, A)[-1] == "follow_up"
    assert rec.base == Command("on", 127, WARM) and rec.diverged == DIV_PARTIAL
    assert await layers(hass, "clear", entity_id=A, layer="trash") == {A: "queued"}
    await settle(hass)
    assert state(hass, A).attributes["color_mode"] == "color_temp"
    assert brightness(hass, A) == 127


async def test_a_dimmer_on_the_signal_colour_takes_only_the_brightness(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #9: a person dims the red trash lamp at a Hue dimmer (no context). The
    base learns the brightness only; the next clear repairs the colour."""
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="trash", priority=70, brightness=255,
                 xy_color=RED)
    await settle(hass)
    await advance(hass, freezer, 20)
    lights["a"].push(brightness=128)
    await advance(hass, freezer, 3.5)
    rec = engine.records[A]
    assert rec.base == Command("on", 128, WARM) and rec.diverged == DIV_PARTIAL
    assert await layers(hass, "clear", entity_id=A, layer="trash") == {A: "queued"}
    await settle(hass)
    assert state(hass, A).attributes["color_mode"] == "color_temp"
    assert brightness(hass, A) == 128


# --------------------------------------------------------------------------- R3-6


async def test_a_no_op_call_does_not_hide_a_bridge_reverting_our_restore(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """Review 3 #6: the film ends, our restore turns a Hue lamp on; an automation then
    sends it the brightness it already shows; the bridge reverts our ON 35 s after it.
    That is still our reverted restore: retried."""
    engine = await start(hass, [B])
    engine._platforms[B] = "hue"                     # noqa: SLF001
    await layers(hass, "set", entity_id=B, layer="tv", priority=40, state="off")
    await settle(hass)
    await advance(hass, freezer, 60)
    assert await layers(hass, "clear", entity_id=B, layer="tv") == {B: "queued"}
    await settle(hass)
    assert state(hass, B).state == "on"
    await advance(hass, freezer, 10)
    await light(hass, "turn_on", B, automation(), brightness=200)   # a no-op
    await settle(hass)
    await advance(hass, freezer, 25)
    lights["b"].push(is_on=False)                    # the bridge reverts, no context
    await settle(hass)
    assert "failed_delivery" in kinds(engine, B)
    assert state(hass, B).state == "on" and brightness(hass, B) == 200
    assert [s for s, _ in sent_by_layers(lights["b"], renders)] == ["turn_off", "turn_on", "turn_on"]


# --------------------------------------------------------------------------- R3-10


async def test_edit_active_lamps_stay_off_after_a_house_wide_off_and_the_clear(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event], externals: list[Event],
) -> None:
    """Review 3 #10: edit_active puts a person's change into the active layer. An Off must
    also reach the base, or the owner's clear relights the lamps."""
    engine = await start(hass, [A, B], edit_active=[A, B])
    await layers(hass, "set", entity_id=[A, B], layer="tv", priority=40, brightness=30)
    await settle(hass)
    await hass.services.async_call("light", "turn_off", {"entity_id": "all"}, blocking=True,
                                   context=automation())
    await settle(hass)
    for eid in (A, B):
        rec = engine.records[eid]
        assert rec.layers["tv"].command == OFF_COMMAND and rec.base == OFF_COMMAND
    assert {e.data["edited"] for e in externals} == {"tv"}
    assert await layers(hass, "clear", layer="tv") == {A: "unchanged", B: "unchanged"}
    await settle(hass)
    assert state(hass, A).state == "off" and state(hass, B).state == "off"
