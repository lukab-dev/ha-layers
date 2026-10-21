"""Climate: layers on thermostats (SPEC section 8).

Pure Python (3.12+), no Home Assistant imports, like the rest of ``logic/``. The
model is the lamps' model, with its own command, comparisons and four answers a
thermostat needs that a lamp does not:

What a layer holds
    an HVAC mode, and a target: a temperature and/or a preset. The target is one
    atomic group, like a lamp's colour: a layer that names a temperature or a
    preset replaces both below it, because on most thermostats a preset carries its
    own setpoint. A ``set`` layer that names no mode keeps the mode below; it never
    turns heating on by itself. An ``adjust`` layer holds an offset in the entity's
    unit ("2 degrees lower"): it shifts the temperature below, only while the mode
    below is not ``off``, and offsets stack.

The expiry safety rule
    a layer that runs out never turns a thermostat on: from ``off`` (or unknown) to
    any other mode. A setpoint may move either way on an expiry, back to the
    schedule after a manual hold included: the thermostat is already on and doing
    its job. Stricter than lamps, there is no exception for a layer still holding
    the mode below: an expiry that would start heating sends nothing at all.

Telling a person from a slow device
    a report with no context inside the report-back window after Layers' own
    command is the device answering late, never a person, unless the device had
    already arrived at the command and then moved away. Outside the window, a
    change with no context is a person's (after the usual debounce). Reports that
    only move the room temperature or what the device is doing (``hvac_action``)
    are not changes at all.

Checking a command arrived
    the mode and the preset must read back exactly; the setpoint within a tolerance,
    because thermostats round (21.3 reads back as 21.5): half the device's
    ``target_temp_step``, and never less than ``TOL_SETPOINT``.

A change Layers did not make (``apply_external``) is never written into the base on
a thermostat. It becomes the ``manual`` layer at a configured priority, which runs
out after a configured time: a person's change outranks every layer below that
priority (holiday, away, night) and is outranked by the ones above it (an open
window, as a locked layer). Soft layers at or above it are taken back, as on a lamp.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .model import (
    ARRIVED_HOLD_S,
    DIV_DELIVERY,
    DIV_MANUAL_KEEP,
    DIV_UNSYNCED,
    LAYER_ACTIVE,
    LAYER_ALL,
    LAYER_BASE,
    MODE_ADJUST,
    MODE_SET,
    NO_STATE,
    ON_EXPIRE_RENDER,
    ON_EXPIRE_SAFE,
    OWED_ON_MAX_AGE_S,
    SCOPE_LAMP,
    SCOPE_ROOM,
    SRC_AUTOMATION,
    SRC_DEVICE,
    SRC_OURS,
    SRC_USER,
    STRENGTH_SOFT,
    STRENGTH_STICKY,
    STRENGTHS,
    STRONG,
    External,
    Tombstone,
)
from .policy import (
    BASE_SRC_EXPIRY,
    BASE_SRC_SERVICE,
    ERR_INVALID_REQUEST,
    ERR_PRIORITY_CONFLICT,
    ERR_PRIORITY_REQUIRED,
    SET_ACTIVE,
    SET_BASE,
    SET_CREATED,
    SET_REFRESHED,
    SET_SKIPPED_ABSENT,
    SET_SKIPPED_TOMBSTONED,
    SET_UPDATED,
    ClearResult,
    ExpireResult,
    ExternalResult,
    PolicyError,
    SetResult,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

HVAC_OFF = "off"
# Home Assistant's HVACMode values. Every one but off is "on" for the safety rule.
HVAC_MODES = ("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only")
PRESET_NONE = "none"        # Home Assistant's PRESET_NONE: no preset

GROUP_MODE = "hvac_mode"
GROUP_TARGET = "target"     # temperature and preset, one atomic group
GROUP_OFFSET = "offset"

# The layer a change Layers did not make is written into. Its id is reserved: an
# automation cannot set it, but `layers.clear layer: manual` ends a hold early.
MANUAL_LAYER = "manual"
POLICY_MANUAL_LAYER = "manual_layer"

# Defaults for the integration's options (the house this was built for uses these).
DEFAULT_MANUAL_PRIORITY = 80
DEFAULT_MANUAL_TIMEOUT_S = 3 * 3600.0
# How long after Layers' own command a report with no context is still the device
# answering. Measured on a thermostat bridged over HomeKit: a setpoint write read back
# correct in 2.0-2.1 s (4 of 4); a radiator valve that follows its zone thermostat
# took ~30 s. 30 s leaves the first a wide margin and does not cover the second on
# purpose: enrol the thermostat Layers commands, not a valve that follows it.
DEFAULT_REPORT_WINDOW_S = 30.0

TOL_SETPOINT = 0.25         # thermostats round to 0.5: 21.3 reads back as 21.5
_EPS = 1e-6

# Service names Layers calls, in the order a render sends them.
SERVICE_SET_HVAC_MODE = "set_hvac_mode"
SERVICE_SET_PRESET_MODE = "set_preset_mode"
SERVICE_SET_TEMPERATURE = "set_temperature"

# ClimateEntityFeature bits.
FEATURE_TARGET_TEMPERATURE = 1
FEATURE_PRESET_MODE = 16


# --------------------------------------------------------------------------- #
# Commands, reports, capabilities
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ClimateCommand:
    """What a thermostat should do. Every field is optional; ``offset`` is adjust's."""

    hvac_mode: str | None = None
    temperature: float | None = None
    preset: str | None = None
    offset: float | None = None

    def __post_init__(self) -> None:
        if self.hvac_mode is not None and self.hvac_mode not in HVAC_MODES:
            raise ValueError(f"unknown hvac_mode {self.hvac_mode!r}")

    @property
    def is_off(self) -> bool:
        return self.hvac_mode == HVAC_OFF

    @property
    def has_target(self) -> bool:
        return self.temperature is not None or self.preset is not None

    def groups(self) -> frozenset[str]:
        g = set()
        if self.hvac_mode is not None:
            g.add(GROUP_MODE)
        if self.has_target:
            g.add(GROUP_TARGET)
        if self.offset is not None:
            g.add(GROUP_OFFSET)
        return frozenset(g)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in (("hvac_mode", self.hvac_mode), ("temperature", self.temperature),
                                  ("preset", self.preset), ("offset", self.offset)) if v is not None}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> ClimateCommand | None:
        if data is None:
            return None
        return cls(data.get("hvac_mode"), data.get("temperature"), data.get("preset"),
                   data.get("offset"))


