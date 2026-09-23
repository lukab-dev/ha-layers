"""Data model shared by every Layers module.

Pure Python (3.12+), no Home Assistant imports. Everything here is plain data
plus JSON round-tripping; the decisions live in resolve / capability / policy /
classify, and the Home Assistant wiring lives outside this package.

Vocabulary
----------
command   what a lamp should do: on/off plus optional brightness and colour
base      the bottom layer of a lamp: what it shows when nothing is layered on it
layer     a named, prioritised command that sits on top of the base
requested what a layer's owner last asked for
effective what the lamp should show: base folded with the live layers
observed  what the lamp actually reported
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ON = "on"
OFF = "off"
NO_STATE = frozenset({"unavailable", "unknown"})

MODE_SET = "set"          # decides on/off and merges its attributes over what is below
MODE_ADJUST = "adjust"    # changes attributes only, and only when what is below is on
MODE_FOLLOW = "follow"    # like adjust, with its attributes read from another entity
MODES = (MODE_SET, MODE_ADJUST, MODE_FOLLOW)

LAYER_BASE = "base"
LAYER_ACTIVE = "active"
LAYER_ALL = "all"
RESERVED_LAYER_IDS = frozenset({LAYER_BASE, LAYER_ACTIVE, LAYER_ALL})

POLICY_TAKE_BACK = "take_back"
POLICY_EDIT_ACTIVE = "edit_active"
POLICY_BASE_KEEP_LAYERS = "base_keep_layers"
# reassert: a change with no service call behind it (a smart plug that comes back on
# by itself, a relay's power-on default) is the device misbehaving, never a person:
# the base and the layers stay and the effective command is sent again. A change
# from the app or an automation still takes the lamp back, and so does a second
# device change inside REASSERT_COOLDOWN_S (someone is at the device's own button).
# Per-entity only: it must not be the default for a house with wall switches.
POLICY_REASSERT = "reassert"
POLICIES = (POLICY_TAKE_BACK, POLICY_EDIT_ACTIVE, POLICY_BASE_KEEP_LAYERS, POLICY_REASSERT)
DEFAULT_POLICIES = (POLICY_TAKE_BACK, POLICY_EDIT_ACTIVE, POLICY_BASE_KEEP_LAYERS)
REASSERT_COOLDOWN_S = 30.0

ON_EXPIRE_SAFE = "safe"      # an expiry may turn a lamp off or dim it, never on or brighter
ON_EXPIRE_RENDER = "render"  # an expiry renders whatever is below, like an explicit clear
ON_EXPIRE_CHOICES = (ON_EXPIRE_SAFE, ON_EXPIRE_RENDER)

# Why a lamp does not show its effective command.
DIV_DELIVERY = "delivery"        # a command did not stick (e.g. a bridge reverted it)
DIV_PARTIAL = "partial"          # a manual change set only some attributes
DIV_COLOUR = "colour"            # the lamp would not take the requested colour
DIV_MANUAL_KEEP = "manual_keep"  # base_keep_layers: a person's change is shown on purpose
DIV_UNSYNCED = "unsynced"        # the apply switch was off, or startup found a mismatch
# Divergences an ordinary layers.set/clear on the lamp repairs. The others need layers.sync.
DIV_REPAIRED_BY_TARGETED_CALL = frozenset({DIV_DELIVERY, DIV_PARTIAL, DIV_COLOUR})

SRC_OURS = "ours"
SRC_USER = "user"
SRC_AUTOMATION = "automation"  # automation, script, scene, or any integration's service call
SRC_DEVICE = "device"          # no service call behind it: a physical switch, a vendor app, the device itself

# Attribute groups a command can specify. A colour is one atomic group.
GROUP_STATE = "state"
GROUP_BRIGHTNESS = "brightness"
GROUP_COLOR = "color"
ALL_GROUPS = frozenset({GROUP_STATE, GROUP_BRIGHTNESS, GROUP_COLOR})

COLOR_XY = "xy_color"
COLOR_HS = "hs_color"
COLOR_RGB = "rgb_color"
COLOR_KELVIN = "color_temp_kelvin"
COLOR_KEYS = (COLOR_XY, COLOR_HS, COLOR_RGB, COLOR_KELVIN)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Color:
    """One colour descriptor, kept exactly as written (xy survives untouched).

    ``value`` is a tuple for every key; colour temperature is a 1-tuple.
    """

    key: str
    value: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.key not in COLOR_KEYS:
            raise ValueError(f"unknown colour key {self.key!r}")
        expected = {COLOR_XY: 2, COLOR_HS: 2, COLOR_RGB: 3, COLOR_KELVIN: 1}[self.key]
        if len(self.value) != expected:
            raise ValueError(f"{self.key} needs {expected} values, got {self.value!r}")

    @classmethod
    def kelvin(cls, k: float) -> Color:
        return cls(COLOR_KELVIN, (int(round(k)),))

    @classmethod
    def xy(cls, x: float, y: float) -> Color:
        return cls(COLOR_XY, (float(x), float(y)))

    def service_value(self) -> Any:
        """The value as a light.turn_on payload wants it."""
        if self.key == COLOR_KELVIN:
            return int(self.value[0])
        if self.key == COLOR_RGB:
            return [int(v) for v in self.value]
        return [float(v) for v in self.value]

    def to_json(self) -> dict[str, Any]:
        return {self.key: self.service_value()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Color:
        (key, value), = data.items()
        if key == COLOR_KELVIN:
            return cls.kelvin(value)
        return cls(key, tuple(float(v) for v in value))


@dataclass(frozen=True, slots=True)
class Command:
    """What a lamp should do.

    ``state`` is ``"on"``/``"off"`` for a full command, or ``None`` for an
    attributes-only command (an ``adjust`` layer, or a partial manual change).
    An ``off`` command carries no attributes.
    """

    state: str | None
    brightness: int | None = None
    color: Color | None = None

    def __post_init__(self) -> None:
        if self.state not in (ON, OFF, None):
            raise ValueError(f"bad state {self.state!r}")
        if self.brightness is not None and not 0 <= self.brightness <= 255:
            raise ValueError(f"brightness out of range: {self.brightness}")

    @property
    def is_on(self) -> bool:
        return self.state == ON

    @property
    def is_off(self) -> bool:
        return self.state == OFF

    def groups(self) -> frozenset[str]:
        """The attribute groups this command specifies."""
        g = set()
        if self.state is not None:
            g.add(GROUP_STATE)
        if self.brightness is not None:
            g.add(GROUP_BRIGHTNESS)
        if self.color is not None:
            g.add(GROUP_COLOR)
        return frozenset(g)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state}
        if self.brightness is not None:
            out["brightness"] = self.brightness
        if self.color is not None:
            out["color"] = self.color.to_json()
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Command | None:
        if data is None:
            return None
        color = data.get("color")
        return cls(
            state=data.get("state"),
            brightness=data.get("brightness"),
            color=Color.from_json(color) if color else None,
        )


OFF_COMMAND = Command(OFF)


def merge_command(below: Command | None, over: Command, groups: frozenset[str] | None = None) -> Command:
    """Overlay ``over`` onto ``below``, taking only ``groups`` from ``over``.

    ``groups=None`` takes every group ``over`` specifies. The result is a full
    command whenever either side supplies a state. Turning off drops attributes;
    turning on keeps the attributes of ``below`` that ``over`` does not replace.
    """
    take = over.groups() if groups is None else (over.groups() & groups)
    state = over.state if GROUP_STATE in take else (below.state if below else None)
    if state == OFF:
        return OFF_COMMAND
    brightness = below.brightness if below and below.state != OFF else None
    color = below.color if below and below.state != OFF else None
    if GROUP_BRIGHTNESS in take:
        brightness = over.brightness
    if GROUP_COLOR in take:
        color = over.color
    return Command(state, brightness, color)


# --------------------------------------------------------------------------- #
# What a lamp reports, and what it can do
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Observed:
    """A lamp's reported state, reduced to what Layers compares."""

    state: str                              # on / off / unavailable / unknown
    brightness: int | None = None
    color_mode: str | None = None
    xy: tuple[float, float] | None = None
    hs: tuple[float, float] | None = None
    kelvin: int | None = None
    at: float = 0.0

    @property
    def available(self) -> bool:
        return self.state not in NO_STATE

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "brightness": self.brightness,
            "color_mode": self.color_mode,
            "xy": list(self.xy) if self.xy else None,
            "hs": list(self.hs) if self.hs else None,
            "kelvin": self.kelvin,
            "at": self.at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Observed | None:
        if data is None:
            return None
        return cls(
            state=data["state"],
            brightness=data.get("brightness"),
            color_mode=data.get("color_mode"),
            xy=tuple(data["xy"]) if data.get("xy") else None,
            hs=tuple(data["hs"]) if data.get("hs") else None,
            kelvin=data.get("kelvin"),
            at=data.get("at", 0.0),
        )


