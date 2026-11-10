"""Follow layers inside Home Assistant: a lamp takes its brightness and colour from
another entity, a person's hand change takes over only what it changed, and the
lamp follows again once it goes off.

The source here is a plain state with attributes, as an Adaptive Lighting switch
or a template sensor writes them.
"""

from __future__ import annotations

from typing import Any

import pytest
from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.layers.const import DOMAIN
from custom_components.layers.logic.model import DEBOUNCE_S, FOLLOW_SETTLE_S

from .conftest import FADE_STEP_S, FakeLamp, settle
from .test_behaviour import advance, start

pytestmark = pytest.mark.freeze_time(tick=True)

SOURCE = "sensor.circadian"
A = "light.lamp_a"


def source(hass: HomeAssistant, brightness: int, kelvin: int, state: str = "on") -> None:
    hass.states.async_set(SOURCE, state, {"brightness": brightness, "color_temp_kelvin": kelvin})


async def follow(hass: HomeAssistant, entity_id: str = A, **extra: Any) -> None:
    await hass.services.async_call(
        DOMAIN, "set",
        {"entity_id": entity_id, "layer": "ambient", "priority": 10, "mode": "follow",
         "source": SOURCE, **extra},
        blocking=True,
    )
    await settle(hass)


def shows(hass: HomeAssistant, entity_id: str = A) -> tuple[str, int | None, int | None]:
    state = hass.states.get(entity_id)
    return state.state, state.attributes.get("brightness"), state.attributes.get("color_temp_kelvin")


async def test_it_follows_the_source_on_a_lamp_that_is_on(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    assert shows(hass) == ("on", 120, 2700)

    source(hass, 80, 3000)
    await settle(hass)
    assert shows(hass) == ("on", 80, 3000)
    assert lights["a"].calls[-1][1].get("transition") == 1.0


async def test_it_never_turns_a_lamp_on(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, ["light.lamp_c"])
    source(hass, 120, 2700)
    await follow(hass, "light.lamp_c")
    source(hass, 80, 3000)
    await settle(hass)
    assert hass.states.get("light.lamp_c").state == "off"
    assert lights["c"].calls == []


async def test_a_lamp_turned_on_by_hand_gets_the_follow_values(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, ["light.lamp_c"])
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c"},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await settle(hass)
    assert shows(hass, "light.lamp_c") == ("on", 128, 2700)   # its power-on level, settling
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert shows(hass, "light.lamp_c") == ("on", 90, 2500)
    rec = engine.records["light.lamp_c"]
    assert rec.layers["ambient"].manual == frozenset()
    assert rec.base.state == "on"


async def test_a_turn_on_that_names_a_brightness_keeps_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, ["light.lamp_c"])
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c", "brightness": 200},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert shows(hass, "light.lamp_c") == ("on", 200, 2500)
    assert engine.records["light.lamp_c"].layers["ambient"].manual == frozenset({"brightness"})

    source(hass, 60, 2200)
    await settle(hass)
    assert shows(hass, "light.lamp_c") == ("on", 200, 2200)    # the lamp's own minimum


async def test_a_dimmer_takes_the_brightness_and_the_colour_keeps_following(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    engine = await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    lights["a"].push(brightness=40)            # a dimmer bound to the lamp: no context
    await advance(hass, freezer, DEBOUNCE_S + 1)
    assert engine.records[A].layers["ambient"].manual == frozenset({"brightness"})
    assert shows(hass) == ("on", 40, 2700)

    source(hass, 150, 3000)
    await settle(hass)
    assert shows(hass) == ("on", 40, 3000)


async def test_off_gives_it_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser,
    freezer: TickingDateTimeFactory,
) -> None:
    engine = await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    person = Context(user_id=hass_admin_user.id)
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 30},
                                   blocking=True, context=person)
    await settle(hass)
    assert engine.records[A].layers["ambient"].manual == frozenset({"brightness"})
    await hass.services.async_call("light", "turn_off", {"entity_id": A},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    assert engine.records[A].layers["ambient"].manual == frozenset()
    await hass.services.async_call("light", "turn_on", {"entity_id": A},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert shows(hass) == ("on", 120, 2700)


async def test_manual_timeout_gives_it_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    hass_admin_user: MockUser,
) -> None:
    engine = await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass, manual_timeout={"minutes": 10})
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 30},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    assert shows(hass) == ("on", 30, 2700)
    await advance(hass, freezer, 599)
    assert shows(hass) == ("on", 30, 2700)
    await advance(hass, freezer, 2)
    assert shows(hass) == ("on", 120, 2700)
    assert engine.records[A].layers["ambient"].manual == frozenset()