def merge(below: ClimateCommand | None, over: ClimateCommand) -> ClimateCommand:
    """``over``'s mode and target on top of ``below``'s; the target is replaced whole."""
    below = below or ClimateCommand()
    mode = over.hvac_mode if over.hvac_mode is not None else below.hvac_mode
    if over.has_target:
        return ClimateCommand(mode, over.temperature, over.preset)
    return ClimateCommand(mode, below.temperature, below.preset)


@dataclass(frozen=True, slots=True)
class ClimateObserved:
    """A thermostat's report, reduced to what Layers compares (and two values it shows)."""

    state: str                          # the HVAC mode, or unavailable / unknown
    temperature: float | None = None    # the setpoint
    preset: str | None = None
    current: float | None = None        # room temperature: shown, never compared
    action: str | None = None           # hvac_action: shown, never compared
    at: float = 0.0

    @property
    def available(self) -> bool:
        return self.state not in NO_STATE

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "temperature": self.temperature, "preset": self.preset,
                "current": self.current, "action": self.action, "at": self.at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> ClimateObserved | None:
        if data is None:
            return None
        return cls(data["state"], data.get("temperature"), data.get("preset"),
                   data.get("current"), data.get("action"), data.get("at", 0.0))


@dataclass(frozen=True, slots=True)
class ClimateCaps:
    """What a thermostat takes, from its state attributes."""

    hvac_modes: frozenset[str] = frozenset()
    preset_modes: frozenset[str] = frozenset()
    min_temp: float | None = None
    max_temp: float | None = None
    step: float | None = None
    setpoint: bool = False          # takes a single target temperature
    presets: bool = False
    platform: str = ""


@dataclass(frozen=True, slots=True)
class ClimateCall:
    """One climate service call (no entity_id)."""

    service: str
    data: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def make(cls, service: str, **data: Any) -> ClimateCall:
        return cls(service, tuple(sorted(data.items())))

    def as_dict(self) -> dict[str, Any]:
        return dict(self.data)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _strs(value: Any) -> frozenset[str]:
    if not value or isinstance(value, str | bytes):
        return frozenset({value}) if isinstance(value, str) and value else frozenset()
    try:
        return frozenset(str(getattr(v, "value", v)) for v in value)
    except TypeError:
        return frozenset()


def caps_from_attrs(attrs: Mapping[str, Any], platform: str = "") -> ClimateCaps:
    """A thermostat's capabilities. Without ``supported_features`` a reported
    ``temperature`` / ``preset_modes`` stands in for the feature bits."""
    attrs = attrs or {}
    features = int(_to_float(attrs.get("supported_features")) or 0)
    presets = _strs(attrs.get("preset_modes"))
    return ClimateCaps(
        hvac_modes=_strs(attrs.get("hvac_modes")),
        preset_modes=presets,
        min_temp=_to_float(attrs.get("min_temp")),
        max_temp=_to_float(attrs.get("max_temp")),
        step=_to_float(attrs.get("target_temp_step")),
        setpoint=bool(features & FEATURE_TARGET_TEMPERATURE) or (not features and "temperature" in attrs),
        presets=bool(features & FEATURE_PRESET_MODE) or (not features and bool(presets)),
        platform=platform,
    )


def observed_from_state(state: str, attrs: Mapping[str, Any], at: float) -> ClimateObserved:
    attrs = attrs or {}
    preset = attrs.get("preset_mode")
    action = attrs.get("hvac_action")
    return ClimateObserved(
        state=str(getattr(state, "value", state)),
        temperature=_to_float(attrs.get("temperature")),
        preset=str(preset) if preset not in (None, "", PRESET_NONE) else None,
        current=_to_float(attrs.get("current_temperature")),
        action=str(getattr(action, "value", action)) if action is not None else None,
        at=float(at),
    )


