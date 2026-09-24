"""The parts of Layers a person or an automation touches directly.

Config flow and options flow, the service schemas and every rejection they make,
not-loaded, target expansion, the per-lamp results, ``layer: base`` / ``layer:
active``, ``only_if_present``, the priority errors (raised before any lamp is
touched), ``clear`` / ``sync`` / ``get``, diagnostics redaction, the status sensor
and the apply switch.

Lamps are the made-up ones from conftest.py (lamp_a colour-temp + xy on at 102,
lamp_b brightness-only on at 200, lamp_c off, lamp_d colour-temp-only off, and
group_ab, a Home Assistant light group of a and b). This module adds three
vendor-style group lights for the rejection and expansion tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
import voluptuous as vol

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.json import json_dumps
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockUser, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.layers import render as render_module
from custom_components.layers.const import (
    CONF_BASE_KEEP,
    CONF_DEFAULT_POLICY,
    CONF_EDIT_ACTIVE,
    CONF_ENTITIES,
    CONF_REASSERT,
    DOMAIN,
    STALE_UNAVAILABLE_S,
)
from custom_components.layers.logic.model import Color, Command

from .conftest import FakeLamp, settle, setup_layers

A, B, C, D = "light.lamp_a", "light.lamp_b", "light.lamp_c", "light.lamp_d"
GROUP = "light.group_ab"          # a Home Assistant light group (platform "group")
ROOM = "light.room_x"             # a vendor room: members in `entity_id`, flagged is_hue_group
ZONE = "light.zone_y"             # a vendor group flagged is_hue_group, no member list
BUNDLE = "light.bundle_z"         # a vendor group listing members in `group_entities`
GHOST = "light.ghost"             # no state at all
SWITCH = "switch.layers_apply"
STATUS = "sensor.layers_status"

A_BASE = Command("on", 102, Color.kelvin(2700))   # what lamp_a shows when set up
REDACTED = "**REDACTED**"


# --------------------------------------------------------------------------- fixtures


class FakeGroupLight(FakeLamp):
    """A vendor-style group light: an ordinary light entity whose attributes mark it as a group."""

    def __init__(self, object_id: str, extra: dict[str, Any]) -> None:
        super().__init__(object_id, on=False)
        self._attr_extra_state_attributes = dict(extra)


@pytest.fixture
def lamps(lamps: dict[str, FakeLamp]) -> dict[str, FakeLamp]:
    """The conftest lamps plus three vendor-style group lights (never enrolled)."""
    return {
        **lamps,
        "room": FakeGroupLight("room_x", {"entity_id": [A, C], "is_hue_group": True}),
        "zone": FakeGroupLight("zone_y", {"is_hue_group": True}),
        "bundle": FakeGroupLight("bundle_z", {"group_entities": [B]}),
    }


# --------------------------------------------------------------------------- helpers


async def start(hass: HomeAssistant, entities: list[str], **kwargs: Any):
    """setup_layers, with the grace timer it leaves behind cancelled (it already ended the
    grace; letting the timer fire later would run _end_grace a second time)."""
    entry = await setup_layers(hass, entities, **kwargs)
    engine = entry.runtime_data.engine
    engine._cancel("grace", "")  # noqa: SLF001
    return entry, engine


async def layers(hass: HomeAssistant, service: str, data: dict[str, Any] | None = None, *,
                 context: Context | None = None) -> dict[str, str]:
    """Call layers.<service> and return its per-lamp results."""
    result = await hass.services.async_call(
        DOMAIN, service, data or {}, blocking=True, context=context, return_response=True
    )
    return result["entities"]


async def get(hass: HomeAssistant, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return await hass.services.async_call(DOMAIN, "get", data or {}, blocking=True,
                                          return_response=True)


async def person_turns_on(hass: HomeAssistant, user: MockUser, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id, **data},
        blocking=True, context=Context(user_id=user.id),
    )


async def switch(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": SWITCH}, blocking=True
    )


def state_of(hass: HomeAssistant, entity_id: str) -> tuple[str, int | None]:
    st = hass.states.get(entity_id)
    return st.state, st.attributes.get("brightness")


async def user_flow(hass: HomeAssistant, entities: list[str], policy: str = "take_back"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITIES: entities, CONF_DEFAULT_POLICY: policy}
    )


async def options_flow(hass: HomeAssistant, entry, entities: list[str], *, policy: str = "take_back",
                       edit: list[str] | None = None, keep: list[str] | None = None):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    return await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ENTITIES: entities, CONF_DEFAULT_POLICY: policy,
         CONF_EDIT_ACTIVE: edit or [], CONF_BASE_KEEP: keep or []},
    )


# =========================================================================== config flow


async def test_config_flow_creates_an_observe_only_entry(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    result = await user_flow(hass, [A, B], policy="edit_active")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Layers"
    assert result["data"] == {}
    assert result["options"] == {
        CONF_ENTITIES: [A, B],
        CONF_DEFAULT_POLICY: "edit_active",
        CONF_EDIT_ACTIVE: [],
        CONF_BASE_KEEP: [],
        CONF_REASSERT: [],
    }
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    engine = entry.runtime_data.engine
    assert engine.enrolled == {A, B}
    assert engine.policy_for(A) == "edit_active"
    # A fresh store starts with commanding switched off (SPEC 7.5).
    assert engine.apply is False
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(STATUS).state == "shadow"


async def test_config_flow_accepts_a_lamp_that_is_away(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """An unavailable lamp is still a lamp: it has a state, just not a useful one right now."""
    lights["c"].set_available(False)
    await hass.async_block_till_done()
    result = await user_flow(hass, [A, C])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_config_flow_is_single_instance(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await start(hass, [A])
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize(
    "bad",
    [GROUP, ROOM, ZONE, BUNDLE, GHOST],
    ids=["ha_light_group", "vendor_room_with_members", "is_hue_group_only", "group_entities", "no_state"],
)
async def test_config_flow_rejects_what_is_not_a_single_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], bad: str
) -> None:
    result = await user_flow(hass, [A, bad])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITIES: "not_a_lamp"}
    assert result["description_placeholders"]["entities"] == bad
    assert hass.config_entries.async_entries(DOMAIN) == []
    # The same flow goes through once the offender is taken out.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITIES: [A], CONF_DEFAULT_POLICY: "take_back"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_config_flow_names_every_offender(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    result = await user_flow(hass, [GROUP, A, GHOST])
    assert result["errors"] == {CONF_ENTITIES: "not_a_lamp"}
    assert set(result["description_placeholders"]["entities"].split(", ")) == {GROUP, GHOST}


# =========================================================================== options flow


async def test_options_saving_reloads_the_entry(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    entry, old = await start(hass, [A])
    result = await options_flow(hass, entry, [A, B], policy="base_keep_layers", edit=[B])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {
        CONF_ENTITIES: [A, B],
        CONF_DEFAULT_POLICY: "base_keep_layers",
        CONF_EDIT_ACTIVE: [B],
        CONF_BASE_KEEP: [],
        CONF_REASSERT: [],
    }
    assert entry.state is ConfigEntryState.LOADED
    engine = entry.runtime_data.engine
    assert engine is not old, "saving the options must reload the entry"
    assert engine.enrolled == {A, B}
    assert set(engine.records) == {A, B}
    assert engine.policy_for(A) == "base_keep_layers"
    assert engine.policy_for(B) == "edit_active"
    assert engine.apply is True   # the apply switch lives in the store and survives the reload


async def test_options_refuse_to_unenroll_a_lamp_holding_a_layer(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    entry, engine = await start(hass, [A, B])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass)

    result = await options_flow(hass, entry, [B])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITIES: "holds_layers"}
    assert result["description_placeholders"]["entities"] == A
    assert entry.options[CONF_ENTITIES] == [A, B]
    assert entry.runtime_data.engine is engine   # nothing saved, nothing reloaded
    assert "hold" in engine.records[A].layers

    # Un-enrolling a lamp without layers is fine, and so is A once its layer is cleared.
    await layers(hass, "clear", {"layer": "hold"})
    await settle(hass)
    result = await options_flow(hass, entry, [B])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.engine.enrolled == {B}


@pytest.mark.parametrize(("field", "kwargs"), [
    (CONF_EDIT_ACTIVE, {"edit": [C]}),
    (CONF_BASE_KEEP, {"keep": [C]}),
])
async def test_options_policy_lists_must_hold_enrolled_lamps(
    hass: HomeAssistant, lights: dict[str, FakeLamp], field: str, kwargs: dict[str, list[str]]
) -> None:
    entry, engine = await start(hass, [A, B])
    result = await options_flow(hass, entry, [A, B], **kwargs)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: "not_enrolled"}
    assert result["description_placeholders"]["entities"] == C
    assert entry.runtime_data.engine is engine


async def test_options_a_lamp_that_leaves_the_list_must_leave_the_policy_lists(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    entry, _ = await start(hass, [A, B], edit_active=[B])
    result = await options_flow(hass, entry, [A], edit=[B])
    assert result["errors"] == {CONF_EDIT_ACTIVE: "not_enrolled"}


async def test_options_a_lamp_cannot_have_both_policies(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    entry, engine = await start(hass, [A, B])
    result = await options_flow(hass, entry, [A, B], edit=[A], keep=[A, B])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BASE_KEEP: "both_policies"}
    assert result["description_placeholders"]["entities"] == A
    assert entry.runtime_data.engine is engine


async def test_options_reject_groups_too(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    entry, engine = await start(hass, [A])
    result = await options_flow(hass, entry, [A, ROOM])
    assert result["errors"] == {CONF_ENTITIES: "not_a_lamp"}
    assert result["description_placeholders"]["entities"] == ROOM
    assert entry.runtime_data.engine is engine


# =========================================================================== service schemas

FUTURE = "2099-01-01T00:00:00+00:00"

INVALID_SET = {
    # base / active take no priority, expiry or only_if_present
    "base_with_priority": {"layer": "base", "priority": 10, "brightness": 50},
    "base_with_ttl": {"layer": "base", "ttl": 60, "brightness": 50},
    "base_with_until": {"layer": "base", "until": FUTURE, "brightness": 50},
    "base_only_if_present": {"layer": "base", "only_if_present": True, "brightness": 50},
    "active_with_priority": {"layer": "active", "priority": 10, "brightness": 50},
    "active_with_ttl": {"layer": "active", "ttl": 60, "brightness": 50},
    "active_with_until": {"layer": "active", "until": FUTURE, "brightness": 50},
    "active_only_if_present": {"layer": "active", "only_if_present": True, "brightness": 50},
    # adjust: attributes only, and at least one
    "adjust_with_state": {"layer": "dim", "priority": 10, "mode": "adjust", "state": "on",
                          "brightness": 50},
    "adjust_without_attributes": {"layer": "dim", "priority": 10, "mode": "adjust"},
    "adjust_with_only_a_transition": {"layer": "dim", "priority": 10, "mode": "adjust",
                                      "transition": 2},
    # off takes nothing else
    "off_with_brightness": {"layer": "hold", "priority": 10, "state": "off", "brightness": 50},
    "off_with_brightness_pct": {"layer": "hold", "priority": 10, "state": "off", "brightness_pct": 20},
    "off_with_colour": {"layer": "hold", "priority": 10, "state": "off", "xy_color": [0.5, 0.4]},
    # a set with nothing to set
    "set_with_nothing": {"layer": "hold", "priority": 10},
    "set_with_only_a_transition": {"layer": "hold", "priority": 10, "transition": 1},
    "base_with_nothing": {"layer": "base"},
    # one brightness, one colour, one expiry
    "brightness_and_pct": {"layer": "hold", "priority": 10, "brightness": 50, "brightness_pct": 20},
    "kelvin_and_xy": {"layer": "hold", "priority": 10, "color_temp_kelvin": 2700,
                      "xy_color": [0.5, 0.4]},
    "hs_and_rgb": {"layer": "hold", "priority": 10, "hs_color": [10, 50], "rgb_color": [255, 0, 0]},
    "xy_and_hs": {"layer": "hold", "priority": 10, "xy_color": [0.5, 0.4], "hs_color": [10, 50]},
    "ttl_and_until": {"layer": "hold", "priority": 10, "state": "off", "ttl": 60, "until": FUTURE},
    # layer ids
    "id_uppercase": {"layer": "Hold", "priority": 10, "state": "off"},
    "id_with_space": {"layer": "tv dim", "priority": 10, "state": "off"},
    "id_with_dash": {"layer": "tv-dim", "priority": 10, "state": "off"},
    "id_too_long": {"layer": "x" * 33, "priority": 10, "state": "off"},
    "id_empty": {"layer": "", "priority": 10, "state": "off"},
    "id_missing": {"priority": 10, "state": "off"},
    # ranges and choices
    "priority_0": {"layer": "hold", "priority": 0, "state": "off"},
    "priority_100": {"layer": "hold", "priority": 100, "state": "off"},
    "unknown_mode": {"layer": "hold", "priority": 10, "mode": "blend", "brightness": 50},
    "unknown_state": {"layer": "hold", "priority": 10, "state": "dim"},
    "brightness_0": {"layer": "hold", "priority": 10, "brightness": 0},
    "brightness_256": {"layer": "hold", "priority": 10, "brightness": 256},
    "brightness_pct_0": {"layer": "hold", "priority": 10, "brightness_pct": 0},
    "xy_out_of_range": {"layer": "hold", "priority": 10, "xy_color": [1.2, 0.3]},
    "xy_three_values": {"layer": "hold", "priority": 10, "xy_color": [0.3, 0.3, 0.3]},
    "kelvin_too_low": {"layer": "hold", "priority": 10, "color_temp_kelvin": 500},
    "on_expire_unknown": {"layer": "hold", "priority": 10, "state": "off", "on_expire": "never"},
    "owner_too_long": {"layer": "hold", "priority": 10, "state": "off", "owner": "o" * 65},
    "negative_ttl": {"layer": "hold", "priority": 10, "state": "off", "ttl": -5},
    "unknown_field": {"layer": "hold", "priority": 10, "state": "off", "effect": "rainbow"},
    # the remaining bounds (review 4 #16)
    "transition_over_300": {"layer": "hold", "priority": 10, "state": "off", "transition": 301},
    "negative_transition": {"layer": "hold", "priority": 10, "state": "off", "transition": -1},
    "hue_over_360": {"layer": "hold", "priority": 10, "hs_color": [361, 50]},
    "saturation_over_100": {"layer": "hold", "priority": 10, "hs_color": [10, 101]},
    "rgb_two_values": {"layer": "hold", "priority": 10, "rgb_color": [255, 0]},
    "rgb_over_255": {"layer": "hold", "priority": 10, "rgb_color": [256, 0, 0]},
    "kelvin_too_high": {"layer": "hold", "priority": 10, "color_temp_kelvin": 12001},
    "brightness_pct_over_100": {"layer": "hold", "priority": 10, "brightness_pct": 101},
}


@pytest.mark.parametrize("data", list(INVALID_SET.values()), ids=list(INVALID_SET))
async def test_set_schema_rejects(
    hass: HomeAssistant, lights: dict[str, FakeLamp], data: dict[str, Any]
) -> None:
    _, engine = await start(hass, [A])
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "set", {"entity_id": A, **data}, blocking=True)
    rec = engine.records[A]
    assert rec.layers == {} and rec.base == A_BASE
    assert lights["a"].calls == []


async def test_set_needs_a_target(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await start(hass, [A])
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "set", {"layer": "hold", "priority": 10, "state": "off"}, blocking=True
        )


@pytest.mark.parametrize(("service", "data"), [
    ("clear", {"layer": "Hold"}),
    ("clear", {"layer": "tv dim"}),
    ("clear", {"layer": ""}),
    ("clear", {}),
    ("clear", {"layer": "hold", "priority": 10}),
    ("clear", {"layer": "hold", "transition": 301}),
    ("clear", {"layer": "hold", "transition": -1}),
    ("clear", {"layer": "base"}),
    ("sync", {"layer": "hold"}),
    ("get", {"layer": "hold"}),
], ids=["clear_uppercase", "clear_space", "clear_empty", "clear_no_layer", "clear_extra_field",
        "clear_transition_over_300", "clear_negative_transition", "clear_base",
        "sync_extra_field", "get_extra_field"])
async def test_other_schemas_reject(
    hass: HomeAssistant, lights: dict[str, FakeLamp], service: str, data: dict[str, Any]
) -> None:
    await start(hass, [A])
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                       return_response=service == "get")


async def test_clear_base_is_refused_as_an_invalid_request(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """``base`` is reserved: it cannot be cleared (SPEC 5.2 raises invalid_request). The
    service must refuse it the way it refuses every other bad request (vol.Invalid from the
    schema, or a ServiceValidationError), not let the policy's internal exception escape."""
    _, engine = await start(hass, [A])
    with pytest.raises(Exception) as err:  # noqa: PT011 — the type is the assertion below
        await hass.services.async_call(DOMAIN, "clear", {"entity_id": A, "layer": "base"},
                                       blocking=True)
    assert isinstance(err.value, vol.Invalid | ServiceValidationError), repr(err.value)


