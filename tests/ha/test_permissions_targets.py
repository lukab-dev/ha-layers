"""Who may call layers.*, and which lamps a call reaches.

The services act on a person's behalf only where Home Assistant would let that
person act directly; an automation's call (no user) is never asked. A target is
resolved the same way whatever order its parts are walked in, and an enrolled lamp
stays addressable while its state is gone for a moment.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.layers.const import DOMAIN
from custom_components.layers.targets import expand

from .conftest import FakeLamp, settle
from .test_behaviour import A, B, automation, layers, start

pytestmark = pytest.mark.freeze_time(tick=True)

ROOM = "light.room_q"
SOURCE = "sensor.follow_source"


def only(user: MockUser, *entity_ids: str) -> Context:
    """A person who may read and control just these entities."""
    user.mock_policy({"entities": {"entity_ids": {eid: True for eid in entity_ids}}})
    return Context(user_id=user.id)


def stack(engine: Any, entity_id: str) -> list[str]:
    return [layer["id"] for layer in engine.describe([entity_id])[entity_id]["layers"]]


async def get(hass: HomeAssistant, context: Context, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, "get", data, blocking=True, context=context, return_response=True
    )
    return response["entities"]


# --------------------------------------------------------------------------- permissions


async def test_a_person_who_may_only_read_cannot_set_a_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    engine = await start(hass, [A])
    sent = len(lights["a"].calls)
    with pytest.raises(Unauthorized):
        await layers(hass, "set", Context(user_id=hass_read_only_user.id),
                     entity_id=A, layer="x", priority=50, brightness=10)
    await settle(hass)
    assert stack(engine, A) == [] and len(lights["a"].calls) == sent


async def test_a_set_is_refused_as_a_whole_when_one_lamp_is_not_theirs(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    engine = await start(hass, [A, B])
    theirs = only(hass_read_only_user, A)
    with pytest.raises(Unauthorized):
        await layers(hass, "set", theirs, entity_id=[A, B], layer="x", priority=50, brightness=10)
    assert stack(engine, A) == [] and stack(engine, B) == []

    assert await layers(hass, "set", theirs, entity_id=A, layer="x", priority=50,
                        brightness=10) == {A: "queued"}
    assert stack(engine, A) == ["x"]


async def test_a_clear_without_a_target_needs_every_enrolled_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    engine = await start(hass, [A, B])
    await layers(hass, "set", automation(), entity_id=[A, B], layer="x", priority=50, brightness=10)
    await settle(hass)
    with pytest.raises(Unauthorized):
        await layers(hass, "clear", only(hass_read_only_user, A), layer="x")
    assert stack(engine, A) == ["x"] and stack(engine, B) == ["x"]


async def test_a_sync_without_a_target_needs_every_enrolled_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    await start(hass, [A, B])
    sent = len(lights["b"].calls)
    with pytest.raises(Unauthorized):
        await layers(hass, "sync", only(hass_read_only_user, A))
    await settle(hass)
    assert len(lights["b"].calls) == sent


async def test_get_shows_a_person_only_what_they_may_read(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    await start(hass, [A, B])
    assert set(await get(hass, Context(user_id=hass_read_only_user.id))) == {A, B}
    theirs = only(hass_read_only_user, A)
    assert set(await get(hass, theirs)) == {A}
    assert set(await get(hass, theirs, entity_id=[A, B])) == {A}


async def test_an_automation_is_never_asked(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    engine = await start(hass, [A, B])
    assert await layers(hass, "set", automation(), entity_id=[A, B], layer="x", priority=50,
                        brightness=10) == {A: "queued", B: "queued"}
    await settle(hass)
    assert set(await layers(hass, "sync", automation())) == {A, B}
    assert set(await layers(hass, "clear", automation(), layer="x")) == {A, B}
    assert stack(engine, A) == [] and set(await get(hass, automation())) == {A, B}


async def test_a_follow_layer_needs_the_right_to_read_its_source(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser,
) -> None:
    """The lamps would show what the source reports: that is reading it."""
    engine = await start(hass, [A])
    hass.states.async_set(SOURCE, "on", {"brightness": 100})
    theirs = only(hass_read_only_user, A)
    with pytest.raises(Unauthorized):
        await layers(hass, "set", theirs, entity_id=A, layer="f", priority=10,
                     mode="follow", source=SOURCE)
    assert stack(engine, A) == []

    hass_read_only_user.mock_policy(
        {"entities": {"entity_ids": {A: True, SOURCE: {"read": True}}}})
    await layers(hass, "set", Context(user_id=hass_read_only_user.id), entity_id=A, layer="f",
                 priority=10, mode="follow", source=SOURCE)
    assert stack(engine, A) == ["f"]


# --------------------------------------------------------------------------- targets


async def test_an_enrolled_lamp_without_a_state_can_still_be_cleared(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    """Its integration is reloading: the state is gone for a moment, the record is not."""
    engine = await start(hass, [A, B])
    await layers(hass, "set", automation(), entity_id=[A, B], layer="x", priority=50, brightness=10)
    await settle(hass)
    hass.states.async_remove(A)
    await hass.async_block_till_done()

    result = await layers(hass, "clear", automation(), entity_id=A, layer="x")
    assert set(result) == {A}
    assert stack(engine, A) == [] and stack(engine, B) == ["x"]


async def test_an_unknown_lamp_without_a_state_is_still_no_target(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    await start(hass, [A])
    with pytest.raises(ServiceValidationError):
        await layers(hass, "clear", automation(), entity_id="light.never_was", layer="x")
    assert expand(hass, ["light.never_was"], {A}) == (set(), set())


async def test_a_lamp_reached_through_a_vendor_room_is_marked_whatever_the_order(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    hass.states.async_set(ROOM, "on", {"entity_id": [A], "is_hue_group": True})
    assert expand(hass, [A, ROOM]) == ({A}, {A})
    assert expand(hass, [ROOM, A]) == ({A}, {A})
    assert expand(hass, [A]) == ({A}, set())


async def test_groups_that_contain_each_other_end(
    hass: HomeAssistant, lights: dict[str, FakeLamp],
) -> None:
    hass.states.async_set("light.g1", "on", {"entity_id": ["light.g2"]})
    hass.states.async_set("light.g2", "on", {"entity_id": ["light.g1", A, ROOM]})
    hass.states.async_set(ROOM, "on", {"entity_id": ["light.g1", B], "is_hue_group": True})
    assert expand(hass, ["light.g1"]) == ({A, B}, {A, B})