def observed_to_command(obs: ClimateObserved | None) -> ClimateCommand | None:
    """What a thermostat shows, as a command (the base, or a person's manual layer)."""
    if obs is None or not obs.available or obs.state not in HVAC_MODES:
        return None
    if obs.state == HVAC_OFF:
        return ClimateCommand(HVAC_OFF)
    return ClimateCommand(obs.state, obs.temperature, obs.preset)


def tolerance(caps: ClimateCaps) -> float:
    """How far a setpoint may read back from what was sent and still count as taken."""
    step = caps.step or 0.0
    return max(TOL_SETPOINT, step / 2)


def _close_temp(a: float | None, b: float | None, tol: float) -> bool:
    return a is None or b is None or abs(a - b) <= tol + _EPS


# --------------------------------------------------------------------------- #
# Layers and records
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ClimateLayer:
    id: str
    priority: int
    mode: str                   # set | adjust
    command: ClimateCommand
    seq: int
    set_at: float
    expires_at: float | None = None
    owner: str | None = None
    on_expire: str = ON_EXPIRE_SAFE
    strength: str = STRENGTH_SOFT

    def live(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    @property
    def strong(self) -> bool:
        return self.strength in STRONG

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "priority": self.priority, "mode": self.mode,
                "command": self.command.to_json(), "seq": self.seq, "set_at": self.set_at,
                "expires_at": self.expires_at, "owner": self.owner,
                "on_expire": self.on_expire, "strength": self.strength}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ClimateLayer:
        return cls(
            id=data["id"], priority=data["priority"], mode=data["mode"],
            command=ClimateCommand.from_json(data["command"]) or ClimateCommand(),
            seq=data["seq"], set_at=data["set_at"], expires_at=data.get("expires_at"),
            owner=data.get("owner"), on_expire=data.get("on_expire", ON_EXPIRE_SAFE),
            strength=data.get("strength", STRENGTH_SOFT),
        )


@dataclass(slots=True)
class ClimateOwed:
    since: float
    target: ClimateCommand | None
    missed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"since": self.since, "target": self.target.to_json() if self.target else None,
                "missed": self.missed}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> ClimateOwed | None:
        if data is None:
            return None
        return cls(data["since"], ClimateCommand.from_json(data.get("target")),
                   data.get("missed", False))


@dataclass(slots=True)
class ClimateLastCommand:
    """The last command Layers sent; ``matched_at`` as for lamps (not persisted)."""

    at: float
    ours: bool
    target: ClimateCommand | None = None
    context_id: str | None = None
    matched_at: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"at": self.at, "ours": self.ours,
                "target": self.target.to_json() if self.target else None}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> ClimateLastCommand | None:
        if data is None:
            return None
        return cls(data["at"], data["ours"], ClimateCommand.from_json(data.get("target")))


@dataclass(slots=True)
class ClimateRecord:
    """Everything Layers knows about one enrolled thermostat."""

    entity_id: str
    base: ClimateCommand | None = None
    base_source: str | None = None
    base_at: float | None = None
    layers: dict[str, ClimateLayer] = field(default_factory=dict)
    tombstones: dict[str, Tombstone] = field(default_factory=dict)
    observed: ClimateObserved | None = None
    observed_prev: ClimateObserved | None = None
    p_at_drop: ClimateObserved | None = None
    available: bool = True
    owed: ClimateOwed | None = None
    diverged: str | None = None
    last_command: ClimateLastCommand | None = None
    last_external: External | None = None
    last_layers_change: float | None = None
    untrusted: bool = False
    # Apply off: the calls Layers would have made for the current command.
    would_send: tuple[ClimateCall, ...] | None = None

    def worth_persisting(self) -> bool:
        return bool(self.layers or self.tombstones or self.owed or self.diverged)

    def to_json(self) -> dict[str, Any]:
        return {
            "base": self.base.to_json() if self.base else None,
            "base_source": self.base_source,
            "base_at": self.base_at,
            "layers": {k: v.to_json() for k, v in self.layers.items()},
            "tombstones": {k: v.to_json() for k, v in self.tombstones.items()},
            "observed": self.observed.to_json() if self.observed else None,
            "p_at_drop": self.p_at_drop.to_json() if self.p_at_drop else None,
            "available": self.available,
            "owed": self.owed.to_json() if self.owed else None,
            "diverged": self.diverged,
            "last_command": self.last_command.to_json() if self.last_command else None,
            "last_external": self.last_external.to_json() if self.last_external else None,
            "last_layers_change": self.last_layers_change,
        }

    @classmethod
    def from_json(cls, entity_id: str, data: Mapping[str, Any]) -> ClimateRecord:
        return cls(
            entity_id=entity_id,
            base=ClimateCommand.from_json(data.get("base")),
            base_source=data.get("base_source"),
            base_at=data.get("base_at"),
            layers={k: ClimateLayer.from_json(v) for k, v in data.get("layers", {}).items()},
            tombstones={k: Tombstone.from_json(v) for k, v in data.get("tombstones", {}).items()},
            observed=ClimateObserved.from_json(data.get("observed")),
            p_at_drop=ClimateObserved.from_json(data.get("p_at_drop")),
            available=data.get("available", True),
            owed=ClimateOwed.from_json(data.get("owed")),
            diverged=data.get("diverged"),
            last_command=ClimateLastCommand.from_json(data.get("last_command")),
            last_external=External.from_json(data.get("last_external")),
            last_layers_change=data.get("last_layers_change"),
        )