@pytest.mark.parametrize("data", [
    {"until": "2000-01-01T00:00:00+00:00"},
    {"until": "2000-01-01 00:00:00"},     # a naive time is local time
    {"ttl": 0},                           # expires the moment it is set
], ids=["until_past", "until_past_naive", "ttl_zero"])
async def test_an_expiry_that_is_already_over_is_an_invalid_request(
    hass: HomeAssistant, lights: dict[str, FakeLamp], data: dict[str, Any]
) -> None:
    _, engine = await start(hass, [A])
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "set", {"entity_id": A, "layer": "hold", "priority": 10, "state": "off", **data},
            blocking=True,
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "invalid_request"
    assert engine.records[A].layers == {}
    assert lights["a"].calls == []


async def test_all_is_not_a_layer_id(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A])
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "set", {"entity_id": A, "layer": "all", "priority": 10, "state": "off"},
            blocking=True,
        )
    assert err.value.translation_key == "invalid_request"
    assert engine.records[A].layers == {}


async def test_until_in_the_future_sets_the_expiry(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A])
    until = dt_util.utcnow() + timedelta(hours=2)
    res = await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 10, "state": "off",
                                     "until": until.isoformat()})
    assert res == {A: "queued"}
    assert engine.records[A].layers["hold"].expires_at == pytest.approx(until.timestamp(), abs=1e-3)
    await settle(hass)


