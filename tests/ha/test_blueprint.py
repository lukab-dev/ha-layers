"""The media-dim blueprint, run as a real automation against real Layers."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.layers.const import DOMAIN

from .conftest import FakeLamp, settle, setup_layers

BLUEPRINT = Path(__file__).parents[2] / "blueprints" / "automation" / "layers" / "media_dim.yaml"
PLAYER = "media_player.tv"


async def _setup(hass: HomeAssistant, *, booting: bool = False) -> None:
    """Layers and the automation. ``booting``: set the automation up before Home
    Assistant starts, as a real boot does, so its `homeassistant: start` trigger is armed."""
    dest = Path(hass.config.path("blueprints/automation/layers/media_dim.yaml"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, dest)
    hass.states.async_set(PLAYER, "idle")
    await setup_layers(hass, ["light.lamp_a", "light.lamp_b", "light.lamp_c"])
    if booting:
        # A layer stored from before the restart; the film ended while Home Assistant was down.
        await hass.services.async_call(
            DOMAIN, "set",
            {"entity_id": "light.lamp_a", "layer": "tv", "priority": 40, "state": "off"},
            blocking=True,
        )
        await settle(hass)
        hass.set_state(CoreState.not_running)
    assert await async_setup_component(hass, "automation", {"automation": {
        "id": "tv_dim",
        "alias": "TV dim",
        "use_blueprint": {
            "path": "layers/media_dim.yaml",
            "input": {
                "player": PLAYER,
                "lights_off": ["light.lamp_a"],
                "lights_dim": ["light.lamp_b", "light.lamp_c"],
            },
        },
    }})
    await hass.async_block_till_done()
    if booting:
        await hass.async_start()
        await settle(hass)
    assert hass.states.get("automation.tv_dim").state == "on"


async def _after(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await settle(hass)


async def _layers(hass: HomeAssistant, entity_id: str) -> list[str]:
    result = await hass.services.async_call(DOMAIN, "get", {"entity_id": entity_id},
                                            blocking=True, return_response=True)
    return [layer["id"] for layer in result["entities"][entity_id]["layers"]]


async def test_a_film_dims_the_room_and_its_end_gives_it_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await _setup(hass)
    hass.states.async_set(PLAYER, "playing")
    await settle(hass)
    assert hass.states.get("light.lamp_a").state == "on"     # not yet: 15 s first

    await _after(hass, 16)
    assert hass.states.get("light.lamp_a").state == "off"
    assert hass.states.get("light.lamp_b").attributes["brightness"] == 64   # 25 %
    assert hass.states.get("light.lamp_c").state == "off"    # was off: adjust never lights it
    assert "tv" in await _layers(hass, "light.lamp_a")

    hass.states.async_set(PLAYER, "paused")
    await _after(hass, 60)
    assert hass.states.get("light.lamp_a").state == "off"    # a pause is not the end

    await _after(hass, 121)
    a, b = hass.states.get("light.lamp_a"), hass.states.get("light.lamp_b")
    assert a.state == "on" and a.attributes["brightness"] == 102
    assert b.state == "on" and b.attributes["brightness"] == 200
    assert not await _layers(hass, "light.lamp_a")


async def test_a_notification_above_the_film_falls_back_to_the_film(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await _setup(hass)
    hass.states.async_set(PLAYER, "playing")
    await _after(hass, 16)
    await hass.services.async_call(
        DOMAIN, "set",
        {"entity_id": "light.lamp_a", "layer": "washer", "priority": 70, "state": "on",
         "xy_color": [0.679, 0.318], "brightness_pct": 100},
        blocking=True,
    )
    await settle(hass)
    assert hass.states.get("light.lamp_a").state == "on"
    await hass.services.async_call(DOMAIN, "clear", {"layer": "washer"}, blocking=True)
    await settle(hass)
    assert hass.states.get("light.lamp_a").state == "off"    # back to the film, not to 40 %


async def test_a_restart_after_the_film_ended_clears_the_stored_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await _setup(hass, booting=True)
    assert not await _layers(hass, "light.lamp_a")
    assert hass.states.get("light.lamp_a").state == "on"


async def test_a_stop_with_nothing_layered_is_harmless(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await _setup(hass)
    hass.states.async_set(PLAYER, "off")
    await _after(hass, 121)
    assert hass.states.get("light.lamp_a").state == "on"
    assert lights["a"].calls == []