@dataclass(frozen=True, slots=True)
class ClimateSetRequest:
    """A layers.set for one thermostat, already validated by the service schema."""

    layer: str
    command: ClimateCommand
    priority: int | None = None
    mode: str = MODE_SET
    expires_at: float | None = None
    on_expire: str = ON_EXPIRE_SAFE
    only_if_present: bool = False
    owner: str | None = None
    strength: str | None = None


# --------------------------------------------------------------------------- #
# Resolve
# --------------------------------------------------------------------------- #


def _stack_key(layer: ClimateLayer) -> tuple[int, int]:
    return (layer.priority, layer.seq)


def live_layers(rec: ClimateRecord, now: float) -> list[ClimateLayer]:
    return sorted((lay for lay in rec.layers.values() if lay.live(now)), key=_stack_key)


def _fold(rec: ClimateRecord, now: float) -> tuple[ClimateCommand | None, ClimateLayer | None]:
    base = rec.base or ClimateCommand()
    mode, temp, preset = base.hvac_mode, base.temperature, base.preset
    top: ClimateLayer | None = None
    for layer in live_layers(rec, now):
        cmd = layer.command
        if layer.mode == MODE_SET:
            if cmd.hvac_mode is not None:
                mode = cmd.hvac_mode
            if cmd.has_target:          # the target below is replaced whole
                temp, preset = cmd.temperature, cmd.preset
            top = layer
        elif (layer.mode == MODE_ADJUST and cmd.offset is not None
              and mode not in (None, HVAC_OFF) and temp is not None):
            temp = temp + cmd.offset
            top = layer
    if mode is None and temp is None and preset is None:
        return None, top
    return ClimateCommand(mode, temp, preset), top


def resolve(rec: ClimateRecord, now: float) -> tuple[ClimateCommand | None, str]:
    """The effective command and what decides it (a layer id, ``base`` or ``none``)."""
    command, top = _fold(rec, now)
    if top is not None:
        return command, top.id
    return command, LAYER_BASE if rec.base is not None else "none"


def active_layer(rec: ClimateRecord, now: float) -> ClimateLayer | None:
    return _fold(rec, now)[1]


def mode_holder(rec: ClimateRecord, now: float) -> ClimateLayer | None:
    """The top live set layer that names a mode: the one that decides on/off."""
    holders = [lay for lay in live_layers(rec, now)
               if lay.mode == MODE_SET and lay.command.hvac_mode is not None]
    return holders[-1] if holders else None


# --------------------------------------------------------------------------- #
# Project, compare
# --------------------------------------------------------------------------- #


def round_setpoint(value: float, caps: ClimateCaps) -> float:
    """To the device's step, then into its range."""
    if caps.step and caps.step > 0:
        value = round(round(value / caps.step) * caps.step, 4)
    if caps.min_temp is not None:
        value = max(value, caps.min_temp)
    if caps.max_temp is not None:
        value = min(value, caps.max_temp)
    return value


def project(cmd: ClimateCommand | None, caps: ClimateCaps) -> tuple[ClimateCall, ...]:
    """The calls an effective command becomes on this thermostat, in sending order.

    Off is one ``set_hvac_mode: off``: a thermostat that is off takes no target.
    Otherwise the mode (when the command names one), then the preset, then the
    temperature (rounded to the step, clamped to the range), each only if the device
    takes it. A mode or preset the device does not list is dropped.
    """
    if cmd is None:
        return ()
    calls: list[ClimateCall] = []
    if cmd.hvac_mode is not None and (not caps.hvac_modes or cmd.hvac_mode in caps.hvac_modes):
        calls.append(ClimateCall.make(SERVICE_SET_HVAC_MODE, hvac_mode=cmd.hvac_mode))
    if cmd.is_off:
        return tuple(calls)
    if cmd.preset is not None and caps.presets and (
        not caps.preset_modes or cmd.preset in caps.preset_modes
    ):
        calls.append(ClimateCall.make(SERVICE_SET_PRESET_MODE, preset_mode=cmd.preset))
    if cmd.temperature is not None and caps.setpoint:
        calls.append(ClimateCall.make(SERVICE_SET_TEMPERATURE,
                                      temperature=round_setpoint(cmd.temperature, caps)))
    return tuple(calls)


def call_shown(obs: ClimateObserved | None, call: ClimateCall, caps: ClimateCaps) -> bool:
    """Does the report show what this one call asks for?"""
    if obs is None or not obs.available:
        return False
    data = call.as_dict()
    if call.service == SERVICE_SET_HVAC_MODE:
        return obs.state == data["hvac_mode"]
    if call.service == SERVICE_SET_PRESET_MODE:
        return obs.preset == data["preset_mode"]
    if call.service == SERVICE_SET_TEMPERATURE:
        # A device that reports no setpoint cannot be checked on it.
        return _close_temp(obs.temperature, data["temperature"], tolerance(caps))
    return False