# =========================================================================== not loaded


async def test_services_say_not_loaded_when_the_entry_is_unloaded(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    entry, _ = await start(hass, [A])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for service, data in [
        ("set", {"entity_id": A, "layer": "hold", "priority": 10, "state": "off"}),
        ("clear", {"layer": "hold"}),
        ("sync", {}),
        ("get", {}),
    ]:
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                           return_response=service == "get")
        assert err.value.translation_key == "not_loaded", service
    assert lights["a"].calls == []


# =========================================================================== targets


async def test_a_light_group_target_expands_to_its_members(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A, B])
    res = await layers(hass, "set", {"entity_id": GROUP, "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "queued", B: "queued"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off" and state_of(hass, B)[0] == "off"
    # Only the member lamps were commanded, never the group itself.
    assert [c[0] for c in lights["a"].calls] == ["turn_off"]
    assert [c[0] for c in lights["b"].calls] == ["turn_off"]


async def test_a_vendor_room_expands_and_reports_members_that_are_not_enrolled(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    res = await layers(hass, "set", {"entity_id": ROOM, "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "queued", C: "skipped_not_enrolled"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off"
    assert lights["room"].calls == [] and lights["c"].calls == []


async def test_a_lamp_that_is_not_enrolled_is_skipped_and_the_others_still_work(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A])
    res = await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "queued", B: "skipped_not_enrolled"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off"
    assert state_of(hass, B) == ("on", 200) and lights["b"].calls == []
    assert B not in engine.records

    assert await layers(hass, "sync", {"entity_id": [A, B]}) == {A: "in_sync", B: "skipped_not_enrolled"}
    assert await layers(hass, "clear", {"entity_id": [A, B], "layer": "hold"}) == {
        A: "queued", B: "skipped_not_enrolled"}
    await settle(hass)
    assert state_of(hass, A) == ("on", 102)
    assert lights["b"].calls == []


async def test_naming_only_lamps_that_are_not_enrolled_reports_them(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """SPEC 7.6: lamps that are not enrolled are skipped and reported, never raised on."""
    await start(hass, [A])
    res = await layers(hass, "set", {"entity_id": B, "layer": "hold", "priority": 40, "state": "off"})
    assert res == {B: "skipped_not_enrolled"}
    assert lights["b"].calls == []


async def test_an_area_reaches_only_its_enrolled_lamps(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    areas, entities = ar.async_get(hass), er.async_get(hass)
    room = areas.async_create("Test room")
    shed = areas.async_create("Test shed")
    entities.async_update_entity(A, area_id=room.id)
    entities.async_update_entity(C, area_id=room.id)
    entities.async_update_entity(D, area_id=shed.id)
    await start(hass, [A, B])

    # lamp_c is in the room but not enrolled: reached indirectly, so not worth reporting.
    res = await layers(hass, "set", {"area_id": room.id, "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "queued"}
    await settle(hass)
    assert lights["c"].calls == []

    # An area holding no enrolled lamp is no target at all.
    with pytest.raises(ServiceValidationError) as err:
        await layers(hass, "set", {"area_id": shed.id, "layer": "hold", "priority": 40, "state": "off"})
    assert err.value.translation_key == "no_targets"


@pytest.mark.parametrize("target", [
    {"entity_id": GHOST},
    {"entity_id": STATUS},
    {"entity_id": "none"},
    {"entity_id": []},
], ids=["stateless_light", "not_a_light", "none", "empty_list"])
async def test_no_enrolled_target_raises_no_targets(
    hass: HomeAssistant, lights: dict[str, FakeLamp], target: dict[str, Any]
) -> None:
    _, engine = await start(hass, [A])
    with pytest.raises(ServiceValidationError) as err:
        await layers(hass, "set", {**target, "layer": "hold", "priority": 40, "state": "off"})
    assert err.value.translation_key == "no_targets"
    assert engine.records[A].layers == {}


async def test_entity_id_all_means_every_enrolled_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A, B])
    res = await layers(hass, "set", {"entity_id": "all", "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "queued", B: "queued"}
    await settle(hass)
    assert lights["c"].calls == []


# =========================================================================== results


async def test_queued_then_unchanged_on_a_refresh(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A])
    res = await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                                     "ttl": 600, "transition": 0.05})
    assert res == {A: "queued"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off"
    assert lights["a"].calls[-1][1].get("transition") == 0.05
    layer = engine.records[A].layers["hold"]
    seq, expires = layer.seq, layer.expires_at

    # The owner renews its lease: a refresh, nothing is sent.
    sent = len(lights["a"].calls)
    res = await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                                     "ttl": 3600})
    assert res == {A: "unchanged"}
    await settle(hass)
    assert len(lights["a"].calls) == sent
    assert layer.seq == seq and layer.expires_at > expires + 2000


async def test_unchanged_when_the_effective_command_stays_the_same(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """A set layer that says only "on" over a lamp that is on changes nothing to send."""
    _, engine = await start(hass, [A])
    res = await layers(hass, "set", {"entity_id": A, "layer": "keep_on", "priority": 40, "state": "on"})
    assert res == {A: "unchanged"}
    assert "keep_on" in engine.records[A].layers
    await settle(hass)
    assert lights["a"].calls == []


async def test_in_sync_when_the_lamp_already_shows_it(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A])
    # 104 is within the brightness tolerance of the 102 the lamp shows.
    assert await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40,
                                      "brightness": 104}) == {A: "in_sync"}
    assert await layers(hass, "clear", {"entity_id": A, "layer": "dim"}) == {A: "in_sync"}
    await settle(hass)
    assert lights["a"].calls == []


async def test_pending_for_a_lamp_that_is_away(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A, B])
    lights["b"].set_available(False)
    await hass.async_block_till_done()
    res = await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "queued", B: "pending"}
    await settle(hass)
    assert engine.records[B].owed is not None
    assert engine.records[B].owed.target == Command("off")
    assert lights["b"].calls == []


