"""The layers.set / clear / sync / get services.

Registered once in ``async_setup`` (not per entry), and routed to the loaded
entry's engine. Validation that needs no state happens in the schemas; anything
that depends on a lamp's layers (priorities, tombstones) is decided per lamp.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_LAYER,
    ATTR_MANUAL_TIMEOUT,
    ATTR_MODE,
    ATTR_ON_EXPIRE,
    ATTR_ONLY_IF_PRESENT,
    ATTR_OWNER,
    ATTR_PRIORITY,
    ATTR_RESUME_AFTER_MANUAL,
    ATTR_SOURCE,
    ATTR_STATE,
    ATTR_TTL,
    ATTR_UNTIL,
    DOMAIN,
    LAYER_ID_PATTERN,
    OWNER_MAX_LEN,
    PRIORITY_MAX,
    PRIORITY_MIN,
    SERVICE_CLEAR,
    SERVICE_GET,
    SERVICE_SET,
    SERVICE_SYNC,
)
from .logic.model import (
    LAYER_ACTIVE,
    LAYER_ALL,
    LAYER_BASE,
    MODE_ADJUST,
    MODE_FOLLOW,
    MODE_SET,
    MODES,
    OFF,
    ON,
    ON_EXPIRE_CHOICES,
    ON_EXPIRE_SAFE,
    Color,
    Command,
    SetRequest,
)
from .logic.policy import PolicyError
from .targets import service_targets

if TYPE_CHECKING:
    from .engine import Engine

ATTR_TRANSITION = "transition"
_BRIGHTNESS = ("brightness", "brightness_pct")
_COLORS = ("color_temp_kelvin", "xy_color", "hs_color", "rgb_color")
_ID = re.compile(LAYER_ID_PATTERN)


def _set_layer_id(value: Any) -> str:
    value = cv.string(value).strip()
    if value in (LAYER_BASE, LAYER_ACTIVE) or _ID.match(value):
        return value
    raise vol.Invalid("layer must be a lowercase id (a-z, 0-9, _), 'base' or 'active'")


def _clear_layer_id(value: Any) -> str:
    value = cv.string(value).strip()
    if value == LAYER_BASE:
        raise vol.Invalid("the base cannot be cleared: set it, or clear the layers above it")
    if value in (LAYER_ACTIVE, LAYER_ALL) or _ID.match(value):
        return value
    raise vol.Invalid("layer must be a lowercase id (a-z, 0-9, _), 'active' or 'all'")


def _pair(lo0: float, hi0: float, lo1: float, hi1: float) -> vol.All:
    return vol.All(
        vol.ExactSequence(
            (vol.All(vol.Coerce(float), vol.Range(lo0, hi0)),
             vol.All(vol.Coerce(float), vol.Range(lo1, hi1)))
        ),
        vol.Coerce(tuple),
    )


def _cross_check(data: dict[str, Any]) -> dict[str, Any]:
    layer, mode = data[ATTR_LAYER], data[ATTR_MODE]
    attrs = [k for k in (*_BRIGHTNESS, *_COLORS) if k in data]
    if layer in (LAYER_BASE, LAYER_ACTIVE):
        for key in (ATTR_PRIORITY, ATTR_TTL, ATTR_UNTIL):
            if key in data:
                raise vol.Invalid(f"{key} does not apply to layer '{layer}'")
        if data[ATTR_ONLY_IF_PRESENT]:
            raise vol.Invalid(f"only_if_present does not apply to layer '{layer}'")
    if mode == MODE_FOLLOW:
        if layer in (LAYER_BASE, LAYER_ACTIVE):
            raise vol.Invalid(f"a follow layer needs a layer id, not '{layer}'")
        if ATTR_STATE in data or attrs:
            raise vol.Invalid("a follow layer takes its brightness and colour from its source: "
                              "drop 'state', brightness and colour")
        if ATTR_SOURCE not in data and not data[ATTR_ONLY_IF_PRESENT]:
            raise vol.Invalid("a follow layer needs a source")
        return data
    for key in (ATTR_SOURCE, ATTR_MANUAL_TIMEOUT):
        if key in data:
            raise vol.Invalid(f"{key} only applies to mode: follow")
    if mode == MODE_ADJUST:
        if ATTR_STATE in data:
            raise vol.Invalid("an adjust layer changes attributes only: drop 'state'")
        if not attrs:
            raise vol.Invalid("an adjust layer needs a brightness or a colour")
    if data.get(ATTR_STATE) == OFF and attrs:
        raise vol.Invalid("state: off takes no brightness or colour")
    if mode == MODE_SET and ATTR_STATE not in data and not attrs:
        raise vol.Invalid("give a state, a brightness or a colour")
    return data


SET_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Required(ATTR_LAYER): _set_layer_id,
            vol.Optional(ATTR_PRIORITY): vol.All(vol.Coerce(int), vol.Range(PRIORITY_MIN, PRIORITY_MAX)),
            vol.Optional(ATTR_MODE, default=MODE_SET): vol.In(MODES),
            vol.Optional(ATTR_STATE): vol.All(cv.string, vol.Lower, vol.In([ON, OFF])),
            vol.Exclusive("brightness", "brightness"): vol.All(vol.Coerce(int), vol.Range(1, 255)),
            vol.Exclusive("brightness_pct", "brightness"): vol.All(vol.Coerce(float), vol.Range(1, 100)),
            vol.Exclusive("color_temp_kelvin", "color"): vol.All(vol.Coerce(int), vol.Range(1000, 12000)),
            vol.Exclusive("xy_color", "color"): _pair(0, 1, 0, 1),
            vol.Exclusive("hs_color", "color"): _pair(0, 360, 0, 100),
            vol.Exclusive("rgb_color", "color"): vol.All(
                vol.ExactSequence((cv.byte, cv.byte, cv.byte)), vol.Coerce(tuple)
            ),
            vol.Optional(ATTR_TRANSITION): vol.All(vol.Coerce(float), vol.Range(0, 300)),
            vol.Exclusive(ATTR_TTL, "expiry"): cv.positive_time_period,
            vol.Exclusive(ATTR_UNTIL, "expiry"): cv.datetime,
            vol.Optional(ATTR_RESUME_AFTER_MANUAL, default=False): cv.boolean,
            vol.Optional(ATTR_ON_EXPIRE, default=ON_EXPIRE_SAFE): vol.In(ON_EXPIRE_CHOICES),
            vol.Optional(ATTR_ONLY_IF_PRESENT, default=False): cv.boolean,
            vol.Optional(ATTR_OWNER): vol.All(cv.string, vol.Length(max=OWNER_MAX_LEN)),
            vol.Optional(ATTR_SOURCE): cv.entity_id,
            vol.Optional(ATTR_MANUAL_TIMEOUT): cv.positive_time_period,
        }
    ),
    _cross_check,
)

CLEAR_SCHEMA = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_LAYER): _clear_layer_id,
        vol.Optional(ATTR_TRANSITION): vol.All(vol.Coerce(float), vol.Range(0, 300)),
    }
)
TARGET_ONLY_SCHEMA = vol.Schema({**cv.ENTITY_SERVICE_FIELDS})


# --------------------------------------------------------------------------- helpers


def _engine(hass: HomeAssistant) -> Engine:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data.engine


def _has_target(data: dict[str, Any]) -> bool:
    return any(k in data for k in (ATTR_ENTITY_ID, "device_id", "area_id", "floor_id", "label_id"))


def _lamps(hass: HomeAssistant, engine: Engine, data: dict[str, Any], *, required: bool) -> list[str] | None:
    """Enrolled lamps a call targets, plus explicitly named lamps that are not
    enrolled (they are reported as skipped). ``None`` means "no target given"."""
    if not _has_target(data):
        if required:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_targets")
        return None
    if data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
        return sorted(engine.records)
    explicit, indirect = service_targets(hass, data)
    lamps = sorted((explicit | (indirect & engine.enrolled)))
    if not lamps:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_targets")
    return lamps


def _command(data: dict[str, Any]) -> Command:
    brightness = data.get("brightness")
    if brightness is None and data.get("brightness_pct") is not None:
        brightness = round(255 * data["brightness_pct"] / 100)
    color = None
    if "xy_color" in data:
        color = Color.xy(*data["xy_color"])
    elif "hs_color" in data:
        color = Color("hs_color", tuple(float(v) for v in data["hs_color"]))
    elif "rgb_color" in data:
        color = Color("rgb_color", tuple(float(v) for v in data["rgb_color"]))
    elif "color_temp_kelvin" in data:
        color = Color.kelvin(data["color_temp_kelvin"])
    state = data.get(ATTR_STATE)
    if (
        state is None
        and data[ATTR_MODE] == MODE_SET
        and data[ATTR_LAYER] not in (LAYER_BASE, LAYER_ACTIVE)
        # A renewal never changes a layer's mode: the policy reads its attributes
        # the way the layer's own mode does (a set layer: "on like this").
        and not data[ATTR_ONLY_IF_PRESENT]
    ):
        state = ON  # a set layer with attributes and no state means "on like this"
    return Command(state, brightness, color)


def _expires_at(data: dict[str, Any]) -> float | None:
    now = dt_util.utcnow()
    if ATTR_TTL in data:
        return (now + data[ATTR_TTL]).timestamp()
    if ATTR_UNTIL in data:
        until = data[ATTR_UNTIL]
        if until.tzinfo is None:  # a naive time from an automation means local time
            until = until.replace(tzinfo=dt_util.get_default_time_zone())
        until = dt_util.as_utc(until)
        if until <= now:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_request",
                translation_placeholders={"reason": "'until' is in the past"},
            )
        return until.timestamp()
    return None


def _policy_error(err: PolicyError, req: SetRequest) -> ServiceValidationError:
    placeholders = {"layer": req.layer, "entity_id": "", "priority": str(req.priority),
                    "other": "", "reason": str(err)}
    placeholders.update({k: str(v) for k, v in err.placeholders.items()})
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=err.code if err.code in ("priority_required", "priority_conflict") else "invalid_request",
        translation_placeholders=placeholders,
    )


# --------------------------------------------------------------------------- handlers


async def _async_set(call: ServiceCall) -> ServiceResponse:
    engine = _engine(call.hass)
    data = dict(call.data)
    lamps = _lamps(call.hass, engine, data, required=True) or []
    req = SetRequest(
        layer=data[ATTR_LAYER],
        command=_command(data),
        priority=data.get(ATTR_PRIORITY),
        mode=data[ATTR_MODE],
        expires_at=_expires_at(data),
        resume_after_manual=data[ATTR_RESUME_AFTER_MANUAL],
        on_expire=data[ATTR_ON_EXPIRE],
        only_if_present=data[ATTR_ONLY_IF_PRESENT],
        owner=data.get(ATTR_OWNER),
        source=data.get(ATTR_SOURCE),
        manual_timeout=(data[ATTR_MANUAL_TIMEOUT].total_seconds()
                        if ATTR_MANUAL_TIMEOUT in data else None),
        transition=data.get(ATTR_TRANSITION) if data[ATTR_MODE] == MODE_FOLLOW else None,
    )
    try:
        engine.validate_set(lamps, req)
    except PolicyError as err:
        raise _policy_error(err, req) from err
    results = engine.set_layer(lamps, req, transition=data.get(ATTR_TRANSITION),
                               parent_id=call.context.id)
    return {"entities": results} if call.return_response else None


async def _async_clear(call: ServiceCall) -> ServiceResponse:
    engine = _engine(call.hass)
    data = dict(call.data)
    # 'all' and 'active' mean something different on every lamp: they need a target.
    lamps = _lamps(call.hass, engine, data, required=data[ATTR_LAYER] in (LAYER_ALL, LAYER_ACTIVE))
    try:
        results = engine.clear_layer(lamps, data[ATTR_LAYER], transition=data.get(ATTR_TRANSITION),
                                     parent_id=call.context.id)
    except PolicyError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_request",
            translation_placeholders={"layer": data[ATTR_LAYER], "entity_id": "",
                                      "reason": str(err)},
        ) from err
    return {"entities": results} if call.return_response else None


async def _async_sync(call: ServiceCall) -> ServiceResponse:
    engine = _engine(call.hass)
    lamps = _lamps(call.hass, engine, dict(call.data), required=False)
    results = engine.sync(lamps, parent_id=call.context.id)
    return {"entities": results} if call.return_response else None


async def _async_get(call: ServiceCall) -> ServiceResponse:
    engine = _engine(call.hass)
    lamps = _lamps(call.hass, engine, dict(call.data), required=False)
    return {"apply": engine.apply, "entities": engine.describe(lamps)}


def async_register_services(hass: HomeAssistant) -> None:
    hass.services.async_register(DOMAIN, SERVICE_SET, _async_set, SET_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR, _async_clear, CLEAR_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_SYNC, _async_sync, TARGET_ONLY_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_GET, _async_get, TARGET_ONLY_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
