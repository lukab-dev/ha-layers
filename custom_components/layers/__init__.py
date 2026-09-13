"""Layers: priority layers for Home Assistant lights.

Each managed lamp has a base and a stack of named layers holding live commands.
Removing a layer makes the lamp fall through to whatever is below it now. When
something else changes a lamp, a per-lamp policy decides what that means; by
default the change becomes the lamp's base and the layers above it are dropped.
See docs/SPEC.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ENTITIES, DOMAIN
from .engine import Engine
from .services import async_register_services
from .store import LayersStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR, Platform.SWITCH]


@dataclass
class LayersData:
    engine: Engine


type LayersConfigEntry = ConfigEntry[LayersData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once; they route to the loaded entry."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LayersConfigEntry) -> bool:
    engine = Engine(hass, entry)
    await engine.async_start()
    entry.runtime_data = LayersData(engine)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _delete_issues(hass: HomeAssistant, entry: LayersConfigEntry) -> None:
    """A failed render's repair issue belongs to the engine that raised it."""
    for entity_id in entry.options.get(CONF_ENTITIES, []):
        ir.async_delete_issue(hass, DOMAIN, f"render_failed_{entity_id}")


async def async_unload_entry(hass: HomeAssistant, entry: LayersConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.engine.async_stop()
        _delete_issues(hass, entry)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: LayersConfigEntry) -> None:
    """Removing Layers removes what it stored: a reinstall starts clean."""
    _delete_issues(hass, entry)
    hass.data.get(DOMAIN, {}).clear()
    await LayersStore(hass).async_remove()