async def test_shadow_when_apply_is_off(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A], apply=False)
    res = await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "shadow"}
    assert engine.records[A].diverged == "unsynced"
    assert "hold" in engine.records[A].layers   # the model still changes
    await settle(hass)
    assert lights["a"].calls == []


async def test_skipped_tombstoned_after_a_person_took_the_lamp_back(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    _, engine = await start(hass, [A, B])
    await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass)
    await person_turns_on(hass, hass_admin_user, A, brightness=180)
    await settle(hass)
    assert "hold" in engine.records[A].tombstones

    sent = len(lights["a"].calls)
    res = await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    assert res == {A: "skipped_tombstoned", B: "unchanged"}
    await settle(hass)
    assert len(lights["a"].calls) == sent
    assert state_of(hass, A) == ("on", 180)


async def test_skipped_absent_with_only_if_present(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A])
    # No priority needed: only_if_present never creates.
    res = await layers(hass, "set", {"entity_id": A, "layer": "hold", "only_if_present": True,
                                     "state": "off"})
    assert res == {A: "skipped_absent"}
    assert engine.records[A].layers == {}
    await settle(hass)
    assert lights["a"].calls == []


# =========================================================================== layer: base / active


async def test_layer_base_changes_what_the_lamp_falls_back_to(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass)
    assert state_of(hass, A)[0] == "off"

    # The base changes underneath: nothing is sent while the hold decides.
    sent = len(lights["a"].calls)
    assert await layers(hass, "set", {"entity_id": A, "layer": "base", "brightness": 30}) == {A: "unchanged"}
    await settle(hass)
    assert len(lights["a"].calls) == sent and state_of(hass, A)[0] == "off"
    assert rec.base == Command("on", 30, Color.kelvin(2700))
    assert rec.base_source == "service"
    assert "hold" in rec.layers

    # Clearing the hold falls through to the base as it is now, not as it was.
    assert await layers(hass, "clear", {"layer": "hold"}) == {A: "queued"}
    await settle(hass)
    assert state_of(hass, A) == ("on", 30)