@dataclass(frozen=True, slots=True)
class Caps:
    """What a lamp supports, from its state attributes."""

    modes: frozenset[str] = frozenset()     # supported_color_modes
    min_kelvin: int | None = None
    max_kelvin: int | None = None
    transition: bool = False
    platform: str = ""


@dataclass(frozen=True, slots=True)
class Call:
    """A light service call Layers would make (no entity_id, no transition)."""

    service: str                            # "turn_on" | "turn_off"
    data: tuple[tuple[str, Any], ...] = ()  # sorted items, so Calls compare and hash

    @classmethod
    def make(cls, service: str, data: dict[str, Any] | None = None) -> Call:
        items = ((k, tuple(v) if isinstance(v, list) else v) for k, v in (data or {}).items())
        return cls(service, tuple(sorted(items)))

    def as_dict(self) -> dict[str, Any]:
        return {k: list(v) if isinstance(v, tuple) else v for k, v in self.data}


# --------------------------------------------------------------------------- #
# Layers and per-lamp records
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Layer:
    id: str
    priority: int
    mode: str
    requested: Command          # what the owner last sent
    command: Command            # requested plus any edit_active / layer:active edits
    seq: int
    set_at: float
    expires_at: float | None = None
    owner: str | None = None
    resume_after_manual: bool = False
    on_expire: str = ON_EXPIRE_SAFE
    # The mode the owner asked for, kept only while an edit has changed ``mode``
    # (an adjust layer turned off becomes set/off). None: the same as ``mode``.
    requested_mode: str | None = None
    # follow only: the entity followed; the groups a person took over, until the lamp
    # is seen off or ``manual_until``; the option that sets it; the update transition.
    source: str | None = None
    manual: frozenset[str] = frozenset()
    manual_until: float | None = None
    manual_timeout: float | None = None
    transition: float | None = None

    def live(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "priority": self.priority,
            "mode": self.mode,
            "requested": self.requested.to_json(),
            "command": self.command.to_json(),
            "seq": self.seq,
            "set_at": self.set_at,
            "expires_at": self.expires_at,
            "owner": self.owner,
            "resume_after_manual": self.resume_after_manual,
            "on_expire": self.on_expire,
            "requested_mode": self.requested_mode,
            "source": self.source,
            "manual": sorted(self.manual),
            "manual_until": self.manual_until,
            "manual_timeout": self.manual_timeout,
            "transition": self.transition,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Layer:
        return cls(
            id=data["id"],
            priority=data["priority"],
            mode=data["mode"],
            requested=Command.from_json(data["requested"]),
            command=Command.from_json(data["command"]),
            seq=data["seq"],
            set_at=data["set_at"],
            expires_at=data.get("expires_at"),
            owner=data.get("owner"),
            resume_after_manual=data.get("resume_after_manual", False),
            on_expire=data.get("on_expire", ON_EXPIRE_SAFE),
            requested_mode=data.get("requested_mode"),
            source=data.get("source"),
            manual=frozenset(data.get("manual") or ()),
            manual_until=data.get("manual_until"),
            manual_timeout=data.get("manual_timeout"),
            transition=data.get("transition"),
        )


@dataclass(slots=True)
class Tombstone:
    """A layer a person took the lamp back from. Blocks re-setting that layer id."""

    layer_id: str
    at: float
    expires_at: float | None = None
    lift_when_off: bool = False     # resume_after_manual: lifts when the lamp is next seen off
    source: str = SRC_DEVICE

    def live(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    def to_json(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "at": self.at,
            "expires_at": self.expires_at,
            "lift_when_off": self.lift_when_off,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Tombstone:
        return cls(
            layer_id=data["layer_id"],
            at=data["at"],
            expires_at=data.get("expires_at"),
            lift_when_off=data.get("lift_when_off", False),
            source=data.get("source", SRC_DEVICE),
        )


@dataclass(slots=True)
class Owed:
    """A command Layers owes a lamp: started but not verified, or aimed at it while it was away."""

    since: float
    target: Command | None
    turns_on: bool = False          # owed ON renders expire (see OWED_ON_MAX_AGE)
    missed: bool = False            # recorded from someone else's call while the lamp was unavailable

    def to_json(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "target": self.target.to_json() if self.target else None,
            "turns_on": self.turns_on,
            "missed": self.missed,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Owed | None:
        if data is None:
            return None
        return cls(
            since=data["since"],
            target=Command.from_json(data.get("target")),
            turns_on=data.get("turns_on", False),
            missed=data.get("missed", False),
        )


@dataclass(slots=True)
class LastCommand:
    """The last command sent to a lamp by anyone, for the late-reversal window.

    ``from_state`` is the lamp's on/off when the command was sent (``None`` when
    unknown): a late reversal flips the lamp back to it.

    ``matched_at`` is when the lamp started showing our command's target without
    reporting anything else since (``None``: not showing it). It is kept by the
    engine from the lamp's own reports and is not persisted. A lamp has ARRIVED
    once that has lasted ``ARRIVED_HOLD_S``; see ``classify_state``.
    """

    at: float
    ours: bool
    source: str
    target: Command | None = None
    context_id: str | None = None
    from_state: str | None = None
    matched_at: float | None = None

    def flips(self) -> bool:
        """The command turned the lamp on or off (as far as is known)."""
        return (
            self.target is not None
            and self.target.state in (ON, OFF)
            and self.from_state in (ON, OFF)
            and self.from_state != self.target.state
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "ours": self.ours,
            "source": self.source,
            "target": self.target.to_json() if self.target else None,
            "from_state": self.from_state,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> LastCommand | None:
        if data is None:
            return None
        return cls(
            at=data["at"],
            ours=data["ours"],
            source=data["source"],
            target=Command.from_json(data.get("target")),
            from_state=data.get("from_state"),
        )


@dataclass(slots=True)
class External:
    """The last change Layers did not make, and what it did about it.

    ``groups`` are the attribute groups the change took (``None``: every group
    the lamp showed), so a follow-up re-applies the same take.
    """

    at: float
    source: str
    policy: str
    user_id: str | None = None
    dropped: tuple[str, ...] = ()
    edited: str | None = None
    groups: frozenset[str] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "source": self.source,
            "policy": self.policy,
            "user_id": self.user_id,
            "dropped": list(self.dropped),
            "edited": self.edited,
            "groups": sorted(self.groups) if self.groups is not None else None,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> External | None:
        if data is None:
            return None
        groups = data.get("groups")
        return cls(
            at=data["at"],
            source=data["source"],
            policy=data["policy"],
            user_id=data.get("user_id"),
            dropped=tuple(data.get("dropped", ())),
            edited=data.get("edited"),
            groups=frozenset(groups) if groups is not None else None,
        )


@dataclass(slots=True)
class Record:
    """Everything Layers knows about one enrolled lamp."""

    entity_id: str
    base: Command | None = None             # a full command, or None when unknown
    base_source: str | None = None
    base_at: float | None = None
    layers: dict[str, Layer] = field(default_factory=dict)
    tombstones: dict[str, Tombstone] = field(default_factory=dict)
    observed: Observed | None = None        # latest report
    observed_prev: Observed | None = None   # before the current debounce started
    p_at_drop: Observed | None = None       # frozen when the lamp went unavailable
    available: bool = True
    owed: Owed | None = None
    diverged: str | None = None
    last_command: LastCommand | None = None
    last_external: External | None = None
    last_layers_change: float | None = None
    untrusted: bool = False

    def worth_persisting(self) -> bool:
        """Lamps with nothing layered keep their base in memory only."""
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
    def from_json(cls, entity_id: str, data: dict[str, Any]) -> Record:
        return cls(
            entity_id=entity_id,
            base=Command.from_json(data.get("base")),
            base_source=data.get("base_source"),
            base_at=data.get("base_at"),
            layers={k: Layer.from_json(v) for k, v in data.get("layers", {}).items()},
            tombstones={k: Tombstone.from_json(v) for k, v in data.get("tombstones", {}).items()},
            observed=Observed.from_json(data.get("observed")),
            p_at_drop=Observed.from_json(data.get("p_at_drop")),
            available=data.get("available", True),
            owed=Owed.from_json(data.get("owed")),
            diverged=data.get("diverged"),
            last_command=LastCommand.from_json(data.get("last_command")),
            last_external=External.from_json(data.get("last_external")),
            last_layers_change=data.get("last_layers_change"),
        )


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Resolution:
    """The effective command and which layer decided it."""

    command: Command | None      # None: base unknown and no set layer -> do nothing
    active: str                  # a layer id, "base", or "none"


@dataclass(frozen=True, slots=True)
class SetRequest:
    """A layers.set call for one lamp, already validated by the service schema."""

    layer: str                   # a layer id, "base" or "active"
    command: Command             # state may be None for adjust / attribute-only edits
    priority: int | None = None
    mode: str = MODE_SET
    expires_at: float | None = None
    resume_after_manual: bool = False
    on_expire: str = ON_EXPIRE_SAFE
    only_if_present: bool = False
    owner: str | None = None
    source: str | None = None            # follow: the entity to follow
    manual_timeout: float | None = None  # follow: a hand change stops following this long
    transition: float | None = None      # follow: the transition of its updates


# Tuning. Constants in v1; measured against a real Hue bridge and Matter lamps.
TOL_BRIGHTNESS = 5          # of 255
TOL_KELVIN = 100
TOL_XY = 0.03
SETTLE_S = 2.0              # wait after a command before verifying
SLOW_OFF_S = 15.0           # Hue can report an optimistic off and correct it ~10 s later
LATE_RECHECK_S = 45.0       # second verification for platforms with late reversals
LATE_WINDOW_S = {"hue": 60.0}   # no-context reversal after a command = failed delivery
# A lamp has ARRIVED at our command once it has shown the target this long without
# reporting anything else. Measured from the lamp's own report, never from when the
# command was sent: how late a lamp answers says nothing about who moved it. A lamp
# answering a turn_on may first echo a remembered level - which is the target itself
# for a nightlight that is always set to the same level - then jump to its power-on
# level and fade down, 4-20 reports 0.1 s apart (IKEA KAJPLATS Matter globe,
# 2026-09-16). That first echo is not an arrival; the end of the fade is.
ARRIVED_HOLD_S = 3.0
LATE_WINDOW_DEFAULT_S = 15.0
DEBOUNCE_S = 3.0            # judge a no-context change only after it has held this long
FOLLOW_UP_S = 10.0          # attribute tails after an external change
CALL_MEMORY_S = 10.0        # how long a foreign service call attributes state changes
RETURN_SETTLE_S = 5.0       # wait after a lamp returns before judging it
REPLAY_QUIET_S = 20.0       # re-render after a replayed (retry-loop) foreign command
OWED_ON_MAX_AGE_S = 600.0   # an owed render that turns a lamp on is dropped after this
STARTUP_GRACE_S = 180.0
RETRY_BACKOFF_S = (1, 2, 4, 8, 16, 32)
