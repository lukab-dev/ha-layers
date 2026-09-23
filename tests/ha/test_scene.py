"""A scene's decision holds for the members it skips because they already match.

Home Assistant's scenes send nothing to a member that already looks the way the
scene wants, so Layers hears no call and sees no state change for it. Before the
fix, a layer that held the ceiling off outlived a bedtime scene that also turned it
off; clearing the layer later put the old base back and the ceiling came on.
"""

from __future__ import annotations

from typing import Any

import pytest
from freezegun.api import TickingDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.layers.const import DOMAIN
from custom_components.layers.engine import SCENE_SETTLE_S

from .conftest import FakeLamp, FakeSwitch, settle
from .test_behaviour import advance, start

pytestmark = pytest.mark.freeze_time(tick=True)


async def scenes(hass: HomeAssistant, **defined: dict[str, Any]) -> None:
    """Home Assistant's own scenes, as YAML defines them."""
    config = [{"name": name, "entities": entities} for name, entities in defined.items()]
    assert await async_setup_component(hass, "scene", {"scene": config})
    await hass.async_block_till_done()


async def hold_off(hass: HomeAssistant, entity_id: str, layer: str = "sleep") -> None:
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": entity_id, "layer": layer, "priority": 50, "state": "off"},
        blocking=True,
    )
    await settle(hass)


def kinds(engine: Any, entity_id: str) -> list[str]:
    return [d["kind"] for d in engine.decisions if d["entity_id"] == entity_id]


async def test_a_scene_that_skips_a_held_lamp_takes_it_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """The roadmap's case: the layer made the lamp match, so Home Assistant skipped it."""
    engine = await start(hass, ["light.lamp_a", "light.lamp_b"])
    await scenes(hass, bedtime={"light.lamp_a": "off", "light.lamp_b": "off"})
    await hold_off(hass, "light.lamp_a")
    assert hass.states.get("light.lamp_a").state == "off"
    sent = len(lights["a"].calls)

    await hass.services.async_call("scene", "turn_on", {"entity_id": "scene.bedtime"}, blocking=True)
    await settle(hass)
    assert len(lights["a"].calls) == sent, "Home Assistant skipped the matching member"
    assert "sleep" in engine.records["light.lamp_a"].layers   # not yet: the scene settles first

    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    rec = engine.records["light.lamp_a"]
    assert not rec.layers and "sleep" in rec.tombstones
    assert rec.base.state == "off"
    assert "scene_skipped" in kinds(engine, "light.lamp_a")

    # Clearing the layer later no longer undoes the scene.
    await hass.services.async_call(DOMAIN, "clear", {"layer": "sleep"}, blocking=True)
    await settle(hass)
    assert hass.states.get("light.lamp_a").state == "off"
    assert len(lights["a"].calls) == sent