def matches(obs: ClimateObserved | None, calls: Iterable[ClimateCall], caps: ClimateCaps) -> bool:
    """Did the thermostat take every call? Mode and preset exactly, setpoint within tolerance."""
    calls = tuple(calls)
    return bool(calls) and all(call_shown(obs, call, caps) for call in calls)


def shows(obs: ClimateObserved | None, cmd: ClimateCommand | None, caps: ClimateCaps) -> bool:
    """Does the thermostat show ``cmd`` (as it would take it)? Nothing to show counts as yes."""
    calls = project(cmd, caps)
    return not calls or matches(obs, calls, caps)


def close(a: ClimateObserved | None, b: ClimateObserved | None, caps: ClimateCaps) -> bool:
    """Two reports show the same setting: mode, preset, and setpoint within tolerance.
    Room temperature and ``hvac_action`` are not a setting."""
    if a is None or b is None:
        return False
    if a.state != b.state or a.preset != b.preset:
        return False
    if a.temperature is None or b.temperature is None:
        return a.temperature is None and b.temperature is None
    return abs(a.temperature - b.temperature) <= tolerance(caps) + _EPS


def starts(before: ClimateObserved | ClimateCommand | None, after: ClimateCommand | None) -> bool:
    """Would going from ``before`` to ``after`` turn the thermostat on (the safety rule)?

    ``True`` when ``after`` names a mode other than off and ``before`` is off,
    unknown or unavailable. A setpoint or a preset never counts.
    """
    if after is None or after.hvac_mode in (None, HVAC_OFF):
        return False
    if before is None:
        return True
    mode = before.state if isinstance(before, ClimateObserved) else before.hvac_mode
    return mode is None or mode in NO_STATE or mode == HVAC_OFF


# --------------------------------------------------------------------------- #
# layers.set / layers.clear
# --------------------------------------------------------------------------- #


def _invalid(rec: ClimateRecord, layer: str, reason: str) -> PolicyError:
    return PolicyError(ERR_INVALID_REQUEST, entity_id=rec.entity_id, layer=layer, reason=reason)


def _check_request(rec: ClimateRecord, req: ClimateSetRequest, manual_priority: int) -> None:
    cmd = req.command
    if req.mode not in (MODE_SET, MODE_ADJUST):
        raise _invalid(rec, req.layer, f"mode {req.mode!r} does not apply to a climate entity")
    if req.strength is not None and req.strength not in STRENGTHS:
        raise _invalid(rec, req.layer, f"unknown strength {req.strength!r}")
    if req.strength is not None and req.layer in (LAYER_BASE, LAYER_ACTIVE):
        raise _invalid(rec, req.layer, "strength belongs to a layer id, not to the base or 'active'")
    if req.mode == MODE_ADJUST:
        if cmd.offset is None or cmd.hvac_mode is not None or cmd.has_target:
            raise _invalid(rec, req.layer, "an adjust layer on a climate entity takes an offset only")
        if req.layer in (LAYER_BASE, LAYER_ACTIVE):
            raise _invalid(rec, req.layer, "an offset belongs to a layer id")
    elif cmd.offset is not None:
        raise _invalid(rec, req.layer, "an offset needs mode: adjust")
    elif not cmd.groups():
        raise _invalid(rec, req.layer, "give an hvac_mode, a temperature or a preset")
    if cmd.is_off and cmd.has_target:
        raise _invalid(rec, req.layer, "hvac_mode: off takes no temperature or preset")
    if req.layer == LAYER_ALL:
        raise _invalid(rec, req.layer, "'all' is not a layer id")
    if req.layer == MANUAL_LAYER:
        raise _invalid(rec, req.layer, "'manual' is the layer a person's change goes into")
    if req.priority is not None and req.priority == manual_priority:
        raise _invalid(rec, req.layer,
                       f"priority {manual_priority} is the manual priority on climate entities")


