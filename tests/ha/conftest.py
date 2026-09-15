"""Test harness for the Home Assistant side of Layers.

Lamps are real ``LightEntity`` objects behind the real ``light`` domain, so every
test goes through Home Assistant's own ``light.turn_on`` handling: unavailable
entities silently dropped, ``brightness_pct`` converted, unsupported attributes
filtered, and the 5-second context reuse on state writes. Mocking the service
would skip exactly the behaviour Layers has to cope with.

A ``FakeLamp`` can also misbehave the way real lamps do: report late, revert a
command with no context seconds after acknowledging it (a vendor bridge), ignore
commands, drop off the network, or desaturate hs/rgb colours.

Run separately from the pure logic tests:  pytest tests/ha
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import pytest

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import color as color_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from custom_components.layers import render as render_module
from custom_components.layers.const import (
    CONF_BASE_KEEP,
    CONF_DEFAULT_POLICY,
    CONF_EDIT_ACTIVE,
    CONF_ENTITIES,
    CONF_REASSERT,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/layers."""


@pytest.fixture(autouse=True)
def fast_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Renders wait in real time; shrink the waits so tests run in milliseconds."""
    monkeypatch.setattr(render_module, "SETTLE_S", 0.01)
    monkeypatch.setattr(render_module, "SLOW_OFF_S", 0.02)
    monkeypatch.setattr(render_module, "LATE_RECHECK_S", 0.05)
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (0.01, 0.01, 0.01, 0.01, 0.01, 0.01))


class FakeLamp(LightEntity):
    """A lamp with configurable capabilities and real-world misbehaviour."""

    _attr_should_poll = False

    def __init__(
        self,
        object_id: str,
        *,
        modes: Iterable[ColorMode] = (ColorMode.COLOR_TEMP, ColorMode.XY),
        min_kelvin: int = 2200,
        max_kelvin: int = 6500,
        transition: bool = True,
        on: bool = False,
        brightness: int = 128,
    ) -> None:
        self.entity_id = f"light.{object_id}"
        self._attr_unique_id = object_id
        self._attr_name = object_id.replace("_", " ").title()
        self._attr_supported_color_modes = set(modes)
        self._attr_min_color_temp_kelvin = min_kelvin
        self._attr_max_color_temp_kelvin = max_kelvin
        if transition:
            self._attr_supported_features = LightEntityFeature.TRANSITION
        self._attr_is_on = on
        self._attr_brightness = brightness if on else None
        mode = next(iter(sorted(self._attr_supported_color_modes)))
        self._attr_color_mode = mode
        self._attr_color_temp_kelvin = 2700 if ColorMode.COLOR_TEMP in self._attr_supported_color_modes else None
        self._attr_xy_color = None
        self._attr_hs_color = None
        self._last_brightness = brightness
        # misbehaviour switches
        self.ignore_commands = False          # acknowledge, then do nothing
        self.revert_after: float | None = None  # revert on/off with no context after N seconds
        self.report_after: float | None = None  # take a command at once, report it N s later
        self.desaturate_hs = False            # the Matter hs trap
        self.calls: list[tuple[str, dict[str, Any], Context | None]] = []

    # -- commands

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.calls.append(("turn_on", dict(kwargs), self._context))
        if self.ignore_commands:
            return
        was_on = self._attr_is_on
        self._attr_is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            self._last_brightness = kwargs[ATTR_BRIGHTNESS]
        elif not was_on:
            self._attr_brightness = self._last_brightness
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
        if ATTR_XY_COLOR in kwargs:
            self._attr_color_mode = ColorMode.XY
            self._attr_xy_color = tuple(kwargs[ATTR_XY_COLOR])
        if ATTR_HS_COLOR in kwargs:
            h, s = kwargs[ATTR_HS_COLOR]
            if self.desaturate_hs:
                s = s * 0.4
            self._attr_color_mode = ColorMode.XY if ColorMode.XY in self._attr_supported_color_modes else ColorMode.HS
            self._attr_hs_color = (h, s)
            self._attr_xy_color = color_util.color_hs_to_xy(h, s)
        self._report()
        self._maybe_revert(was_on)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.calls.append(("turn_off", dict(kwargs), self._context))
        if self.ignore_commands:
            return
        was_on = self._attr_is_on
        self._attr_is_on = False
        self._report()
        self._maybe_revert(was_on)

    def _report(self) -> None:
        """Write the new state now, or ``report_after`` seconds later (it then shows
        whatever the lamp holds by then, under whichever context HA still reuses)."""
        if self.report_after is None:
            self.async_write_ha_state()
        else:
            self.hass.loop.call_later(self.report_after, self.async_write_ha_state)

    def _maybe_revert(self, was_on: bool) -> None:
        if self.revert_after is None or was_on == self._attr_is_on:
            return
        delay, self.revert_after = self.revert_after, None

        def _revert() -> None:
            self._attr_is_on = was_on
            self.push(context=Context())

        self.hass.loop.call_later(delay, _revert)

    # -- things the outside world does to the lamp

    def push(self, *, context: Context | None = None, **attrs: Any) -> None:
        """Write the lamp's state as if the device reported it (no context by default)."""
        for key, value in attrs.items():
            setattr(self, f"_attr_{key}", value)
        self.async_set_context(context or Context())
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        self._attr_available = available
        self.async_set_context(Context())
        self.async_write_ha_state()


