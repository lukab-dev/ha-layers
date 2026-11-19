"""Sensors for dashboards and health checks.

``sensor.layers_status``: ok / pending / failed / shadow.

One sensor per managed lamp (``sensor.layers_<the lamp's object id>``) and per managed
thermostat (``sensor.layers_climate_<its object id>``): its state is the layer on top
(a layer id, ``base`` or ``none``), and its attributes hold the whole stack, top first.
Only the state is recorded, so history shows which layer held the entity when.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LayersConfigEntry
from .const import STATUS_FAILED, STATUS_OK, STATUS_PENDING, STATUS_SHADOW
from .engine import SIGNAL_UPDATE, Engine
from .switch import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: LayersConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    registry = er.async_get(hass)
    engine: Engine = entry.runtime_data.engine
    managed = [*sorted(engine.records), *sorted(engine.climate.records)]
    stacks = [StackSensor(entry, eid, registry.async_get(eid)) for eid in managed]

    # An entity taken out of Layers takes its sensor with it.
    wanted = {stack.unique_id for stack in stacks}
    prefix = f"{entry.entry_id}{STACK_ID_PART}"
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (reg_entry.domain == "sensor" and reg_entry.unique_id.startswith(prefix)
                and reg_entry.unique_id not in wanted):
            registry.async_remove(reg_entry.entity_id)

    async_add_entities([StatusSensor(entry), *stacks])

STACK_ID_PART = "_stack_"

# What a dashboard needs of each layer; the rest is in layers.get and diagnostics.
LAYER_KEYS = ("id", "priority", "mode", "strength", "owner", "command", "source",
              "set_at", "expires_at")


class StatusSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_OK, STATUS_PENDING, STATUS_FAILED, STATUS_SHADOW]
    # The lamp lists change often and say nothing the logbook doesn't; keep them
    # out of the recorder.
    _unrecorded_attributes = frozenset({"pending_since", "layered", "lamps", "climate"})

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


class StackSensor(SensorEntity):
    """Which layer is on top of one lamp or thermostat, and the stack below it."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "stack"
    # The state (the top layer) is worth its history; the stack is not.
    _unrecorded_attributes = frozenset(
        {"layers", "base", "effective", "pending", "diverged", "target_available",
         "untrusted", "policy", "skipped", "would_send"}
    )

    def __init__(
        self, entry: LayersConfigEntry, target: str, target_entry: er.RegistryEntry | None
    ) -> None:
        self._engine: Engine = entry.runtime_data.engine
        self._target = target
        self._target_entry = target_entry
        # Read once per refresh: the state and the attributes are written together.
        self._description: dict[str, Any] | None = None
        # The target's registry id survives a rename; its entity id does not.
        key = target_entry.id if target_entry is not None else target
        self._attr_unique_id = f"{entry.entry_id}{STACK_ID_PART}{key}"
        self._attr_device_info = device_info(entry)
        # A first-time suggestion, so a dashboard can find light.sofa at
        # sensor.layers_sofa and climate.study at sensor.layers_climate_study (a
        # room's lamp and its thermostat often share a name). The registry keeps
        # whatever id the sensor has later.
        domain, object_id = split_entity_id(target)
        middle = "climate_" if domain == "climate" else ""
        self.entity_id = f"sensor.layers_{middle}{object_id}"

    async def async_added_to_hass(self) -> None:
        self._description = self._describe()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._refresh))

    @callback
    def _refresh(self) -> None:
        self._description = self._describe()
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        """The target's own name, as it is now."""
        if (state := self.hass.states.get(self._target)) is not None:
            return state.name
        entry = self._target_entry
        if entry is not None and (named := entry.name or entry.original_name):
            return named
        return split_entity_id(self._target)[1].replace("_", " ").capitalize()

    def _describe(self) -> dict[str, Any] | None:
        return self._engine.describe([self._target]).get(self._target)

    @property
    def native_value(self) -> str | None:
        described = self._description
        return described["active"] if described else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        described = self._description
        if described is None:
            return {"entity_id": self._target}
        attrs = {
            "entity_id": self._target,
            "layers": [
                {key: layer[key] for key in LAYER_KEYS if key in layer}
                for layer in reversed(described["layers"])
            ],
            "base": described["base"],
            "effective": described["effective"],
            "pending": described["pending"],
            "diverged": described["diverged"],
            "target_available": described["available"],
            "untrusted": described["untrusted"],
            "policy": described["policy"],
            "skipped": described["tombstones"],
        }
        if "would_send" in described:      # thermostats: what watch-only held back
            attrs["would_send"] = described["would_send"]
        return attrs