async def test_layer_base_on_a_lamp_without_layers_is_sent_at_once(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [C])
    # lamp_c is off: a base given only a brightness keeps it off (the state is the base's).
    assert await layers(hass, "set", {"entity_id": C, "layer": "base", "brightness": 40}) == {C: "unchanged"}
    assert engine.records[C].base == Command("off")
    assert await layers(hass, "set", {"entity_id": C, "layer": "base", "state": "on",
                                      "brightness": 40}) == {C: "queued"}
    await settle(hass)
    assert state_of(hass, C) == ("on", 40)


async def test_layer_active_edits_the_top_layer_and_survives_the_owners_refresh(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A])
    rec = engine.records[A]
    await layers(hass, "set", {"entity_id": A, "layer": "low", "priority": 10, "brightness": 40})
    await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40, "brightness": 60})
    await settle(hass)
    assert state_of(hass, A) == ("on", 60)

    assert await layers(hass, "set", {"entity_id": A, "layer": "active", "brightness": 90}) == {A: "queued"}
    await settle(hass)
    assert state_of(hass, A) == ("on", 90)
    dim = rec.layers["dim"]
    assert dim.command.brightness == 90 and dim.requested.brightness == 60
    assert rec.layers["low"].command.brightness == 40   # only the top layer is edited
    assert rec.base == A_BASE

    # The owner re-sends what it asked for: a refresh only, the edit stays.
    sent = len(lights["a"].calls)
    assert await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40, "brightness": 60,
                                      "ttl": 600}) == {A: "unchanged"}
    await settle(hass)
    assert len(lights["a"].calls) == sent and dim.command.brightness == 90

    # clear active removes the top layer: the lamp falls to the one below.
    assert await layers(hass, "clear", {"entity_id": A, "layer": "active"}) == {A: "queued"}
    await settle(hass)
    assert set(rec.layers) == {"low"}
    assert state_of(hass, A) == ("on", 40)


async def test_layer_active_with_no_layer_is_the_base(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A])
    assert await layers(hass, "set", {"entity_id": A, "layer": "active", "brightness": 50}) == {A: "queued"}
    await settle(hass)
    rec = engine.records[A]
    assert rec.layers == {}
    assert rec.base == Command("on", 50, Color.kelvin(2700)) and rec.base_source == "service"
    assert state_of(hass, A) == ("on", 50)


async def test_layer_active_off_turns_an_adjust_layer_into_set_off(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40, "mode": "adjust",
                               "brightness_pct": 25})
    await settle(hass)
    assert state_of(hass, A) == ("on", 64)

    assert await layers(hass, "set", {"entity_id": A, "layer": "active", "state": "off"}) == {A: "queued"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off"
    dim = engine.records[A].layers["dim"]
    assert (dim.mode, dim.requested_mode) == ("set", "adjust")

    # The owner renews its adjust layer as it asked for it: the edit survives.
    sent = len(lights["a"].calls)
    assert await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40, "mode": "adjust",
                                      "brightness_pct": 25, "ttl": 600}) == {A: "unchanged"}
    await settle(hass)
    assert len(lights["a"].calls) == sent and state_of(hass, A)[0] == "off"


# =========================================================================== only_if_present


async def test_only_if_present_refreshes_where_the_layer_is_and_creates_nothing(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A, B])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off", "ttl": 60,
                               "owner": "first"})
    await settle(hass)
    layer = engine.records[A].layers["hold"]
    expires, seq = layer.expires_at, layer.seq

    res = await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "only_if_present": True,
                                     "state": "off", "ttl": 3600, "owner": "second"})
    assert res == {A: "unchanged", B: "skipped_absent"}
    assert engine.records[B].layers == {}
    assert layer.expires_at > expires + 3000 and layer.seq == seq
    assert layer.owner == "second"
    await settle(hass)
    assert lights["b"].calls == []


