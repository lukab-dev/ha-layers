"""Describe Layers' events, so Activity says which layer changed a lamp and why.

Each render fires ``layers_render`` with the render's own context just before
the light call, so the lamp's Activity row is attributed to it: "Layers: tv
(tv_dim) set" instead of an anonymous "action light.turn_on".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_ENTITY_ID, LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_EXTERNAL, EVENT_RENDER, EVENT_RENDER_FAILED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    @callback
    def describe_render(event: Event) -> dict[str, Any]:
        data = event.data
        layer = data.get("layer") or "base"
        owner = f" ({data['owner']})" if data.get("owner") else ""
        return {
            LOGBOOK_ENTRY_NAME: "Layers",
            LOGBOOK_ENTRY_MESSAGE: f"{layer}{owner}: {data.get('reason', 'set')}",
            LOGBOOK_ENTRY_ENTITY_ID: data.get(ATTR_ENTITY_ID),
        }

    @callback
    def describe_failed(event: Event) -> dict[str, Any]:
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: "Layers",
            LOGBOOK_ENTRY_MESSAGE: f"could not set it after {data.get('attempts')} attempts",
            LOGBOOK_ENTRY_ENTITY_ID: data.get(ATTR_ENTITY_ID),
        }

    @callback
    def describe_external(event: Event) -> dict[str, Any]:
        data = event.data
        dropped = ", ".join(data.get("dropped") or []) or "nothing"
        edited = f", edited {data['edited']}" if data.get("edited") else ""
        return {
            LOGBOOK_ENTRY_NAME: "Layers",
            LOGBOOK_ENTRY_MESSAGE: f"changed by {data.get('source')}: dropped {dropped}{edited}",
            LOGBOOK_ENTRY_ENTITY_ID: data.get(ATTR_ENTITY_ID),
        }

    async_describe_event(DOMAIN, EVENT_RENDER, describe_render)
    async_describe_event(DOMAIN, EVENT_RENDER_FAILED, describe_failed)
    async_describe_event(DOMAIN, EVENT_EXTERNAL, describe_external)