def apply_set(rec: ClimateRecord, req: ClimateSetRequest, now: float,
              next_seq: Callable[[], int], manual_priority: int) -> SetResult:
    """One ``layers.set`` on one thermostat. Raises ``PolicyError`` before changing anything."""
    _check_request(rec, req, manual_priority)
    if req.layer == LAYER_BASE:
        rec.base = merge(rec.base, req.command)
        rec.base_source, rec.base_at = BASE_SRC_SERVICE, now
        rec.last_layers_change = now
        return SetResult(SET_BASE, LAYER_BASE)
    if req.layer == LAYER_ACTIVE:
        top = active_layer(rec, now)
        if top is None or top.mode != MODE_SET:
            if top is None:
                rec.base = merge(rec.base, req.command)
                rec.base_source, rec.base_at = BASE_SRC_SERVICE, now
                rec.last_layers_change = now
                return SetResult(SET_BASE, LAYER_BASE)
            raise _invalid(rec, req.layer, "the active layer is an offset: set it by its id")
        top.command = merge(top.command, req.command)
        rec.last_layers_change = now
        return SetResult(SET_ACTIVE, top.id)

    if req.expires_at is not None and req.expires_at <= now:
        raise _invalid(rec, req.layer, "expires_at is not in the future")
    tomb = rec.tombstones.get(req.layer)
    if tomb is not None and tomb.live(now):
        return SetResult(SET_SKIPPED_TOMBSTONED, req.layer)
    layer = rec.layers.get(req.layer)
    if layer is None or not layer.live(now):
        if req.only_if_present:
            return SetResult(SET_SKIPPED_ABSENT, req.layer)
        if req.priority is None:
            raise PolicyError(ERR_PRIORITY_REQUIRED, entity_id=rec.entity_id, layer=req.layer)
        _check_priority(rec, req.priority, req.layer, now)
        rec.layers[req.layer] = ClimateLayer(
            id=req.layer, priority=req.priority, mode=req.mode, command=req.command,
            seq=next_seq(), set_at=now, expires_at=req.expires_at, owner=req.owner,
            on_expire=req.on_expire, strength=req.strength or STRENGTH_SOFT,
        )
        rec.last_layers_change = now
        return SetResult(SET_CREATED, req.layer)

    mode = layer.mode if req.only_if_present else req.mode
    new_priority = req.priority is not None and req.priority != layer.priority
    if req.command == layer.command and mode == layer.mode and not new_priority:
        if req.expires_at is not None:
            layer.expires_at = req.expires_at
        if req.owner is not None:
            layer.owner = req.owner
        if req.strength is not None:
            layer.strength = req.strength
        return SetResult(SET_REFRESHED, req.layer)
    if req.only_if_present and mode != req.mode:
        raise _invalid(rec, req.layer, "a renewal cannot change a layer's mode")
    if new_priority:
        _check_priority(rec, req.priority, req.layer, now)
        layer.priority = req.priority
    layer.mode = mode
    layer.command = req.command
    layer.set_at = now
    layer.expires_at = req.expires_at
    layer.on_expire = req.on_expire
    if req.owner is not None:
        layer.owner = req.owner
    if req.strength is not None:
        layer.strength = req.strength
    rec.last_layers_change = now
    return SetResult(SET_UPDATED, req.layer)


def _check_priority(rec: ClimateRecord, priority: int, layer_id: str, now: float) -> None:
    for other in live_layers(rec, now):
        if other.priority == priority and other.id != layer_id:
            raise PolicyError(ERR_PRIORITY_CONFLICT, entity_id=rec.entity_id, layer=layer_id,
                              priority=str(priority), other=other.id)


def apply_clear(rec: ClimateRecord, layer: str, now: float) -> ClearResult:
    """One ``layers.clear`` on one thermostat; the same rules as a lamp's (SPEC 5.2)."""
    if layer == LAYER_BASE:
        raise _invalid(rec, layer, "the base cannot be cleared")
    expired: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    lifted: tuple[str, ...] = ()
    if layer == LAYER_ACTIVE:
        top = active_layer(rec, now)
        removed = (top.id,) if top is not None and not top.strong else ()
        kept = (top.id,) if top is not None and top.strong else ()
    elif layer == LAYER_ALL:
        live = live_layers(rec, now)
        removed = tuple(lay.id for lay in live if not lay.strong)
        kept = tuple(lay.id for lay in live if lay.strong)
        expired = tuple(lay.id for lay in sorted(rec.layers.values(), key=_stack_key)
                        if not lay.live(now) and not lay.strong)
        lifted = tuple(sorted(t for t, tomb in rec.tombstones.items()
                              if tomb.strength == STRENGTH_SOFT))
    else:
        target = rec.layers.get(layer)
        removed = (layer,) if target is not None and target.live(now) else ()
        expired = (layer,) if target is not None and not target.live(now) else ()
        lifted = (layer,) if layer in rec.tombstones else ()
    for layer_id in removed:
        del rec.layers[layer_id]
    for layer_id in expired:
        rec.layers[layer_id].on_expire = ON_EXPIRE_RENDER
    for layer_id in lifted:
        del rec.tombstones[layer_id]
    if removed or expired:
        rec.last_layers_change = now
    return ClearResult(removed, lifted, expired, kept)


# --------------------------------------------------------------------------- #
# A change Layers did not make
# --------------------------------------------------------------------------- #


def _drop(rec: ClimateRecord, layers: list[ClimateLayer], source: str, now: float) -> tuple[str, ...]:
    dropped = sorted(layers, key=_stack_key)
    for layer in dropped:
        rec.tombstones[layer.id] = Tombstone(layer_id=layer.id, at=now,
                                             expires_at=layer.expires_at, source=source,
                                             strength=layer.strength)
        del rec.layers[layer.id]
    return tuple(layer.id for layer in dropped)


