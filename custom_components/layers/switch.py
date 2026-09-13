"""switch.layers_apply: whether Layers sends commands at all.

Off is observe-only: layers are still set, cleared and classified, but nothing is
sent and lamps whose command changed are marked unsynced. Turning it on sends
nothing by itself; ``layers.sync`` pushes the model onto the lamps. The state is
kept in Layers' own store (saved immediately), not in the restore cache, so a
crash cannot silently turn commanding back on and a fresh install starts off.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LayersConfigEntry
from .const import DOMAIN
from .engine import SIGNAL_UPDATE, Engine


def device_info(entry: LayersConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Layers",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: LayersConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ApplySwitch(entry)])


class ApplySwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "apply"
    _attr_should_poll = False

    def __init__(self, entry: LayersConfigEntry) -> None:
        self._engine: Engine = entry.runtime_data.engine
        self._attr_unique_id = f"{entry.entry_id}_apply"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._engine.apply

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._engine.async_set_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._engine.async_set_apply(False)
