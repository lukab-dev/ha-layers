"""Strength inside Home Assistant: sticky and locked layers against scenes, wall
buttons, the app and the lamp's own switch (SPEC 5.3 Strength, 6.5, 7.3 j)."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from custom_components.layers import engine as engine_module
from custom_components.layers.const import EVENT_EXTERNAL
from custom_components.layers.logic.model import DEBOUNCE_S, HOLD_SETTLE_S, OFF_COMMAND, Command

from .conftest import FakeLamp, settle
from .test_behaviour import (
    SIGNAL_XY,
    STATUS,
    A,
    B,
    advance,
    automation,
    brightness,
    kinds,
    layers,
    light,
    state,
)
from .test_behaviour import start as start_layers
from .test_scene import scenes

pytestmark = pytest.mark.freeze_time(tick=True)

PAST_LATE_WINDOW = 20       # a no-context change this soon after our command is a bridge's


@pytest.fixture
def externals(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_EXTERNAL)


@pytest.fixture
def person(hass_admin_user: MockUser) -> Context:
    return Context(user_id=hass_admin_user.id)


async def start(hass: HomeAssistant, freezer: TickingDateTimeFactory, strength: str,
                lamp: str = A, **options: Any) -> Any:
    """Layers on A and B, with a signal of ``strength`` on ``lamp``, settled."""
    engine = await start_layers(hass, [A, B], **options)
    assert await layers(hass, "set", entity_id=lamp, layer="trash", priority=70,
                        brightness=255, xy_color=SIGNAL_XY, strength=strength) == {lamp: "queued"}
    await settle(hass)
    await advance(hass, freezer, PAST_LATE_WINDOW)
    return engine


def shows_signal(hass: HomeAssistant, entity_id: str = A) -> bool:
    st = state(hass, entity_id)
    return st.state == "on" and st.attributes.get("brightness") == 255 \
        and st.attributes.get("color_mode") == "xy"


async def after_hold(hass: HomeAssistant, freezer: TickingDateTimeFactory) -> None:
    await advance(hass, freezer, HOLD_SETTLE_S + 0.5)
    await settle(hass)


# --------------------------------------------------------------------------- sticky


async def test_a_scene_from_a_wall_button_goes_under_a_sticky_signal(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    externals: list[Event],
) -> None:
    engine = await start(hass, freezer, "sticky")
    await scenes(hass, relax={A: {"state": "on", "brightness": 90, "color_temp_kelvin": 2700},
                              B: {"state": "on", "brightness": 90}})

    await hass.services.async_call("scene", "turn_on", {"entity_id": "scene.relax"},
                                   blocking=True, context=automation())
    await settle(hass)
    assert brightness(hass, B) == 90                 # the room got the scene
    await after_hold(hass, freezer)

    rec = engine.records[A]
    assert "trash" in rec.layers and rec.tombstones == {}
    assert rec.base == Command("on", 90, rec.base.color)    # the scene, underneath
    assert shows_signal(hass)
    assert "hold" in kinds(engine, A)
    assert externals[-1].data["scope"] == "room" and externals[-1].data["held"] == ["trash"]

    # The owner's clear: the lamp falls to the scene, not to what it was before it.
    await layers(hass, "clear", entity_id=A, layer="trash")
    await settle(hass)
    assert brightness(hass, A) == 90


async def test_an_automation_on_this_lamp_alone_goes_under_a_sticky_signal(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """A wall button bound to one lamp, or a motion sensor: neither is a hand on it."""
    engine = await start(hass, freezer, "sticky")
    await light(hass, "turn_on", A, automation(), brightness=60)
    await after_hold(hass, freezer)
    assert "trash" in engine.records[A].layers
    assert shows_signal(hass)


async def test_the_room_turning_off_leaves_a_sticky_signal_lit(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, freezer, "sticky")
    await hass.services.async_call("light", "turn_off", {"entity_id": [A, B]},
                                   blocking=True, context=automation())
    await settle(hass)
    await after_hold(hass, freezer)
    assert engine.records[A].base == OFF_COMMAND
    assert shows_signal(hass)
    assert state(hass, B).state == "off"


async def test_the_app_on_this_lamp_dismisses_a_sticky_signal(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    person: Context, externals: list[Event],
) -> None:
    engine = await start(hass, freezer, "sticky")
    await light(hass, "turn_on", A, person, brightness=40)
    await after_hold(hass, freezer)

    rec = engine.records[A]
    assert rec.layers == {} and set(rec.tombstones) == {"trash"}
    assert brightness(hass, A) == 40                 # what the person set stays
    assert externals[-1].data["scope"] == "lamp" and externals[-1].data["dropped"] == ["trash"]
    # A reminder re-setting it is skipped on this lamp: only the lamp is silenced.
    assert await layers(hass, "set", entity_id=A, layer="trash", priority=70, brightness=255,
                        xy_color=SIGNAL_XY, strength="sticky") == {A: "skipped_tombstoned"}


async def test_the_lamps_own_switch_dismisses_a_sticky_signal(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, freezer, "sticky")
    lights["a"].push(is_on=False)
    await advance(hass, freezer, DEBOUNCE_S + 0.5)
    await after_hold(hass, freezer)
    assert engine.records[A].layers == {}
    assert state(hass, A).state == "off"


async def test_clear_all_leaves_a_sticky_signal(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, freezer, "sticky")
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, brightness=30)
    await layers(hass, "clear", entity_id=A, layer="all")
    await settle(hass)
    assert set(engine.records[A].layers) == {"trash"}
    assert "clear_kept" in kinds(engine, A)


# --------------------------------------------------------------------------- locked


async def test_the_lamps_own_switch_goes_under_a_locked_alarm(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, freezer, "locked")
    lights["a"].push(is_on=False)
    await advance(hass, freezer, DEBOUNCE_S + 0.5)
    await after_hold(hass, freezer)
    rec = engine.records[A]
    assert "trash" in rec.layers
    assert rec.base == OFF_COMMAND
    assert shows_signal(hass)


async def test_the_app_on_this_lamp_goes_under_a_locked_alarm(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    person: Context,
) -> None:
    engine = await start(hass, freezer, "locked")
    await light(hass, "turn_off", A, person)
    await after_hold(hass, freezer)
    assert "trash" in engine.records[A].layers
    assert shows_signal(hass)


async def test_a_lamp_that_keeps_changing_back_is_re_shown_only_up_to_the_cap(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "HOLD_CAP", 2)
    engine = await start(hass, freezer, "locked")
    for _ in range(2):
        lights["a"].push(is_on=False)
        await advance(hass, freezer, DEBOUNCE_S + 0.5)
        await after_hold(hass, freezer)
        assert shows_signal(hass)
        await advance(hass, freezer, PAST_LATE_WINDOW)

    lights["a"].push(is_on=False)
    await advance(hass, freezer, DEBOUNCE_S + 0.5)
    await after_hold(hass, freezer)

    rec = engine.records[A]
    assert "hold_capped" in kinds(engine, A)
    assert state(hass, A).state == "off"             # it keeps what it shows
    assert "trash" in rec.layers                      # and the layer stays
    assert rec.diverged == "delivery"
    assert state(hass, STATUS).state == "failed"
    assert A in state(hass, STATUS).attributes["failed"]

    # The owner's clear repairs it like any failed lamp.
    await layers(hass, "clear", entity_id=A, layer="trash")
    await settle(hass)
    assert state(hass, STATUS).state == "ok"


# --------------------------------------------------------------------------- service


@pytest.mark.parametrize("data", [
    {"layer": "base", "state": "on", "strength": "sticky"},
    {"layer": "circ", "priority": 10, "mode": "follow", "source": "sensor.x",
     "strength": "locked"},
    {"layer": "tv", "priority": 40, "brightness": 30, "strength": "loud"},
])
async def test_set_refuses_a_strength_where_it_means_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], data: dict[str, Any],
) -> None:
    await start_layers(hass, [A])
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await layers(hass, "set", entity_id=A, **data)


async def test_get_shows_the_strength(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    await start(hass, freezer, "sticky")
    response = await hass.services.async_call("layers", "get", {"entity_id": A}, blocking=True,
                                              return_response=True)
    (described,) = response["entities"][A]["layers"]
    assert described["strength"] == "sticky"