async def test_a_nightlight_above_wins_and_hands_back_to_follow(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": A, "layer": "night", "priority": 50, "brightness": 10,
                        "color_temp_kelvin": 2200},
        blocking=True,
    )
    await settle(hass)
    assert shows(hass) == ("on", 10, 2200)
    sent = len(lights["a"].calls)
    source(hass, 80, 3000)                     # hidden under the nightlight: nothing sent
    await settle(hass)
    assert len(lights["a"].calls) == sent
    await hass.services.async_call(DOMAIN, "clear", {"layer": "night"}, blocking=True)
    await settle(hass)
    assert shows(hass) == ("on", 80, 3000)


async def test_a_take_back_drops_the_others_and_keeps_follow(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    engine = await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": A, "layer": "tv", "priority": 40, "brightness": 20},
        blocking=True,
    )
    await settle(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 90},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    rec = engine.records[A]
    assert "tv" in rec.tombstones and "tv" not in rec.layers
    assert rec.layers["ambient"].manual == frozenset({"brightness"})
    assert shows(hass) == ("on", 90, 2700)


async def test_a_source_that_goes_away_keeps_its_last_values(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    sent = len(lights["a"].calls)
    hass.states.async_set(SOURCE, "unavailable", {})
    await settle(hass)
    assert shows(hass) == ("on", 120, 2700) and len(lights["a"].calls) == sent


async def test_a_source_switched_off_lets_the_lamp_fall_through(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    engine = await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    base = engine.records[A].base
    source(hass, 120, 2700, state="off")       # Adaptive Lighting switched off
    await settle(hass)
    assert shows(hass)[1] == base.brightness


async def test_a_reload_keeps_following(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    engine = await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    entry = engine.entry
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    engine._end_grace()                        # noqa: SLF001 — skip the settle wait
    engine._cancel("grace", "")                # noqa: SLF001
    source(hass, 70, 3300)
    await settle(hass)
    assert shows(hass) == ("on", 70, 3300)


@pytest.mark.parametrize(
    "data",
    [
        {"mode": "follow"},                                          # no source
        {"mode": "follow", "source": SOURCE, "brightness": 100},     # values of its own
        {"mode": "follow", "source": SOURCE, "state": "on"},
        {"mode": "set", "state": "on", "source": SOURCE},            # source without follow
    ],
)
async def test_the_service_refuses_a_bad_follow_request(
    hass: HomeAssistant, lights: dict[str, FakeLamp], data: dict[str, Any]
) -> None:
    await start(hass, [A])
    with pytest.raises((HomeAssistantError, Exception)):
        await hass.services.async_call(
            DOMAIN, "set", {"entity_id": A, "layer": "ambient", "priority": 10, **data},
            blocking=True,
        )


async def test_get_shows_the_source_and_what_was_taken_over(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 30},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await settle(hass)
    result = await hass.services.async_call(DOMAIN, "get", {"entity_id": A}, blocking=True,
                                            return_response=True)
    (layer,) = result["entities"][A]["layers"]
    assert layer["source"] == SOURCE and layer["manual"] == ["brightness"]
    assert layer["mode"] == "follow"


async def test_a_nightlight_without_a_colour_keeps_the_follow_colour(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    source(hass, 120, 2700)
    await follow(hass)
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": A, "layer": "night", "priority": 50, "brightness": 10},
        blocking=True,
    )
    await settle(hass)
    source(hass, 80, 3000)
    await settle(hass)
    assert shows(hass) == ("on", 10, 3000)


async def test_a_fading_lamp_switched_on_by_a_scene_keeps_the_scene_brightness(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """A scene turns an off lamp on at 255. A Matter lamp first reports "on" at the
    level it was last lit at, then climbs. Sending the follow colour on that first
    report sent the base brightness with it, 26, which stopped the fade there and
    left 26 as the base. The follow values wait for the change to settle."""
    engine = await start(hass, ["light.lamp_c"])
    lamp = lights["c"]
    lamp._last_brightness = 26     # noqa: SLF001 — it was last lit low
    lamp.fade_reports = 5
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")

    reports: list[int | None] = []

    @callback
    def _report(event: Event[EventStateChangedData]) -> None:
        reports.append(event.data["new_state"].attributes.get("brightness"))

    async_track_state_change_event(hass, "light.lamp_c", _report)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c", "brightness": 255},
        blocking=True, context=Context(),            # an automation running a scene
    )
    await settle(hass, (lamp.fade_reports + 2) * FADE_STEP_S)
    assert reports[0] == 26 and reports[-1] == 255    # the old level first, then the fade
    assert len(lamp.calls) == 1                       # nothing of ours yet

    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    rec = engine.records["light.lamp_c"]
    assert rec.base.brightness == 255
    assert rec.layers["ambient"].manual == frozenset({"brightness"})
    assert shows(hass, "light.lamp_c") == ("on", 255, 2500)
    sent = [kwargs for service, kwargs, _ in lamp.calls[1:]]
    assert sent and all("brightness" not in kwargs for kwargs in sent)   # the colour alone
    assert sent[-1].get("color_temp_kelvin") == 2500


async def test_a_quiet_fading_lamp_keeps_the_scene_brightness_through_its_transition(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """An IKEA Matter lamp, turned on from off by a scene with transition 15, reported
    three times in its first 0.26 s, then nothing for two seconds while it was still
    fading. FOLLOW_SETTLE_S after the last report counted
    that silence as settled and sent the follow values with the base brightness it had
    learned so far, a level near the start of the fade, which stopped the fade there.
    The follow values must wait for the transition the change asked for."""
    engine = await start(hass, ["light.lamp_c"])
    lamp = lights["c"]
    lamp._last_brightness = 26     # noqa: SLF001 — it was last lit low
    lamp.fade_reports = 2
    lamp.fade_sparse = True
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c", "brightness": 255, "transition": 15},
        blocking=True, context=Context(),            # a scene run by an automation
    )
    await settle(hass, (lamp.fade_reports + 2) * FADE_STEP_S)
    assert len(lamp.calls) == 1                       # nothing of ours yet

    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)  # quiet, but still fading
    assert len(lamp.calls) == 1, f"sent mid-fade: {lamp.calls[1:]}"

    await advance(hass, freezer, 15)                  # the fade arrives, then settles
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    rec = engine.records["light.lamp_c"]
    assert rec.base.brightness == 255
    assert shows(hass, "light.lamp_c") == ("on", 255, 2500)
    sent = [kwargs for service, kwargs, _ in lamp.calls[1:]]
    assert sent and all(kwargs.get("brightness", 255) == 255 for kwargs in sent)


async def test_a_colour_change_during_a_fade_does_not_dim_the_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """A lamp fading to 255 over 15 s got a colour-only call 3 s in. The colour call
    named no brightness, so the base stopped learning it from the lamp's reports and
    kept the level heard at the start of the fade; once the fade was over the follow
    render sent that level and the lamp dropped from full to almost nothing. The base
    takes the brightness the call asked for, not the level the lamp reports on its way."""
    engine = await start(hass, ["light.lamp_c"])
    lamp = lights["c"]
    lamp._last_brightness = 2      # it was last lit very low
    lamp.fade_reports = 2
    lamp.fade_sparse = True
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c", "brightness": 255, "transition": 15},
        blocking=True, context=Context(),
    )
    await settle(hass, (lamp.fade_reports + 2) * FADE_STEP_S)
    rec = engine.records["light.lamp_c"]
    assert rec.base.brightness == 255                 # asked for, though the lamp shows less
    assert hass.states.get("light.lamp_c").attributes["brightness"] < 255

    await advance(hass, freezer, 3)
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": "light.lamp_c", "color_temp_kelvin": 4000, "transition": 12},
        blocking=True, context=Context(),
    )
    await advance(hass, freezer, 15)                  # both transitions run out
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert rec.base.brightness == 255
    assert shows(hass, "light.lamp_c") == ("on", 255, 4000)
    ours = [kwargs for _service, kwargs, _ctx in lamp.calls[2:]]
    assert all("brightness" not in kwargs for kwargs in ours), ours


