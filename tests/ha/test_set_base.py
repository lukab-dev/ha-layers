"""layers.set_base inside Home Assistant: a scene that goes through Layers (SPEC 5.6).

A button that calls ``layers.set_base`` instead of ``scene.turn_on`` gets the same
changes to the layers, but the lamps get one command each, already resolved: the
scene's values with what the layers give them, nothing over a held signal, and the
delivery checks of Layers' own renders.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from custom_components.layers.const import DOMAIN, EVENT_EXTERNAL, EVENT_RENDER
from custom_components.layers.logic.model import (
    DIV_UNSYNCED,
    FOLLOW_SETTLE_S,
    HOLD_SETTLE_S,
    OFF_COMMAND,
    Color,
    Command,
)

from .conftest import FakeLamp, settle
from .test_behaviour import (
    A,
    B,
    C,
    advance,
    automation,
    brightness,
    kinds,
    layers,
    sent_by_layers,
    start,
    state,
)
from .test_follow import SOURCE, follow, shows, source
from .test_scene import scenes
from .test_strength import shows_signal
from .test_strength import start as start_signal

pytestmark = pytest.mark.freeze_time(tick=True)

D = "light.lamp_d"


@pytest.fixture
def renders(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_RENDER)


@pytest.fixture
def externals(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_EXTERNAL)


async def set_base(hass: HomeAssistant, context: Context | None = None, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, "set_base", data, blocking=True, context=context or automation(),
        return_response=True,
    )
    await settle(hass)
    return response


# --------------------------------------------------------------------------- one command


async def test_a_scene_and_a_follow_colour_reach_the_lamp_in_one_command(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    """The case behind set_base: brightness from the scene, colour from the follow layer."""
    engine = await start(hass, [C])
    source(hass, 90, 2500)
    await follow(hass, C)
    assert lights["c"].calls == []          # off: a follow layer never turns it on

    response = await set_base(hass, entities={C: {"state": "on", "brightness": 255}})
    assert response == {"entities": {C: "queued"}, "passed_on": []}
    ((service, kwargs, _ctx),) = lights["c"].calls
    assert service == "turn_on"
    assert kwargs["brightness"] == 255 and kwargs["color_temp_kelvin"] == 2500
    assert shows(hass, C) == ("on", 255, 2500)

    rec = engine.records[C]
    assert rec.base == Command("on", 255, None)      # the scene's values, not the layer's
    assert rec.layers["ambient"].manual == frozenset({"brightness"})
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert len(lights["c"].calls) == 1               # no second step
    assert "follow_on" not in kinds(engine, C)
    assert [e.data["reason"] for e in renders] == ["set_base"]

    source(hass, 60, 3000)                           # the colour keeps following
    await settle(hass)
    assert shows(hass, C) == ("on", 255, 3000)


async def test_a_fading_lamp_gets_the_scene_level_and_the_base_keeps_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
) -> None:
    """The 2026-10-18 race: a Matter lamp reports its old level first. Through set_base
    the base is the scene's, not the first report's."""
    engine = await start(hass, [C])
    lights["c"].fade_reports = 3
    source(hass, 90, 2500)
    await follow(hass, C)

    await set_base(hass, entities={C: {"state": "on", "brightness": 255}})
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert shows(hass, C) == ("on", 255, 2500)
    first = lights["c"].calls[0][1]
    assert first["brightness"] == 255 and first["color_temp_kelvin"] == 2500
    assert engine.records[C].base.brightness == 255
    assert "follow_on" not in kinds(engine, C)


async def test_a_bare_on_takes_both_from_the_follow_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    engine = await start(hass, [C])
    source(hass, 90, 2500)
    await follow(hass, C)
    await set_base(hass, entities={C: "on"})
    assert shows(hass, C) == ("on", 90, 2500)
    assert len(lights["c"].calls) == 1
    assert engine.records[C].layers["ambient"].manual == frozenset()


# --------------------------------------------------------------------------- scenes


async def test_a_scene_entity_routes_managed_lamps_and_passes_on_the_rest(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
) -> None:
    engine = await start(hass, [A])
    await scenes(hass, evening={A: {"state": "on", "brightness": 60, "color_temp_kelvin": 3000},
                                D: {"state": "on", "brightness": 40, "color_temp_kelvin": 2700}})
    response = await set_base(hass, entity_id="scene.evening", transition=2)
    assert response == {"entities": {A: "queued"}, "passed_on": [D]}

    assert brightness(hass, A) == 60 and brightness(hass, D) == 40
    (sent,) = sent_by_layers(lights["a"], renders)
    assert sent[1]["transition"] == 2
    assert lights["d"].calls[-1][1]["transition"] == 2       # Home Assistant's scene, for D
    assert engine.records[A].base == Command("on", 60, Color.kelvin(3000))
    # The scene's own call for D is not mistaken for anything on A.
    assert "scene_skipped" not in kinds(engine, A)


async def test_set_base_needs_a_scene_or_entities_but_not_both(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    await start(hass, [A])
    await scenes(hass, evening={A: "on"})
    for data in ({}, {"entity_id": "scene.evening", "entities": {A: "on"}}):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "set_base", data, blocking=True)


# --------------------------------------------------------------------------- the layers react


async def test_a_soft_layer_is_taken_back_as_by_the_scene_itself(
    hass: HomeAssistant, lights: dict[str, FakeLamp], externals: list[Event],
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, brightness=20)
    await settle(hass)
    await set_base(hass, entities={A: {"state": "on", "brightness": 180}})
    rec = engine.records[A]
    assert rec.layers == {} and set(rec.tombstones) == {"tv"}
    assert brightness(hass, A) == 180
    assert externals[-1].data["dropped"] == ["tv"] and externals[-1].data["scope"] == "room"


async def test_off_over_a_soft_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="tv", priority=40, brightness=20)
    await settle(hass)
    await set_base(hass, entities={A: "off"})
    assert state(hass, A).state == "off"
    assert engine.records[A].base == OFF_COMMAND and engine.records[A].layers == {}


@pytest.mark.parametrize("strength", ["sticky", "locked"])
async def test_nothing_reaches_a_lamp_under_a_strong_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    externals: list[Event], strength: str,
) -> None:
    """No dip: the lamp already shows its effective command, the signal."""
    engine = await start_signal(hass, freezer, strength)
    sent = len(lights["a"].calls)
    await scenes(hass, relax={A: {"state": "on", "brightness": 90, "color_temp_kelvin": 2700},
                              B: {"state": "on", "brightness": 90}})
    response = await set_base(hass, entity_id="scene.relax")
    assert response["entities"] == {A: "in_sync", B: "queued"}
    assert brightness(hass, B) == 90
    assert len(lights["a"].calls) == sent and shows_signal(hass)

    await advance(hass, freezer, HOLD_SETTLE_S + 1)
    assert len(lights["a"].calls) == sent            # and no re-show afterwards
    rec = engine.records[A]
    assert "trash" in rec.layers and rec.diverged is None
    assert rec.base == Command("on", 90, Color.kelvin(2700))
    assert "hold" not in kinds(engine, A)
    assert externals[-1].data["held"] == ["trash"]

    await layers(hass, "clear", entity_id=A, layer="trash")
    await settle(hass)
    assert brightness(hass, A) == 90


async def test_a_strong_layer_with_a_brightness_alone_gets_only_the_scene_colour(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    engine = await start(hass, [A])
    await layers(hass, "set", entity_id=A, layer="alarm", priority=90, brightness=255,
                 strength="locked")
    await settle(hass)
    sent = len(lights["a"].calls)
    await set_base(hass, entities={A: {"state": "on", "brightness": 50, "color_temp_kelvin": 4000}})
    ((_service, kwargs, _ctx),) = lights["a"].calls[sent:]
    assert kwargs["brightness"] == 255 and kwargs["color_temp_kelvin"] == 4000
    assert engine.records[A].base == Command("on", 50, Color.kelvin(4000))


# --------------------------------------------------------------------------- delivery


async def test_a_bridge_reverting_it_is_retried_once(
    hass: HomeAssistant, lights: dict[str, FakeLamp], renders: list[Event],
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [C])
    engine._platforms[C] = "hue"            # a 60 s late window
    lights["c"].revert_after = 25
    await set_base(hass, entities={C: {"state": "on", "brightness": 150}})
    assert state(hass, C).state == "on"
    await advance(hass, freezer, 25.5)
    await settle(hass)
    assert "failed_delivery" in kinds(engine, C)
    assert state(hass, C).state == "on" and brightness(hass, C) == 150
    assert engine.records[C].base.state == "on"      # the revert did not become the base
    assert [e.data["reason"] for e in renders] == ["set_base", "retry"]


async def test_the_base_learns_the_colour_the_lamp_chose(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    """A scene naming only a brightness turns an off lamp on in the colour it
    remembers. That colour becomes the base's, so a colour layer cleared later has
    something to go back to."""
    engine = await start(hass, [C])
    await set_base(hass, entities={C: {"state": "on", "brightness": 200}})
    assert "base_learned" in kinds(engine, C)
    assert engine.records[C].base == Command("on", 200, Color.kelvin(2700))

    await layers(hass, "set", entity_id=C, layer="signal", priority=60, color_temp_kelvin=6000)
    await settle(hass)
    await layers(hass, "clear", entity_id=C, layer="signal")
    await settle(hass)
    assert shows(hass, C) == ("on", 200, 2700)


async def test_with_apply_off_nothing_is_sent(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    engine = await start(hass, [A], apply=False)
    sent = len(lights["a"].calls)
    response = await set_base(hass, entities={A: {"state": "on", "brightness": 10}})
    assert response["entities"] == {A: "shadow"}
    assert len(lights["a"].calls) == sent
    assert engine.records[A].base.brightness == 10 and engine.records[A].diverged == DIV_UNSYNCED


# --------------------------------------------------------------------------- permissions


async def test_a_person_who_may_not_control_the_lamp_is_refused(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    engine = await start(hass, [A])
    base = engine.records[A].base
    sent = len(lights["a"].calls)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "set_base", {"entities": {A: {"state": "on", "brightness": 10}}},
            blocking=True, context=Context(user_id=hass_read_only_user.id),
        )
    await settle(hass)
    assert engine.records[A].base == base and len(lights["a"].calls) == sent


async def test_a_person_is_a_user_source_but_still_the_room(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    hass_admin_user: MockUser,
) -> None:
    """A scene tile in the app names one lamp, and is still a scene: room scope."""
    engine = await start_signal(hass, freezer, "sticky")
    await set_base(hass, Context(user_id=hass_admin_user.id),
                   entities={A: {"state": "on", "brightness": 90}})
    rec = engine.records[A]
    assert "trash" in rec.layers
    assert rec.last_external.source == "user" and rec.last_external.scope == "room"


async def test_a_lamp_that_fades_on_gets_brightness_and_colour_in_one_command(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """A Matter lamp turned on from off reports its old level first and says little
    while it fades. Through set_base none of that is read: the lamp gets the scene's
    brightness and the follow colour as one command with the scene's transition, and
    nothing is sent after it."""
    engine = await start(hass, [C])
    lamp = lights["c"]
    lamp._last_brightness = 2
    lamp.fade_reports = 2
    lamp.fade_sparse = True
    hass.states.async_set(SOURCE, "on", {"color_temp_kelvin": 2500})
    await follow(hass, C)
    before = len(lamp.calls)

    await set_base(hass, entities={C: {"state": "on", "brightness": 255}}, transition=15)
    sent = [(service, kwargs) for service, kwargs, _ctx in lamp.calls[before:]]
    assert sent == [("turn_on", {"brightness": 255, "color_temp_kelvin": 2500, "transition": 15})]
    assert engine.records[C].base.brightness == 255

    await advance(hass, freezer, 15)                  # the fade arrives
    await advance(hass, freezer, FOLLOW_SETTLE_S + 3)  # ... and is verified
    assert shows(hass, C) == ("on", 255, 2500)
    assert engine.records[C].base.brightness == 255
    assert len(lamp.calls) == before + 1, lamp.calls[before + 1:]
    assert engine.records[C].owed is None and engine.records[C].diverged is None


async def test_a_scene_gives_the_same_room_whatever_was_pressed_before(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """One scene names a colour, so the follow layer stops following it. The next scene
    names only a brightness: the colour it leaves open is the follow layer's again.
    Without that, the same button gave the follow colour from off and the earlier
    scene's colour after it."""
    engine = await start(hass, [C])
    lamp = lights["c"]
    hass.states.async_set(SOURCE, "on", {"color_temp_kelvin": 3000})
    await follow(hass, C)

    await set_base(hass, entities={C: {"state": "on", "brightness": 100, "color_temp_kelvin": 2500}})
    assert shows(hass, C) == ("on", 100, 2500)
    assert engine.records[C].layers["ambient"].manual == frozenset({"brightness", "color"})

    before = len(lamp.calls)
    await set_base(hass, entities={C: {"state": "on", "brightness": 255}})
    assert shows(hass, C) == ("on", 255, 3000)
    assert engine.records[C].layers["ambient"].manual == frozenset({"brightness"})
    sent = [kwargs for _service, kwargs, _ctx in lamp.calls[before:]]
    assert sent == [{"brightness": 255, "color_temp_kelvin": 3000}]


async def test_a_member_saved_with_no_effect_goes_through_layers(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """The scene editor saves `effect: "off"` for a lamp that can do effects. That is
    not an intent Layers cannot read: the lamp is routed like any other. A lamp that
    is running an effect is passed on, so Home Assistant's scene switches it off."""
    await start(hass, [A, B])
    await scenes(hass, unused={D: {"state": "on"}})     # loads the scene services
    wanted = {"state": "on", "brightness": 120, "effect": "off"}
    result = await set_base(hass, entities={A: dict(wanted)})
    assert A in result["entities"] and result["passed_on"] == []
    assert brightness(hass, A) == 120

    hass.states.async_set(B, "on", {**hass.states.get(B).attributes, "effect": "candle"})
    result = await set_base(hass, entities={B: dict(wanted)})
    assert result["passed_on"] == [B] and B not in result["entities"]
