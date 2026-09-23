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
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.layers.const import DOMAIN
from custom_components.layers.logic.model import DEBOUNCE_S

from .conftest import FakeLamp, settle
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
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    engine = await start(hass, ["light.lamp_c"])
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c"},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await settle(hass)
    assert shows(hass, "light.lamp_c") == ("on", 90, 2500)
    rec = engine.records["light.lamp_c"]
    assert rec.layers["ambient"].manual == frozenset()
    assert rec.base.state == "on"


async def test_a_turn_on_that_names_a_brightness_keeps_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    engine = await start(hass, ["light.lamp_c"])
    source(hass, 90, 2500)
    await follow(hass, "light.lamp_c")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.lamp_c", "brightness": 200},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await settle(hass)
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
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
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
    await settle(hass)
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