async def test_only_if_present_renewal_of_an_adjust_layer_keeps_its_options(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """SPEC 5.1: an unchanged renewal only refreshes, "options a renewal leaves out keep
    their values", and a renewal that leaves ``mode`` out must not change the layer.
    services._command gives the request ``state: on`` because ``mode`` defaults to ``set``,
    so it never equals the adjust layer's ``requested`` (no state)."""
    _, engine = await start(hass, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "dim", "priority": 40, "mode": "adjust",
                               "brightness_pct": 25, "ttl": 600, "on_expire": "render",
                               "resume_after_manual": True})
    await settle(hass)
    rec = engine.records[A]
    dim = rec.layers["dim"]
    requested, changed = dim.requested, rec.last_layers_change

    res = await layers(hass, "set", {"entity_id": A, "layer": "dim", "only_if_present": True,
                                     "brightness_pct": 25, "ttl": 1200})
    assert res == {A: "unchanged"}
    assert dim.mode == "adjust"
    assert dim.requested == requested
    assert dim.on_expire == "render"
    assert dim.resume_after_manual is True
    assert rec.last_layers_change == changed


# =========================================================================== priority errors


async def test_priority_required_is_raised_before_any_lamp_is_touched(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A, B])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "brightness": 60})
    await settle(hass)
    before = engine.records[A].layers["hold"].to_json()
    sent = (len(lights["a"].calls), len(lights["b"].calls))

    # hold exists on A (no priority needed there) but not on B: the whole call is refused.
    with pytest.raises(ServiceValidationError) as err:
        await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "state": "off"})
    assert err.value.translation_key == "priority_required"
    assert err.value.translation_placeholders["entity_id"] == B
    assert err.value.translation_placeholders["layer"] == "hold"
    assert B in str(err.value)
    await settle(hass)
    assert engine.records[A].layers["hold"].to_json() == before
    assert engine.records[B].layers == {}
    assert (len(lights["a"].calls), len(lights["b"].calls)) == sent
    assert state_of(hass, A) == ("on", 60)


async def test_priority_conflict_is_raised_before_any_lamp_is_touched(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A, B])
    await layers(hass, "set", {"entity_id": B, "layer": "other", "priority": 40, "brightness": 90})
    await settle(hass)
    sent = (len(lights["a"].calls), len(lights["b"].calls))

    with pytest.raises(ServiceValidationError) as err:
        await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    assert err.value.translation_key == "priority_conflict"
    placeholders = err.value.translation_placeholders
    assert (placeholders["entity_id"], placeholders["other"], placeholders["priority"]) == (B, "other", "40")
    assert B in str(err.value) and "other" in str(err.value)
    await settle(hass)
    assert engine.records[A].layers == {}
    assert set(engine.records[B].layers) == {"other"}
    assert (len(lights["a"].calls), len(lights["b"].calls)) == sent
    assert state_of(hass, A) == ("on", 102)


# =========================================================================== clear


@pytest.mark.parametrize("layer", ["all", "active"])
async def test_clear_all_or_active_needs_a_target(
    hass: HomeAssistant, lights: dict[str, FakeLamp], layer: str
) -> None:
    """SPEC 7.6: 'all' and 'active' mean something different on every lamp."""
    _, engine = await start(hass, [A])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass)
    with pytest.raises(ServiceValidationError) as err:
        await layers(hass, "clear", {"layer": layer})
    assert err.value.translation_key == "no_targets"
    assert "hold" in engine.records[A].layers


async def test_clear_all_with_a_target_removes_layers_and_tombstones(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    _, engine = await start(hass, [A, B])
    await layers(hass, "set", {"entity_id": A, "layer": "sig", "priority": 70, "state": "off"})
    await settle(hass)
    await person_turns_on(hass, hass_admin_user, A, brightness=150)   # tombstones "sig"
    await settle(hass)
    await layers(hass, "set", {"entity_id": [A, B], "layer": "low", "priority": 10, "brightness": 40})
    await layers(hass, "set", {"entity_id": [A, B], "layer": "high", "priority": 60, "brightness": 20})
    await settle(hass)
    rec = engine.records[A]
    assert set(rec.layers) == {"low", "high"} and set(rec.tombstones) == {"sig"}

    assert await layers(hass, "clear", {"entity_id": A, "layer": "all"}) == {A: "queued"}
    await settle(hass)
    assert rec.layers == {} and rec.tombstones == {}
    assert state_of(hass, A) == ("on", 150)
    assert set(engine.records[B].layers) == {"low", "high"}   # the other lamp is untouched
    # The tombstone is gone: the signal can be set again.
    assert await layers(hass, "set", {"entity_id": A, "layer": "sig", "priority": 70,
                                      "state": "off"}) == {A: "queued"}
    await settle(hass)


async def test_clear_without_target_clears_wherever_the_layer_is(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    _, engine = await start(hass, [A, B, C, D])
    await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    await layers(hass, "set", {"entity_id": C, "layer": "other", "priority": 40, "brightness": 50})
    await layers(hass, "set", {"entity_id": D, "layer": "hold", "priority": 40, "state": "on",
                               "brightness": 70})
    await settle(hass)
    await person_turns_on(hass, hass_admin_user, D, brightness=120)   # D keeps only a tombstone
    await settle(hass)
    assert "hold" in engine.records[D].tombstones

    res = await layers(hass, "clear", {"layer": "hold"})
    assert res == {A: "queued", B: "queued", D: "unchanged"}
    await settle(hass)
    assert state_of(hass, A) == ("on", 102) and state_of(hass, B) == ("on", 200)
    assert set(engine.records[C].layers) == {"other"} and state_of(hass, C) == ("on", 50)
    assert engine.records[D].tombstones == {} and state_of(hass, D) == ("on", 120)
    # A layer that is nowhere: nothing to do, and no error.
    assert await layers(hass, "clear", {"layer": "nowhere"}) == {}


# =========================================================================== sync


async def test_sync_pushes_what_observe_only_left_unsynced(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A, B], apply=False)
    assert await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40,
                                      "state": "off"}) == {A: "shadow"}
    assert await layers(hass, "sync", {}) == {A: "shadow", B: "in_sync"}
    await switch(hass, True)
    await settle(hass)
    assert lights["a"].calls == [] and lights["b"].calls == []

    assert await layers(hass, "sync", {}) == {A: "queued", B: "in_sync"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off"
    assert engine.records[A].diverged is None
    assert lights["b"].calls == []
    assert await layers(hass, "sync", {"entity_id": GROUP}) == {A: "in_sync", B: "in_sync"}


# =========================================================================== get


LAMP_KEYS = {"active", "effective", "base", "layers", "tombstones", "observed", "available",
             "diverged", "pending", "rendering", "untrusted", "policy", "last_external"}
LAYER_KEYS = {"id", "priority", "mode", "requested", "command", "seq", "set_at", "expires_at",
              "owner", "resume_after_manual", "on_expire", "requested_mode",
              # follow layers (None / empty on the others)
              "source", "manual", "manual_until", "manual_timeout", "transition",
              "strength"}


async def test_get_describes_each_lamp(
    hass: HomeAssistant, lights: dict[str, FakeLamp], hass_admin_user: MockUser
) -> None:
    await start(hass, [A, B, C], base_keep=[C])
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "brightness": 60,
                               "owner": "tester", "ttl": 600})
    await layers(hass, "set", {"entity_id": B, "layer": "gone", "priority": 30, "state": "off"})
    await settle(hass)
    await person_turns_on(hass, hass_admin_user, B, brightness=90)
    await settle(hass)

    result = await get(hass)
    json_dumps(result)   # a service response must be JSON
    assert set(result) == {"apply", "entities"} and result["apply"] is True
    assert set(result["entities"]) == {A, B, C}

    a = result["entities"][A]
    assert set(a) == LAMP_KEYS
    assert a["active"] == "hold"
    assert a["effective"] == {"state": "on", "brightness": 60, "color": {"color_temp_kelvin": 2700}}
    assert a["base"] == {"state": "on", "brightness": 102, "color": {"color_temp_kelvin": 2700}}
    assert a["available"] is True and a["pending"] is False and a["rendering"] is False
    assert a["untrusted"] is False
    assert a["diverged"] is None and a["tombstones"] == [] and a["policy"] == "take_back"
    assert a["observed"]["state"] == "on" and a["observed"]["brightness"] == 60
    (layer,) = a["layers"]
    assert set(layer) == LAYER_KEYS
    assert (layer["id"], layer["priority"], layer["mode"], layer["owner"]) == ("hold", 40, "set", "tester")
    assert layer["requested"] == layer["command"] == {"state": "on", "brightness": 60}
    assert dt_util.parse_datetime(layer["set_at"]) is not None
    assert dt_util.parse_datetime(layer["expires_at"]) > dt_util.utcnow()

    b = result["entities"][B]
    assert b["active"] == "base" and b["layers"] == [] and b["tombstones"] == ["gone"]
    assert b["last_external"]["source"] == "user" and b["last_external"]["dropped"] == ["gone"]
    assert "user_id" not in b["last_external"]
    assert result["entities"][C]["policy"] == "base_keep_layers"

    # A target narrows it; a group names its members.
    assert set((await get(hass, {"entity_id": B}))["entities"]) == {B}
    assert set((await get(hass, {"entity_id": GROUP}))["entities"]) == {A, B}


