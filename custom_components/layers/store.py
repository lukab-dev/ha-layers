"""Persistence for Layers: one versioned JSON document in .storage."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION


class LayersStore(Store[dict[str, Any]]):
    """`.storage/layers.state`.

    Layout: ``{"seq": int, "apply": bool, "entities": {entity_id: Record json}}``.
    Only records worth persisting are written (see ``Record.worth_persisting``);
    lamps with nothing layered keep their base in memory.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            minor_version=STORAGE_MINOR_VERSION,
            atomic_writes=True,
        )

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Bring older data up to date; pass a newer minor version through unchanged.

        A newer minor version comes from rolling back to an older release of this
        integration. Its extra fields are ignored on load, so the data is still
        readable: returning it unchanged keeps the lamps' layers instead of losing
        them. A newer major version is refused by Home Assistant before this runs.
        """
        data = dict(old_data)
        data.setdefault("seq", 0)
        data.setdefault("apply", False)
        data.setdefault("entities", {})
        return data
