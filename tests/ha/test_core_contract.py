"""The Home Assistant behaviour Layers is built on (plan: test_core_contract.py).

None of this is Layers' code. If a Home Assistant release changes one of these,
this file fails first and names the assumption, instead of a behaviour test
failing somewhere downstream for a reason that is hard to see.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity as entity_helper
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from pytest_homeassistant_custom_component.common import MockUser, async_capture_events

from .conftest import FakeLamp

A = "light.lamp_a"


def test_context_reuse_is_five_seconds() -> None:
    """A state written within this long of a service call carries the call's context
    (classify: our context on someone else's write; engine: OURS_KEEP_S = 10)."""
    assert entity_helper.CONTEXT_RECENT_TIME_SECONDS == 5


async def test_target_selection_and_group_expansion(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """targets.py: TargetSelection from a service call's data; expand_group expands
    old-style group.* entities but not light groups (Layers expands those itself)."""
    assert "expand_group" in inspect.signature(async_extract_referenced_entity_ids).parameters
    await hass.services.async_call("group", "set", {"object_id": "den", "entities": [A]},
                                   blocking=True)
    selection = TargetSelection({"entity_id": ["group.den", "light.group_ab"]})
    selected = async_extract_referenced_entity_ids(hass, selection, expand_group=True)
    assert selected.referenced == {A, "light.group_ab"}
    assert TargetSelection({"entity_id": "none"}).entity_ids == set()


async def test_the_call_service_event(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_read_only_user: MockUser
) -> None:
    """engine._on_call: the event carries domain / service / service_data, fires with the
    caller's context, and fires before the light service checks the caller's permissions."""
    seen = async_capture_events(hass, EVENT_CALL_SERVICE)
    ctx = Context(parent_id="parent")
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 10},
                                   blocking=True, context=ctx)
    event = seen[-1]
    assert {"domain", "service", "service_data"} <= set(event.data)
    assert event.data["service_data"] == {"entity_id": A, "brightness": 10}
    assert event.context is ctx

    seen.clear()
    with pytest.raises(Unauthorized):
        await hass.services.async_call("light", "turn_off", {"entity_id": A}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    assert [e.data["service"] for e in seen] == ["turn_off"]     # fired, then refused


async def test_looking_up_a_user_does_not_suspend(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    """engine._on_user_call runs as an eager task: the permission check completes inside
    the call event, before the service writes any state, as long as this holds."""

    async def lookup() -> Any:
        return await hass.auth.async_get_user(hass_admin_user.id)

    task = hass.async_create_task(lookup(), eager_start=True)
    assert task.done() and task.result() is not None


def test_background_tasks_can_start_lazily() -> None:
    """render.py registers a render task before it runs (eager_start=False)."""
    params = inspect.signature(ConfigEntry.async_create_background_task).parameters
    assert "eager_start" in params


async def test_a_longer_delayed_save_postpones_a_pending_sooner_one(hass: HomeAssistant) -> None:
    """Why Engine.save never asks for a longer delay while a sooner write is pending: the
    Store keeps one timer, and a later call with a longer delay pushes the pending write
    out to it. (If this starts failing, Home Assistant fixed it; Engine.save still works.)"""
    store: Store[dict[str, Any]] = Store(hass, 1, "layers_contract_probe")
    store.async_delay_save(lambda: {"n": 1}, 0)
    store.async_delay_save(lambda: {"n": 2}, 60)
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert store._data is not None, "the immediate write happened after all"    # noqa: SLF001
    store._async_cleanup_delay_listener()                                        # noqa: SLF001
    store._async_cleanup_final_write_listener()                                  # noqa: SLF001
    # store.py overrides the migration hook with this signature.
    assert list(inspect.signature(Store._async_migrate_func).parameters) == [
        "self", "old_major_version", "old_minor_version", "old_data"]


async def test_a_rename_reports_the_old_entity_id(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """engine._on_registry_update follows a rename through old_entity_id."""
    seen: list[Event] = []

    @callback
    def _seen(event: Event) -> None:
        seen.append(event)

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _seen)
    er.async_get(hass).async_update_entity(A, new_entity_id="light.lamp_renamed")
    await hass.async_block_till_done()
    assert any(e.data.get("action") == "update" and e.data.get("old_entity_id") == A
               and e.data.get("entity_id") == "light.lamp_renamed" for e in seen)