def apply_external(
    rec: ClimateRecord,
    shown: ClimateCommand,
    source: str,
    now: float,
    next_seq: Callable[[], int],
    *,
    manual_priority: int,
    manual_timeout: float | None,
    caps: ClimateCaps,
    user_id: str | None = None,
    scope: str = SCOPE_ROOM,
) -> ExternalResult:
    """A change Layers did not make becomes the ``manual`` layer (SPEC 8.5).

    - ``shown`` (what the thermostat now shows) is written into the ``manual``
      layer at ``manual_priority``, running out ``manual_timeout`` from now (never,
      when ``None``). A manual layer already there is replaced, its timer restarted.
    - Soft layers at or above ``manual_priority`` are dropped and tombstoned: the
      person's change wins over them, as it would on a lamp.
    - Sticky layers above it are dropped too when the change was a hand on this
      thermostat (``SCOPE_LAMP``); every other sticky or locked layer above it stays
      on top, the change goes under it (``held``), and the record is marked
      ``delivery`` when the thermostat does not show the effective command, so the
      engine shows it again.
    - Layers below ``manual_priority`` stay as they are: the manual layer covers
      them until it runs out or is cleared.

    The base never changes: it stays what the schedule last set.
    """
    above = [lay for lay in rec.layers.values()
             if lay.priority >= manual_priority and lay.id != MANUAL_LAYER]
    dismissed = [lay for lay in above
                 if not lay.strong or (lay.strength == STRENGTH_STICKY and scope == SCOPE_LAMP)]
    dropped = _drop(rec, dismissed, source, now)
    old = rec.layers.get(MANUAL_LAYER)
    rec.layers[MANUAL_LAYER] = ClimateLayer(
        id=MANUAL_LAYER, priority=manual_priority, mode=MODE_SET, command=shown,
        seq=old.seq if old is not None else next_seq(), set_at=now,
        expires_at=now + manual_timeout if manual_timeout else None,
        owner=source, on_expire=ON_EXPIRE_SAFE, strength=STRENGTH_SOFT,
    )
    rec.tombstones.pop(MANUAL_LAYER, None)
    held = tuple(lay.id for lay in sorted(rec.layers.values(), key=_stack_key)
                 if lay.strong and lay.priority > manual_priority and lay.live(now))
    effective = resolve(rec, now)[0]
    rec.diverged = DIV_DELIVERY if held and not shows(rec.observed, effective, caps) else None
    rec.owed = None
    rec.last_external = External(at=now, source=source, policy=POLICY_MANUAL_LAYER,
                                 user_id=user_id, dropped=dropped, edited=MANUAL_LAYER,
                                 scope=scope, held=held)
    return ExternalResult(dropped=dropped, edited=MANUAL_LAYER, held=held)


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


def expire(rec: ClimateRecord, now: float, shown: ClimateObserved | None) -> ExpireResult:
    """Remove what ran out; decide whether the new effective command may be sent.

    - a layer cleared after it ran out, or set with ``on_expire: render``: render;
    - else the safety rule: if the new command would turn the thermostat on from
      what it shows (``shown``: the report, or what it showed before it went away),
      nothing is sent. With no layer left holding the mode the base becomes what the
      thermostat shows; with one, the record is marked ``unsynced`` and the next
      ``layers.*`` call that changes it, or ``layers.sync``, sends it;
    - else render.
    """
    lifted = tuple(sorted(t for t, tomb in rec.tombstones.items() if not tomb.live(now)))
    for layer_id in lifted:
        del rec.tombstones[layer_id]
    gone = sorted((lay for lay in rec.layers.values() if not lay.live(now)), key=_stack_key)
    if not gone:
        return ExpireResult((), False, lifted)
    for layer in gone:
        del rec.layers[layer.id]
    rec.last_layers_change = now
    effective = resolve(rec, now)[0]
    if any(layer.on_expire == ON_EXPIRE_RENDER for layer in gone):
        render = True
    elif starts(shown, effective):
        render = False
        if mode_holder(rec, now) is None:
            rec.base = observed_to_command(shown) if shown is not None else None
            rec.base_source, rec.base_at = BASE_SRC_EXPIRY, now
        else:
            rec.diverged = DIV_UNSYNCED
    else:
        render = True
    return ExpireResult(tuple(lay.id for lay in gone), render and effective is not None, lifted)


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #

# Verdict kinds (the lamps' names where they mean the same).
GONE = "gone"
TRANSPORT_DOWN = "transport_down"
IGNORE = "ignore"
FIRST = "first"
TRANSPORT_UP = "transport_up"
MEASUREMENT = "measurement"     # only the room temperature or hvac_action moved
OURS = "ours"
EXTERNAL = "external"
NOISE = "noise"
LATE = "late"                   # inside the report-back window: judged when it ends
DEBOUNCE = "debounce"
REREPORT = "rereport"

# decide_return kinds
RECORD = "record"
NOTHING = "nothing"
SEND = "send"
WAIT = "wait"


@dataclass(frozen=True, slots=True)
class ClimateEvent:
    entity_id: str
    old: ClimateObserved | None
    new: ClimateObserved | None
    context_id: str | None
    parent_id: str | None
    user_id: str | None
    at: float


@dataclass(frozen=True, slots=True)
class ClimateRuntime:
    ours: Mapping[str, ClimateCommand] = field(default_factory=dict)  # context id -> target
    calls: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)  # ctx -> (source, user)
    render_alive: bool = False
    returning: bool = False
    window: float = DEFAULT_REPORT_WINDOW_S


@dataclass(frozen=True, slots=True)
class ClimateVerdict:
    kind: str
    source: str | None = None
    user_id: str | None = None