async def test_the_follow_values_go_out_without_the_base_brightness(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """A follow layer that gives a colour sends the colour. A brightness in the same
    call is a new move-to-level on a Matter lamp: it stops a fade, and it puts back
    whatever level Layers believes in."""
    await start(hass, ["light.lamp_c"])
    lamp = lights["c"]
    hass.states.async_set(SOURCE, "on", {"color_temp_kelvin": 2500})
    await follow(hass, "light.lamp_c")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c", "brightness": 200},
        blocking=True, context=Context(),
    )
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert shows(hass, "light.lamp_c") == ("on", 200, 2500)
    before = len(lamp.calls)

    hass.states.async_set(SOURCE, "on", {"color_temp_kelvin": 3000})
    await settle(hass)
    assert shows(hass, "light.lamp_c") == ("on", 200, 3000)
    sent = [kwargs for _service, kwargs, _ctx in lamp.calls[1:]]
    assert len(lamp.calls) > before
    assert all("brightness" not in kwargs for kwargs in sent), sent
    assert sent[-1]["color_temp_kelvin"] == 3000


async def test_a_lamp_switched_off_while_settling_gets_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser,
    freezer: TickingDateTimeFactory,
) -> None:
    await start(hass, ["light.lamp_c"])
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")
    person = Context(user_id=hass_admin_user.id)
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.lamp_c"},
                                   blocking=True, context=person)
    await settle(hass)
    await hass.services.async_call("light", "turn_off", {"entity_id": "light.lamp_c"},
                                   blocking=True, context=Context(user_id=hass_admin_user.id))
    await advance(hass, freezer, FOLLOW_SETTLE_S + 1)
    assert hass.states.get("light.lamp_c").state == "off"
    assert [service for service, _kwargs, _ctx in lights["c"].calls] == ["turn_on", "turn_off"]
