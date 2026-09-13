"""sensor.layers_status: ok / pending / failed / shadow, for dashboards and health checks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LayersConfigEntry
from .const import STATUS_FAILED, STATUS_OK, STATUS_PENDING, STATUS_SHADOW
from .engine import SIGNAL_UPDATE, Engine
from .switch import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: LayersConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([StatusSensor(entry)])


class StatusSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_OK, STATUS_PENDING, STATUS_FAILED, STATUS_SHADOW]
    # The lamp lists change often and say nothing the logbook doesn't; keep them
    # out of the recorder.
    _unrecorded_attributes = frozenset({"pending_since", "layered", "lamps"})

    def __init__(self, entry: LayersConfigEntry) -> None:
        self._engine: Engine = entry.runtime_data.engine
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self._engine.status()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._engine.status()[1]