async def test_a_member_the_scene_sends_is_left_to_the_call_path(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    engine = await start(hass, ["light.lamp_a", "light.lamp_b"])
    await scenes(hass, bedtime={"light.lamp_a": "off", "light.lamp_b": "off"})
    await hass.services.async_call("scene", "turn_on", {"entity_id": "scene.bedtime"}, blocking=True)
    await settle(hass)
    assert lights["b"].calls[-1][0] == "turn_off"   # b was on: the scene sent it a call

    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    assert "scene_skipped" not in kinds(engine, "light.lamp_b")
    assert engine.records["light.lamp_b"].base.state == "off"


async def test_scene_apply_on_a_held_switch(
    hass: HomeAssistant, switches: dict[str, FakeSwitch], freezer: TickingDateTimeFactory
) -> None:
    engine = await start(hass, ["switch.relay"])
    await scenes(hass)
    await hold_off(hass, "switch.relay")
    sent = len(switches["relay"].calls)

    # YAML's `off` arrives as a boolean.
    await hass.services.async_call(
        "scene", "apply", {"entities": {"switch.relay": False}}, blocking=True
    )
    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    assert len(switches["relay"].calls) == sent
    rec = engine.records["switch.relay"]
    assert not rec.layers and rec.base.state == "off"


async def test_a_snapshot_scene_keeps_the_brightness_a_layer_made(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """A scene made from the lamp as it is (the editor, scene.create) matches every
    attribute, so Home Assistant skips even an `on` member."""
    engine = await start(hass, ["light.lamp_b"])
    await scenes(hass)
    await hass.services.async_call(
        DOMAIN, "set", {"entity_id": "light.lamp_b", "layer": "tv", "priority": 40, "brightness": 30},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("light.lamp_b").attributes["brightness"] == 30
    await hass.services.async_call(
        "scene", "create", {"scene_id": "evening", "snapshot_entities": ["light.lamp_b"]},
        blocking=True,
    )
    sent = len(lights["b"].calls)

    await hass.services.async_call("scene", "turn_on", {"entity_id": "scene.evening"}, blocking=True)
    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    assert len(lights["b"].calls) == sent
    rec = engine.records["light.lamp_b"]
    assert not rec.layers
    assert rec.base.state == "on" and rec.base.brightness == 30


async def test_a_skipped_member_that_does_not_match_keeps_its_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """Home Assistant also sends nothing for a colour mode without its colour. The
    lamp does not show what the scene wanted, so nothing is recorded."""
    engine = await start(hass, ["light.lamp_a"])
    await scenes(hass, broken={"light.lamp_a": {"state": "on", "color_mode": "xy"}})
    await hold_off(hass, "light.lamp_a")
    sent = len(lights["a"].calls)

    await hass.services.async_call("scene", "turn_on", {"entity_id": "scene.broken"}, blocking=True)
    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    assert len(lights["a"].calls) == sent
    assert "sleep" in engine.records["light.lamp_a"].layers
    assert "scene_skipped" not in kinds(engine, "light.lamp_a")


async def test_a_skipped_on_member_that_is_off_is_not_claimed(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory
) -> None:
    """A member reached by no call because the scene run was cut short: the lamp shows
    off, the scene wanted on, so the intent path refuses it."""
    engine = await start(hass, ["light.lamp_a"])
    await scenes(hass)
    await hold_off(hass, "light.lamp_a")
    engine._start_scene(  # noqa: SLF001 — a run whose call never reached the lamp
        type("E", (), {"data": {"service": "apply", "service_data": {
            "entities": {"light.lamp_a": {"state": "on", "brightness": 200}}}},
            "context": Context()})(),
        None,
    )
    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    assert "sleep" in engine.records["light.lamp_a"].layers
    assert "scene_skip_ignored" in kinds(engine, "light.lamp_a")


async def test_a_user_who_may_not_control_the_lamp_does_not_take_it_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    hass_read_only_user: MockUser,
) -> None:
    engine = await start(hass, ["light.lamp_a"])
    await scenes(hass)
    await hold_off(hass, "light.lamp_a")
    hass.bus.async_fire(
        "call_service",
        {"domain": "scene", "service": "apply",
         "service_data": {"entities": {"light.lamp_a": "off"}}},
        context=Context(user_id=hass_read_only_user.id),
    )
    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    assert "sleep" in engine.records["light.lamp_a"].layers


async def test_an_admin_scene_is_the_persons(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: TickingDateTimeFactory,
    hass_admin_user: MockUser,
) -> None:
    engine = await start(hass, ["light.lamp_a"])
    await scenes(hass, bedtime={"light.lamp_a": "off"})
    await hold_off(hass, "light.lamp_a")
    await hass.services.async_call(
        "scene", "turn_on", {"entity_id": "scene.bedtime"},
        blocking=True, context=Context(user_id=hass_admin_user.id),
    )
    await advance(hass, freezer, SCENE_SETTLE_S + 1)
    rec = engine.records["light.lamp_a"]
    assert not rec.layers and rec.base.state == "off"
    assert rec.base_source == "user"


def test_the_reproduce_tables_mirror_home_assistants() -> None:
    """scenes.py mirrors light reproduce_state's tables (their shape changed between
    releases, so they are not imported). Where this release has them as pairs, the
    names must match ours; plain names must be ours as they are."""
    from homeassistant.components.light import reproduce_state as ha

    from custom_components.layers import scenes

    def names(table: Any) -> tuple[str, ...]:
        return tuple(str(item[0] if isinstance(item, tuple) else item) for item in table)

    assert names(ha.ATTR_GROUP) == scenes._ATTR_GROUP                   # noqa: SLF001
    assert set(names(ha.COLOR_GROUP)) == set(scenes._COLOR_GROUP)       # noqa: SLF001
    for mode, entry in ha.COLOR_MODE_TO_ATTRIBUTE.items():
        parameter, attribute = scenes._BY_COLOR_MODE[str(mode)]         # noqa: SLF001
        assert (entry.parameter, str(entry.state_attr)) == (parameter, attribute)
