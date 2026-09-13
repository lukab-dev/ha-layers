"""Diagnostics download: the model, what is in flight, and the last decisions.

A diagnostics file is what people attach to public issues, so user ids and
context ids are redacted.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import LayersConfigEntry

TO_REDACT = {"user_id", "context", "context_id", "parent_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LayersConfigEntry
) -> dict[str, Any]:
    engine = entry.runtime_data.engine
    status, status_attrs = engine.status()
    return async_redact_data(
        {
            "options": dict(entry.options),
            "apply": engine.apply,
            "in_grace": engine.in_grace,
            "status": status,
            "status_attributes": status_attrs,
            "lamps": engine.describe(),
            "stored": engine._data_to_save(),  # noqa: SLF001 — the exact persisted form
            "in_flight": engine.renderer.in_flight(),
            "decisions": list(engine.decisions),
        },
        TO_REDACT,
    )