def _consistent(old: ClimateObserved, new: ClimateObserved, target: ClimateCommand,
                caps: ClimateCaps) -> bool:
    """Could ``new`` be our ``target`` landing? Each value either took the target or
    has not moved yet (a thermostat may report the mode and the setpoint separately)."""
    for call in project(target, caps):
        if not (call_shown(new, call, caps) or _same_part(old, new, call, caps)):
            return False
    return True


def _same_part(old: ClimateObserved, new: ClimateObserved, call: ClimateCall,
               caps: ClimateCaps) -> bool:
    if call.service == SERVICE_SET_HVAC_MODE:
        return old.state == new.state
    if call.service == SERVICE_SET_PRESET_MODE:
        return old.preset == new.preset
    return _close_temp(old.temperature, new.temperature, tolerance(caps))


def classify(rec: ClimateRecord, ev: ClimateEvent, rt: ClimateRuntime, caps: ClimateCaps,
             now: float) -> ClimateVerdict:
    """What a ``state_changed`` on an enrolled thermostat means; first match wins.

    GONE, TRANSPORT_DOWN / IGNORE, FIRST, TRANSPORT_UP, MEASUREMENT (the setting did
    not change), OURS (our context and consistent with our command), EXTERNAL (a
    known call, a ``user_id`` or a ``parent_id``), and without a usable context:
    NOISE while a return settles or our render runs and the device has not arrived,
    LATE inside the report-back window when it has not arrived either, else DEBOUNCE.
    """
    old, new = ev.old, ev.new
    if new is None:
        return ClimateVerdict(GONE)
    if not new.available:
        return ClimateVerdict(IGNORE if old is not None and not old.available else TRANSPORT_DOWN)
    if old is None:
        return ClimateVerdict(FIRST)
    if not old.available:
        return ClimateVerdict(TRANSPORT_UP)
    if close(old, new, caps):
        return ClimateVerdict(MEASUREMENT)

    context_id, parent_id, user_id = ev.context_id, ev.parent_id, ev.user_id
    last = rec.last_command
    if context_id is not None:
        target = rt.ours.get(context_id)
        if target is not None and _consistent(old, new, target, caps):
            return ClimateVerdict(OURS, SRC_OURS)
        if target is not None or (last is not None and last.ours and last.context_id == context_id):
            # Our context on someone else's write (Home Assistant reuses it for 5 s).
            context_id = parent_id = user_id = None
    if context_id is not None and context_id in rt.calls:
        source, call_user = rt.calls[context_id]
        return ClimateVerdict(EXTERNAL, source, call_user)
    if user_id:
        return ClimateVerdict(EXTERNAL, SRC_USER, user_id)
    if parent_id:
        return ClimateVerdict(EXTERNAL, SRC_AUTOMATION)

    if rt.returning:
        return ClimateVerdict(NOISE)
    arrived = (last is not None and last.ours and last.matched_at is not None
               and now - last.matched_at >= ARRIVED_HOLD_S)
    if not arrived and last is not None and last.ours:
        if rt.render_alive:
            return ClimateVerdict(NOISE)        # the render's verification judges it
        if now - last.at <= rt.window:
            return ClimateVerdict(LATE)
    return ClimateVerdict(DEBOUNCE)


def settle(rec: ClimateRecord, current: ClimateObserved | None, caps: ClimateCaps,
           now: float) -> ClimateVerdict:
    """A no-context change that held ``DEBOUNCE_S``: a re-report, or a person's (device)."""
    if current is None or not current.available:
        return ClimateVerdict(IGNORE)
    if close(current, rec.observed_prev, caps):
        return ClimateVerdict(REREPORT)
    effective = resolve(rec, now)[0]
    if effective is not None and shows(current, effective, caps):
        return ClimateVerdict(REREPORT)
    return ClimateVerdict(EXTERNAL, SRC_DEVICE)


def shows_target(new: ClimateObserved | None, target: ClimateCommand | None,
                 caps: ClimateCaps) -> bool:
    """Keeps ``ClimateLastCommand.matched_at``, as for lamps."""
    return target is not None and bool(project(target, caps)) and shows(new, target, caps)


def decide_return(rec: ClimateRecord, shown: ClimateObserved | None, caps: ClimateCaps,
                  now: float) -> str:
    """What to do with a thermostat that came back (the lamps' SPEC 6.4, with ``starts``
    in place of "lights")."""
    if shown is None or not shown.available:
        return WAIT
    effective = resolve(rec, now)[0]
    if effective is None:
        return RECORD
    if shows(shown, effective, caps):
        return NOTHING
    before = rec.p_at_drop
    known = before is not None and before.available
    if known and not close(shown, before, caps):
        return EXTERNAL
    if rec.owed is None and rec.diverged in (DIV_MANUAL_KEEP, DIV_UNSYNCED):
        return NOTHING
    if mode_holder(rec, now) is not None:
        return SEND
    on = starts(shown, effective)
    if active_layer(rec, now) is not None and (known or not on):
        return SEND
    if rec.owed is not None and (not on or (known and now - rec.owed.since <= OWED_ON_MAX_AGE_S)):
        return SEND
    return RECORD


def change_scope(source: str) -> str:
    """A hand on the thermostat (its dial, its app tile) or an automation (the room)."""
    return SCOPE_LAMP if source in (SRC_DEVICE, SRC_USER) else SCOPE_ROOM