async def test_get_reports_a_lamp_that_is_owed_its_command(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    await start(hass, [A, B])
    lights["b"].set_available(False)
    await hass.async_block_till_done()
    await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass)
    lamps = (await get(hass))["entities"]
    assert (lamps[B]["pending"], lamps[B]["available"]) == (True, False)
    assert lamps[A]["pending"] is False


# =========================================================================== diagnostics


async def test_diagnostics_redact_user_and_context_ids(
    hass: HomeAssistant,
    lights: dict[str, FakeLamp],
    hass_admin_user: MockUser,
    hass_client: ClientSessionGenerator,
) -> None:
    entry, _ = await start(hass, [A])
    caller = Context()
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "owner": "tester"}, context=caller)
    await settle(hass)
    person = Context(user_id=hass_admin_user.id)
    await hass.services.async_call("light", "turn_on", {"entity_id": A, "brightness": 170},
                                   blocking=True, context=person)
    await settle(hass)

    ids = {caller.id, person.id, hass_admin_user.id, hass.states.get(A).context.id}
    for _service, _data, ctx in lights["a"].calls:
        if ctx is not None:
            ids |= {ctx.id, ctx.parent_id, ctx.user_id}
    ids.discard(None)

    dump = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(dump)
    leaked = sorted(i for i in ids if i in text)
    assert leaked == [], f"ids in the diagnostics: {leaked}"

    assert {"options", "apply", "status", "lamps", "stored", "in_flight", "decisions"} <= set(dump)
    assert dump["decisions"], "the classifier trace is part of the dump"
    stored = dump["stored"]["entities"][A]
    assert stored["last_external"]["user_id"] == REDACTED   # redacted, not dropped
    assert stored["last_external"]["source"] == "user"
    assert stored["tombstones"]["hold"]["source"] == "user"

    # Context ids are redacted wherever a decision or record carries one.
    entry.runtime_data.engine.decisions.append(
        {"entity_id": A, "kind": "probe", "context_id": "ctx-probe", "parent_id": "parent-probe",
         "context": {"id": "ctx-nested"}, "user_id": "user-probe"})
    dump = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    probe = dump["decisions"][-1]
    assert {k: probe[k] for k in ("context_id", "parent_id", "context", "user_id")} == {
        "context_id": REDACTED, "parent_id": REDACTED, "context": REDACTED, "user_id": REDACTED}


# =========================================================================== status sensor