class FakeSwitch(SwitchEntity):
    """A relay or a smart plug: on/off only, with the same misbehaviour switches."""

    _attr_should_poll = False

    def __init__(self, object_id: str, *, on: bool = False) -> None:
        self.entity_id = f"switch.{object_id}"
        self._attr_unique_id = object_id
        self._attr_name = object_id.replace("_", " ").title()
        self._attr_is_on = on
        self.ignore_commands = False
        self.revert_after: float | None = None
        self.calls: list[tuple[str, dict[str, Any], Context | None]] = []

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.calls.append(("turn_on", dict(kwargs), self._context))
        if self.ignore_commands:
            return
        was_on = self._attr_is_on
        self._attr_is_on = True
        self.async_write_ha_state()
        self._maybe_revert(was_on)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.calls.append(("turn_off", dict(kwargs), self._context))
        if self.ignore_commands:
            return
        was_on = self._attr_is_on
        self._attr_is_on = False
        self.async_write_ha_state()
        self._maybe_revert(was_on)

    def _maybe_revert(self, was_on: bool) -> None:
        if self.revert_after is None or was_on == self._attr_is_on:
            return
        delay, self.revert_after = self.revert_after, None

        def _revert() -> None:
            self._attr_is_on = was_on
            self.push(context=Context())

        self.hass.loop.call_later(delay, _revert)

    def push(self, *, context: Context | None = None, **attrs: Any) -> None:
        """Write the switch's state as if the device reported it (no context by default)."""
        for key, value in attrs.items():
            setattr(self, f"_attr_{key}", value)
        self.async_set_context(context or Context())
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        self._attr_available = available
        self.async_set_context(Context())
        self.async_write_ha_state()


@pytest.fixture
def plugs() -> dict[str, FakeSwitch]:
    return {
        "relay": FakeSwitch("relay", on=True),
        "plug": FakeSwitch("plug", on=False),
    }


@pytest.fixture
async def switches(hass: HomeAssistant, plugs: dict[str, FakeSwitch]) -> dict[str, FakeSwitch]:
    """The fake switches behind the real switch domain."""
    setup_test_component_platform(hass, "switch", list(plugs.values()))
    assert await async_setup_component(hass, "switch", {"switch": [{"platform": "test"}]})
    await hass.async_block_till_done()
    return plugs


@pytest.fixture
def lamps() -> dict[str, FakeLamp]:
    return {
        "a": FakeLamp("lamp_a", on=True, brightness=102),
        "b": FakeLamp("lamp_b", modes=(ColorMode.BRIGHTNESS,), on=True, brightness=200),
        "c": FakeLamp("lamp_c", on=False),
        "d": FakeLamp("lamp_d", modes=(ColorMode.COLOR_TEMP,), min_kelvin=2202, on=False),
    }


@pytest.fixture
async def lights(hass: HomeAssistant, lamps: dict[str, FakeLamp]) -> dict[str, FakeLamp]:
    """The fake lamps behind the real light domain, plus a light group of a+b."""
    setup_test_component_platform(hass, "light", list(lamps.values()))
    assert await async_setup_component(
        hass,
        "light",
        {"light": [{"platform": "test"},
                   {"platform": "group", "name": "Group AB",
                    "entities": ["light.lamp_a", "light.lamp_b"]}]},
    )
    await hass.async_block_till_done()
    return lamps


async def setup_layers(
    hass: HomeAssistant,
    entities: list[str],
    *,
    policy: str = "take_back",
    edit_active: list[str] | None = None,
    base_keep: list[str] | None = None,
    reassert: list[str] | None = None,
    apply: bool = True,
    end_grace: bool = True,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Layers",
        data={},
        options={
            CONF_ENTITIES: entities,
            CONF_DEFAULT_POLICY: policy,
            CONF_EDIT_ACTIVE: edit_active or [],
            CONF_BASE_KEEP: base_keep or [],
            CONF_REASSERT: reassert or [],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine
    if apply:
        await engine.async_set_apply(True)
    if end_grace:
        engine._end_grace()  # noqa: SLF001 — skip the settle wait in tests
    return entry


async def settle(hass: HomeAssistant, seconds: float = 0.2) -> None:
    """Let background renders finish (they sleep for real, briefly, in tests)."""
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)
        await asyncio.sleep(seconds / 3)
    await hass.async_block_till_done(wait_background_tasks=True)