async def test_status_sensor_shadow_ok_and_pending(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    await start(hass, [A, B], apply=False)
    state = hass.states.get(STATUS)
    assert state.state == "shadow"
    assert state.attributes["failed"] == [] and state.attributes["pending_since"] == {}
    assert state.attributes["lamps"] == 2 and state.attributes["layered"] == []

    await switch(hass, True)
    assert hass.states.get(STATUS).state == "ok"

    lights["b"].set_available(False)
    await hass.async_block_till_done()
    assert await layers(hass, "set", {"entity_id": B, "layer": "hold", "priority": 40,
                                      "state": "off"}) == {B: "pending"}
    await settle(hass)
    state = hass.states.get(STATUS)
    assert state.state == "pending"
    assert list(state.attributes["pending_since"]) == [B]
    assert dt_util.parse_datetime(state.attributes["pending_since"][B]) is not None
    assert state.attributes["layered"] == [B]

    await switch(hass, False)
    assert hass.states.get(STATUS).state == "shadow"   # shadow wins over pending


async def test_status_sensor_failed(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    _, engine = await start(hass, [A, B])
    lights["a"].ignore_commands = True
    lights["b"].set_available(False)
    await hass.async_block_till_done()
    await layers(hass, "set", {"entity_id": [A, B], "layer": "hold", "priority": 40, "state": "off"})
    await settle(hass, 0.5)
    state = hass.states.get(STATUS)
    assert state.state == "failed"   # failed wins over pending
    assert state.attributes["failed"] == [A]
    assert list(state.attributes["pending_since"]) == [B]
    assert engine.records[A].diverged == "delivery"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"render_failed_{A}") is not None


async def test_status_sensor_ignores_lamps_away_for_more_than_a_day(
    hass: HomeAssistant, lights: dict[str, FakeLamp], freezer: FrozenDateTimeFactory
) -> None:
    await start(hass, [A])
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    assert await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40,
                                      "state": "off"}) == {A: "pending"}
    assert hass.states.get(STATUS).state == "pending"

    freezer.tick(timedelta(seconds=STALE_UNAVAILABLE_S + 60))
    assert await layers(hass, "sync", {"entity_id": A}) == {A: "pending"}   # still owed...
    state = hass.states.get(STATUS)
    assert state.state == "ok"                                              # ...but not counted
    assert state.attributes["pending_since"] == {}


async def test_status_sensor_agrees_with_the_engine_while_a_render_is_in_flight(
    hass: HomeAssistant, lights: dict[str, FakeLamp], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sensor is refreshed whenever a render owes the lamp something: when it starts,
    at each retry (a new pending_since), and for renders no service call started."""
    _, engine = await start(hass, [C])
    # The second attempt comes at once; the third is parked, so nothing finishes by itself.
    monkeypatch.setattr(render_module, "RETRY_BACKOFF_S", (0.01, 30.0))

    def agrees() -> bool:
        shown = hass.states.get(STATUS)
        status, attrs = engine.status()
        return (shown.state, shown.attributes["pending_since"]) == (status, attrs["pending_since"])

    # 1. A set whose lamp does not take it: attempt 1, then attempt 2 with a new since.
    lights["c"].ignore_commands = True
    await layers(hass, "set", {"entity_id": C, "layer": "hold", "priority": 40, "brightness": 150})
    await asyncio.sleep(0.1)
    assert engine.renderer.alive(C) and len(lights["c"].calls) == 2
    assert engine.status()[0] == "pending"
    assert agrees()

    # 2. A render no service call started: the retry of a reverted command of ours.
    await switch(hass, False)                       # cancel the parked render...
    assert agrees()                                 # (shadow, nothing owed)
    lights["c"].ignore_commands = False
    await switch(hass, True)
    await layers(hass, "sync", {"entity_id": C})    # ...and push it: the lamp takes it now
    await settle(hass)
    assert hass.states.get(STATUS).state == "ok" and hass.states.get(C).state == "on"
    lights["c"].ignore_commands = True
    lights["c"].push(is_on=False)                   # no context, inside the late window
    await asyncio.sleep(0.05)
    assert engine.renderer.alive(C) and engine.status()[0] == "pending"
    assert agrees()
    await switch(hass, False)                       # cancel the parked retry


# =========================================================================== apply switch


async def test_apply_switch_off_cancels_on_sends_nothing_sync_pushes(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    _, engine = await start(hass, [A, B])
    assert hass.states.get(SWITCH).state == "on"
    lights["a"].ignore_commands = True
    assert await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                                      "transition": 30}) == {A: "queued"}
    await asyncio.sleep(0.05)   # the first attempt goes out, then waits out the transition
    assert len(lights["a"].calls) == 1 and engine.renderer.alive(A)
    task = engine.renderer._tasks[A]  # noqa: SLF001

    await switch(hass, False)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert not engine.renderer.alive(A)
    assert engine.records[A].diverged == "unsynced"
    assert engine.records[B].diverged is None       # nothing was in flight there
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(STATUS).state == "shadow"

    # Off: the model still changes, nothing is sent.
    assert await layers(hass, "set", {"entity_id": B, "layer": "dim", "priority": 20,
                                      "brightness": 50}) == {B: "shadow"}
    assert engine.records[B].diverged == "unsynced"

    lights["a"].ignore_commands = False
    await switch(hass, True)
    await settle(hass)
    assert hass.states.get(SWITCH).state == "on"
    assert len(lights["a"].calls) == 1 and lights["b"].calls == []   # on sends nothing

    assert await layers(hass, "sync", {}) == {A: "queued", B: "queued"}
    await settle(hass)
    assert state_of(hass, A)[0] == "off" and state_of(hass, B) == ("on", 50)
    assert engine.records[A].diverged is None and engine.records[B].diverged is None


async def test_apply_switch_state_survives_a_reload(hass: HomeAssistant, lights: dict[str, FakeLamp]) -> None:
    entry, _ = await start(hass, [A])
    await switch(hass, False)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.engine.apply is False
    assert hass.states.get(SWITCH).state == "off"


async def test_a_render_cancelled_by_the_apply_switch_waits_for_sync(
    hass: HomeAssistant, lights: dict[str, FakeLamp]
) -> None:
    """SPEC 7.5: turning the switch off cancels in-flight renders and marks the lamp
    unsynced; turning it on sends nothing; only layers.sync repairs ``unsynced`` (SPEC 2,
    6.4: "nothing owed and diverged unsynced -> NOTHING")."""
    _, engine = await start(hass, [A])
    rec = engine.records[A]
    lights["a"].ignore_commands = True
    await layers(hass, "set", {"entity_id": A, "layer": "hold", "priority": 40, "state": "off",
                               "transition": 30})
    await asyncio.sleep(0.05)
    assert len(lights["a"].calls) == 1
    await switch(hass, False)
    assert rec.diverged == "unsynced"
    lights["a"].ignore_commands = False
    await switch(hass, True)

    # The lamp drops off the network and comes back exactly as it was.
    lights["a"].set_available(False)
    await hass.async_block_till_done()
    lights["a"].set_available(True)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await settle(hass)

    assert len(lights["a"].calls) == 1, "the return delivered the cancelled render"
    assert state_of(hass, A) == ("on", 102)
    assert rec.diverged == "unsynced"
